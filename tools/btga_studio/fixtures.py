"""Synthetic fixtures for tests and screenshot capture (no game data).

* ``synthetic_rom(seed)``: bytes with a valid N64 header and content unique to
  ``seed``; it is not any real game.
* ``supported_revision(rom, platform, title)``: a context manager that makes
  ``core.sources`` treat that synthetic ROM as a supported revision, so the
  whole Source Manager flow can be exercised without a real ROM.
* ``simulated_catalog_builder(delay, fail)``: replaces the real catalog
  builder with one that writes Studio's synthetic sample catalog, reports
  progress, honours cancellation and can be told to fail part-way.
* ``ScriptedDialogs``: answers file-picker requests from a queue.
* ``recording_launcher()``: records folder/URL launches instead of opening them.

These patch module globals for the duration of a ``with`` block only.
"""

from __future__ import annotations

import contextlib
import hashlib
import time
from pathlib import Path


def synthetic_rom(seed: int = 1, size: int = 1 << 20) -> bytes:
    body = hashlib.sha256(f"studio-fixture-{seed}".encode()).digest() * (size // 32)
    return b"\x80\x37\x12\x40" + body[4:size]


@contextlib.contextmanager
def supported_revision(rom: bytes, platform: str = "n64_btga", title: str = "BattleTanx: Global Assault (USA) N64"):
    from tools.btga_studio.core import sources

    digest = hashlib.sha1(rom).hexdigest()
    original = dict(sources.N64_REVISIONS)
    sources.N64_REVISIONS[digest] = (platform, title)
    try:
        yield digest
    finally:
        sources.N64_REVISIONS.clear()
        sources.N64_REVISIONS.update(original)


@contextlib.contextmanager
def simulated_catalog_builder(delay: float = 0.0, fail_after: int | None = None, steps: int = 8):
    from tools.btga_studio.core import sources
    from tools.btga_studio.samples import write_sample_catalog

    original = sources.build_catalog

    def build(source, output: Path, progress=None, cancel=None, model_glb=False):
        for step in range(steps):
            if cancel is not None and cancel.is_set():
                raise sources.Cancelled()
            if fail_after is not None and step >= fail_after:
                raise sources.SourceError("simulated read error part-way through the build")
            if progress:
                progress("scenes", step, steps)
            time.sleep(delay)
        write_sample_catalog(Path(output))

    sources.build_catalog = build
    try:
        yield build
    finally:
        sources.build_catalog = original


class ScriptedDialogs:
    """File pickers that return queued answers (None = the user cancelled)."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests: list[str] = []

    def _next(self, title):
        self.requests.append(title)
        return Path(self.answers.pop(0)) if self.answers and self.answers[0] is not None else (self.answers.pop(0) if self.answers else None)

    def open_file(self, title, filters):
        return self._next(title)

    def select_folder(self, title, start=None):
        return self._next(title)

    def save_file(self, title, default, filters):
        return self._next(title)


@contextlib.contextmanager
def recording_launcher():
    from tools.btga_studio.core import desktop

    launched: list[str] = []
    original = desktop.LAUNCHER
    desktop.LAUNCHER = launched.append
    try:
        yield launched
    finally:
        desktop.LAUNCHER = original
