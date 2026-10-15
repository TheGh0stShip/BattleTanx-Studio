"""Inspect a Studio catalog: levels, marker categories, the models markers depend on, and model contact sheets.

Works on any catalog Studio's Source Manager builds (or the starter catalog), so
other BattleTanx projects can reuse it.  It reads only the catalog folder and
prints or writes summaries; it never reads a ROM and never writes a path into
its output (catalog folders are named by content hash).

    python tools/catalog_inspect.py CATALOG levels
    python tools/catalog_inspect.py CATALOG markers SCENE_ID [--category pickup ...]
    python tools/catalog_inspect.py CATALOG sheet OUTPUT.png MODEL_ID [MODEL_ID ...]
    python tools/catalog_inspect.py CATALOG find --category powerup_pad --category crate

``find`` lists, across all levels, every model a marker of the given
categories depends on, with how many placements use it and in which levels:
the quickest way to find a pickup, base or prop model by role rather than by
guessing from pictures.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.btga_assets.studio import StudioLibrary  # noqa: E402


def level_summary(library) -> list[dict]:
    """One row per level: identity, name, record counts, environment, minimap and navigation state, marker categories."""

    rows = []
    for level in library.levels():
        scene = library.scene(level.id)
        categories: dict[str, int] = defaultdict(int)
        for marker in scene.get("markers", []):
            categories[marker.get("category", "?")] += 1
        navigation = scene.get("navigation") or {}
        rows.append({"id": level.id, "name": level.name, "campaign": level.campaign, "variant": level.variant,
                     "instances": level.instances, "markers": level.markers, "collision": level.collision,
                     "environment": len(scene.get("environment") or []), "minimap": (scene.get("minimap") or {}).get("status"),
                     "navigation_points": len(navigation.get("points") or []) if navigation else 0,
                     "navigation": "decoded" if navigation else "absent",
                     "categories": dict(sorted(categories.items()))})
    return rows


def marker_models(scene: dict, categories=None) -> dict[str, dict[str, list[str]]]:
    """category -> dependency field -> sorted model IDs, for the markers of one scene."""

    out: dict[str, dict[str, set]] = defaultdict(lambda: defaultdict(set))
    for marker in scene.get("markers", []):
        if categories and marker.get("category") not in categories:
            continue
        for dependency in marker.get("dependencies", []):
            if dependency.get("kind") == "model" and dependency.get("id"):
                out[marker["category"]][dependency.get("field", "?")].add(dependency["id"])
    return {category: {name: sorted(ids) for name, ids in sorted(fields.items())} for category, fields in sorted(out.items())}


def find_models(library, categories) -> list[dict]:
    """Every model that markers of ``categories`` depend on, across all levels, most used first."""

    uses: dict[str, dict] = {}
    for level in library.levels():
        scene = library.scene(level.id)
        for marker in scene.get("markers", []):
            if marker.get("category") not in categories:
                continue
            for dependency in marker.get("dependencies", []):
                if dependency.get("kind") != "model" or not dependency.get("id"):
                    continue
                item = uses.setdefault(dependency["id"], {"model": dependency["id"], "placements": 0, "levels": set(), "categories": set(),
                                                          "fields": set()})
                item["placements"] += 1
                item["levels"].add(level.name)
                item["categories"].add(marker["category"])
                item["fields"].add(dependency.get("field", "?"))
    rows = []
    for item in uses.values():
        model = library._models.get(item["model"], {})
        rows.append({**item, "levels": sorted(item["levels"]), "categories": sorted(item["categories"]), "fields": sorted(item["fields"]),
                     "triangles": model.get("triangles"), "bounds": model.get("bounds"), "library": model.get("library")})
    return sorted(rows, key=lambda r: (-r["placements"], r["model"]))


def contact_sheet(library, model_ids: list[str], size: int = 128, columns: int = 8, labels: list[str] | None = None) -> bytes:
    """PNG of model thumbnails in a grid, each with its position number drawn in the corner."""

    import numpy as np

    from tools.btga_assets.ps1_formats import encode_png_rgba

    digits = {"0": "111101101101111", "1": "010110010010111", "2": "111001111100111", "3": "111001111001111", "4": "101101111001001",
              "5": "111100111001111", "6": "111100111101111", "7": "111001001001001", "8": "111101111101111", "9": "111101111001111"}
    rows = max(1, (len(model_ids) + columns - 1) // columns)
    image = np.zeros((rows * size, columns * size, 4), np.uint8)
    image[..., 3] = 255
    for number, model_id in enumerate(model_ids):
        row, column = divmod(number, columns)
        if model_id in library._models:
            tile = np.frombuffer(library.thumbnail(model_id, size), np.uint8).reshape(size, size, 4)
            image[row * size:(row + 1) * size, column * size:(column + 1) * size] = tile
        label = (labels[number] if labels else str(number))
        for k, character in enumerate(label[:6]):
            bits = digits.get(character)
            if bits is None:
                continue
            for r in range(5):
                for c in range(3):
                    if bits[r * 3 + c] == "1":
                        y, x = row * size + 3 + r * 2, column * size + 3 + k * 8 + c * 2
                        image[y:y + 2, x:x + 2] = (255, 255, 0, 255)
    return encode_png_rgba(image.shape[1], image.shape[0], image.tobytes())


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("catalog", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("levels")
    markers = sub.add_parser("markers")
    markers.add_argument("scene")
    markers.add_argument("--category", action="append")
    find = sub.add_parser("find")
    find.add_argument("--category", action="append", required=True)
    sheet = sub.add_parser("sheet")
    sheet.add_argument("output", type=Path)
    sheet.add_argument("models", nargs="+")
    args = parser.parse_args(argv)
    library = StudioLibrary.open(args.catalog)
    if args.command == "levels":
        print(json.dumps(level_summary(library), indent=1, sort_keys=True))
    elif args.command == "markers":
        print(json.dumps(marker_models(library.scene(args.scene), set(args.category or [])), indent=1, sort_keys=True))
    elif args.command == "find":
        print(json.dumps(find_models(library, set(args.category)), indent=1, sort_keys=True))
    else:
        args.output.write_bytes(contact_sheet(library, args.models))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
