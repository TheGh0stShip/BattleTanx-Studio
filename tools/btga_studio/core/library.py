"""Several catalogs behind one StudioLibrary-compatible interface.

Studio always has the ROM-free starter catalog and may add catalogs built
from the user's own sources.  ``LibrarySet`` merges their levels, models and
textures by ID (IDs are content-derived and platform-prefixed, so catalogs
never collide); each entry remembers which catalog root it came from.
"""

from __future__ import annotations

import json
from pathlib import Path

from tools.btga_assets.studio import StudioError, StudioLibrary


class LibrarySet(StudioLibrary):
    def __init__(self, libraries: list[StudioLibrary]):
        if not libraries:
            raise StudioError("no catalogs")
        self.libraries = list(libraries)
        self.root = libraries[0].root
        self.catalog = {"schema": libraries[0].catalog["schema"], "schema_version": libraries[0].catalog["schema_version"],
                        "sources": [], "levels": [], "models": [], "textures": [], "inventories": []}
        self._levels, self._models, self._textures, self._owner = {}, {}, {}, {}
        for library in self.libraries:
            for key in ("sources", "levels", "models", "textures"):
                self.catalog[key] += library.catalog.get(key, [])
            for family, table in (("_levels", library._levels), ("_models", library._models), ("_textures", library._textures)):
                for identifier, entry in table.items():
                    getattr(self, family).setdefault(identifier, entry)
                    self._owner.setdefault((family, identifier), library)
        from collections import OrderedDict

        self._thumbnails = OrderedDict()

    @classmethod
    def open_all(cls, roots: list[Path]) -> "LibrarySet":
        return cls([StudioLibrary.open(Path(root)) for root in roots])

    def owner(self, family: str, identifier: str) -> StudioLibrary:
        library = self._owner.get((family, identifier))
        if library is None:
            raise StudioError(f"unknown {family.strip('_')} {identifier!r}")
        return library

    def root_for_model(self, model_id: str) -> Path:
        return self.owner("_models", model_id).root

    def scene(self, level_id: str) -> dict:
        return self.owner("_levels", level_id).scene(level_id)

    def model(self, model_id: str):
        return self.owner("_models", model_id).model(model_id)

    def texture(self, texture_id: str):
        return self.owner("_textures", texture_id).texture(texture_id)

    def _read(self, relative: str) -> bytes:  # pragma: no cover - entries are always read through their owner
        raise StudioError("LibrarySet reads through the owning catalog")

    def library_models(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for library in self.libraries:
            for key, value in library.library_models().items():
                out.setdefault(key, []).extend(value)
        return {key: sorted(set(value)) for key, value in sorted(out.items())}

    def starter(self) -> dict | None:
        """The starter manifest (templates and model names) when the starter catalog is loaded."""

        for library in self.libraries:
            path = Path(library.root) / "starter.json"
            if path.is_file():
                return json.loads(path.read_text(encoding="utf-8"))
        return None
