"""Repeated map open / edit / close cycles on ROM-free starter content: memory plateau and latency.

Each cycle opens every map of the starter example through the controller,
places, moves, duplicates, deletes and undoes objects, validates, saves the
project (atomically, to a path with spaces and non-ASCII), then closes the map.  It reports per-cycle RSS (Linux ``/proc``; None elsewhere),
the traced Python heap after a collection, and p50/p95 latencies.  The
heap of the second half of the run must stay within ``--tolerance-kib``.

    python tools/benchmark_studio_cycles.py [--cycles 30] [--json OUT]

Exit status 1 when the second-half heap spread exceeds the tolerance.
"""

from __future__ import annotations

import argparse
import array
import gc
import json
import os
import statistics
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def rss_kib() -> int | None:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except OSError:
        return None
    return None


def _percentile(values, fraction):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))]


def run(cycles: int) -> dict:
    from tools.btga_studio.content.examples import build_example
    from tools.btga_studio.core.controller import EditorController
    from tools.btga_studio.core.settings import Settings

    # Samples go into preallocated arrays so the probe's own bookkeeping does not show up as heap growth.
    samples = cycles * 3
    timings = {name: array.array("d", bytes(8 * samples)) for name in ("open", "edit", "undo", "validate", "save")}
    heap = array.array("q", bytes(8 * cycles))
    rss = array.array("q", bytes(8 * cycles))
    slot = 0
    with tempfile.TemporaryDirectory() as temp:
        os.environ["BTGA_STUDIO_HOME"] = temp
        controller = EditorController(Settings.open())
        controller.project = build_example(controller.library)
        save_path = Path(temp) / "cycle project ü.btgaproj"
        cube = controller.library.starter()["models"]["cube"]
        tracemalloc.start()
        try:
            for cycle in range(cycles):
                for key in sorted(controller.project.maps):
                    start = time.perf_counter()
                    controller.switch_map(key)
                    timings["open"][slot] = time.perf_counter() - start
                    document = controller.document
                    start = time.perf_counter()
                    placed = document.place_model(cube, (64.0 * (cycle % 8), 0.0, -256.0))
                    document.set_transform(placed, [100.0, 0.0, 100.0], 0x4000)
                    copies = document.duplicate([placed])
                    document.delete(copies)
                    timings["edit"][slot] = time.perf_counter() - start
                    start = time.perf_counter()
                    for _ in range(4):
                        document.undo()
                    timings["undo"][slot] = time.perf_counter() - start
                    start = time.perf_counter()
                    controller.request_diagnostics(immediate=True)
                    controller.poll_diagnostics(wait=True)
                    timings["validate"][slot] = time.perf_counter() - start
                    start = time.perf_counter()
                    controller.save(save_path)
                    timings["save"][slot] = time.perf_counter() - start
                    document.redo_stack.clear()
                    controller.close_map()
                    slot += 1
                gc.collect()
                heap[cycle] = tracemalloc.get_traced_memory()[0]
                rss[cycle] = rss_kib() or -1
        finally:
            tracemalloc.stop()
            controller.shutdown()
    half = heap[len(heap) // 2:]
    return {"cycles": cycles, "maps_per_cycle": 3,
            "heap_bytes": list(heap), "rss_kib": [v if v >= 0 else None for v in rss],
            "second_half_heap_spread_bytes": max(half) - min(half),
            "latency_ms": {name: {"p50": round(statistics.median(v) * 1000, 3), "p95": round(_percentile(v, 0.95) * 1000, 3),
                                  "samples": len(v)} for name, v in timings.items()},
            "heap_slope_bytes_per_cycle": round((half[-1] - half[0]) / max(1, len(half) - 1), 1)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cycles", type=int, default=30)
    parser.add_argument("--tolerance-kib", type=int, default=512)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args(argv)
    result = run(args.cycles)
    result["tolerance_bytes"] = args.tolerance_kib * 1024
    result["ok"] = result["second_half_heap_spread_bytes"] <= result["tolerance_bytes"]
    text = json.dumps(result, indent=1, sort_keys=True)
    if args.json:
        args.json.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
