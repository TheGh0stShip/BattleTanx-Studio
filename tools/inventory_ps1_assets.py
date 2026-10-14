#!/usr/bin/env python3
"""Inventory and decode a locally extracted BattleTanx: Global Assault PS1 disc.

Walks every file under the extracted directory, decodes the families whose
layouts are proven (see docs/PS1_ASSET_FORMATS.md), validates cross-file
relationships, and writes a deterministic JSON manifest plus optional PNG
previews.  The extracted disc is only read.  Output must go to a directory
the repository ignores (or outside the repository) and never into the disc
tree.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import ps1_assets as ps1  # noqa: E402
from tools.btga_assets import ps1_model  # noqa: E402

MANIFEST_VERSION = 1
REPO_ROOT = Path(__file__).resolve().parents[1]

# Extension -> family.  Level BINs are told apart from the line tables by
# whether a model bank with the same stem exists (the level loader builds
# "<level>.bin" next to "<level>.MMM"; line tables are named explicitly).
INVENTORY_ONLY = {
    ".STR": "movie stream (inventory only)",
    ".PCK": "audio pack (inventory only)",
    ".FUL": "full-screen bitmap (inventory only)",
    ".SFX": "sound bank (inventory only)",
    ".TXT": "text",
    ".CNF": "system config",
    ".DAT": "license data",
    ".44": "executable",
}


class OutputError(RuntimeError):
    pass


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def check_output_dir(output: Path, corpus: Path, repo_root: Path = REPO_ROOT) -> Path:
    """Refuse output paths inside the disc tree or tracked repository paths."""

    resolved = output.resolve()
    if _is_relative_to(resolved, corpus.resolve()):
        raise OutputError(f"refusing to write into the extracted disc: {resolved}")
    if _is_relative_to(resolved, repo_root.resolve()):
        probe = resolved / "manifest.json"
        result = subprocess.run(
            ["git", "-C", str(repo_root), "check-ignore", "-q", str(probe)],
            check=False,
        )
        if result.returncode != 0:
            raise OutputError(f"output directory is not git-ignored: {resolved}")
    return resolved


def load_disc_listing(corpus: Path) -> dict[str, str]:
    """Read the sibling extracted-sha256.txt written when the disc was dumped."""

    listing = corpus.parent / "extracted-sha256.txt"
    hashes = {}
    if not listing.is_file():
        return hashes
    for line in listing.read_text(encoding="utf-8").splitlines():
        digest, _, path = line.partition("  ")
        marker = f"/{corpus.name}/"
        if marker in path:
            hashes[path.split(marker, 1)[1]] = digest
    return hashes


def texture_summary(data: bytes, texture: ps1.Texture) -> dict:
    entry = {
        "index": texture.index,
        "width": texture.width,
        "height": texture.height,
        "mode": ps1.MODE_NAMES[texture.mode],
        "header_offset": texture.header_offset,
        "pixels": ps1.raw_range(data, texture.pixel_offset, texture.pixel_size),
        "pixel_advance": texture.pixel_advance,
    }
    if texture.mode != ps1.MODE_16BPP:
        entry["max_index"] = ps1.max_index(data, texture)
        entry["palette_count"] = len(texture.palettes)
        entry["palette_offset"] = texture.palette_offset
        entry["palette_sha256"] = [
            ps1.sha256(b"".join(color.to_bytes(2, "little") for color in palette))
            for palette in texture.palettes
        ]
        entry["palette0_transparent_index0"] = bool(texture.palettes) and texture.palettes[0][0] == 0
        entry["palette_stp_entries"] = [
            sum(1 for color in palette if color & 0x8000) for palette in texture.palettes
        ]
    if texture.misaligned_palette:
        entry["loader_palette_shift_bytes"] = texture.pixel_size - texture.pixel_advance
        entry["authored_palette_offset"] = texture.authored_palette_offset
    if texture.mode == ps1.MODE_4BPP and texture.width % 4:
        entry["note"] = "4bpp width is not a multiple of 4; VRAM row packing unproven"
    if texture.mode == ps1.MODE_8BPP and texture.width % 2:
        entry["note"] = "8bpp width is odd; VRAM row packing unproven"
    return entry


class Inventory:
    def __init__(self, corpus: Path, previews: Path | None):
        self.corpus = corpus
        self.previews = previews
        self.files: dict[str, dict] = {}
        self.data: dict[str, bytes] = {}
        self.parsed: dict[str, dict] = {}

    def preview(self, relative: str, suffix: str, width: int, height: int, rgba: bytes) -> str | None:
        if self.previews is None:
            return None
        target = self.previews / f"{relative}.{suffix}.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(ps1.encode_png_rgba(width, height, rgba))
        return target.relative_to(self.previews.parent).as_posix()

    def scan(self) -> None:
        listing = load_disc_listing(self.corpus)
        for path in sorted(self.corpus.rglob("*"), key=lambda item: item.relative_to(self.corpus).as_posix()):
            if not path.is_file():
                continue
            relative = path.relative_to(self.corpus).as_posix()
            data = path.read_bytes()
            self.data[relative] = data
            digest = ps1.sha256(data)
            self.files[relative] = {
                "path": relative,
                "size": len(data),
                "sha256": digest,
                "header_hex": data[:16].hex(),
                "extension": path.suffix.upper(),
            }
            if relative in listing:
                self.files[relative]["matches_disc_listing"] = listing[relative] == digest

    def stem_has(self, relative: str, extension: str) -> str | None:
        parent, _, name = relative.rpartition("/")
        stem = name.rsplit(".", 1)[0]
        for candidate in (f"{stem}{extension}", f"{stem}{extension.lower()}"):
            path = f"{parent}/{candidate}" if parent else candidate
            if path in self.files:
                return path
        return None

    def decode(self, relative: str) -> None:
        entry = self.files[relative]
        data = self.data[relative]
        extension = entry["extension"]
        try:
            if extension == ".TEX":
                self.decode_tex(relative, entry, data)
            elif extension == ".IMG":
                self.decode_img(relative, entry, data)
            elif extension == ".PLT":
                entry["family"] = "PLT palette bank"
                parsed = ps1.parse_plt(data, relative)
                self.parsed[relative] = parsed
                entry.update(
                    status="decoded",
                    records=parsed["count"],
                    palettes=sum(len(record["palettes"]) for record in parsed["records"]),
                    trailing=parsed["trailing"],
                )
                if not self.stem_has(relative, ".IMG"):
                    entry["relationship_error"] = "no IMG with the same stem"
            elif extension == ".TTT":
                entry["family"] = "TTT model texture lists"
                parsed = ps1.parse_ttt(data, relative)
                self.parsed[relative] = parsed
                entry.update(status="decoded", lists=parsed["count"], trailing=parsed["trailing"])
                entry["references"] = sum(len(item["textures"]) for item in parsed["lists"])
            elif extension == ".MMM":
                self.decode_mmm(relative, entry, data)
            elif extension == ".FLB":
                self.decode_flb(relative, entry, data)
            elif extension == ".BIN":
                if self.stem_has(relative, ".MMM"):
                    entry["family"] = "level BIN"
                    parsed = ps1.parse_level_bin(data, relative)
                    self.parsed[relative] = parsed
                    entry.update(
                        status="structural",
                        offsets=parsed["offsets"],
                        header_words=parsed["header_words"],
                        section1_count=parsed["section1_count"],
                        section1_first=parsed["section1_first"],
                        placements=parsed["placements"],
                        section1_reads=parsed["section1_reads"],
                        section2_records=parsed["section2_records"],
                        sections=parsed["sections"],
                    )
                else:
                    entry["family"] = "line table BIN (inventory only)"
                    entry["status"] = "inventory-only"
            else:
                entry["family"] = INVENTORY_ONLY.get(extension, "unclassified")
                entry["status"] = "inventory-only"
        except ps1.FormatError as error:
            entry["status"] = "rejected"
            entry["reason"] = str(error)

    def decode_tex(self, relative: str, entry: dict, data: bytes) -> None:
        entry["family"] = "TEX texture bank"
        parsed = ps1.parse_tex(data, relative)
        textures = []
        for texture in parsed["textures"]:
            summary = texture_summary(data, texture)
            rgba = ps1.texture_rgba(data, texture)
            summary["preview"] = self.preview(relative, f"{texture.index:03d}", texture.width, texture.height, rgba)
            if texture.authored_palettes:
                authored = ps1.texture_rgba(data, texture, texture.authored_palettes[0])
                summary["authored_preview"] = self.preview(
                    relative, f"{texture.index:03d}.authored", texture.width, texture.height, authored
                )
            textures.append(summary)
        entry.update(
            status="decoded",
            magic=parsed["magic"],
            textures=textures,
            consumed=parsed["consumed"],
            trailing=parsed["trailing"],
        )

    def decode_img(self, relative: str, entry: dict, data: bytes) -> None:
        entry["family"] = "IMG pixel bank"
        img = ps1.parse_img(data, relative)
        self.parsed[relative] = img
        plt_path = self.stem_has(relative, ".PLT")
        if plt_path is None:
            raise ps1.FormatError(f"{relative}: no PLT with the same stem")
        plt = ps1.parse_plt(self.data[plt_path], plt_path)
        textures = ps1.pair_img_plt(img, plt, relative)
        summaries = []
        for texture in textures:
            summary = texture_summary(data, texture)
            if texture.palettes or texture.mode == ps1.MODE_16BPP:
                limit = len(texture.palettes[0]) if texture.palettes else None
                if limit is not None and summary["max_index"] >= limit:
                    raise ps1.FormatError(f"{relative}[{texture.index}]: index exceeds palette")
                rgba = ps1.texture_rgba(data, texture)
                summary["preview"] = self.preview(
                    relative, f"{texture.index:03d}", texture.width, texture.height, rgba
                )
            summaries.append(summary)
        entry.update(
            status="decoded",
            palette_file=plt_path,
            textures=summaries,
            consumed=img["consumed"],
            trailing=img["trailing"],
        )

    def decode_mmm(self, relative: str, entry: dict, data: bytes) -> None:
        entry["family"] = "MMM model bank"
        opcodes: dict[int, int] = {}
        gpu_codes: dict[str, int] = {}
        models = []
        for index, model in ps1_model.decode_bank(data, relative):
            if model is None:
                models.append({"index": index, "empty": True})
                continue
            for command in model.commands:
                opcodes[command.opcode] = opcodes.get(command.opcode, 0) + 1
            for primitive in model.primitives:
                key = f"{primitive.gpu_code:#04x}"
                gpu_codes[key] = gpu_codes.get(key, 0) + 1
            models.append(
                {
                    "index": index,
                    "empty": False,
                    "offset": model.offset,
                    "size": model.size,
                    "sha256": model.sha256,
                    "radius": model.radius,
                    "footprint": list(model.footprint),
                    "parts": model.part_count,
                    "part_markers_match": model.part_markers_match,
                    "vertices": model.vertex_count,
                    "body_word_0c": model.body_word_0c,
                    "body_word_10": model.body_word_10,
                    "header_raw": model.header_raw,
                    "stream_offset": model.stream_offset,
                    "stream_bytes": model.stream_end - model.stream_offset,
                    "stream_covered_bytes": model.covered_bytes,
                    "commands": len(model.commands),
                    "primitives": len(model.primitives),
                    "primitives_missing_state": sum(1 for primitive in model.primitives if primitive.missing),
                }
            )
        entry.update(
            status="decoded",
            models=models,
            opcode_histogram={f"{key:#04x}": value for key, value in sorted(opcodes.items())},
            gpu_code_histogram=dict(sorted(gpu_codes.items())),
        )

    def decode_flb(self, relative: str, entry: dict, data: bytes) -> None:
        entry["family"] = "FLB floor grid"
        parsed = ps1.parse_flb(data, relative)
        dimension = parsed["dimension"]
        rgba = bytearray()
        for cell in parsed["cells"]:
            red, green, blue = ps1.flb_cell_color(cell)
            rgba.extend((red, green, blue, 255))
        entry.update(
            status="structural",
            dimension=dimension,
            header_word_2=parsed["header_word_2"],
            shade_histogram={str(key): value for key, value in parsed["shade_histogram"].items()},
            distinct_high_bits=parsed["distinct_high_bits"],
            unreachable_tail=parsed["unreachable_tail"],
            preview=self.preview(relative, "shade", dimension, dimension, bytes(rgba)),
        )

    def level_names(self) -> list[str]:
        executable = self.data.get("SLUS_010.44")
        if executable is None:
            return []
        try:
            return ps1.read_level_names(executable)
        except ps1.FormatError:
            return []

    def relationships(self) -> list[dict]:
        """Cross-check TTT against MMM and IMG for every model-bank stem."""

        levels = {name.upper() for name in self.level_names()}
        groups = []
        for relative in sorted(path for path in self.files if self.files[path]["extension"] == ".MMM"):
            ttt = self.stem_has(relative, ".TTT")
            img = self.stem_has(relative, ".IMG")
            group = {
                "stem": relative.rsplit(".", 1)[0],
                "files": {
                    extension: self.stem_has(relative, extension)
                    for extension in (".MMM", ".TTT", ".IMG", ".PLT", ".TEX", ".FLB", ".BIN")
                },
                "errors": [],
            }
            name = relative.rsplit("/", 1)[-1].rsplit(".", 1)[0]
            group["in_level_table"] = name.upper() in levels if levels else None
            radar = f"RADAR/{relative.rsplit('/', 1)[-1].rsplit('.', 1)[0]}.TEX"
            group["files"]["RADAR"] = radar if radar in self.files else None
            mmm = self.parsed.get(relative)
            ttt_parsed = self.parsed.get(ttt) if ttt else None
            img_parsed = self.parsed.get(img) if img else None
            if mmm and ttt_parsed:
                if ttt_parsed["count"] > mmm["count"]:
                    group["errors"].append("TTT lists more models than MMM holds")
                group["model_count"] = mmm["count"]
                group["texture_list_count"] = ttt_parsed["count"]
            if ttt_parsed and img_parsed:
                limit = img_parsed["count"]
                bad = [
                    (item["index"], index)
                    for item in ttt_parsed["lists"]
                    for index in item["textures"]
                    if index >= limit
                ]
                if bad:
                    group["errors"].append(f"{len(bad)} TTT indices exceed {limit} IMG records")
                group["img_records"] = limit
            elif ttt and not img:
                group["errors"].append("TTT without IMG")
            groups.append(group)
        return groups

    def manifest(self) -> dict:
        for relative in self.files:
            self.decode(relative)
        families: dict[str, dict] = {}
        for entry in self.files.values():
            stats = families.setdefault(entry["family"], {"files": 0, "bytes": 0, "status": {}})
            stats["files"] += 1
            stats["bytes"] += entry["size"]
            stats["status"][entry["status"]] = stats["status"].get(entry["status"], 0) + 1
        rejected = {path: entry["reason"] for path, entry in self.files.items() if entry["status"] == "rejected"}
        executable = self.files.get("SLUS_010.44")
        return {
            "manifest_version": MANIFEST_VERSION,
            "corpus": {
                "files": len(self.files),
                "bytes": sum(entry["size"] for entry in self.files.values()),
                "executable_sha256": executable["sha256"] if executable else None,
                "disc_listing_mismatches": sorted(
                    path for path, entry in self.files.items() if entry.get("matches_disc_listing") is False
                ),
            },
            "families": dict(sorted(families.items())),
            "rejected": rejected,
            "level_table": self.level_names(),
            "relationships": self.relationships(),
            "files": [self.files[path] for path in sorted(self.files)],
        }


def build_manifest(corpus: Path, previews: Path | None = None) -> dict:
    inventory = Inventory(corpus, previews)
    inventory.scan()
    return inventory.manifest()


def dump_manifest(manifest: dict) -> str:
    return json.dumps(manifest, indent=1, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("corpus", type=Path, help="extracted disc directory (read only)")
    parser.add_argument("output", type=Path, help="ignored output directory")
    parser.add_argument("--no-previews", action="store_true", help="skip PNG previews")
    args = parser.parse_args(argv)

    if not args.corpus.is_dir():
        parser.error(f"{args.corpus} is not a directory")
    try:
        output = check_output_dir(args.output, args.corpus)
    except OutputError as error:
        parser.error(str(error))
    output.mkdir(parents=True, exist_ok=True)
    previews = None if args.no_previews else output / "previews"
    manifest = build_manifest(args.corpus, previews)
    (output / "manifest.json").write_text(dump_manifest(manifest), encoding="utf-8")
    for family, stats in manifest["families"].items():
        print(f"{family}: {stats['files']} files {stats['status']}")
    for path, reason in manifest["rejected"].items():
        print(f"rejected {path}: {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
