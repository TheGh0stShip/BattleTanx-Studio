#!/usr/bin/env python3
"""Prove the BTGA Studio editor tests catch deliberately broken editor-core logic.

Each mutation rewrites one snippet in a temporary copy of the tools and
tests, runs tests.test_studio_editor there with this interpreter (which must
have tools/btga_studio/requirements.txt installed), and expects a failure.
The working tree is never modified.
"""

from __future__ import annotations

import os
import importlib.util
import importlib.util
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GEOMETRY = "tools/btga_studio/core/geometry.py"
SCENE = "tools/btga_studio/core/scene.py"
DOCUMENT = "tools/btga_studio/core/document.py"
PROJECT = "tools/btga_studio/core/project.py"
THUMBS = "tools/btga_studio/core/thumbnails.py"
INPUT = "tools/btga_studio/gui/input.py"
OVERLAY = "tools/btga_assets/overlay.py"
INSPECTOR = "tools/btga_studio/core/inspector.py"
COPIED = ("tools/btga_assets", "tools/btga_studio", "tools/benchmark_studio_editor.py", "tools/btga_world.py", "tools/lzari.py",
          "tools/n64_texture.py", "tests/__init__.py", "tests/test_studio_editor.py", "tests/test_n64_assets.py")
TESTS = ("tests.test_studio_editor",)

MUTATIONS = (
    (GEOMETRY, "picking: barycentric bound", "hit = valid & (u >= 0) & (v >= 0) & (u + v <= 1) & (t >= 0)",
     "hit = valid & (u >= 0) & (v >= 0) & (u + v <= 2) & (t >= 0)"),
    (GEOMETRY, "picking: farthest triangle", "index = int(np.argmin(candidates))", "index = int(np.argmax(np.where(np.isinf(candidates), -1, candidates)))"),
    (GEOMETRY, "axis swap: screen y", "ndc_y = 1.0 - 2.0 * (y + 0.5) / height", "ndc_y = 2.0 * (y + 0.5) / height - 1.0"),
    (GEOMETRY, "axis swap: x/z in yaw", "return np.array([[c, 0.0, s], [0.0, 1.0, 0.0], [-s, 0.0, c]])",
     "return np.array([[c, 0.0, -s], [0.0, 1.0, 0.0], [s, 0.0, c]])"),
    (GEOMETRY, "yaw direction: units", "return int(round(radians * TURN / (2 * math.pi))) & 0xFFFF", "return int(round(-radians * TURN / (2 * math.pi))) & 0xFFFF"),
    (GEOMETRY, "camera: look direction", "self.yaw -= dx_pixels * self.look_sensitivity", "self.yaw += dx_pixels * self.look_sensitivity"),
    (GEOMETRY, "snap: yaw step", "step = TURN * degrees / 360", "step = TURN * degrees / 180"),
    (SCENE, "picking: tie-break priority", 'PICK_PRIORITY = {"markers": 0, "collision": 1, "instances": 2}', 'PICK_PRIORITY = {"markers": 2, "collision": 1, "instances": 0}'),
    (SCENE, "picking: world ray not localised", "local = g.Ray(rotation @ (ray.origin - translation) / scale, rotation @ ray.direction / scale)",
     "local = g.Ray((ray.origin - translation) / scale, ray.direction / scale)"),
    (DOCUMENT, "undo grouping: per-update commits", "        self._transform = {\"ids\": ids, \"originals\": originals, \"pivot\": pivot, \"delta\": np.zeros(3), \"yaw\": 0, \"scales\": scales,\n                           \"scale\": np.ones(3)}",
     "        self._transform = None"),
    (DOCUMENT, "cancel keeps preview", "        if state is not None:\n            self.scene.refresh(self.overlay, set(state[\"originals\"]))",
     "        if False:\n            self.scene.refresh(self.overlay, set(state[\"originals\"]))"),
    (DOCUMENT, "lost unknown bytes on duplicate", '            for key in ("id", "overlay", "disabled"):\n                copy_.pop(key, None)',
     '            for key in ("id", "overlay", "disabled", "raw_hex"):\n                copy_.pop(key, None)'),
    (DOCUMENT, "missing validation: fields", "        problems = field_errors(name, spec, value)\n        if problems:\n            raise EditError(\"; \".join(problems))",
     "        problems = []"),
    (DOCUMENT, "missing validation: read-only", '    if record.get("confidence") == "unresolved" or record.get("category") in READ_ONLY_CATEGORIES:', "    if False:"),
    (DOCUMENT, "missing validation: bounds", "        if bounds and not (bounds[\"x\"][0] <= translation[0] <= bounds[\"x\"][1] and bounds[\"z\"][0] <= translation[2] <= bounds[\"z\"][1]):\n            raise EditError(f\"({translation[0]:.0f}",
     "        if False:\n            raise EditError(f\"({translation[0]:.0f}"),
    (DOCUMENT, "redo reassigns ids", "        self.overlay.next_user_index = transaction.user_index_after", "        pass"),
    (DOCUMENT, "pivot ignored", "        moved = rotation @ relative + state[\"pivot\"] + state[\"delta\"]", "        moved = rotation @ np.asarray(translation, float) + state[\"delta\"]"),
    (PROJECT, "nondeterministic serialisation", "text = json.dumps(project.as_dict(), sort_keys=True,", "text = json.dumps(project.as_dict() | {\"nonce\": id(project)}, sort_keys=True,"),
    (PROJECT, "source hash not checked", "            if actual != entry.source_fingerprint:", "            if False:"),
    (THUMBS, "stale thumbnail keys", 'text = "\\x1f".join((content_id, renderer, preset, str(int(width)), str(int(height))))',
     'text = "\\x1f".join((content_id, str(int(width)), str(int(height))))'),
    (THUMBS, "corrupt thumbnails accepted", " or hashlib.sha256(payload).digest() != digest:", ":"),
    (THUMBS, "unbounded cache", "                if not self._entries or (self.bytes <= self.max_bytes and len(self._entries) <= self.max_entries):",
     "                if True:"),
    (THUMBS, "cancel ignored", "        kept = [job for job in self._queue if job.generation >= self.generation]", "        kept = list(self._queue)"),
    (OVERLAY, "resource id instability", '        digest = hashlib.sha256(self.scene_id.encode()).hexdigest()[:12]', '        digest = hex(id(self))'),
    (INPUT, "shortcuts while typing", "    if state.text_input:\n        return out", "    if False:\n        return out"),
    (INSPECTOR, "unresolved fields editable", "    reason = record_reason or field_editable(spec)", "    reason = record_reason"),
)


def run(workdir: Path) -> bool:
    env = dict(os.environ, PYOPENGL_PLATFORM=os.environ.get("PYOPENGL_PLATFORM", "egl"), BTGA_MUTATION_GATE="1")
    result = subprocess.run([sys.executable, "-m", "unittest", *TESTS], cwd=workdir, env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, check=False)
    return result.returncode == 0


def main() -> int:
    if importlib.util.find_spec("numpy") is None:
        print("numpy is unavailable; refusing to treat skipped editor tests as a mutation baseline")
        return 2
    if importlib.util.find_spec("numpy") is None:
        print("numpy is unavailable; refusing to treat skipped editor tests as a mutation baseline")
        return 2
    originals = {path: (ROOT / path).read_text(encoding="utf-8") for path in {m[0] for m in MUTATIONS}}
    with tempfile.TemporaryDirectory() as temp:
        work = Path(temp)
        for relative in COPIED:
            source, target = ROOT / relative, work / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
            else:
                shutil.copy2(source, target)
        (work / "tools" / "__init__.py").touch()
        if not run(work):
            print("baseline tests fail; refusing to judge mutations")
            return 2
        survivors = []
        for path, name, before, after in MUTATIONS:
            original = originals[path]
            if original.count(before) != 1:
                print(f"STALE {name}: original text occurs {original.count(before)} times")
                survivors.append(name)
                continue
            (work / path).write_text(original.replace(before, after), encoding="utf-8")
            passed = run(work)
            (work / path).write_text(original, encoding="utf-8")
            print(f"{'SURVIVED' if passed else 'killed  '} {name}")
            if passed:
                survivors.append(name)
    print(f"{len(MUTATIONS) - len(survivors)}/{len(MUTATIONS)} mutations killed")
    return 1 if survivors else 0


if __name__ == "__main__":
    raise SystemExit(main())
