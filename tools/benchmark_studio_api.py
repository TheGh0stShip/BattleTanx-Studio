#!/usr/bin/env python3
"""Measure the BTGA Studio resource API against an exported catalog.

Reports wall time, Python heap peak (tracemalloc) and resident set size for:
catalog startup, listing campaigns and maps, opening one map, lazy
thumbnails, opening and closing N maps, applying and reverting overlays,
and memory after repeated map cycles.  It also counts the files the API
reads, proving that the project-browser views touch only catalog.json.

    python3 tools/benchmark_studio_api.py private-work/catalog --json report.json

The report never contains host paths.  Nothing is written unless --json is given.
"""

from __future__ import annotations

import argparse
import gc
import json
import sys
import time
import tracemalloc
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.btga_assets.studio import StudioLibrary  # noqa: E402


def rss_kib() -> int | None:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except OSError:
        return None
    return None


class Counter:
    def __init__(self, library: StudioLibrary):
        self.reads: list[str] = []
        original = library._read

        def counted(relative: str) -> bytes:
            self.reads.append(relative.split("/", 1)[0])
            return original(relative)

        library._read = counted

    def take(self) -> dict:
        out: dict[str, int] = {}
        for kind in self.reads:
            out[kind] = out.get(kind, 0) + 1
        self.reads.clear()
        return dict(sorted(out.items()))


def measure(name: str, action, results: dict, counter: Counter | None = None) -> object:
    gc.collect()
    tracemalloc.reset_peak()
    start = time.perf_counter()
    value = action()
    elapsed = time.perf_counter() - start
    _, peak = tracemalloc.get_traced_memory()
    results[name] = {"seconds": round(elapsed, 4), "python_peak_kib": peak // 1024, "rss_kib": rss_kib()}
    if counter is not None:
        results[name]["files_read"] = counter.take()
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("catalog", type=Path)
    parser.add_argument("--maps", type=int, default=100)
    parser.add_argument("--thumbnails", type=int, default=24)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args(argv)

    tracemalloc.start()
    results: dict = {"rss_start_kib": rss_kib()}
    library = measure("catalog_startup", lambda: StudioLibrary.open(args.catalog), results)
    counter = Counter(library)
    levels = measure("list_campaigns_and_maps", lambda: (library.campaigns(), library.levels()), results, counter)[1]
    results["catalog"] = {"levels": len(levels), "models": len(library.models()), "textures": len(library.textures())}
    results["browser_reads_only_catalog"] = results["list_campaigns_and_maps"]["files_read"] == {}
    by_size = sorted(levels, key=lambda level: -level.instances)
    largest = by_size[0]
    game_map = measure("open_largest_map", lambda: library.load_map(largest.id), results, counter)
    results["open_largest_map"].update({"instances": len(game_map.instances), "markers": len(game_map.markers),
                                        "collision": len(game_map.collision)})
    models = game_map.placeable_models()[: args.thumbnails]
    measure("thumbnails_first", lambda: [library.thumbnail(m) for m in models], results, counter)
    measure("thumbnails_cached", lambda: [library.thumbnail(m) for m in models], results, counter)
    results["thumbnails_first"]["count"] = len(models)
    del game_map

    cycle = [levels[i % len(levels)].id for i in range(args.maps)]
    rss_trace = []

    def open_close() -> None:
        for number, level_id in enumerate(cycle):
            loaded = library.load_map(level_id)
            del loaded
            if number % 25 == 24:
                gc.collect()
                rss_trace.append(rss_kib())

    measure("open_close_maps", open_close, results, counter)
    results["open_close_maps"]["maps"] = len(cycle)
    results["open_close_maps"]["rss_every_25_kib"] = rss_trace
    second = []

    def second_pass() -> None:
        for number, level_id in enumerate(cycle):
            del number
            library.load_map(level_id)
        gc.collect()
        second.append(rss_kib())

    measure("open_close_maps_repeat", second_pass, results, counter)

    target = next((level for level in by_size if level.markers), largest)
    scene = library.scene(target.id)
    overlay = library.new_overlay(target.id)
    templates = library.marker_templates(target.id)

    def edit_cycle() -> int:
        template = next(iter(templates.values()))
        for index in range(50):
            overlay.add_marker(template, [index, 0, index], index * 16)
        for marker in scene["markers"][:50]:
            overlay.move(marker["id"], [m + 1 for m in marker["translation"]])
        edited = library.load_map(target.id, overlay)
        errors = library.validate_overlay(target.id, overlay)
        overlay.revert(len(overlay.edits))
        reverted = library.load_map(target.id, overlay)
        assert len(reverted.markers) == len(scene["markers"])
        return len(edited.markers) - len(scene["markers"]), len(errors)

    added, errors = measure("apply_validate_revert_overlay", edit_cycle, results, counter)
    results["apply_validate_revert_overlay"].update({"edits": 100, "markers_added": added, "validation_errors": errors})
    results["memory_stable"] = {
        "rss_after_first_pass_kib": rss_trace[-1] if rss_trace else None,
        "rss_after_second_pass_kib": second[-1] if second else None,
        "growth_kib": (second[-1] - rss_trace[-1]) if rss_trace and second and None not in (second[-1], rss_trace[-1]) else None,
    }
    results["rss_end_kib"] = rss_kib()
    text = json.dumps(results, indent=1, sort_keys=True)
    print(text)
    if args.json:
        args.json.write_text(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
