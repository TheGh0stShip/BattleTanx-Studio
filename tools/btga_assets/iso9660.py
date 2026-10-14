"""Read-only ISO 9660 access to PlayStation disc images (2048-byte ISO or raw 2352-byte BIN).

Only what Studio needs: locate the primary volume descriptor, walk the
directory tree, and copy files out.  Raw sectors are recognised by the CD
sync pattern; Mode 1 user data starts at byte 16 and Mode 2 Form 1 at byte
24.  Every offset and length is bounds-checked; names are normalised
(``;1`` version suffixes removed) and refused if they could escape the
output directory.
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass
from pathlib import Path

SYNC = bytes([0x00] + [0xFF] * 10 + [0x00])
SECTOR = 2048
MAX_DEPTH = 16
MAX_ENTRIES = 100_000


class IsoError(ValueError):
    pass


@dataclass(frozen=True)
class Entry:
    path: str
    extent: int
    size: int
    directory: bool


class DiscImage:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.handle = open(self.path, "rb")
        try:
            self.length = os.fstat(self.handle.fileno()).st_size
            self.raw, self.data_offset = self._layout()
            self.sector_size = 2352 if self.raw else SECTOR
            pvd = self.sector(16)
            if pvd[0] != 1 or pvd[1:6] != b"CD001":
                raise IsoError(f"{self.path.name}: no ISO 9660 primary volume descriptor")
            self.volume_id = pvd[40:72].decode("ascii", "replace").strip()
            self.root = self._record(pvd, 156)
        except BaseException:
            self.handle.close()
            raise

    def close(self) -> None:
        self.handle.close()

    def __enter__(self) -> "DiscImage":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _layout(self) -> tuple[bool, int]:
        self.handle.seek(0)
        head = self.handle.read(16)
        if head[:12] == SYNC:
            mode = head[15]
            if mode not in (1, 2):
                raise IsoError(f"{self.path.name}: unsupported raw sector mode {mode}")
            return True, 16 if mode == 1 else 24
        return False, 0

    def sector(self, number: int) -> bytes:
        size = 2352 if self.raw else SECTOR
        offset = number * size + self.data_offset
        if number < 0 or offset + SECTOR > self.length:
            raise IsoError(f"{self.path.name}: sector {number} lies outside the image")
        self.handle.seek(offset)
        return self.handle.read(SECTOR)

    def read(self, extent: int, size: int) -> bytes:
        out = bytearray()
        sectors = (size + SECTOR - 1) // SECTOR
        for index in range(sectors):
            out += self.sector(extent + index)
        return bytes(out[:size])

    @staticmethod
    def _record(data: bytes, offset: int) -> dict:
        length = data[offset]
        if length < 34 or offset + length > len(data):
            raise IsoError("malformed directory record")
        extent = struct.unpack_from("<I", data, offset + 2)[0]
        size = struct.unpack_from("<I", data, offset + 10)[0]
        flags = data[offset + 25]
        name_length = data[offset + 32]
        if 33 + name_length > length:
            raise IsoError("directory record name overruns the record")
        name = data[offset + 33 : offset + 33 + name_length]
        return {"length": length, "extent": extent, "size": size, "directory": bool(flags & 2), "name": name}

    def entries(self) -> list[Entry]:
        out: list[Entry] = []
        seen = set()

        def walk(record: dict, prefix: str, depth: int) -> None:
            if depth > MAX_DEPTH:
                raise IsoError("directory tree is too deep")
            if record["extent"] in seen:
                raise IsoError("directory loop")
            seen.add(record["extent"])
            data = self.read(record["extent"], record["size"])
            offset = 0
            while offset < len(data):
                if data[offset] == 0:  # records do not cross sector boundaries
                    offset = (offset // SECTOR + 1) * SECTOR
                    continue
                child = self._record(data, offset)
                offset += child["length"]
                if child["name"] in (b"\x00", b"\x01"):
                    continue
                name = child["name"].decode("ascii", "replace").split(";")[0].rstrip(".")
                if not name or "/" in name or "\\" in name or name in (".", ".."):
                    raise IsoError(f"unsafe file name {child['name']!r}")
                path = f"{prefix}{name}"
                out.append(Entry(path, child["extent"], child["size"], child["directory"]))
                if len(out) > MAX_ENTRIES:
                    raise IsoError("too many directory entries")
                if child["directory"]:
                    walk(child, path + "/", depth + 1)

        walk(self.root, "", 0)
        return sorted(out, key=lambda e: e.path)

    def extract(self, target: Path, progress=None) -> list[Path]:
        target = Path(target).resolve()
        files = [e for e in self.entries() if not e.directory]
        written = []
        for number, entry in enumerate(files):
            destination = (target / entry.path).resolve()
            if target not in destination.parents:
                raise IsoError(f"{entry.path} would leave the extraction directory")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(self.read(entry.extent, entry.size))
            written.append(destination)
            if progress:
                progress(number + 1, len(files), entry.path)
        return written


def image_from_cue(cue: Path) -> Path:
    """The single data track named by a CUE sheet (spaces and Unicode allowed)."""

    text = Path(cue).read_text(encoding="utf-8", errors="replace")
    files = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("FILE "):
            rest = stripped[5:].strip()
            if rest.startswith('"'):
                try:
                    name = rest[1:rest.index('"', 1)]
                except ValueError as error:
                    raise IsoError(f"{Path(cue).name}: unterminated FILE name") from error
            else:
                name = rest.rsplit(" ", 1)[0]
            files.append(name)
    if not files:
        raise IsoError(f"{Path(cue).name}: no FILE entry")
    data = (Path(cue).parent / files[0]).resolve()
    if Path(cue).parent.resolve() not in data.parents:
        raise IsoError("the CUE sheet points outside its folder")
    if not data.is_file():
        raise IsoError(f"{Path(cue).name}: data track {files[0]!r} is missing")
    return data
