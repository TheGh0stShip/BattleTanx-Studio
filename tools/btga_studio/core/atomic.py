"""Atomic file replacement for projects, registries, settings, recovery copies and packages.

``atomic_write_bytes(path, data)`` writes to a uniquely named temporary file
in the target's own directory, flushes and fsyncs it, then ``os.replace``s
it over the target.  Readers therefore see either the complete old file or
the complete new one; a failure at any point (full disk, permission error,
an exception while producing the data) leaves the old file untouched and
removes the temporary file.

Inputs: a path and bytes or text.  Output: the final path.  Ownership: the
caller owns the target; this module owns only its temporary file.  Limits:
the data is held in memory once.  Platform: POSIX and Windows (``os.replace``
is atomic on the same volume on both).  Consumers: project save, autosave,
source registry, preferences, content package export, release tooling.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

TEMPORARY_PREFIX = ".partial-"


def atomic_write_bytes(path: Path, data: bytes) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=TEMPORARY_PREFIX + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return path


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> Path:
    return atomic_write_bytes(path, text.encode(encoding))


def remove_partials(directory: Path) -> int:
    """Delete temporary files left by an interrupted write (for example after a power loss); returns how many."""

    removed = 0
    directory = Path(directory)
    if not directory.is_dir():
        return 0
    for candidate in directory.glob(TEMPORARY_PREFIX + "*"):
        try:
            candidate.unlink()
            removed += 1
        except OSError:
            pass
    return removed
