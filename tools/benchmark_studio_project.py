"""Measure time and peak memory of Studio's main operations on one project (off-screen, no window).

    python tools/benchmark_studio_project.py PROJECT.btgaproj --home STUDIO_HOME [--catalog CATALOG] [--frames 120] OUTPUT.json

Steps, each timed with the process's peak resident memory after it: open the
project, open its first map, map validation (diagnostics), authoring checks,
export validation, package build, project serialisation, Drive Preview setup
(ground and wall index), the first viewport frame and steady editor frames,
then Drive Preview frames.  Frame times are wall-clock per off-screen frame
(software or GPU rendering, whichever the EGL context provides; the report
records the OpenGL renderer string).  The output is JSON; no paths are written.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import resource
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def peak_mib() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(usage / (1024 * 1024) if sys.platform == "darwin" else usage / 1024, 1)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("project", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--catalog", type=Path)
    parser.add_argument("--frames", type=int, default=120)
    args = parser.parse_args(argv)
    os.environ["PYOPENGL_PLATFORM"] = "egl"
    os.environ["BTGA_STUDIO_HOME"] = str(args.home.resolve())
    steps = []

    def step(name, function):
        start = time.perf_counter()
        result = function()
        steps.append({"step": name, "seconds": round(time.perf_counter() - start, 3), "peak_rss_mib": peak_mib()})
        print(f"{name}: {steps[-1]['seconds']} s, peak {steps[-1]['peak_rss_mib']} MiB", flush=True)
        return result

    steps.append({"step": "start", "seconds": 0.0, "peak_rss_mib": peak_mib()})
    from tools.btga_studio.core import authoring, package, project as projects
    from tools.btga_studio.core import diagnostics
    from tools.btga_studio.core.drive import DrivePreview
    from tools.btga_studio.core.settings import Settings
    from tools.btga_studio.gui.app import StudioApp
    from tools.btga_studio.gui.offscreen import OffscreenUI

    settings = Settings.open()
    prefs = settings.preferences
    prefs.welcome_seen, prefs.show_hub_at_start, prefs.tutorial_done, prefs.tutorial_active = True, False, True, False
    app = StudioApp(settings, args.catalog.resolve() if args.catalog else None, None)
    ui = OffscreenUI(app, 1600, 960)
    app.show_hub = False
    c = app.controller
    step("open project", lambda: c.open_project(args.project.resolve()))
    key = next(iter(c.project.maps))
    step("open map", lambda: app.switch_map(key))
    doc = c.document
    records = sum(1 for _ in doc.scene.records())
    step("map validation (diagnostics)", lambda: diagnostics.collect(doc, c.library))
    step("authoring checks", lambda: authoring.content_problems(c.project, c.library, {key: doc}))
    report = step("export validation", lambda: package.validate_for_export(c.project, c.library))
    data = step("package build", lambda: package.build_package(c.project, c.library, "bench")) if report.ok else b""
    text = step("project serialisation", lambda: projects.dumps(c.project))
    step("drive preview setup", lambda: DrivePreview(doc.scene))
    app._load_into_renderer()
    step("first frame", ui.frame)
    times = []
    for _ in range(args.frames):
        start = time.perf_counter()
        ui.frame()
        times.append(time.perf_counter() - start)
    steps.append({"step": f"{args.frames} editor frames", "seconds": round(sum(times), 3), "peak_rss_mib": peak_mib()})
    app.start_drive_preview()
    app.drive_input = (1.0, 0.1, False)
    drive_times = []
    for _ in range(args.frames):
        start = time.perf_counter()
        ui.frame()
        drive_times.append(time.perf_counter() - start)
    app.stop_drive_preview()
    steps.append({"step": f"{args.frames} Drive Preview frames", "seconds": round(sum(drive_times), 3), "peak_rss_mib": peak_mib()})
    from OpenGL import GL as gl

    renderer = gl.glGetString(gl.GL_RENDERER)
    result = {
        "project": c.project.project.get("title"), "records": records, "package_bytes": len(data), "project_bytes": len(text.encode("utf-8")),
        "validation": {"errors": len(report.errors), "warnings": len(report.warnings)},
        "editor_frame_ms": {"median": round(statistics.median(times) * 1000, 1), "p95": round(sorted(times)[int(len(times) * 0.95) - 1] * 1000, 1)},
        "drive_frame_ms": {"median": round(statistics.median(drive_times) * 1000, 1),
                           "p95": round(sorted(drive_times)[int(len(drive_times) * 0.95) - 1] * 1000, 1)},
        "peak_rss_mib": peak_mib(), "steps": steps,
        "machine": {"python": platform.python_version(), "platform": platform.platform(), "cpus": os.cpu_count(),
                    "gl_renderer": renderer.decode() if isinstance(renderer, bytes) else str(renderer)},
    }
    args.output.write_text(json.dumps(result, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    ui.close()
    c.shutdown()
    print(json.dumps({k: v for k, v in result.items() if k != "steps"}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
