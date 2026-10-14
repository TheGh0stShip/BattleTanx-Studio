"""Assemble a PS1 level into a neutral scene manifest.

Resolution rules, all from the retail executable:

* `assets\\<level>` + .MMM/.TTT/.img/.plt/.flb/.bin are loaded together
  (0x8003BDB4, 0x8003D5B8, 0x8003DE58).
* Level BIN section 2 holds placements; section 1's {count, first} pair
  selects which ones 0x8002457C places.  Position is s16 << 14 in 20.12
  space (x4 model units); the u16 at +6 is a yaw angle, 65536 per turn,
  turned into [[c,0,s],[0,1,0],[-s,0,c]] by 0x800249A0.
* The u32 at +8 points into section 3.  Byte 0 there is an object kind
  dispatched through the 44-entry table at 0x800BD6A4 by 0x80025094.
  Kinds 0, 1, 11, 14, 22 and 43 read the u16 at +2 as a level MMM model
  index (0xFFFF = no model); other kinds go to dedicated handlers whose
  records are retained raw.
* A model's texture slot indexes its TTT list, whose entries index the
  level IMG/PLT records (0x8003DB70).  The CLUT row is a per-instance
  runtime value (0x80069244 -> 0x8008DC5C); exports use row 0.
"""

from __future__ import annotations

from pathlib import Path

from . import ps1_formats as pf
from . import ps1_model as pm
from .neutral import PS1_COORDINATES, envelope, ps1_model_record, safe_source_path, stable_id

MODEL_KINDS = (0, 1, 11, 12, 14, 22, 43)
# Offset of the u16 model index: kinds 0/1/11/14/22/43 read +2; kind 12
# (0x80025768) reads +4 before the same footprint path.
MODEL_FIELD = {0: 2, 1: 2, 11: 2, 12: 4, 14: 2, 22: 2, 43: 2}
NO_MODEL = 0xFFFF
KIND_TABLE = 0x800BD6A4


class SceneError(pf.FormatError):
    pass


class LevelSet:
    """The files of one level stem, read from a corpus root."""

    def __init__(self, corpus: Path, stem: str):
        self.corpus = Path(corpus)
        self.stem = safe_source_path(stem)
        self.files: dict[str, tuple[str, bytes]] = {}
        for extension in (".MMM", ".TTT", ".IMG", ".PLT", ".BIN", ".FLB", ".TEX"):
            relative = f"{self.stem}{extension}"
            path = self.corpus / relative
            if path.is_file():
                self.files[extension] = (relative, path.read_bytes())

    def require(self, extension: str) -> tuple[str, bytes]:
        if extension not in self.files:
            raise SceneError(f"{self.stem}: missing {extension} file")
        return self.files[extension]


def texture_table(level: LevelSet) -> tuple[dict[int, dict], list[str]]:
    """Decode every IMG/PLT record of the level once; returns index -> texture."""

    problems = []
    if ".IMG" not in level.files or ".PLT" not in level.files:
        return {}, [f"{level.stem}: no IMG/PLT pair; textured primitives stay unresolved"]
    img_path, img_data = level.files[".IMG"]
    plt_path, plt_data = level.files[".PLT"]
    img = pf.parse_img(img_data, img_path)
    plt = pf.parse_plt(plt_data, plt_path)
    textures = {}
    img_sha = pf.sha256(img_data)
    for texture in pf.pair_img_plt(img, plt, img_path):
        rgba = pf.texture_rgba(img_data, texture)
        textures[texture.index] = {
            "key": stable_id("ps1", img_sha, img_path, "texture", texture.index),
            "name": f"{img_path}#{texture.index}",
            "png": pf.encode_png_rgba(texture.width, texture.height, rgba),
            "width": texture.width,
            "height": texture.height,
            "has_transparent_texels": any(rgba[offset] == 0 for offset in range(3, len(rgba), 4)),
            "metadata": {
                "id": stable_id("ps1", img_sha, img_path, "texture", texture.index),
                "file": img_path,
                "file_sha256": img_sha,
                "record": texture.index,
                "pixels": pf.raw_range(img_data, texture.pixel_offset, texture.pixel_size),
                "palette_file": plt_path,
                "palette_row": 0,
                "palette_rows_available": len(texture.palettes),
                "palette_row_evidence": "proven per-instance runtime value (0x8007D4FC adds row << 6 to the CLUT)",
                "mode": pf.MODE_NAMES[texture.mode],
                "uv_unit": "texel (0x8007CB50 adds the texture's page offset at draw time)",
            },
        }
    return textures, problems


def model_textures(record: dict, textures: dict[int, dict], problems: list[str]) -> dict[int, dict | None]:
    out: dict[int, dict | None] = {}
    texture_list = record["texture_list"] or []
    for primitive in record["primitives"]:
        slot = primitive["texture_slot"]
        if not primitive["textured"] or slot is None or slot in out:
            continue
        if slot >= len(texture_list):
            problems.append(f"{record['id']}: texture slot {slot} beyond its {len(texture_list)}-entry TTT list")
            out[slot] = None
            continue
        out[slot] = textures.get(texture_list[slot])
        if out[slot] is None and textures:
            problems.append(f"{record['id']}: TTT entry {texture_list[slot]} has no IMG record")
    return out


def decode_level_models(level: LevelSet) -> dict[int, dict]:
    mmm_path, mmm_data = level.require(".MMM")
    ttt = None
    if ".TTT" in level.files:
        ttt_path, ttt_data = level.files[".TTT"]
        ttt = pf.parse_ttt(ttt_data, ttt_path)
    sha = pf.sha256(mmm_data)
    models = {}
    for index, model in pm.decode_bank(mmm_data, mmm_path):
        if model is None:
            continue
        texture_list = ttt["lists"][index]["textures"] if ttt and index < ttt["count"] else None
        models[index] = ps1_model_record(model, path=mmm_path, file_sha256=sha, bank_index=index, texture_list=texture_list)
    return models


def assemble_scene(level: LevelSet) -> tuple[dict, dict[int, dict], dict[int, dict]]:
    """Return (scene manifest, models used, per-model texture maps)."""

    bin_path, bin_data = level.require(".BIN")
    bin_sha = pf.sha256(bin_data)
    parsed = pf.parse_level_bin(bin_data, bin_path)
    models = decode_level_models(level)
    textures, problems = texture_table(level)
    model_count = pf.parse_mmm(level.require(".MMM")[1])["count"]

    offsets = parsed["offsets"]
    section3_start, section3_end = offsets[3], offsets[4]
    targets = sorted({placement["section3_offset"] for placement in parsed["placements"]})
    extents = dict(zip(targets, targets[1:] + [section3_end - section3_start]))
    first, count = parsed["section1_first"], parsed["section1_count"]

    placements = []
    used: set[int] = set()
    for index, placement in enumerate(parsed["placements"]):
        record_offset = section3_start + placement["section3_offset"]
        kind = bin_data[record_offset]
        entry = {
            "id": stable_id("ps1", bin_sha, bin_path, "placement", index),
            "index": index,
            "placed_by_loader": first <= index < first + count,
            "position_raw": placement["position"],
            "translation": [value * 4 for value in placement["position"]],
            "word_6": placement["word_6"],
            "yaw_turns": placement["word_6"] / 65536,
            "kind": kind,
            "kind_handler": None,
            "section3": pf.raw_range(bin_data, record_offset, extents[placement["section3_offset"]]),
            "model_index": None,
            "model_id": None,
            "status": "resolved",
        }
        if kind >= 0x2C:
            entry["status"] = "invalid kind (outside the 44-entry table)"
        elif kind in MODEL_KINDS:
            field_offset = record_offset + MODEL_FIELD[kind]
            model_index = int.from_bytes(bin_data[field_offset : field_offset + 2], "little")
            if model_index == NO_MODEL:
                entry["status"] = "no model"
            elif model_index >= model_count or model_index not in models:
                entry["status"] = f"model index {model_index} outside the {model_count}-model bank"
                problems.append(f"{entry['id']}: {entry['status']}")
            else:
                entry["model_index"] = model_index
                entry["model_id"] = models[model_index]["id"]
                used.add(model_index)
        else:
            entry["status"] = "kind handled by dedicated code; model reference not decoded"
            entry["kind_handler"] = f"table 0x{KIND_TABLE:08X}[{kind}]"
        placements.append(entry)

    texture_maps = {index: model_textures(models[index], textures, problems) for index in sorted(used)}
    floor = None
    if ".FLB" in level.files:
        flb_path, flb_data = level.files[".FLB"]
        flb = pf.parse_flb(flb_data, flb_path)
        floor = {
            "file": flb_path,
            "file_sha256": pf.sha256(flb_data),
            "dimension": flb["dimension"],
            "cell_index_rule": "floor coordinate c = model units / 4; point = (c >> 7) + dimension / 2 (0x8003AF64); "
            "object lookup col = ((256 - x) >> 9) + dimension / 2, row = ((z + 256) >> 9) + dimension / 2 (0x8003BADC)",
            "cell_size": 512,
            "cell_size_evidence": "proven: the floor camera is the viewer position >> 12 in model units (0x80039F80..0x8003A05C), "
            "vertices are 4 * coordinate - camera and a quad edge is 0x200 (0x8003B094..0x8003B0D8); values are lattice points",
            "header_word_2": flb["header_word_2"],
            "shade_histogram": {str(key): value for key, value in flb["shade_histogram"].items()},
            "unreachable_tail": flb["unreachable_tail"],
            "collision": None,
            "navigation": None,
            "note": "cell bits 0-6 drive the near floor vertex colour, bits 24-31 the far floor colour, bits 0-14 are cleared "
            "under destroyed objects; other bits unexplained; no collision or "
            "navigation data has been proven in the PS1 files",
        }

    scene = envelope(
        "scene",
        stable_id("ps1", bin_sha, bin_path, "scene", 0),
        {"platform": "ps1", "file": bin_path, "file_sha256": bin_sha, "level": level.stem},
    )
    status_counts: dict[str, int] = {}
    for placement in placements:
        status_counts[placement["status"]] = status_counts.get(placement["status"], 0) + 1
    scene.update(
        {
            "coordinate_system": PS1_COORDINATES,
            "files": {extension: {"file": path, "sha256": pf.sha256(data)} for extension, (path, data) in sorted(level.files.items())},
            "section1": {"first": first, "count": count, "reads": parsed["section1_reads"]},
            "placements": placements,
            "models": {str(index): models[index]["id"] for index in sorted(used)},
            "floor": floor,
            "placement_status": dict(sorted(status_counts.items())),
            "dependency_problems": sorted(set(problems)),
        }
    )
    return scene, {index: models[index] for index in sorted(used)}, texture_maps
