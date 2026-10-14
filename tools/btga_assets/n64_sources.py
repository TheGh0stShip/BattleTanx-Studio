"""N64 ROM identities and the per-level stream tables of both games.

Global Assault (N64 USA Rev 0): level streams come from func_800E8380,
whose matching C translation unit is evaluated here (the unit matches the
retail ROM byte for byte, so its control flow is the retail behaviour).

BattleTanx (N64 USA): the 28-slot table at 0x80125A68 is filled by
0x800865E0 with {world start, world inclusive end, nav start, nav
inclusive end}; the instruction sequence is simulated from the ROM.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from pathlib import Path

from .n64_binary import FormatError, sha1, sha256

GA_SHA1 = "805248fb0a0ee694cad8d7dc927b631d860dd8cf"
BT1_SHA1 = "535860d941738ac1210c20a9b80114fea0e0ff17"

GA_POOLS = {"texture": (0x102C70, 0x2F8070), "state": (0x2F8070, 0x3013F0), "geometry": (0x3013F0, 0x3F6EE8)}
GA_COMMON_WORLD = (0x3F6EE8, 0x3F9B5C)
GA_BT1_LEFTOVER = (0x100000, 0x102068)
GA_IMAGE_TABLE = 0xA6860
GA_IMAGE_RECORDS = 196
GA_IMAGE_RECORD = struct.Struct(">BBHHHHHIIII")

BT1_CODE_VRAM = 0x80071000
BT1_CODE_ROM = 0x1000
BT1_STREAM_TABLE = 0x80125A68
BT1_STREAM_INIT = (0x800865E0, 0x80086B00)
BT1_STREAM_SLOTS = 28
BT1_POOLS = {"texture": 0x5A1110, "geometry": 0x3EA970}
BT1_SCENARIO_TABLE = 0x80142DE8
BT1_SCENARIO_RECORDS = 17
BT1_SCENARIO_SIZE = 0x220

GA_LEVEL_NAMES = {
    0: "SAN FRANCISCO - BREAKOUT", 1: "ARIZONA - TRUCK STOP", 2: "TEXAS - SLAVE FORTRESS",
    3: "ROUTE 66 - DRIVE IN", 4: "WASHINGTON DC - MALL", 5: "WASHINGTON DC - WHITE HOUSE",
    6: "LONDON - PARLIAMENT", 7: "LONDON - TOWER BRIDGE", 8: "LONDON - TOWER", 9: "PARIS - BISTRO",
    10: "PARIS - CHAMPS ELYSEES", 11: "PARIS - EIFFEL TOWER", 12: "GERMANY - BERLIN WAR ZONE",
    13: "GERMANY - BRANDENBURG GATE", 14: "GERMANY - ESCAPE FROM BERLIN", 15: "SAN FRANCISCO - ASSAULT",
    16: "SAN FRANCISCO - ALCATRAZ", 17: "PARIS - LAKEPARK", 18: "SAN FRANCISCO - PANHANDLE",
    19: "GERMANY - RAILYARD", 20: "SAN FRANCISCO - SFO", 21: "PARIS - CROSSFIRE",
    24: "SAN FRANCISCO - SECRET BOATS", 26: "SAN FRANCISCO - AIRPORT",
}
GA_LEVEL_NAME_SOURCE = "campaign map tables in src/code/frontend/8011B318_hud_list_layout_data.c"


@dataclass(frozen=True)
class RomImage:
    game: str
    data: bytes

    @property
    def sha1(self) -> str:
        return sha1(self.data)

    @property
    def sha256(self) -> str:
        return sha256(self.data)


def identify(data: bytes) -> RomImage:
    digest = sha1(data)
    if digest == GA_SHA1:
        return RomImage("n64_btga", data)
    if digest == BT1_SHA1:
        return RomImage("n64_bt1", data)
    raise FormatError(f"unrecognised ROM (SHA-1 {digest}); expected Global Assault USA Rev 0 or BattleTanx USA")


def load_rom(path: Path) -> RomImage:
    return identify(Path(path).read_bytes())


def decode_stream(rom: bytes, start: int, end: int) -> bytes:
    """Decompress an LZARI stream occupying rom[start:end]."""

    from tools import lzari

    if not 0 <= start < end <= len(rom):
        raise FormatError(f"stream {start:#x}-{end:#x} is outside the ROM")
    try:
        return lzari.decompress(rom[start:end], max_output=8 * 1024 * 1024)
    except lzari.LzariError as error:
        raise FormatError(f"LZARI stream at {start:#x}: {error}") from error


# ----------------------------------------------------- Global Assault -----

_ADDRESS = re.compile(r"\(u32\)\(?D_B0([0-9A-Fa-f]{6})(?:\s*([+-])\s*0x([0-9A-Fa-f]+))?\)?")


def _address(expression: str) -> int:
    match = _ADDRESS.search(expression)
    if not match:
        raise FormatError(f"unrecognised address expression {expression!r}")
    value = int(match.group(1), 16)
    if match.group(2):
        delta = int(match.group(3), 16)
        value = value + delta if match.group(2) == "+" else value - delta
    return value


def parse_slot_ranges(source: str) -> dict[int, list[tuple]]:
    """Tokenise func_800E8380's switch into per-case statement lists."""

    body = source[source.index("switch (arg0)") :]
    cases: dict[int, list[tuple]] = {}
    order: list[int] = []
    current = None
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.startswith("/*") or line == "switch (arg0) {":
            continue
        match = re.fullmatch(r"case (\d+):", line)
        if match:
            current = int(match.group(1))
            cases[current] = []
            order.append(current)
            continue
        if line == "default:":
            current = -1
            cases[-1] = []
            order.append(-1)
            continue
        if current is None:
            continue
        assign = re.fullmatch(r"a([23])\[(\d)\] = (.+);", line)
        if assign:
            cases[current].append(("set", int(assign.group(1)), int(assign.group(2)), _address(assign.group(3))))
        elif line in ("if (a1) {", "if (!a1) {", "if (D_80117EB4 == 10) {"):
            cases[current].append(("if", line))
        elif line == "}":
            cases[current].append(("end",))
        elif line == "goto ret2;":
            cases[current].append(("return", 2))
        elif re.fullmatch(r"return (\d+);", line):
            cases[current].append(("return", int(line.split()[1].rstrip(";"))))
        elif line == "ret2:":
            continue
        else:
            raise FormatError(f"unrecognised statement in func_800E8380: {line!r}")
    cases["order"] = order  # type: ignore[index]
    return cases


def ga_level_streams(source: str, level: int, flag: bool, mode10: bool) -> list[tuple[int, int]]:
    """Return the [start, end) ROM ranges func_800E8380 selects."""

    cases = parse_slot_ranges(source)
    order = cases.pop("order")  # type: ignore[arg-type]
    if level not in cases:
        return []
    starts, ends = [0, 0], [0, 0]
    position = order.index(level)
    while position < len(order):
        statements = cases[order[position]]
        index = 0
        skipping = 0
        while index < len(statements):
            statement = statements[index]
            index += 1
            if skipping:
                if statement[0] == "if":
                    skipping += 1
                elif statement[0] == "end":
                    skipping -= 1
                continue
            if statement[0] == "set":
                (starts if statement[1] == 2 else ends)[statement[2]] = statement[3]
            elif statement[0] == "if":
                condition = {"if (a1) {": flag, "if (!a1) {": not flag, "if (D_80117EB4 == 10) {": mode10}[statement[1]]
                if not condition:
                    skipping = 1
            elif statement[0] == "return":
                count = statement[1]
                ranges = []
                for slot in range(count):
                    # func_800BA6C0 DMA-reads ((end - start) + 1) & ~1 bytes.
                    ranges.append((starts[slot], starts[slot] + ((ends[slot] - starts[slot] + 1) & ~1)))
                return ranges
        position += 1
    return []


def ga_level_catalog(source: str) -> list[dict]:
    levels = []
    for level in sorted(key for key in parse_slot_ranges(source) if isinstance(key, int) and key >= 0):
        variants = {}
        for label, flag, mode10 in (("default", False, False), ("flag", True, False), ("mode10", False, True)):
            variants[label] = ga_level_streams(source, level, flag, mode10)
        levels.append({"level": level, "name": GA_LEVEL_NAMES.get(level), "variants": variants})
    return levels


def ga_image_ranges(rom: bytes) -> list[dict]:
    images = []
    for index in range(GA_IMAGE_RECORDS):
        fields = GA_IMAGE_RECORD.unpack_from(rom, GA_IMAGE_TABLE + index * 28)
        start = fields[-2] & 0x0FFFFFFF
        inclusive_end = fields[-1] & 0x0FFFFFFF
        if start == 0:
            continue
        images.append(
            {
                "table_index": index,
                "start": start,
                "end": start + ((inclusive_end - start + 1) & ~1),
                "format": fields[0],
                "size": fields[1],
                "width": fields[2],
                "height": fields[3],
            }
        )
    return images


# ------------------------------------------------------------ BattleTanx --


def bt1_offset(address: int) -> int:
    return address - BT1_CODE_VRAM + BT1_CODE_ROM


def _simulate_stores(rom: bytes, first: int, last: int) -> dict[int, int]:
    """Run the straight-line lui/addiu/sw initialiser and collect its stores."""

    registers = {0: 0}
    stores: dict[int, int] = {}
    for address in range(first, last, 4):
        word = struct.unpack_from(">I", rom, bt1_offset(address))[0]
        opcode = word >> 26
        rs, rt, immediate = (word >> 21) & 31, (word >> 16) & 31, word & 0xFFFF
        signed = immediate - 0x10000 if immediate & 0x8000 else immediate
        if word == 0x03E00008:  # jr $ra
            break
        if opcode == 0x0F:
            registers[rt] = immediate << 16
        elif opcode == 0x09:
            registers[rt] = (registers.get(rs, 0) + signed) & 0xFFFFFFFF
        elif opcode == 0x2B:
            stores[(registers.get(rs, 0) + signed) & 0xFFFFFFFF] = registers.get(rt, 0)
        elif word != 0:
            raise FormatError(f"unexpected instruction {word:08x} at {address:#x} in the stream initialiser")
    return stores


def bt1_stream_table(rom: bytes) -> list[dict]:
    stores = _simulate_stores(rom, *BT1_STREAM_INIT)
    slots = []
    for slot in range(BT1_STREAM_SLOTS):
        base = BT1_STREAM_TABLE + slot * 16
        words = []
        for field in range(4):
            address = base + field * 4
            if address in stores:
                words.append(stores[address])
            else:
                words.append(struct.unpack_from(">I", rom, bt1_offset(address))[0])
        entry = {"slot": slot, "world": None, "nav": None}
        for key, (start, end) in (("world", words[0:2]), ("nav", words[2:4])):
            if start and end:
                if start >> 28 != 0xB or end >> 28 != 0xB:
                    raise FormatError(f"BattleTanx slot {slot} {key} is not a cartridge address")
                start, end = start & 0x0FFFFFFF, end & 0x0FFFFFFF
                # func_80088CF0 / func_800DDA08 DMA (end - start + 7) & ~7 bytes.
                entry[key] = (start, start + ((end - start + 7) & ~7))
        slots.append(entry)
    return slots


def bt1_scenarios(rom: bytes) -> list[dict]:
    """The scenario records func_8007B280 scans (mode, world id, sequence).

    func_8007B9E4 treats mode 7 as the bonus round, reading the kill target
    at +0x208 and a difficulty-block index byte at +0x217.
    """

    base = bt1_offset(BT1_SCENARIO_TABLE)
    records = []
    for index in range(BT1_SCENARIO_RECORDS):
        offset = base + index * BT1_SCENARIO_SIZE
        record = rom[offset : offset + BT1_SCENARIO_SIZE]
        mode, world, sequence, word_c = struct.unpack_from(">4I", record, 0)
        records.append(
            {
                "index": index,
                "mode": mode,
                "world": world,
                "sequence": sequence,
                "word_0c": word_c,
                "bonus_kill_target": struct.unpack_from(">i", record, 0x208)[0] if mode == 7 else None,
                "difficulty_index": record[0x217],
                "rom_range": [offset, offset + BT1_SCENARIO_SIZE],
                "sha256": sha256(record),
            }
        )
    return records
