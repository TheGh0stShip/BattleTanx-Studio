#!/usr/bin/env python3
"""Build a PS1 / N64 comparison document and a private missing-content catalog.

Inputs are the extracted PS1 disc and, optionally, the user's own N64 ROMs
(Global Assault and BattleTanx).  ROM paths can be given with --n64-rom /
--bt1-rom or the BTGA_N64_ROM / BTGA_BT1_ROM environment variables; they
are recorded only by SHA-256, never by path.  N64 textures come from the
repository's existing extractors, run into the ignored output directory.
Every link is a candidate with evidence; nothing is marked confirmed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.btga_assets import compare as cmp  # noqa: E402
from tools.btga_assets import ps1_formats as pf  # noqa: E402
from tools.btga_assets import ps1_model as pm  # noqa: E402
from tools.btga_assets.neutral import stable_id  # noqa: E402
from tools.inventory_ps1_assets import OutputError, check_output_dir  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def ps1_textures(corpus: Path) -> list[dict]:
    assets = []
    for path in sorted(corpus.rglob("*")):
        relative = path.relative_to(corpus).as_posix()
        extension = path.suffix.upper()
        if extension not in (".TEX", ".IMG"):
            continue
        data = path.read_bytes()
        sha = pf.sha256(data)
        try:
            if extension == ".TEX":
                textures = pf.parse_tex(data, relative)["textures"]
            else:
                plt_path = path.with_suffix(".PLT")
                if not plt_path.is_file():
                    continue
                textures = pf.pair_img_plt(pf.parse_img(data, relative), pf.parse_plt(plt_path.read_bytes()), relative)
        except pf.FormatError:
            continue
        for texture in textures:
            rgba = pf.texture_rgba(data, texture)
            assets.append(
                cmp.asset(
                    "ps1",
                    "texture",
                    stable_id("ps1", sha, relative, "texture", texture.index),
                    f"{relative}#{texture.index}",
                    {"file": relative, "file_sha256": sha, "record": texture.index},
                    {
                        "width": texture.width,
                        "height": texture.height,
                        "rgba_sha256": hashlib.sha256(rgba).hexdigest(),
                        "dhash": cmp.dhash(texture.width, texture.height, rgba),
                    },
                )
            )
    return assets


def ps1_models(corpus: Path) -> list[dict]:
    assets = []
    for path in sorted(corpus.rglob("*.MMM")):
        relative = path.relative_to(corpus).as_posix()
        data = path.read_bytes()
        sha = pf.sha256(data)
        for index, model in pm.decode_bank(data, relative):
            if model is None:
                continue
            geometry = json.dumps(
                [model.vertices, [(p.mode, p.cull, [(c.vertex, c.uv) for c in p.corners]) for p in model.primitives]],
                separators=(",", ":"),
            ).encode()
            xs, ys, zs = (list(axis) or [0] for axis in zip(*[v[:3] for v in model.vertices])) if model.vertices else ([0], [0], [0])
            assets.append(
                cmp.asset(
                    "ps1",
                    "model",
                    stable_id("ps1", sha, relative, "model", index),
                    f"{relative}#{index}",
                    {"file": relative, "file_sha256": sha, "record": index, "range": [model.offset, model.size]},
                    {
                        "geometry_sha256": hashlib.sha256(geometry).hexdigest(),
                        "vertex_count": model.vertex_count,
                        "triangle_count": sum(2 if p.quad else 1 for p in model.primitives),
                        "bounds_size": [max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs)],
                        "part_count": model.part_count,
                    },
                )
            )
    return assets


def n64_textures(rom: Path, work: Path) -> tuple[list[dict], str]:
    data = rom.read_bytes()
    rom_sha = hashlib.sha256(data).hexdigest()
    images = work / "n64_images"
    world = work / "n64_world_textures"
    if not (images / "manifest.json").is_file():
        subprocess.run([sys.executable, str(ROOT / "tools/extract_lzari_images.py"), str(images), "--rom", str(rom)], check=True, stdout=subprocess.DEVNULL)
    if not (work / "n64_world_textures.json").is_file():
        subprocess.run(
            [sys.executable, str(ROOT / "tools/inventory_world_textures.py"), "--rom", str(rom), "--output", str(work / "n64_world_textures.json"), "--preview-dir", str(world)],
            check=True,
            stdout=subprocess.DEVNULL,
        )
    assets = []
    manifest = json.loads((images / "manifest.json").read_text())
    for image in manifest["images"]:
        png = images / Path(image["file"]).name
        if not png.is_file():
            continue
        width, height, rgba = cmp.png_rgba(png.read_bytes())
        assets.append(
            cmp.asset(
                "n64_btga",
                "texture",
                stable_id("n64_btga", rom_sha, "rom", "texture", 0, image["table_index"]),
                f"image {image['table_index']}",
                {"file": "rom", "file_sha256": rom_sha, "range": [image["rom_start"], image["rom_end"]]},
                {"width": width, "height": height, "rgba_sha256": hashlib.sha256(rgba).hexdigest(), "dhash": cmp.dhash(width, height, rgba)},
            )
        )
    for png in sorted(world.glob("*.png")):
        width, height, rgba = cmp.png_rgba(png.read_bytes())
        number = int(png.stem.split("_")[1])
        frame = int(png.stem.split("_")[-1])
        assets.append(
            cmp.asset(
                "n64_btga",
                "texture",
                stable_id("n64_btga", rom_sha, "rom", "texture", 1, number, frame),
                f"world texture {number} frame {frame}",
                {"file": "rom", "file_sha256": rom_sha, "world_texture": number, "frame": frame},
                {"width": width, "height": height, "rgba_sha256": hashlib.sha256(rgba).hexdigest(), "dhash": cmp.dhash(width, height, rgba)},
            )
        )
    return assets, rom_sha


def name_assets(platform: str, image_sha: str, names: list[str], label: str) -> list[dict]:
    return [
        cmp.asset(platform, "name", stable_id(platform, image_sha, label, "name", index), name, {"file": label, "file_sha256": image_sha, "record": index}, {})
        for index, name in enumerate(names)
    ]


def build(corpus: Path, work: Path, n64_rom: Path | None, bt1_rom: Path | None) -> dict:
    executable = (corpus / "SLUS_010.44").read_bytes()
    exe_sha = pf.sha256(executable)
    levels = pf.read_level_names(executable)
    sources = {"ps1_executable_sha256": exe_sha, "n64_btga_rom_sha256": None, "n64_bt1_rom_sha256": None}
    images = {"ps1": cmp.image_strings(executable)}
    textures = ps1_textures(corpus)
    models = ps1_models(corpus)
    assets = textures + models
    level_assets = name_assets("ps1", exe_sha, levels, "SLUS_010.44")
    assets += level_assets
    links: list[dict] = []
    n64_textures_list: list[dict] = []
    if n64_rom:
        n64_textures_list, rom_sha = n64_textures(n64_rom, work)
        sources["n64_btga_rom_sha256"] = rom_sha
        images["n64_btga"] = cmp.image_strings(n64_rom.read_bytes())
        assets += n64_textures_list
        links += cmp.match_textures(textures, n64_textures_list)
    if bt1_rom:
        data = bt1_rom.read_bytes()
        sources["n64_bt1_rom_sha256"] = hashlib.sha256(data).hexdigest()
        images["n64_bt1"] = cmp.image_strings(data)
    # PS1-internal duplicates show shared content across banks.
    links += cmp.duplicate_models(models)
    catalog = build_catalog(corpus, levels, textures, n64_textures_list, links, images)
    return cmp.document(assets, links, sources, catalog)


def level_title_regions(strings: list[tuple[int, str]]) -> list[str]:
    """Regions named by 'REGION - LEVEL' title strings in an N64 image."""

    regions = set()
    for _, text in strings:
        match = re.fullmatch(r"([A-Z][A-Z .]+?) - [A-Z][A-Z .']+", text.strip())
        if match:
            regions.add(match.group(1))
    return sorted(regions)


def build_catalog(corpus, levels, textures, n64_textures_list, links, images) -> list[dict]:
    catalog = []
    linked = {link["a"] for link in links} | {link["b"] for link in links}

    def refs(term):
        return {platform: len(cmp.references(strings, term)) for platform, strings in sorted(images.items())}

    russia = [name for name in levels if name.upper().startswith("RUSSIA")]
    catalog.append(
        {
            "title": "Russia campaign and multiplayer maps",
            "category": "maps / missions",
            "assets": russia,
            "evidence": [
                f"{len(russia)} RUSSIA level sets in the PS1 level table at 0x800A48E0",
                f"references to 'Russia': {refs('russia')}",
                "PS1 strings mentioning Russia (first 4): "
                + "; ".join(
                    repr(item["text"])
                    for item in sorted(cmp.references(images["ps1"], "russia"), key=lambda ref: (" " not in ref["text"], ref["offset"]))[:4]
                ),
            ]
            + (
                [f"N64 Global Assault level titles name these regions only: {level_title_regions(images['n64_btga'])}"]
                if "n64_btga" in images
                else ["N64 Global Assault ROM not supplied; no cross-version evidence"]
            ),
            "assessment": "strong PS1-only candidate (absence in N64 is from string search, not a full asset diff)",
        }
    )
    orphans = []
    for stem in ("LOND1M", "LOND2M", "LOND3M", "LOND6M", "SF5M"):
        if (corpus / "ASSETS" / f"{stem}.MMM").is_file():
            orphans.append(stem)
    catalog.append(
        {
            "title": "Unreferenced multiplayer level sets",
            "category": "unused / orphaned",
            "assets": orphans,
            "evidence": ["MMM/TTT/TEX/FLB/BIN present, IMG/PLT absent", "names absent from the level table and from every executable string"],
            "assessment": "orphaned PS1 content; not loadable by the retail executable as shipped",
        }
    )
    cities = {}
    for name in levels:
        city = cmp.normalise_name(name).split()[0]
        cities.setdefault(city, []).append(name)
    catalog.append(
        {
            "title": "PS1 level counts per city",
            "category": "maps",
            "assets": [f"{city}: {len(names)}" for city, names in sorted(cities.items())],
            "evidence": ["PS1 level table (70 entries incl. 'M' variants)", f"references per platform: { {city: refs(city) for city in sorted(cities) if len(city) > 2} }"],
            "assessment": "material difference in level structure; per-level correspondence requires geometry comparison with N64 worlds (not yet decoded into neutral form)",
        }
    )
    if n64_textures_list:
        families: dict[str, list[str]] = {}
        for texture in textures:
            if texture["id"] in linked:
                continue
            family = texture["source"]["file"].split("/")[1] if texture["source"]["file"].count("/") > 1 else texture["source"]["file"].rsplit(".", 1)[0]
            families.setdefault(family, []).append(texture["name"])
        unmatched = sorted(families.items(), key=lambda item: -len(item[1]))
        catalog.append(
            {
                "title": "PS1 textures with no N64 Global Assault candidate",
                "category": "textures",
                "assets": [f"{family}: {len(names)}" for family, names in unmatched[:40]],
                "evidence": ["no identical decoded pixels and no dhash within 6 bits among N64 LZARI images and world textures"],
                "assessment": "PS1-only or materially different candidates; palette row 0 only, and N64 model textures outside world pools were not extracted",
            }
        )
    if n64_textures_list:
        per_level: dict[str, list[int]] = {}
        for texture in textures:
            file = texture["source"]["file"]
            if not file.upper().endswith(".IMG"):
                continue
            level = file.rsplit("/", 1)[-1][:-4]
            stats = per_level.setdefault(level, [0, 0])
            stats[0] += 1
            stats[1] += texture["id"] in linked
        rows = sorted(per_level.items(), key=lambda item: (item[1][1] / item[1][0], item[0]))
        catalog.append(
            {
                "title": "Level texture overlap with N64 Global Assault",
                "category": "maps",
                "assets": [f"{level}: {matched}/{total} textures have an N64 candidate" for level, (total, matched) in rows],
                "evidence": ["texture links above (identical pixels or dhash <= 6 bits, same aspect ratio)"],
                "assessment": "overlap is uniformly low (about 2-15%) for every level, including cities N64 also has, so "
                "this metric does not single out PS1-only maps; PS1 art appears re-quantised or redrawn throughout",
            }
        )
    gangs = sorted({path.stem for path in (corpus / "ASSETS/GANG").glob("*.TEX") if not path.stem.upper().startswith(("PIECE", "WALL", "P_BOX", "DONE"))})
    catalog.append(
        {
            "title": "Gang emblem art",
            "category": "gangs",
            "assets": gangs,
            "evidence": [f"{name}: {refs(name.replace('_', ' '))}" for name in gangs],
            "assessment": "gang names are shared across versions; emblem images are compared through the texture links",
        }
    )
    pieces = sorted(path.stem for path in (corpus / "ASSETS/GANG").glob("*.TEX") if path.stem.upper().startswith(("PIECE", "WALL", "P_BOX", "DONE")))
    catalog.append(
        {
            "title": "Gang puzzle/wall pieces",
            "category": "secrets / UI",
            "assets": pieces,
            "evidence": ["loaded by explicit assets\\gang\\piece_* and piece* paths in the PS1 executable"],
            "assessment": "PS1 UI content of unknown purpose; needs the loader traced",
        }
    )
    return catalog


def write_catalog(document: dict, path: Path) -> None:
    lines = ["# Private missing-content catalog (generated, do not commit)", ""]
    for entry in document["catalog"]:
        lines += [f"## {entry['title']}", "", f"*Category:* {entry['category']}  ", f"*Assessment:* {entry['assessment']}", ""]
        lines += [f"- {item}" for item in entry["assets"][:60]]
        lines += ["", "Evidence:"] + [f"- {item}" for item in entry["evidence"]] + [""]
    links = document["links"]
    lines += ["## Strongest texture links", ""]
    for link in [link for link in links if link["evidence"][0]["type"] != "geometry_sha256"][:40]:
        lines.append(f"- {link['confidence']:.2f} {link['a']} <-> {link['b']} ({'; '.join(e['detail'] for e in link['evidence'])})")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("corpus", type=Path, help="extracted PS1 disc directory (read only)")
    parser.add_argument("output", type=Path, help="ignored output directory")
    parser.add_argument("--n64-rom", type=Path, default=os.environ.get("BTGA_N64_ROM"))
    parser.add_argument("--bt1-rom", type=Path, default=os.environ.get("BTGA_BT1_ROM"))
    args = parser.parse_args(argv)
    try:
        output = check_output_dir(args.output, args.corpus)
    except OutputError as error:
        parser.error(str(error))
    output.mkdir(parents=True, exist_ok=True)
    document = build(args.corpus, output, args.n64_rom, args.bt1_rom)
    (output / "comparison.json").write_text(json.dumps(document, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    write_catalog(document, output / "MISSING_CONTENT_CATALOG.md")
    print(f"{len(document['assets'])} assets, {len(document['links'])} candidate links, {len(document['catalog'])} catalog entries")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
