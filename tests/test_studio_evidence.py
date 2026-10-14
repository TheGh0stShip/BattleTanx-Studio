"""Reusable acceptance tooling: evidence records, image checks, manifests and the package audit (synthetic inputs)."""

from __future__ import annotations

import json
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None


def _pe(subsystem: int) -> bytes:
    data = bytearray(1024)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", data, 0x80 + 24, 0x20B)
    struct.pack_into("<H", data, 0x80 + 24 + 68, subsystem)
    return bytes(data)


class PackageAuditTests(unittest.TestCase):
    def _dist(self, root: Path, subsystem: int = 2) -> Path:
        dist = root / "BTGAStudio"
        for name in ("_internal/python313.dll", "README.txt", "samples/sample.btgaproj", "samples/sample-catalog/catalog.json",
                     "samples/starter-example.btgaproj"):
            (dist / name).parent.mkdir(parents=True, exist_ok=True)
            (dist / name).write_bytes(b"neutral content")
        (dist / "BTGAStudio.exe").write_bytes(_pe(subsystem))
        return dist

    def test_gui_subsystem_required_files_and_leaks(self):
        from tools.audit_studio_package import audit, pe_subsystem

        with tempfile.TemporaryDirectory() as temp:
            dist = self._dist(Path(temp))
            self.assertEqual(pe_subsystem(dist / "BTGAStudio.exe"), "windows-gui")
            self.assertEqual(audit(dist, ["C:\\Users\\builder"])["problems"], [])
            (dist / "_internal" / "leak.pyc").write_bytes("x C:\\USERS\\builder\\src y".encode("utf-16-le"))
            import zipfile

            with zipfile.ZipFile(dist / "_internal" / "base_library.zip", "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("os.pyc", b"clean")
                archive.writestr("site.pyc", b"built in C:\\Users\\builder\\venv")
                archive.writestr("game.z64", b"")
            (dist / "crash.dmp").write_bytes(b"")
            (dist / "samples" / "starter-example.btgaproj").unlink()
            problems = audit(dist, ["C:\\Users\\builder"])["problems"]
            self.assertIn("missing required samples/starter-example.btgaproj", problems)
            self.assertIn("forbidden file type crash.dmp", problems)
            self.assertIn("_internal/leak.pyc contains 1 forbidden fragment encoding(s)", problems)
            self.assertIn("_internal/base_library.zip:site.pyc contains 1 forbidden fragment encoding(s)", problems)
            self.assertIn("forbidden file type _internal/base_library.zip:game.z64", problems)
            self.assertNotIn("forbidden file type _internal/base_library.zip", problems)
            self.assertIn(
                "forbidden file type _internal/base_library.zip:game.z64",
                audit(dist, [])["problems"],
            )
        with tempfile.TemporaryDirectory() as temp:
            dist = self._dist(Path(temp), subsystem=3)
            self.assertIn("BTGAStudio.exe uses the windows-console subsystem", audit(dist, [])["problems"])
            (dist / "BTGAStudio.exe").write_bytes(b"not a program")
            with self.assertRaises(ValueError):
                pe_subsystem(dist / "BTGAStudio.exe")

    def test_fragment_split_across_scan_windows_is_found(self):
        from tools import audit_studio_package as module

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "big.bin"
            path.write_bytes(b"a" * (module.WINDOW - 3) + b"SECRET" + b"b" * 10)
            self.assertEqual(module._contains(path, [b"secret"]), [b"secret"])

    def test_manifests_name_every_difference(self):
        from tools.studio_evidence import compare_manifests, manifest

        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            for root in (a, b):
                (Path(root) / "same.txt").write_text("x")
            (Path(a) / "diff.bin").write_bytes(b"1")
            (Path(b) / "diff.bin").write_bytes(b"2")
            (Path(a) / "only_a").write_text("a")
            difference = compare_manifests(manifest(Path(a)), manifest(Path(b)))
            self.assertEqual(difference, {"only_first": ["only_a"], "only_second": [], "different": ["diff.bin"]})
            self.assertEqual(manifest(Path(a)), manifest(Path(a)))


@unittest.skipIf(np is None, "needs numpy")
class EvidenceTests(unittest.TestCase):
    def test_blank_and_flat_images_are_rejected(self):
        from tools.studio_evidence import image_stats, inspect_image

        blank = bytes([10, 10, 12, 255]) * (32 * 16)
        self.assertTrue(inspect_image(image_stats(32, 16, blank)))
        noise = bytes(np.random.default_rng(1).integers(0, 255, 32 * 16 * 4, dtype=np.uint8))
        self.assertEqual(inspect_image(image_stats(32, 16, noise)), [])
        self.assertTrue(inspect_image(image_stats(32, 16, noise, region=(40, 0, 8, 8))))  # region outside the image is empty

    def test_record_and_source_identity(self):
        """Read-only git use (rev-parse, diff, ls-files, status) against this checkout."""

        from tools.studio_evidence import source_identity, stale, write_record

        root = Path(__file__).resolve().parents[1]
        identity = source_identity(root)
        head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
        self.assertEqual(identity["commit"], head)
        self.assertRegex(identity["tree_sha256"], "^[0-9a-f]{64}$")
        self.assertEqual(identity, source_identity(root))  # deterministic for an unchanged tree
        self.assertFalse(stale(identity, root))
        self.assertTrue(stale({**identity, "tree_sha256": "0" * 64}, root))
        self.assertTrue(stale({**identity, "commit": "0" * 40}, root))
        with tempfile.TemporaryDirectory() as temp:
            png = Path(temp) / "shot.png"
            png.write_bytes(b"\x89PNG fixture")
            record = json.loads(write_record(png, identity, "scenario", "expected", "observed", True).read_text())
            self.assertEqual((record["filename"], record["result"]), ("shot.png", "pass"))
            self.assertNotIn(str(temp), json.dumps(record))

if __name__ == "__main__":
    unittest.main()
