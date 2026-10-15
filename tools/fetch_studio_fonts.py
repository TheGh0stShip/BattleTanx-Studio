#!/usr/bin/env python3
"""Fetch and verify BattleTanx Studio's redistributable UI fonts on Linux/WSL.

The source packages, package hashes, installed paths and final font hashes are
all pinned in ``tools/btga_studio/fonts/fonts.json``. Files are downloaded only
from HTTPS Ubuntu archive URLs, verified before extraction, then verified again
after extraction. Nothing is installed system-wide.

Usage:
    python tools/fetch_studio_fonts.py private-work/toolchains/fonts

Requires ``dpkg-deb``. Output may be passed to the Windows build script as
``-FontDir``. The destination must be ignored by Git.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.btga_studio.core import fontset  # noqa: E402


def _download(url: str, destination: Path) -> None:
    if not url.startswith("https://archive.ubuntu.com/ubuntu/"):
        raise ValueError(f"font package URL is not an approved Ubuntu archive URL: {url}")
    request = urllib.request.Request(url, headers={"User-Agent": "BattleTanx-Studio-font-fetch/1"})
    with urllib.request.urlopen(request, timeout=60) as response, open(destination, "wb") as output:
        shutil.copyfileobj(response, output, length=1 << 20)


def fetch(destination: Path) -> list[Path]:
    destination = Path(destination).resolve()
    if shutil.which("dpkg-deb") is None:
        raise RuntimeError("dpkg-deb is required; run this helper from Linux or WSL")
    destination.mkdir(parents=True, exist_ok=True)
    written = []
    with tempfile.TemporaryDirectory(prefix="btga-studio-fonts-") as temp_text:
        temp = Path(temp_text)
        for index, entry in enumerate(fontset.manifest()):
            package = temp / f"font-{index}.deb"
            unpacked = temp / f"font-{index}"
            _download(entry.package_url, package)
            if fontset._sha256(package) != entry.package_sha256:
                raise RuntimeError(f"downloaded package hash does not match for {entry.family}")
            subprocess.run(["dpkg-deb", "-x", str(package), str(unpacked)], check=True,
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
            source = unpacked / entry.package_path
            if not source.is_file() or source.stat().st_size != entry.size or fontset._sha256(source) != entry.sha256:
                raise RuntimeError(f"extracted font hash does not match for {entry.family}")
            target = destination / entry.file
            shutil.copyfile(source, target)
            written.append(target)
    return written


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("destination", type=Path)
    args = parser.parse_args(argv)
    for path in fetch(args.destination):
        print(f"{fontset._sha256(path)}  {path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
