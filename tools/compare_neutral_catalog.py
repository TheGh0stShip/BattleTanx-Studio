#!/usr/bin/env python3
"""Compare PS1, N64 Global Assault and N64 BattleTanx assets through a neutral catalog.

Reads an export written by export_neutral_assets.py and writes, under an
ignored output directory, a btga-compare document of scored candidates,
a private missing-content / restoration backlog, and contact sheets.
Nothing is marked confirmed; texture-only or name-only evidence yields a
"needs visual inspection" candidate at most.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.btga_assets import compare as cmp  # noqa: E402
from tools.btga_assets import cross_platform as xp  # noqa: E402
from tools.btga_assets import preview  # noqa: E402
from tools.inventory_ps1_assets import OutputError, check_output_dir  # noqa: E402

PAIRS = (("ps1", "n64_btga"), ("n64_bt1", "n64_btga"), ("ps1", "n64_bt1"))
DISTINCTIVE = 3
LAYOUT_LIMIT = 0.3
CITY_TOKENS = {
    "berlin": "germany", "germany": "germany", "paris": "paris", "london": "london", "texas": "texas", "dc": "washington",
    "washington": "washington", "sf": "san francisco", "san": "san francisco", "russia": "russia", "arizona": "arizona", "route": "route 66",
}


def load_catalog(root: Path) -> dict:
    return json.loads((root / "catalog.json").read_text())


def city(level: dict) -> str | None:
    name = (level.get("name") or str(level["level"])).lower()
    for token in re.findall(r"[a-z]+", name):
        if token in CITY_TOKENS:
            return CITY_TOKENS[token]
    return None


def texture_fingerprints(root: Path, catalog: dict) -> dict[str, dict]:
    out = {}
    for texture in catalog["textures"]:
        if not texture.get("file"):
            continue
        width, height, rgba = cmp.png_rgba((root / texture["file"]).read_bytes())
        out[texture["id"]] = {"width": width, "height": height, "rgba_sha256": hashlib.sha256(rgba).hexdigest(), "dhash": cmp.dhash(width, height, rgba),
                              "platform": texture["id"].split(":")[0]}
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("export", type=Path, help="directory written by export_neutral_assets.py")
    parser.add_argument("output", type=Path, help="ignored output directory")
    parser.add_argument("--sheet-pairs", type=int, default=24)
    args = parser.parse_args(argv)
    try:
        output = check_output_dir(args.output, args.export)
    except OutputError as error:
        parser.error(str(error))
    output.mkdir(parents=True, exist_ok=True)
    catalog = load_catalog(args.export)

    signatures: dict[str, dict] = {}
    model_platform: dict[str, str] = {}
    for entry in catalog["models"]:
        record = json.loads((args.export / entry["file"]).read_text())
        signature = xp.model_signature(record)
        model_platform[entry["id"]] = entry["platform"]
        if signature:
            signatures[entry["id"]] = signature
    by_platform: dict[str, list] = defaultdict(list)
    for model_id, signature in sorted(signatures.items()):
        by_platform[model_platform[model_id]].append((model_id, signature))

    model_links = []
    for left, right in PAIRS:
        if by_platform[left] and by_platform[right]:
            model_links += xp.match_models(by_platform[left], by_platform[right])

    fingerprints = texture_fingerprints(args.export, catalog)
    texture_assets = [cmp.asset(fp["platform"], "texture", tid, tid, {}, {k: fp[k] for k in ("width", "height", "rgba_sha256", "dhash")})
                      for tid, fp in sorted(fingerprints.items())]
    texture_links = []
    for left, right in PAIRS:
        a = [t for t in texture_assets if t["platform"] == left]
        b = [t for t in texture_assets if t["platform"] == right]
        if a and b:
            texture_links += cmp.match_textures(a, b, max_distance=5)

    # Level comparison: layout, shared-model candidates, shared-texture candidates, names.
    scenes = {level["scene"]: json.loads((args.export / level["file"]).read_text()) for level in catalog["levels"]}
    level_by_scene = {level["scene"]: level for level in catalog["levels"]}
    model_link_index = defaultdict(set)
    for link in model_links:
        if link["confidence"] >= 0.6:
            model_link_index[link["a"]].add(link["b"])
            model_link_index[link["b"]].add(link["a"])
    texture_link_index = defaultdict(set)
    for link in texture_links:
        texture_link_index[link["a"]].add(link["b"])
        texture_link_index[link["b"]].add(link["a"])

    layouts = {scene_id: xp.normalise_layout(xp.layout_points(scene)) for scene_id, scene in scenes.items()}
    # A model shared with many levels (crates, barrels) says little about which
    # level matches: only count links whose partner appears in at most
    # DISTINCTIVE levels of the other platform.
    levels_with_model = defaultdict(set)
    for scene_id, scene in scenes.items():
        for model_id in scene["models"]:
            levels_with_model[model_id].add(scene_id)

    def distinctive_partners(model_id: str, platform: str) -> set:
        partners = set()
        for other in model_link_index[model_id]:
            homes = {s for s in levels_with_model[other] if level_by_scene[s]["platform"] == platform}
            if 0 < len(homes) <= DISTINCTIVE:
                partners.add(other)
        return partners

    level_links = []
    for left, right in PAIRS:
        left_scenes = [s for s in scenes if level_by_scene[s]["platform"] == left]
        right_scenes = [s for s in scenes if level_by_scene[s]["platform"] == right]
        for ls in left_scenes:
            for rs in right_scenes:
                a, b = scenes[ls], scenes[rs]
                right_models = set(b["models"])
                shared_models = sum(1 for m in a["models"] if distinctive_partners(m, right) & right_models)
                evidence = []
                fraction = shared_models / len(a["models"]) if a["models"] else 0.0
                if shared_models and fraction >= 0.05:
                    evidence.append({"type": "shared_distinctive_models", "detail": f"{shared_models}/{len(a['models'])} models",
                                     "weight": round(min(0.6, 2 * fraction), 3)})
                same_region = city(level_by_scene[ls]) is not None and city(level_by_scene[ls]) == city(level_by_scene[rs])
                # Layouts are compared only where other evidence already links the pair.
                layout = xp.compare_layouts(layouts[ls], layouts[rs]) if (shared_models or same_region) else None
                if layout and layout["rms_distance"] < LAYOUT_LIMIT:
                    evidence.append({"type": "layout", "detail": json.dumps(layout, sort_keys=True), "weight": round(max(0.05, 0.5 - layout["rms_distance"]), 3)})
                if city(level_by_scene[ls]) and city(level_by_scene[ls]) == city(level_by_scene[rs]):
                    evidence.append({"type": "name_region", "detail": city(level_by_scene[ls]), "weight": 0.15})
                if evidence:
                    kinds = {e["type"] for e in evidence}
                    if "shared_distinctive_models" in kinds and len(kinds) >= 2:
                        strength = "strong"
                    elif len(kinds) >= 2:
                        strength = "moderate"
                    else:
                        strength = "weak: needs visual inspection"
                    level_links.append({"a": ls, "b": rs, "confidence": xp.combine([e["weight"] for e in evidence]), "status": "candidate",
                                        "strength": strength, "evidence": evidence})
    level_links.sort(key=lambda link: (-link["confidence"], link["a"], link["b"]))

    def best(scene_id, other_platform):
        options = [l for l in level_links if (l["a"] == scene_id and level_by_scene[l["b"]]["platform"] == other_platform)
                   or (l["b"] == scene_id and level_by_scene[l["a"]]["platform"] == other_platform)]
        rank = {"strong": 0, "moderate": 1}
        options.sort(key=lambda l: (rank.get(l["strength"], 2), -l["confidence"], l["a"], l["b"]))
        return options[0] if options else None

    backlog = {"ps1_exclusive": [], "n64_exclusive": [], "shared_with_conversion": [], "bt1_restorable": [],
               "probable_counterparts_need_inspection": [], "needs_visual_inspection": []}

    def classify(scene_id: str, level: dict, other: str, exclusive_key: str) -> None:
        match = best(scene_id, other)
        entry = {"platform": level["platform"], "level": level["name"], "variant": level.get("variant"), "scene": scene_id, "best_candidate": match}
        if match is not None and match["strength"] == "strong":
            if level["platform"] == "ps1" or other == "ps1":
                backlog["shared_with_conversion"].append(entry)
            return
        if match is not None and match["strength"] == "moderate":
            backlog["probable_counterparts_need_inspection"].append(entry)
            return
        backlog[exclusive_key].append(entry)

    for scene_id, level in sorted(level_by_scene.items(), key=lambda kv: (kv[1]["platform"], str(kv[1]["level"]), kv[1].get("variant", ""))):
        if level["platform"] == "ps1":
            classify(scene_id, level, "n64_btga", "ps1_exclusive")
        elif level["platform"] == "n64_btga":
            classify(scene_id, level, "ps1", "n64_exclusive")
        elif level["platform"] == "n64_bt1":
            match = best(scene_id, "n64_btga")
            if match is None or match["strength"] != "strong":
                backlog["bt1_restorable"].append({"world": level["level"], "scene": scene_id, "scenario_modes": level.get("scenario_modes"),
                                                  "best_ga_candidate": match,
                                                  "conversion_evidence": "BattleTanx worlds share Global Assault's placement transform, model/part/reference "
                                                                         "chain and GBI pools (F3DEX vs F3DEX2); world 9 was previously restored this way"})
    for link in texture_links[:200]:
        backlog["needs_visual_inspection"].append({"texture_a": link["a"], "texture_b": link["b"], "confidence": link["confidence"],
                                                    "reason": "texture-only evidence"})

    document = {"schema": "btga-compare", "schema_version": 2, "model_links": model_links, "texture_links": texture_links,
                "level_links": level_links, "backlog": backlog,
                "counts": {"models": Counter(model_platform.values()), "signatures": len(signatures), "model_links": len(model_links),
                           "texture_links": len(texture_links), "level_links": len(level_links)}}
    (output / "comparison.json").write_text(json.dumps(document, indent=1, sort_keys=True) + "\n")

    # Contact sheets of the strongest cross-platform model candidates.
    textures = {}
    for texture in catalog["textures"]:
        if texture.get("file"):
            width, height, rgba = cmp.png_rgba((args.export / texture["file"]).read_bytes())
            textures[texture["id"]] = {**texture, "rgba": rgba}
    rows = []
    for link in model_links[: args.sheet_pairs]:
        row = []
        for model_id in (link["a"], link["b"]):
            entry = next(e for e in catalog["models"] if e["id"] == model_id)
            record = json.loads((args.export / entry["file"]).read_text())
            row.append(preview.model_thumbnail(record["mesh"], textures, 96))
        rows.append(row)
    if rows:
        (output / "model_candidates_sheet.png").write_bytes(preview.contact_sheet(rows, 96))
    lines = ["# Private missing-content and restoration backlog (generated; do not commit)", ""]
    for section, items in backlog.items():
        lines += [f"## {section.replace('_', ' ')} ({len(items)})", ""]
        for item in items[:60]:
            lines.append("- " + json.dumps(item, sort_keys=True)[:400])
        lines.append("")
    (output / "BACKLOG.md").write_text("\n".join(lines) + "\n")
    print(json.dumps(document["counts"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
