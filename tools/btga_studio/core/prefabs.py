"""Reusable prefabs: named groups of objects saved per user and placed into any map.

A prefab is the editor's deterministic clipboard text (``EditorDocument.copy``)
stored as ``<prefabs>/<key>.json`` with a title.  Placing one pastes it at a
point, so it gets new IDs and becomes ordinary user-created records.
Prefabs never contain source bytes: instances refer to models by content ID,
and placement is refused when a model is not available in the loaded
catalogs (for example a prefab made from a game the user has not registered).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .atomic import atomic_write_text
from .hub import slug

PREFAB_FORMAT = "btga-studio-prefab"
MAX_RECORDS = 500


class PrefabError(ValueError):
    pass


@dataclass(frozen=True)
class Prefab:
    key: str
    title: str
    records: int
    clipboard: str
    models: tuple[str, ...]


def _parse(text: str, title: str, key: str) -> Prefab:
    try:
        data = json.loads(text)
    except ValueError as error:
        raise PrefabError(f"prefab {key} is not readable: {error}") from error
    if not isinstance(data, dict) or data.get("format") != "btga-studio-clipboard" or not isinstance(data.get("records"), list):
        raise PrefabError(f"prefab {key} does not hold Studio objects")
    if not data["records"] or len(data["records"]) > MAX_RECORDS:
        raise PrefabError(f"a prefab holds 1 to {MAX_RECORDS} objects")
    models = sorted({entry["record"]["model"] for entry in data["records"]
                     if isinstance(entry, dict) and entry.get("family") == "instances" and isinstance(entry.get("record"), dict)
                     and isinstance(entry["record"].get("model"), str)})
    return Prefab(key, title, len(data["records"]), text, tuple(models))


class PrefabLibrary:
    def __init__(self, directory: Path):
        self.directory = Path(directory)

    def save(self, title: str, clipboard: str) -> Prefab:
        key = slug(title, "prefab")
        prefab = _parse(clipboard, title, key)
        atomic_write_text(self.directory / f"{key}.json",
                          json.dumps({"format": PREFAB_FORMAT, "version": 1, "title": title, "clipboard": clipboard}, sort_keys=True, indent=1) + "\n")
        return prefab

    def list(self) -> tuple[list[Prefab], list[str]]:
        """(prefabs, problems): an unreadable file is reported, never fatal."""

        out, problems = [], []
        if not self.directory.is_dir():
            return out, problems
        for path in sorted(self.directory.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict) or data.get("format") != PREFAB_FORMAT:
                    raise PrefabError("not a prefab file")
                out.append(_parse(data["clipboard"], str(data.get("title") or path.stem), path.stem))
            except (OSError, ValueError, KeyError, TypeError) as error:
                problems.append(f"{path.name}: {error}")
        return out, problems

    def delete(self, key: str) -> None:
        if slug(key, "prefab") != key:
            raise PrefabError("invalid prefab name")
        (self.directory / f"{key}.json").unlink(missing_ok=True)

    @staticmethod
    def missing_models(prefab: Prefab, library) -> list[str]:
        available = set(library._models) if library is not None else set()
        return [model for model in prefab.models if model not in available]
