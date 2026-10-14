#!/usr/bin/env python3
"""Export PS1 and N64 BattleTanx assets into one deterministic neutral catalog.

Sources are local reference inputs: the extracted PS1 disc (--ps1), the
Global Assault N64 USA Rev 0 ROM (--ga-rom or BTGA_N64_ROM) and the
BattleTanx N64 USA ROM (--bt1-rom or BTGA_BT1_ROM).  ROMs are identified
and recorded by hash only.  Output goes to a git-ignored directory (or
outside the repository) and never next to the sources.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.btga_assets import export as ex  # noqa: E402
from tools.btga_assets import n64_sources as src  # noqa: E402
from tools.inventory_ps1_assets import OutputError, check_output_dir  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SLOT_SOURCE = ROOT / "src/code/frontend/800E8380_slot_asset_ranges.c"


def _ints(values: list[str] | None) -> list[int] | None:
    return None if not values else [int(value, 0) for value in values]


def ga_layout(rom_path: Path) -> dict:
    with tempfile.TemporaryDirectory() as temp:
        output = Path(temp) / "layout.json"
        subprocess.run([sys.executable, str(ROOT / "tools/inventory_rom_layout.py"), "--rom", str(rom_path), "--output", str(output)],
                       check=True, cwd=ROOT, stdout=subprocess.DEVNULL)
        return json.loads(output.read_text())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("output", type=Path, help="ignored output directory")
    parser.add_argument("--ps1", type=Path, help="extracted PS1 disc directory (read only)")
    parser.add_argument("--ga-rom", type=Path, default=os.environ.get("BTGA_N64_ROM"))
    parser.add_argument("--bt1-rom", type=Path, default=os.environ.get("BTGA_BT1_ROM"))
    parser.add_argument("--ga-level", action="append", help="limit Global Assault levels (repeatable)")
    parser.add_argument("--bt1-slot", action="append", help="limit BattleTanx stream slots (repeatable)")
    parser.add_argument("--ps1-stem", action="append", help="limit PS1 level stems, e.g. ASSETS/BERLIN1")
    parser.add_argument("--no-model-glb", action="store_true", help="skip per-model GLB files")
    parser.add_argument("--no-inventory", action="store_true", help="skip ROM-wide inventories")
    args = parser.parse_args(argv)

    guard_root = args.ps1 if args.ps1 else ROOT / "PS1-source-not-given"
    try:
        output = check_output_dir(args.output, guard_root)
    except OutputError as error:
        parser.error(str(error))
    exporter = ex.Exporter(output)
    try:
        if args.ga_rom:
            rom = src.load_rom(args.ga_rom)
            if rom.game != "n64_btga":
                parser.error("--ga-rom is not Global Assault USA Rev 0")
            ex.export_ga(exporter, rom, SLOT_SOURCE.read_text(), _ints(args.ga_level))
            if not args.no_inventory:
                exporter.inventories.append({"platform": "n64_btga", "file": "inventory/n64_btga.json"})
                exporter._write("inventory/n64_btga.json", ex.dump(ex.ga_inventory(rom, ga_layout(args.ga_rom))))
        if args.bt1_rom:
            rom = src.load_rom(args.bt1_rom)
            if rom.game != "n64_bt1":
                parser.error("--bt1-rom is not BattleTanx USA")
            ex.export_bt1(exporter, rom, _ints(args.bt1_slot))
            if not args.no_inventory:
                exporter.inventories.append({"platform": "n64_bt1", "file": "inventory/n64_bt1.json"})
                exporter._write("inventory/n64_bt1.json", ex.dump(ex.bt1_inventory(rom)))
        if args.ps1:
            ex.export_ps1(exporter, args.ps1, args.ps1_stem)
    except src.FormatError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    catalog = exporter.finish(write_model_glb=not args.no_model_glb)
    print(f"{len(catalog['levels'])} levels, {len(catalog['models'])} models, {len(catalog['textures'])} textures")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
