"""Deterministic neutral export of PS1 and N64 sources into one catalog.

Layout (all names derive from stable IDs; nothing from host paths):

    catalog.json                     sources, levels, models, textures, inventories
    scenes/<scene>.json  .glb        neutral scene manifest and inspection GLB
    models/<model>.json  .glb        model record (mesh included) and GLB
    textures/<texture>.png           decoded texture (palette row 0 / loaded TLUT)
    inventory/<platform>.json        every source range: decoded, preserved_raw or unsupported
"""

from __future__ import annotations

import json
import re
import struct
from pathlib import Path

from . import gltf
from . import n64_bt1, n64_btga
from . import n64_sources as src
from .n64_binary import FormatError, sha256
from .ps1_formats import encode_png_rgba
from .neutral import safe_source_path

CATALOG_SCHEMA = "btga-catalog"
CATALOG_VERSION = 2
_ID = re.compile(r"^[a-z0-9_]+:[a-z_]+:[0-9a-z_:]+$")


def file_stem(record_id: str) -> str:
    if not _ID.match(record_id):
        raise FormatError(f"record ID {record_id!r} is not exportable")
    return record_id.replace(":", "_")


def dump(value) -> str:
    return json.dumps(value, indent=1, sort_keys=True, default=_default) + "\n"


def _default(value):
    if isinstance(value, (bytes, bytearray)):
        raise TypeError("binary data must not be serialised into manifests")
    raise TypeError(f"cannot serialise {type(value).__name__}")


def _public_texture(texture: dict) -> dict:
    return {k: v for k, v in texture.items() if k not in ("rgba", "png")}


class Exporter:
    def __init__(self, output: Path):
        self.output = Path(output)
        self.models: dict[str, dict] = {}
        self.textures: dict[str, dict] = {}
        self.levels: list[dict] = []
        self.sources: list[dict] = []
        self.inventories: list[dict] = []

    # -- writing -------------------------------------------------------
    def _write(self, relative: str, data: bytes | str) -> None:
        path = self.output / safe_source_path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, str):
            path.write_text(data, encoding="utf-8")
        else:
            path.write_bytes(data)

    def add_scene(self, scene: dict, models: dict[str, dict], textures: dict[str, dict], level_entry: dict) -> None:
        stem = file_stem(scene["id"])
        self.models.update(models)
        for key, texture in textures.items():
            self.textures.setdefault(key, texture)
        self._write(f"scenes/{stem}.json", dump(scene))
        self._write(f"scenes/{stem}.glb", gltf.scene_mesh_glb(scene, models, textures))
        self.levels.append({**level_entry, "scene": scene["id"], "file": f"scenes/{stem}.json",
                            "instances": len(scene["instances"]), "markers": len(scene["markers"]), "collision": len(scene["collision"])})

    def finish(self, write_model_glb: bool = True) -> dict:
        textures = {k: v for k, v in self.textures.items() if "rgba" in v or "png" in v}
        for texture_id, texture in sorted(textures.items()):
            png = texture["png"] if "png" in texture else encode_png_rgba(texture["width"], texture["height"], texture["rgba"])
            self._write(f"textures/{file_stem(texture_id)}.png", png)
        model_entries = []
        for model_id in sorted(self.models):
            record = self.models[model_id]
            stem = file_stem(model_id)
            self._write(f"models/{stem}.json", dump(record))
            if write_model_glb:
                self._write(f"models/{stem}.glb", gltf.mesh_glb(record, textures))
            mesh = record["mesh"]
            model_entries.append({"id": model_id, "platform": record["source"]["platform"], "file": f"models/{stem}.json",
                                  "triangles": len(mesh["triangles"]), "rejected_triangles": len(mesh.get("rejected_triangles", [])),
                                  "bounds": record.get("bounds"), "library": record["source"].get("library"), **({"label": record["source"]["label"]} if record["source"].get("label") else {})})
        catalog = {
            "schema": CATALOG_SCHEMA,
            "schema_version": CATALOG_VERSION,
            "sources": sorted(self.sources, key=lambda s: s["platform"]),
            "levels": sorted(self.levels, key=lambda l: (l["platform"], str(l["level"]), l.get("variant", ""))),
            "models": model_entries,
            "textures": [
                {**_public_texture(self.textures[k]), "file": f"textures/{file_stem(k)}.png" if k in textures else None}
                for k in sorted(self.textures)
            ],
            "inventories": sorted(self.inventories, key=lambda i: i["platform"]),
        }
        self._write("catalog.json", dump(catalog))
        return catalog


# ------------------------------------------------------------ sources ------


def export_ga(exporter: Exporter, rom: src.RomImage, slot_source: str, levels: list[int] | None = None) -> None:
    exporter.sources.append({"platform": "n64_btga", "title": "BattleTanx: Global Assault (N64 USA Rev 0)", "rom_sha1": rom.sha1, "rom_sha256": rom.sha256})
    catalog = src.ga_level_catalog(slot_source)
    images = {image["table_index"]: image for image in src.ga_image_ranges(rom.data)}
    for entry in catalog:
        if levels is not None and entry["level"] not in levels:
            continue
        # One scene per distinct stream set; every variant label that loads it is
        # recorded because the minimap depends on the loading condition.
        by_ranges: dict[tuple, list[str]] = {}
        for variant, ranges in sorted(entry["variants"].items()):
            if ranges:
                by_ranges.setdefault(tuple(ranges), []).append(variant)
        for ranges, variants in sorted(by_ranges.items(), key=lambda item: item[1][0]):
            variant = variants[0]
            level = n64_btga.load_level(rom.data, slot_source, entry["level"], variant)
            scene, models = n64_btga.scene_record(rom.data, rom.sha256, level, exporter.textures, images, variants)
            exporter.add_scene(scene, models, exporter.textures,
                               {"platform": "n64_btga", "level": entry["level"], "name": entry["name"], "variant": variant,
                                "variants": variants, "campaign": entry["name"].split(" - ")[0] if entry["name"] else None})
    # The common world bundle holds the shared model library (tanks, weapons, effects).
    from tools.btga_world import parse_world

    data = src.decode_stream(rom.data, *src.GA_COMMON_WORLD)
    world = parse_world(data)
    common = n64_btga.Level(-1, "COMMON WORLD", "common", [n64_btga.Bundle(0, src.GA_COMMON_WORLD, data, world, 0)])
    for index in range(len(world["models"])):
        record = n64_btga.model_record(rom.data, rom.sha256, common, 0, index, exporter.textures)
        record["source"]["library"] = "common_world"
        exporter.models.setdefault(record["id"], record)
    for image in src.ga_image_ranges(rom.data):
        texture = ga_image_texture(rom, image)
        exporter.textures.setdefault(texture["id"], texture)


def ga_image_texture(rom: src.RomImage, image: dict) -> dict:
    from tools.n64_texture import TextureError, decode_texture

    data = src.decode_stream(rom.data, image["start"], image["end"])
    texture_id = n64_btga.content_id("texture", rom.sha256, ["image_table", image["table_index"]])
    record = {"id": texture_id, "width": image["width"], "height": image["height"], "format": f"fmt{image['format']}siz{image['size']}",
              "wrap": {"s": "clamp", "t": "clamp"}, "category": "ui_image",
              "source": {"platform": "n64_btga", "image_table_index": image["table_index"], "rom_range": [image["start"], image["end"]],
                         "decoded_sha256": sha256(data), "decoder": "tools/n64_texture.py"}}
    try:
        record["rgba"] = decode_texture(data, image["format"], image["size"], image["width"], image["height"])
        alpha = {record["rgba"][i] for i in range(3, len(record["rgba"]), 4)}
        record["alpha"] = "opaque" if alpha == {255} else ("mask" if alpha <= {0, 255} else "blend")
    except TextureError as error:
        record["status"] = f"undecodable: {error}"
    return record


def export_bt1(exporter: Exporter, rom: src.RomImage, slots: list[int] | None = None) -> None:
    exporter.sources.append({"platform": "n64_bt1", "title": "BattleTanx (N64 USA)", "rom_sha1": rom.sha1, "rom_sha256": rom.sha256})
    scenarios = src.bt1_scenarios(rom.data)
    by_world = {}
    for scenario in scenarios:
        by_world.setdefault(scenario["world"], []).append(scenario)
    for entry in src.bt1_stream_table(rom.data):
        if entry["world"] is None or (slots is not None and entry["slot"] not in slots):
            continue
        level = n64_bt1.load_level(rom.data, entry["slot"])
        scene, models = n64_bt1.scene_record(rom.data, rom.sha256, level, exporter.textures)
        scene["scenarios"] = by_world.get(entry["slot"], [])
        exporter.add_scene(scene, models, exporter.textures,
                           {"platform": "n64_bt1", "level": entry["slot"], "name": f"world {entry['slot']}", "variant": "default",
                            "campaign": "BattleTanx", "scenario_modes": sorted({s["mode"] for s in scene["scenarios"]})})


def export_ps1(exporter: Exporter, corpus: Path, stems: list[str] | None = None) -> None:
    from . import ps1_adapter
    from .ps1_formats import read_level_names

    executable = (Path(corpus) / "SLUS_010.44").read_bytes()
    exporter.sources.append({"platform": "ps1", "title": "BattleTanx: Global Assault (PS1 SLUS-01044)", "executable_sha256": sha256(executable)})
    level_names = {name.upper() for name in read_level_names(executable)}
    all_stems = sorted(p.relative_to(corpus).as_posix()[:-4] for p in Path(corpus).glob("ASSETS/*.BIN") if p.with_suffix(".MMM").is_file())
    # ENUMS is the shared object/vehicle bank, loaded by explicit path (0x8003D98C).
    enums = ps1_adapter.library_assets(Path(corpus), "ASSETS/ENUMS")
    for record in enums[0].values():
        record["source"]["library"] = "ps1_enums"
        exporter.models.setdefault(record["id"], record)
    for key, texture in enums[1].items():
        exporter.textures.setdefault(key, texture)
    for stem in all_stems:
        if stems is not None and stem not in stems:
            continue
        scene, models, textures = ps1_adapter.level_assets(Path(corpus), stem)
        name = stem.rsplit("/", 1)[-1]
        exporter.add_scene(scene, models, textures,
                           {"platform": "ps1", "level": name, "name": name, "variant": "default",
                            "campaign": re.sub(r"\d.*$", "", name).upper(), "in_level_table": name.upper() in level_names})


# ---------------------------------------------------------- inventories ----


def ga_inventory(rom: src.RomImage, layout: dict) -> dict:
    status = {
        "world": "decoded", "leftover": "decoded_structure", "image": "decoded", "texture_pool": "decoded_where_referenced",
        "state_pool": "decoded_where_referenced", "geometry_pool": "decoded_where_referenced",
    }
    regions = []
    for region in layout["regions"]:
        state = status.get(region["kind"], "preserved_raw")
        regions.append({"kind": region["kind"], "name": region["name"], "start": region["start"], "end": region["end"], "sha256": region["sha256"],
                        "status": state, "adapter": {"world": "n64_btga + tools/btga_world.py", "image": "tools/n64_texture.py",
                                                     "leftover": "n64_bt1.parse_world (pools absent in this ROM)"}.get(region["kind"])})
    return _inventory_summary("n64_btga", rom, regions)


def bt1_inventory(rom: src.RomImage) -> dict:
    data = rom.data
    regions = [{"kind": "header", "name": "rom_header", "start": 0, "end": 0x40}, {"kind": "ipl3", "name": "ipl3", "start": 0x40, "end": 0x1000},
               {"kind": "main_image", "name": "code_and_data", "start": 0x1000, "end": src.bt1_offset(0x80147040)}]
    for entry in src.bt1_stream_table(data):
        for key in ("world", "nav"):
            if entry[key]:
                start, end = entry[key]
                regions.append({"kind": key, "name": f"{key}_{entry['slot']:02d}", "start": start, "end": end})
    regions.append({"kind": "geometry_pool", "name": "geometry_pool", "start": src.BT1_POOLS["geometry"], "end": 0x49C740})
    regions.append({"kind": "texture_pool", "name": "texture_pool", "start": src.BT1_POOLS["texture"], "end": 0x6F9C40})
    regions = sorted({(r["start"], r["end"]): r for r in regions}.values(), key=lambda r: (r["start"], r["end"]))
    filled = []
    cursor = 0
    for region in regions:
        if region["start"] > cursor:
            filled.append({"kind": "unclassified", "name": f"gap_{cursor:06x}", "start": cursor, "end": region["start"]})
        start = max(region["start"], cursor)
        if region["end"] > start:
            filled.append({**region, "start": start, "overlap_trimmed": start != region["start"]})
            cursor = region["end"]
    if cursor < len(data):
        filled.append({"kind": "unclassified", "name": f"gap_{cursor:06x}", "start": cursor, "end": len(data)})
    status = {"world": "decoded", "nav": "decoded", "geometry_pool": "decoded_where_referenced", "texture_pool": "decoded_where_referenced",
              "main_image": "preserved_raw (scenario table 0x80142DE8 decoded)"}
    for region in filled:
        region["sha256"] = sha256(data[region["start"] : region["end"]])
        region["status"] = status.get(region["kind"], "preserved_raw" if region["kind"] in ("header", "ipl3") else "unsupported")
    inventory = _inventory_summary("n64_bt1", rom, filled)
    inventory["pool_extent_evidence"] = "pool ends taken from the next cartridge address the code references (hypothesis)"
    return inventory


def _inventory_summary(platform: str, rom: src.RomImage, regions: list[dict]) -> dict:
    total = sum(r["end"] - r["start"] for r in regions)
    if total != len(rom.data):
        raise FormatError(f"{platform} inventory covers {total} of {len(rom.data)} bytes")
    by_status: dict[str, int] = {}
    for region in regions:
        by_status[region["status"]] = by_status.get(region["status"], 0) + region["end"] - region["start"]
    return {"platform": platform, "rom_sha1": rom.sha1, "bytes": len(rom.data), "regions": regions, "bytes_by_status": dict(sorted(by_status.items()))}
