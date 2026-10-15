"""Version parsing and comparison for releases, upgrades and the installer.

Versions are dotted integers with an optional pre-release tag
(``0.2.0``, ``0.2.1-rc1``).  A pre-release sorts before its release.
``compare`` returns -1, 0 or 1; anything unparseable raises ``ValueError``
so a damaged install record is never mistaken for an older version.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_VERSION = re.compile(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:\.(\d+))?(?:-([0-9A-Za-z.]+))?$")


@dataclass(frozen=True, order=False)
class Version:
    numbers: tuple[int, int, int, int]
    pre: str | None = None

    @classmethod
    def parse(cls, text: str) -> "Version":
        match = _VERSION.match(str(text).strip())
        if not match:
            raise ValueError(f"not a version: {text!r}")
        numbers = tuple(int(part or 0) for part in match.groups()[:4])
        return cls(numbers, match.group(5))

    def key(self) -> tuple:
        # A release (no pre-release tag) sorts after any pre-release of the same numbers.
        return (self.numbers, self.pre is None, self.pre or "")

    def __str__(self) -> str:
        text = ".".join(str(n) for n in self.numbers[:3]) + (f".{self.numbers[3]}" if self.numbers[3] else "")
        return text + (f"-{self.pre}" if self.pre else "")


def compare(a: str, b: str) -> int:
    left, right = Version.parse(a).key(), Version.parse(b).key()
    return (left > right) - (left < right)
