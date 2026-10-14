#!/usr/bin/env python3
"""Memory and latency stress test for the BTGA Studio editor core.

Ten cycles of: open the largest map, apply 1,000 mixed edits (move, rotate,
set field, duplicate, place, delete, disable, undo/redo), revert them all,
close the map.  After each cycle it records RSS, the retained Python heap
(tracemalloc "current"), the live object count and cache sizes.  The only
eviction point is the documented bounded mesh cache (scene.MESH_CACHE_TRIANGLES),
which is kept across cycles like the editor keeps it across map switches.

It then repeats the earlier StudioLibrary overlay step (apply + validate +
revert, the source of the 99 -> 148 MiB observation) ten times.

    python3 tools/benchmark_studio_editor.py CATALOG --json report.json
"""

from __future__ import annotations

import argparse
import gc
import json
import random
import sys
import time
import tracemalloc
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.btga_assets.studio import StudioLibrary  # noqa: E402
from tools.btga_studio.core.document import EditError, EditorDocument, is_read_only  # noqa: E402
from tools.btga_studio.core.scene import EditorScene, MeshCache  # noqa: E402


def rss_kib() -> int | None:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except OSError:
        pass
    try:  # Windows / macOS fallback
        import resource

        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except Exception:
        return None


def mixed_edits(document: EditorDocument, count: int, rng: random.Random) -> dict:
    scene = document.scene
    editable = [r["id"] for f, r in scene.records() if f != "collision" and not is_read_only(r)]
    fields = [(r["id"], name) for f, r in scene.records("markers") if not is_read_only(r)
              for name, spec in r.get("fields", {}).items() if spec.get("evidence") != "unresolved" and "range" in spec]
    models = sorted({r["model"] for f, r in scene.records("instances")})
    bounds = scene.bounds or {"x": [-1000, 1000], "z": [-1000, 1000]}
    latencies = {"edit": [], "undo": []}
    refused = 0
    for number in range(count):
        roll = rng.random()
        start = time.perf_counter()
        try:
            if roll < 0.35 and editable:
                target = rng.choice(editable)
                x, z = rng.uniform(*bounds["x"]), rng.uniform(*bounds["z"])
                document.set_transform(target, [x, 0.0, z], rng.randrange(65536))
            elif roll < 0.5 and fields:
                target, name = rng.choice(fields)
                spec = document.record(target)["fields"][name]
                document.set_field(target, name, rng.randint(*spec["range"]) if "bitmask_bits" not in spec else rng.randint(1, (1 << spec["bitmask_bits"]) - 1))
            elif roll < 0.6 and editable:
                document.duplicate([rng.choice(editable)], (16.0, 0.0, 16.0))
            elif roll < 0.7 and models:
                document.place_model(rng.choice(models), (rng.uniform(*bounds["x"]), 0.0, rng.uniform(*bounds["z"])))
            elif roll < 0.8 and document.selection:
                document.delete()
            elif roll < 0.85 and editable:
                document.begin_transform([rng.choice(editable)])
                for step in range(5):
                    document.update_transform((step * 4.0, 0.0, 0.0), step * 100)
                document.cancel_transform() if rng.random() < 0.3 else document.commit_transform()
            elif roll < 0.95:
                t = time.perf_counter()
                document.undo()
                latencies["undo"].append(time.perf_counter() - t)
            else:
                document.redo()
        except EditError:
            refused += 1
        latencies["edit"].append(time.perf_counter() - start)
    return {"refused": refused, "latencies": latencies}


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("catalog", type=Path)
    parser.add_argument("--cycles", type=int, default=10)
    parser.add_argument("--edits", type=int, default=1000)
    parser.add_argument("--level", help="scene ID (default: the map with most instances)")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args(argv)

    tracemalloc.start()
    library = StudioLibrary.open(args.catalog)
    level = args.level or max(library.levels(), key=lambda l: l.instances).id
    cache = MeshCache(library)
    rng = random.Random(1234)
    cycles = []
    edit_latency, undo_latency = [], []
    for cycle in range(args.cycles):
        start = time.perf_counter()
        document = EditorDocument(EditorScene(library, level, cache))
        opened = time.perf_counter() - start
        result = mixed_edits(document, args.edits, rng)
        edit_latency += result["latencies"]["edit"]
        undo_latency += result["latencies"]["undo"]
        applied = len(document.overlay.edits)
        while document.undo_stack:
            document.undo()
        assert not document.overlay.edits, "revert left edits behind"
        del document
        gc.collect()
        current, _ = tracemalloc.get_traced_memory()
        cycles.append({"cycle": cycle, "open_seconds": round(opened, 3), "edits_applied_before_revert": applied, "refused": result["refused"],
                       "rss_kib": rss_kib(), "python_heap_kib": current // 1024, "gc_objects": len(gc.get_objects()),
                       "mesh_cache_models": len(cache.entries), "mesh_cache_triangles": cache.triangles})
        print(json.dumps(cycles[-1]))
    second = cycles[len(cycles) // 2 :]
    plateau = {
        "rss_spread_second_half_kib": max(c["rss_kib"] for c in second) - min(c["rss_kib"] for c in second),
        "heap_spread_second_half_kib": max(c["python_heap_kib"] for c in second) - min(c["python_heap_kib"] for c in second),
        "objects_spread_second_half": max(c["gc_objects"] for c in second) - min(c["gc_objects"] for c in second),
    }
    legacy = []
    for repeat in range(args.cycles):
        scene = library.scene(level)
        overlay = library.new_overlay(level)
        template = next(iter(library.marker_templates(level).values()))
        for index in range(50):
            overlay.add_marker(template, [index, 0, index], index * 16)
        for marker in scene["markers"][:50]:
            overlay.move(marker["id"], [m + 1 for m in marker["translation"]])
        library.load_map(level, overlay)
        library.validate_overlay(level, overlay)
        overlay.revert(len(overlay.edits))
        del scene, overlay
        gc.collect()
        current, peak = tracemalloc.get_traced_memory()
        tracemalloc.reset_peak()
        legacy.append({"repeat": repeat, "rss_kib": rss_kib(), "python_heap_kib": current // 1024, "python_peak_kib": peak // 1024})
    report = {
        "level": level, "cycles": cycles, "plateau": plateau,
        "edit_latency_ms": {"p50": round(percentile(edit_latency, 0.5) * 1000, 3), "p95": round(percentile(edit_latency, 0.95) * 1000, 3)},
        "undo_latency_ms": {"p50": round(percentile(undo_latency, 0.5) * 1000, 3), "p95": round(percentile(undo_latency, 0.95) * 1000, 3)},
        "legacy_overlay_step": legacy,
        "eviction_points": {"mesh cache": "LRU bounded by scene.MESH_CACHE_TRIANGLES triangles; kept across maps"},
    }
    text = json.dumps(report, indent=1, sort_keys=True)
    if args.json:
        args.json.write_text(text + "\n")
    print(json.dumps({k: report[k] for k in ("plateau", "edit_latency_ms", "undo_latency_ms")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
