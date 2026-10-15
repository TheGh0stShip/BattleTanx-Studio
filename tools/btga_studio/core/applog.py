"""Structured local logs and problem reports.

``open_log(directory)`` returns a logger that appends one JSON object per
line to ``<logs>/studio.log`` (rotated at 1 MiB, 5 files kept).  Records
hold a UTC timestamp, level, event name and fields.  Absolute paths are
reduced to their file names before they are written (``redact``), so logs
and reports can be attached to a public issue without exposing the user's
folder layout.

``problem_report(...)`` writes a plain-text report (version, platform,
recent log records, an optional error) for the user to attach to an issue;
it never includes project contents, source files or hashes of private data.

Consumers: the editor (all user-visible errors), crash handling, the
packaged self-check.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import platform
import re
import time
from pathlib import Path

# Folder parts of absolute paths.  Windows folder names may contain spaces; a POSIX path must not follow ":" (URLs keep their host).
_WINDOWS = re.compile(r"(?:[A-Za-z]:\\|\\\\)(?:[^\\\n\"'<>|*?:]+\\)+")
_POSIX = re.compile(r"(?<![:\w/])/(?:[^/\n\"'<>|*?]+/)+")
LOG_NAME = "studio.log"


def redact(text: str) -> str:
    """Remove the folders of absolute paths, keeping file names (``…/name``), so logs never reveal the user's folder layout."""

    text = str(text)
    home = str(Path.home())
    if len(home) > 3:
        text = text.replace(home, "~")
    text = _WINDOWS.sub("…/", text)
    return _POSIX.sub("…/", text)


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)), "level": record.levelname.lower(),
                 "event": getattr(record, "event", "message"), "message": redact(record.getMessage())}
        for key, value in (getattr(record, "fields", None) or {}).items():
            entry[key] = redact(value) if isinstance(value, str) else value
        return json.dumps(entry, sort_keys=True, ensure_ascii=False)


def open_log(directory: Path, name: str = "btga_studio") -> logging.Logger:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(f"{name}:{directory}")
    if not logger.handlers:
        handler = logging.handlers.RotatingFileHandler(directory / LOG_NAME, maxBytes=1 << 20, backupCount=5, encoding="utf-8")
        handler.setFormatter(_JsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


def write(logger: logging.Logger | None, level: str, event: str, message: str, **fields) -> None:
    if logger is None:
        return
    logger.log({"error": logging.ERROR, "warning": logging.WARNING}.get(level, logging.INFO), message,
               extra={"event": event, "fields": fields})


def close(logger: logging.Logger | None) -> None:
    if logger is None:
        return
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)


def recent_records(directory: Path, limit: int = 200) -> list[dict]:
    path = Path(directory) / LOG_NAME
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def problem_report(directory: Path, version: str, error: str | None = None, limit: int = 200) -> str:
    lines = [f"BattleTanx Studio {version}", f"Platform: {platform.platform()}", f"Python: {platform.python_version()}",
             f"Time: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}", ""]
    if error:
        lines += ["Error:", redact(error), ""]
    lines.append("Recent log:")
    for record in recent_records(directory, limit):
        lines.append(json.dumps(record, sort_keys=True, ensure_ascii=False))
    return "\n".join(lines) + "\n"
