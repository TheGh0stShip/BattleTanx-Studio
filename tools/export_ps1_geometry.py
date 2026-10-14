#!/usr/bin/env python3
"""Export decoded PS1 models and level scenes as glTF 2.0 (GLB) plus neutral JSON.

Output goes only to a git-ignored directory (or outside the repository) and
never into the extracted disc; file names are derived from stable IDs, not
from disc names, so nothing in the corpus can steer where files land.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.btga_assets import gltf  # noqa: E402
from tools.btga_assets import ps1_formats as pf  # noqa: E402
from tools.btga_assets.scene import LevelSet, assemble_scene, decode_level_models, model_textures, texture_table  # noqa: E402
from tools.inventory_ps1_assets import OutputError, check_output_dir  # noqa: E402


def dump_json(record) -> str:
    return json.dumps(record, indent=1, sort_keys=True) + "\n"


def file_name(record_id: str) -> str:
    return record_id.replace(":", "_")


def model_stems(corpus: Path) -> list[str]:
    return sorted(path.relative_to(corpus).as_posix()[:-4] for path in corpus.rglob("*.MMM"))


def export_models(corpus: Path, stem: str, output: Path) -> dict:
    level = LevelSet(corpus, stem)
    models = decode_level_models(level)
    textures, problems = texture_table(level)
    target = output / "models"
    target.mkdir(parents=True, exist_ok=True)
    summary = {"stem": level.stem, "models": 0, "primitives": 0, "problems": problems}
    for index, record in sorted(models.items()):
        texture_map = model_textures(record, textures, summary["problems"])
        name = file_name(record["id"])
        (target / f"{name}.glb").write_bytes(gltf.model_glb(record, texture_map))
        (target / f"{name}.json").write_text(dump_json(record), encoding="utf-8")
        summary["models"] += 1
        summary["primitives"] += len(record["primitives"])
    return summary


def export_scene(corpus: Path, stem: str, output: Path) -> dict:
    level = LevelSet(corpus, stem)
    scene, models, texture_maps = assemble_scene(level)
    target = output / "scenes"
    target.mkdir(parents=True, exist_ok=True)
    name = file_name(scene["id"])
    (target / f"{name}.glb").write_bytes(gltf.scene_glb(scene, models, texture_maps))
    (target / f"{name}.json").write_text(dump_json(scene), encoding="utf-8")
    return {"stem": level.stem, "scene": name, "placements": len(scene["placements"]), "status": scene["placement_status"], "problems": scene["dependency_problems"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("corpus", type=Path, help="extracted disc directory (read only)")
    parser.add_argument("output", type=Path, help="ignored output directory")
    parser.add_argument("--models", action="append", default=[], metavar="STEM", help="export every model of a bank, e.g. ASSETS/BERLIN1; 'all' for every bank")
    parser.add_argument("--scene", action="append", default=[], metavar="STEM", help="export a level scene, e.g. ASSETS/BERLIN1")
    args = parser.parse_args(argv)
    if not args.corpus.is_dir():
        parser.error(f"{args.corpus} is not a directory")
    try:
        output = check_output_dir(args.output, args.corpus)
    except OutputError as error:
        parser.error(str(error))
    stems = model_stems(args.corpus) if "all" in args.models else args.models
    output.mkdir(parents=True, exist_ok=True)
    report = {"models": [], "scenes": []}
    try:
        for stem in stems:
            report["models"].append(export_models(args.corpus, stem, output))
        for stem in args.scene:
            report["scenes"].append(export_scene(args.corpus, stem, output))
    except pf.FormatError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    (output / "export_report.json").write_text(dump_json(report), encoding="utf-8")
    for item in report["models"]:
        print(f"models {item['stem']}: {item['models']} models, {item['primitives']} primitives, {len(item['problems'])} problems")
    for item in report["scenes"]:
        print(f"scene {item['stem']}: {item['placements']} placements {item['status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
