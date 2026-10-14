"""Reusable acceptance-evidence helpers: source identity, process conflicts, hashes, image checks, records.

Inputs and outputs are plain files and JSON so other BattleTanx projects can
reuse them.  Nothing here launches an application, opens a window or talks
to the network; git is used read-only (``rev-parse``, ``diff``, ``ls-files``).

* ``source_identity(root)``: the commit plus a SHA-256 over the tracked diff
  and every untracked, non-ignored file, so evidence from a dirty tree is
  tied to its exact contents.  ``stale(record, root)`` compares a recorded
  identity with the current tree.
* ``conflicting_processes(patterns)``: other processes whose command line
  matches (Linux ``/proc``); used before builds and tests to avoid sharing
  an output directory with another worker.
* ``image_stats(width, height, rgba)``: blank/flat detection and colour
  counts for an RGBA8 framebuffer; ``inspect_image`` applies thresholds.
* ``write_record(png, ...)``: the adjacent JSON evidence record.
* ``manifest(directory)`` / ``compare_manifests(a, b)``: deterministic
  per-file SHA-256 listings and their differences.

Limits: files are hashed in 1 MiB chunks (constant memory); image checks
hold one framebuffer in memory.  Paths in returned records are relative.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git(root: Path, *args: str) -> bytes:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True).stdout


def source_identity(root: Path) -> dict:
    """Commit plus a content hash of uncommitted changes (tracked diff and untracked, non-ignored files)."""

    root = Path(root)
    head = _git(root, "rev-parse", "HEAD").decode().strip()
    digest = hashlib.sha256(_git(root, "diff", "--binary", "HEAD"))
    untracked = sorted(_git(root, "ls-files", "--others", "--exclude-standard", "-z").decode().split("\0"))
    for name in filter(None, untracked):
        digest.update(b"\0untracked\0" + name.encode() + b"\0" + sha256_file(root / name).encode())
    dirty = bool(_git(root, "status", "--porcelain").strip())
    return {"commit": head, "dirty": dirty, "tree_sha256": digest.hexdigest(), "label": f"{head[:12]}{'+' + digest.hexdigest()[:12] if dirty else ''}"}


def stale(record_identity: dict, root: Path) -> bool:
    """True when evidence recorded for ``record_identity`` no longer matches the tree at ``root``."""

    current = source_identity(root)
    return (record_identity.get("commit"), record_identity.get("tree_sha256")) != (current["commit"], current["tree_sha256"])


def conflicting_processes(patterns, own_pids=None) -> list[dict]:
    """Processes (other than ours) whose command line contains any pattern.  Linux /proc only; [] elsewhere."""

    own = set(own_pids or ()) | {os.getpid(), os.getppid()}
    out = []
    proc = Path("/proc")
    if not proc.is_dir():
        return out
    for entry in proc.iterdir():
        if not entry.name.isdigit() or int(entry.name) in own:
            continue
        try:
            command = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
        except OSError:
            continue
        if command and any(p in command for p in patterns):
            out.append({"pid": int(entry.name), "command": command[:300]})
    return sorted(out, key=lambda item: item["pid"])


def tool_versions() -> dict:
    versions = {"python": sys.version.split()[0], "platform": platform.platform()}
    for module in ("numpy", "imgui_bundle", "OpenGL", "PyInstaller"):
        try:
            versions[module] = getattr(__import__(module), "__version__", "unknown")
        except Exception:
            versions[module] = None
    return versions


def image_stats(width: int, height: int, rgba: bytes, region=None) -> dict:
    """Colour statistics for an RGBA8 image (optionally a region x, y, w, h)."""

    import numpy as np

    pixels = np.frombuffer(rgba, np.uint8).reshape(height, width, 4)
    if region is not None:
        x, y, w, h = (int(v) for v in region)
        pixels = pixels[max(0, y):y + h, max(0, x):x + w]
    rgb = pixels[..., :3].reshape(-1, 3)
    if not len(rgb):
        return {"pixels": 0, "unique_colours": 0, "dominant_fraction": 1.0, "luma_stddev": 0.0}
    packed = (rgb[:, 0].astype(np.uint32) << 16) | (rgb[:, 1].astype(np.uint32) << 8) | rgb[:, 2]
    values, counts = np.unique(packed, return_counts=True)
    luma = rgb.astype(float) @ (0.299, 0.587, 0.114)
    return {"pixels": int(len(rgb)), "unique_colours": int(len(values)), "dominant_fraction": round(float(counts.max() / len(rgb)), 4),
            "luma_stddev": round(float(luma.std()), 3)}


def inspect_image(stats: dict, min_colours: int = 16, max_dominant: float = 0.97) -> list[str]:
    """Automatic rejection reasons (blank or flat images); an empty list still needs a human look."""

    problems = []
    if stats["pixels"] == 0:
        problems.append("empty region")
    if stats["unique_colours"] < min_colours:
        problems.append(f"only {stats['unique_colours']} colours")
    if stats["dominant_fraction"] > max_dominant:
        problems.append(f"{stats['dominant_fraction']:.0%} of pixels share one colour")
    return problems


def write_record(png: Path, identity: dict, scenario: str, expected: str, observed: str, passed: bool, notes: str = "", **extra) -> Path:
    """Adjacent ``<name>.json`` evidence record for a screenshot."""

    png = Path(png)
    record = {"filename": png.name, "source_revision": identity, "platform": platform.platform(), "scenario": scenario,
              "expected": expected, "observed": observed, "result": "pass" if passed else "fail", "reviewer_notes": notes,
              "sha256": sha256_file(png), **extra}
    path = png.with_suffix(".json")
    path.write_text(json.dumps(record, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return path


def manifest(directory: Path) -> dict[str, dict]:
    """Relative path -> {sha256, size} for every file under ``directory``, sorted."""

    directory = Path(directory)
    out = {}
    for path in sorted(p for p in directory.rglob("*") if p.is_file()):
        out[path.relative_to(directory).as_posix()] = {"sha256": sha256_file(path), "size": path.stat().st_size}
    return out


def compare_manifests(first: dict, second: dict) -> dict:
    """Only-in-first, only-in-second and differing paths (deterministic order)."""

    return {"only_first": sorted(set(first) - set(second)), "only_second": sorted(set(second) - set(first)),
            "different": sorted(k for k in set(first) & set(second) if first[k] != second[k])}


def timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="print the source identity and any conflicting processes")
    parser.add_argument("root", type=Path, nargs="?", default=Path.cwd())
    parser.add_argument("--pattern", action="append", default=[])
    args = parser.parse_args(argv)
    print(json.dumps({"identity": source_identity(args.root), "conflicts": conflicting_processes(args.pattern) if args.pattern else []},
                     indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
