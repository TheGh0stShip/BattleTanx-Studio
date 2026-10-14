"""Source Manager: register the user's own game sources and build catalogs from them.

Studio never ships, copies or downloads game data.  The user registers
files they legally obtained; the registry (in the per-user data directory,
never in a repository) stores only a local reference, hashes and status.
Catalogs derived from a source are rebuilt deterministically into the same
data directory and are local derived data.

Supported sources (identified by exact hash):

    n64_bt1   BattleTanx (USA) N64 ROM                     SHA-1 535860d9...
    n64_btga  BattleTanx: Global Assault (USA) N64 ROM     SHA-1 805248fb...
    ps1       BattleTanx: Global Assault (USA) PS1         SLUS_010.44 SHA-256 7bc99c17...
              as an extracted folder, a CUE/BIN pair, a raw BIN or a 2048-byte ISO

ROMs in byte-swapped (.v64) or little-endian (.n64) order are normalised
before hashing.  Status values: supported, unknown (not a recognised
revision), mismatched (the file changed since registration), incomplete
(a disc or folder lacks required files) and missing.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

N64_REVISIONS = {
    "805248fb0a0ee694cad8d7dc927b631d860dd8cf": ("n64_btga", "BattleTanx: Global Assault (USA) N64"),
    "535860d941738ac1210c20a9b80114fea0e0ff17": ("n64_bt1", "BattleTanx (USA) N64"),
}
PS1_EXECUTABLES = {
    "7bc99c17872dc244886ab85033be1596c206b298da450bea8c774851b4ab8f2f": ("ps1", "BattleTanx: Global Assault (USA) PS1", "SLUS_010.44"),
}
PS1_REQUIRED = ("SLUS_010.44", "SYSTEM.CNF", "ASSETS/ENUMS.MMM", "ASSETS/LONDON1.BIN", "ASSETS/LONDON1.MMM")
N64_ROM_SIZE = 8 * 1024 * 1024
STATUSES = ("supported", "unknown", "mismatched", "incomplete", "missing")
REGISTRY_VERSION = 1
EXPECTED_SCENES = {"n64_btga": 58, "n64_bt1": 22, "ps1": 76}
CATALOG_NAME = re.compile(r"^[a-z0-9_]+-[0-9a-f]{16}$")


class SourceError(ValueError):
    pass


class Cancelled(Exception):
    pass


@dataclass
class Source:
    id: str  # "<platform>:<hash prefix>" (content-derived)
    platform: str | None
    title: str
    kind: str  # rom | folder | image
    path: str  # local reference only; never written into projects or packages
    sha1: str | None
    sha256: str
    size: int
    mtime_ns: int
    status: str
    detail: str = ""
    catalog: str | None = None  # catalog directory name inside the data directory
    identity: dict = field(default_factory=dict)  # what catalogs and projects record (hashes only)


def normalise_n64(data: bytes) -> tuple[bytes, str]:
    """Return big-endian (.z64) ROM bytes and the input byte order."""

    magic = data[:4]
    if magic == b"\x80\x37\x12\x40":
        return data, "z64"
    if magic == b"\x37\x80\x40\x12":
        swapped = bytearray(data)
        swapped[0::2], swapped[1::2] = data[1::2], data[0::2]
        return bytes(swapped), "v64"
    if magic == b"\x40\x12\x37\x80":
        words = bytearray(len(data))
        words[0::4], words[1::4], words[2::4], words[3::4] = data[3::4], data[2::4], data[1::4], data[0::4]
        return bytes(words), "n64"
    raise SourceError("not an N64 ROM (unrecognised header)")


def _hash_file(path: Path, progress=None) -> tuple[str, str, int]:
    sha1, sha256 = hashlib.sha1(), hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(4 * 1024 * 1024)
            if not chunk:
                break
            sha1.update(chunk)
            sha256.update(chunk)
            size += len(chunk)
            if progress:
                progress("hash", size, None)
    return sha1.hexdigest(), sha256.hexdigest(), size


def identify(path: Path, progress=None) -> Source:
    """Identify one path without registering it."""

    path = Path(path)
    if not path.exists():
        raise SourceError(f"{path.name}: does not exist")
    stat = path.stat()
    if path.is_dir():
        return _identify_ps1_folder(path, stat)
    suffix = path.suffix.lower()
    if suffix in (".cue", ".bin", ".iso", ".img"):
        return _identify_ps1_image(path, stat)
    data = path.read_bytes()
    if len(data) > 64 * 1024 * 1024:
        raise SourceError(f"{path.name}: too large to be an N64 ROM")
    try:
        rom, order = normalise_n64(data)
    except SourceError as error:
        raise SourceError(f"{path.name}: {error}") from error
    sha1, sha256 = hashlib.sha1(rom).hexdigest(), hashlib.sha256(rom).hexdigest()
    known = N64_REVISIONS.get(sha1)
    status, detail = ("supported", f"{order} byte order" + (" (normalised)" if order != "z64" else "")) if known else ("unknown", "")
    if not known:
        detail = (f"SHA-1 {sha1} is not a supported revision; Studio supports "
                  + ", ".join(f"{title} ({digest[:8]}…)" for digest, (_, title) in N64_REVISIONS.items()))
        if len(rom) != N64_ROM_SIZE:
            detail += f"; size {len(rom)} differs from the 8 MiB retail ROMs (truncated or modified?)"
    platform, title = known if known else (None, "Unknown N64 ROM")
    return Source(f"{platform or 'unknown'}:{sha256[:16]}", platform, title, "rom", str(path.resolve()), sha1, sha256, len(rom), stat.st_mtime_ns,
                  status, detail, identity={"platform": platform, "rom_sha1": sha1, "rom_sha256": sha256} if platform else {})


def _identify_ps1_folder(path: Path, stat) -> Source:
    executable = path / "SLUS_010.44"
    if not executable.is_file():
        raise SourceError(f"{path.name}: no SLUS_010.44 in this folder (choose the folder that holds the extracted disc files)")
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    return _ps1_source(path, "folder", digest, [p for p in PS1_REQUIRED if not (path / p).is_file()], stat)


def _identify_ps1_image(path: Path, stat) -> Source:
    from tools.btga_assets.iso9660 import DiscImage, IsoError, image_from_cue

    try:
        image = image_from_cue(path) if path.suffix.lower() == ".cue" else path
        with DiscImage(image) as disc:
            entries = {e.path: e for e in disc.entries()}
            executable = entries.get("SLUS_010.44")
            if executable is None:
                raise SourceError(f"{path.name}: the disc has no SLUS_010.44; is this the right game and region?")
            digest = hashlib.sha256(disc.read(executable.extent, executable.size)).hexdigest()
    except IsoError as error:
        raise SourceError(f"{path.name}: {error}") from error
    return _ps1_source(path, "image", digest, [p for p in PS1_REQUIRED if p not in entries], stat)


def _ps1_source(path: Path, kind: str, digest: str, missing: list[str], stat) -> Source:
    known = PS1_EXECUTABLES.get(digest)
    if not known:
        status, detail = "unknown", f"SLUS_010.44 SHA-256 {digest[:16]}… is not the supported USA release"
    elif missing:
        status, detail = "incomplete", "missing " + ", ".join(missing)
    else:
        status, detail = "supported", f"{kind} source"
    platform, title = (known[0], known[1]) if known else (None, "Unknown PlayStation disc")
    return Source(f"{platform or 'unknown'}:{digest[:16]}", platform, title, kind, str(path.resolve()), None, digest, 0, stat.st_mtime_ns, status, detail,
                  identity={"platform": platform, "file_sha256": digest, "file": "SLUS_010.44"} if platform else {})


class SourceRegistry:
    """The user's registered sources, stored in ``<data>/sources.json``."""

    def __init__(self, data_directory: Path):
        self.directory = Path(data_directory)
        self.sources: dict[str, Source] = {}
        self._lock = threading.Lock()
        self.load()

    @property
    def file(self) -> Path:
        return self.directory / "sources.json"

    @property
    def catalogs(self) -> Path:
        return self.directory / "catalogs"

    def _catalog_path(self, name: str) -> Path:
        """Resolve a registry catalog name without trusting editable JSON."""

        if not isinstance(name, str) or not CATALOG_NAME.fullmatch(name):
            raise SourceError(f"unsafe catalog name {name!r}")
        return self.catalogs / name

    def load(self) -> None:
        try:
            data = json.loads(self.file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if data.get("version") != REGISTRY_VERSION:
            return
        for item in data.get("sources", []):
            try:
                source = Source(**item)
            except TypeError:
                continue
            self.sources[source.id] = source

    def save(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        text = json.dumps({"version": REGISTRY_VERSION, "sources": [asdict(s) for s in sorted(self.sources.values(), key=lambda s: s.id)]},
                          indent=1, sort_keys=True, ensure_ascii=False) + "\n"
        temporary = self.file.with_name(self.file.name + ".tmp")
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, self.file)

    def register(self, path: Path, progress=None) -> Source:
        source = identify(Path(path), progress)
        if source.platform is None:
            raise SourceError(f"{Path(path).name}: {source.detail}")
        with self._lock:
            existing = self.sources.get(source.id)
            if existing is not None:
                source.catalog = existing.catalog
            self.sources[source.id] = source
            self.save()
        return source

    def remove(self, source_id: str, delete_catalog: bool = True) -> None:
        with self._lock:
            source = self.sources[source_id]
            catalog = self._catalog_path(source.catalog) if delete_catalog and source.catalog else None
            del self.sources[source_id]
            self.save()
        if catalog is not None:
            shutil.rmtree(catalog, ignore_errors=True)

    def verify(self, source_id: str) -> Source:
        """Re-check one source: missing, moved content (mismatched) or unchanged."""

        source = self.sources[source_id]
        path = Path(source.path)
        if not path.exists():
            source.status, source.detail = "missing", "the file or folder is no longer at its registered location; relocate it"
        else:
            stat = path.stat()
            if path.is_file() and stat.st_mtime_ns == source.mtime_ns:
                pass  # unchanged since registration
            else:
                try:
                    fresh = identify(path)
                except SourceError as error:
                    source.status, source.detail = "mismatched", str(error)
                else:
                    if fresh.sha256 != source.sha256:
                        source.status, source.detail = "mismatched", "the file at this location has different contents"
                    else:
                        source.status, source.detail, source.mtime_ns = fresh.status, fresh.detail, fresh.mtime_ns
        self.save()
        return source

    def relocate(self, source_id: str, new_path: Path) -> Source:
        source = self.sources[source_id]
        fresh = identify(Path(new_path))
        if fresh.sha256 != source.sha256:
            raise SourceError(f"{Path(new_path).name} is not the registered source (different contents)")
        fresh.catalog = source.catalog
        self.sources[source_id] = fresh
        self.save()
        return fresh

    def search(self, directory: Path, limit: int = 2000) -> dict[str, Path]:
        """Find missing sources by content under ``directory`` (does not register anything)."""

        wanted = {s.sha256: s.id for s in self.sources.values() if s.status in ("missing", "mismatched")}
        found: dict[str, Path] = {}
        checked = 0
        for root, folders, files in os.walk(directory):
            folders.sort()
            for name in sorted(files):
                if checked >= limit or not wanted:
                    return found
                candidate = Path(root) / name
                if candidate.suffix.lower() not in (".z64", ".v64", ".n64", ".cue", ".bin", ".iso"):
                    continue
                checked += 1
                try:
                    source = identify(candidate)
                except (SourceError, OSError):
                    continue
                if source.sha256 in wanted:
                    found[wanted.pop(source.sha256)] = candidate
        return found

    # -- catalogs ---------------------------------------------------------------------------
    def catalog_directory(self, source: Source) -> Path:
        return self.catalogs / f"{source.platform}-{source.sha256[:16]}"

    def catalog_roots(self) -> list[Path]:
        roots = []
        for source in sorted(self.sources.values(), key=lambda s: s.id):
            if source.catalog:
                root = self._catalog_path(source.catalog)
                if (root / "catalog.json").is_file():
                    roots.append(root)
        return roots

    def build_catalog(self, source_id: str, progress=None, cancel: threading.Event | None = None, model_glb: bool = False) -> Path:
        """Decode a supported source into its catalog directory (deterministic; replaces any previous build)."""

        source = self.verify(source_id)
        if source.status != "supported":
            raise SourceError(f"{source.title}: {source.status} ({source.detail})")
        target = self.catalog_directory(source)
        work = target.with_name(target.name + ".building")
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        try:
            build_catalog(source, work, progress, cancel, model_glb)
        except BaseException:
            shutil.rmtree(work, ignore_errors=True)
            raise
        shutil.rmtree(target, ignore_errors=True)
        os.replace(work, target)
        source.catalog = target.name
        self.save()
        return target


def _slot_source() -> str:
    from tools.btga_studio.core.settings import program_directory

    candidates = [Path(__file__).resolve().parents[3] / "src/code/frontend/800E8380_slot_asset_ranges.c",
                  program_directory() / "data" / "800E8380_slot_asset_ranges.c"]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
    raise SourceError("the Global Assault level table source (800E8380_slot_asset_ranges.c) is missing from this installation")


def build_catalog(source: Source, output: Path, progress=None, cancel: threading.Event | None = None, model_glb: bool = False) -> None:
    from tools.btga_assets import export as ex
    from tools.btga_assets import n64_sources as src

    done = [0]
    total = EXPECTED_SCENES.get(source.platform, 1)

    class Exporter(ex.Exporter):
        def add_scene(self, scene, models, textures, level_entry):
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            super().add_scene(scene, models, textures, level_entry)
            done[0] += 1
            if progress:
                progress("scenes", done[0], total)

    exporter = Exporter(output)
    if source.platform in ("n64_btga", "n64_bt1"):
        data, _ = normalise_n64(Path(source.path).read_bytes())
        if hashlib.sha256(data).hexdigest() != source.sha256:
            raise SourceError("the ROM changed while building")
        rom = src.identify(data)
        if source.platform == "n64_btga":
            ex.export_ga(exporter, rom, _slot_source())
        else:
            ex.export_bt1(exporter, rom)
    elif source.platform == "ps1":
        if source.kind == "folder":
            corpus = Path(source.path)
        else:
            from tools.btga_assets.iso9660 import DiscImage, image_from_cue

            corpus = output / "_disc"
            image = image_from_cue(Path(source.path)) if Path(source.path).suffix.lower() == ".cue" else Path(source.path)
            with DiscImage(image) as disc:
                disc.extract(corpus, (lambda n, t, name: progress("extract", n, t)) if progress else None)
        ex.export_ps1(exporter, corpus)
        if source.kind != "folder":
            shutil.rmtree(output / "_disc", ignore_errors=True)
    else:
        raise SourceError(f"no catalog builder for {source.platform}")
    if progress:
        progress("finish", 0, 1)
    exporter.finish(write_model_glb=model_glb)
