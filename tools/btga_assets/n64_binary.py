"""Big-endian, bounds- and alignment-checked reads for N64 data.

N64 data is big-endian with 32-bit words.  Every read names its width and
checks its range and natural alignment; nothing is ever cast to a host
pointer, and no native struct layout is assumed.
"""

from __future__ import annotations

import hashlib
import struct

from .ps1_formats import FormatError

__all__ = ["BEReader", "FormatError", "sha256", "raw_range", "sha1"]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def raw_range(data: bytes, offset: int, size: int) -> dict:
    if offset < 0 or size < 0 or offset + size > len(data):
        raise FormatError(f"range {offset:#x}+{size:#x} is outside {len(data):#x} bytes")
    return {"offset": offset, "size": size, "sha256": sha256(data[offset : offset + size])}


class BEReader:
    def __init__(self, data: bytes, name: str = "buffer"):
        self.data = bytes(data)
        self.name = name

    def __len__(self) -> int:
        return len(self.data)

    def _check(self, offset: int, size: int, align: int) -> None:
        if offset < 0 or offset + size > len(self.data):
            raise FormatError(f"{self.name}: {size}-byte read at {offset:#x} exceeds {len(self.data):#x}")
        if offset % align:
            raise FormatError(f"{self.name}: {size}-byte read at {offset:#x} is misaligned")

    def u8(self, offset: int) -> int:
        self._check(offset, 1, 1)
        return self.data[offset]

    def s8(self, offset: int) -> int:
        value = self.u8(offset)
        return value - 0x100 if value & 0x80 else value

    def u16(self, offset: int) -> int:
        self._check(offset, 2, 2)
        return struct.unpack_from(">H", self.data, offset)[0]

    def s16(self, offset: int) -> int:
        self._check(offset, 2, 2)
        return struct.unpack_from(">h", self.data, offset)[0]

    def u32(self, offset: int) -> int:
        self._check(offset, 4, 4)
        return struct.unpack_from(">I", self.data, offset)[0]

    def s32(self, offset: int) -> int:
        self._check(offset, 4, 4)
        return struct.unpack_from(">i", self.data, offset)[0]

    def f32(self, offset: int) -> float:
        self._check(offset, 4, 4)
        return struct.unpack_from(">f", self.data, offset)[0]

    def bytes_at(self, offset: int, size: int) -> bytes:
        self._check(offset, size, 1)
        return self.data[offset : offset + size]

    def records(self, start: int, end: int, size: int, what: str) -> int:
        """Validate a record array span and return its record count."""

        if start < 0 or end < start or end > len(self.data):
            raise FormatError(f"{self.name}: {what} span {start:#x}-{end:#x} is outside the data")
        if (end - start) % size:
            raise FormatError(f"{self.name}: {what} span of {end - start} bytes is not whole {size}-byte records")
        return (end - start) // size
