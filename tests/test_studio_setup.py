"""Setup program (pages, silent mode, self-check), uninstall confirmation, release assembly and the installer audit.

Only ``SandboxSystem`` is used: nothing touches the registry, Start menu or desktop.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

from tests.test_studio_authoring import _egl_available
from tests.test_studio_evidence import _pe


def build_dist(root: Path, version: str = "0.2.0") -> Path:
    """A synthetic program folder shaped like the real release (no game data)."""

    from tools.btga_studio.packaging import release

    dist = root / "dist ü" / "BattleTanxStudio"
    for name in ("_internal/python313.dll", "_internal/data/build.json", "_internal/data/800E8380_slot_asset_ranges.c",
                 "_internal/data/fonts/DejaVuSans.ttf", "_internal/data/fonts/fonts.json", "samples/sample.btgaproj", "samples/sample-catalog/catalog.json",
                 "samples/starter-example.btgaproj"):
        (dist / name).parent.mkdir(parents=True, exist_ok=True)
        (dist / name).write_bytes(b"synthetic " + name.encode())
    (dist / "BattleTanxStudio.exe").write_bytes(_pe(2))
    (dist / "README.txt").write_text("readme", encoding="utf-8")
    (dist / "COPYING.txt").write_text("licence", encoding="utf-8")
    release.prepare_dist(dist)
    return dist


class ReleaseAndAuditTests(unittest.TestCase):
    def test_release_folder_has_only_end_user_files(self):
        from tools.btga_studio.core import installer
        from tools.btga_studio.packaging import release

        with tempfile.TemporaryDirectory() as temp:
            dist = build_dist(Path(temp))
            setup = Path(temp) / "BattleTanxStudioSetup.exe"
            setup.write_bytes(_pe(2))
            manifest = json.loads((dist / installer.PAYLOAD_MANIFEST).read_text())
            self.assertEqual((manifest["version"], manifest["upgrade_code"]), (release.__version__, installer.UPGRADE_CODE))
            first = release.finalize(dist, Path(temp) / "out1", "rev", setup)
            second = release.finalize(dist, Path(temp) / "out2", "rev", setup)
            self.assertEqual(first["archive"]["sha256"], second["archive"]["sha256"])
            self.assertEqual(sorted(p.name for p in (Path(temp) / "out1").iterdir()), sorted(release.RELEASE_FILES))
            self.assertFalse(first["signed"])
            sums = (Path(temp) / "out1" / "SHA256SUMS.txt").read_text()
            for name in ("BattleTanxStudioSetup.exe", "BattleTanxStudio-portable-x64.zip", "build-manifest.json"):
                self.assertIn(name, sums)
            notes = (Path(temp) / "out1" / "RELEASE-NOTES.txt").read_text()
            self.assertIn(release.__version__, notes)
            self.assertIn("not digitally signed", notes)
            names = zipfile.ZipFile(Path(temp) / "out1" / "BattleTanxStudio-portable-x64.zip").namelist()
            self.assertIn(f"BattleTanxStudio-{release.__version__}/payload-manifest.json", names)
            self.assertIn("Fonts", (dist / "THIRD-PARTY-NOTICES.txt").read_text())
            (Path(temp) / "out1" / "stray.log").write_text("x")
            with self.assertRaises(SystemExit):
                release.finalize(dist, Path(temp) / "out1", "rev", setup)

    def test_installer_audit_passes_and_detects_problems(self):
        from tools.audit_studio_installer import audit
        from tools.btga_studio.core import installer

        with tempfile.TemporaryDirectory() as temp:
            dist = build_dist(Path(temp))
            result = audit(None, dist, ["/home/someone"])
            self.assertTrue(result["ok"], result)
            for name in ("registration", "shortcuts", "quoting", "upgrade", "repair", "uninstall", "user_data_preserved"):
                self.assertIs(result["checks"][name], True, name)
            self.assertEqual(len([k for k in result["checks"] if k.startswith("rollback_")]), len(installer.STEPS))
            old = Path(temp) / "old-manifest.json"
            old.write_text(json.dumps({"upgrade_code": "{00000000-0000-0000-0000-000000000000}"}))
            self.assertIsNot(audit(None, dist, [], old)["checks"]["upgrade_code_stable"], True)
            (dist / "_internal" / "python313.dll").write_bytes(b"changed")
            self.assertIsNot(audit(None, dist, [])["checks"]["payload_manifest"], True)
            manifest_path = dist / installer.PAYLOAD_MANIFEST
            manifest = json.loads(manifest_path.read_text())
            manifest["files"]["../outside.dll"] = {"sha256": "0" * 64, "size": 0}
            manifest_path.write_text(json.dumps(manifest))
            unsafe = audit(None, dist, [])
            self.assertFalse(unsafe["ok"])
            self.assertIn("unsafe file path", unsafe["checks"]["payload_manifest"])
            wrong = dist / "BattleTanxStudio.exe"  # a GUI program whose version resource names the editor, not the setup
            self.assertIsNot(audit(wrong, dist, [])["checks"]["setup_version"], True)


class SetupModeTests(unittest.TestCase):
    def test_silent_install_upgrade_refusal_and_self_check(self):
        from tools.btga_studio.core import installer
        from tools.btga_studio.setup import main as setup

        with tempfile.TemporaryDirectory(prefix="silent ü ") as temp:
            dist = build_dist(Path(temp))
            sandbox, target = Path(temp) / "sandbox", Path(temp) / "Programs" / "BattleTanx Studio"
            report = Path(temp) / "report.json"
            args = ["--payload", str(dist), "--silent", "--sandbox", str(sandbox), "--target", str(target), "--report", str(report)]
            self.assertEqual(setup.main(args + ["--desktop"]), 0)
            self.assertEqual(json.loads(report.read_text())["kind"], "fresh")
            self.assertTrue((target / installer.EXECUTABLE).is_file())
            self.assertEqual(setup.main(args), 0)
            self.assertEqual(json.loads(report.read_text())["kind"], "reinstall")
            manifest = json.loads((dist / installer.PAYLOAD_MANIFEST).read_text())
            manifest["version"] = "0.0.9"
            (dist / installer.PAYLOAD_MANIFEST).write_text(json.dumps(manifest))
            self.assertEqual(setup.main(args), 1)
            self.assertIn("newer", json.loads(report.read_text())["refused"])
            self.assertEqual(setup.main(args + ["--allow-downgrade"]), 0)
            check = Path(temp) / "check.json"
            self.assertEqual(setup.main(["--payload", str(dist), "--self-check", str(check)]), 0)
            (dist / "README.txt").write_text("tampered")
            self.assertEqual(setup.main(["--payload", str(dist), "--self-check", str(check)]), 1)
            self.assertEqual(json.loads(check.read_text())["damaged"], ["README.txt"])


@unittest.skipIf(np is None or not _egl_available(), "needs imgui-bundle and EGL (off-screen pages)")
class SetupPageTests(unittest.TestCase):
    def setUp(self):
        os.environ.setdefault("PYOPENGL_PLATFORM", "egl")
        from tools.btga_studio.gui.offscreen import OffscreenSurface

        self.temp = tempfile.TemporaryDirectory(prefix="setup pages ü ")
        self.root = Path(self.temp.name)
        self.surface = OffscreenSurface(760, 520)

    def tearDown(self):
        self.surface.close()
        self.temp.cleanup()

    def test_pages_install_and_launch(self):
        from tools.btga_studio.core import installer
        from tools.btga_studio.setup.ui import SetupUI

        dist = build_dist(self.root)
        system = installer.SandboxSystem(self.root / "sandbox", running=[])
        launched = []
        ui = SetupUI(installer.Payload.from_folder(dist, "0"), system, {"Third-party notices": "notices"},
                     self.root / "Programs ü" / "BattleTanx Studio", launcher=launched.append)
        for page in ("welcome", "options"):
            ui.page = page
            self.surface.frame(ui.draw)
        self.assertEqual(ui.plan().kind, "fresh")
        ui.licence_name = "Third-party notices"
        self.surface.frame(ui.draw)
        ui.licence_name = None
        ui.desktop = True
        ui.start()
        ui.thread.join(30)
        self.surface.frame(ui.draw)
        self.assertEqual(ui.page, "done")
        ui.finish()
        self.assertEqual(launched, [ui.target / installer.EXECUTABLE])
        self.assertTrue(system.desktop_link().is_file())

    def test_failure_shows_problem_and_rolls_back(self):
        from tools.btga_studio.core import installer
        from tools.btga_studio.setup.ui import SetupUI

        dist = build_dist(self.root)
        system = installer.SandboxSystem(self.root / "sandbox", running=[])

        def broken(*args):
            raise OSError("simulated shortcut failure")

        system.create_shortcut = broken
        ui = SetupUI(installer.Payload.from_folder(dist, "0"), system, {}, self.root / "Programs" / "BattleTanx Studio", launcher=None)
        ui.page = "options"
        ui.start()
        ui.thread.join(30)
        self.surface.frame(ui.draw)
        self.assertEqual(ui.page, "problem")
        self.assertIn("undone", ui.error)
        self.assertFalse(ui.target.exists())
        self.assertIsNone(system.read_registration())

    def test_uninstall_confirmation_keeps_data_by_default(self):
        from tools.btga_studio.core import installer
        from tools.btga_studio.setup.ui import UninstallUI

        dist = build_dist(self.root)
        system = installer.SandboxSystem(self.root / "sandbox", running=[])
        target = self.root / "Programs" / "BattleTanx Studio"
        installer.install(installer.Payload.from_folder(dist, "0"), target, system)
        data = self.root / "Roaming" / "BattleTanx Studio"
        data.mkdir(parents=True)
        ui = UninstallUI(target, system, (data,))
        self.surface.frame(ui.draw)
        self.assertFalse(ui.remove_data)
        ui.result = installer.uninstall(target, system, user_data=(data,))
        self.surface.frame(ui.draw)
        self.assertTrue(data.is_dir())
        self.assertFalse((target / installer.EXECUTABLE).exists())


if __name__ == "__main__":
    unittest.main()
