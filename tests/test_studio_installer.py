"""Installer engine: fresh install, repair, upgrade, downgrade, rollback, uninstall (sandbox adapter only; no registry or Start menu)."""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path


def make_payload(root: Path, version: str, extra: bytes = b"") -> "installer.Payload":
    from tools.btga_studio.core import installer

    (root / "_internal" / "data").mkdir(parents=True)
    (root / installer.EXECUTABLE).write_bytes(b"MZ program " + version.encode() + extra)
    (root / "_internal" / "runtime.dll").write_bytes(b"runtime " + version.encode())
    (root / "_internal" / "data" / "build.json").write_text(json.dumps({"version": version}))
    (root / "README.txt").write_text("readme")
    return installer.Payload.from_folder(root, version)


class Sandbox:
    def __init__(self):
        self.root = Path(tempfile.mkdtemp(prefix="installer ü "))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        for path in self.root.rglob("*"):
            try:
                os.chmod(path, 0o700)
            except OSError:
                pass
        shutil.rmtree(self.root, ignore_errors=True)

    def system(self, running=None):
        from tools.btga_studio.core.installer import SandboxSystem

        return SandboxSystem(self.root / "system", running if running is not None else [])

    def payload(self, version, extra=b""):
        return make_payload(self.root / f"payload {version} Ωμέγα", version, extra)

    @property
    def target(self) -> Path:
        return self.root / "AppData Local" / "Programs" / "BattleTanx Studio Ελληνικά Кириллица 日本語"

    def snapshot(self, system) -> dict:
        files = {p.relative_to(self.target).as_posix(): p.read_bytes() for p in self.target.rglob("*") if p.is_file()} if self.target.exists() else {}
        links = {p.name: p.read_text() for p in (self.root / "system").rglob("*.lnk.json")}
        return {"files": files, "registration": system.read_registration(), "links": links}

    def leftovers(self) -> list[str]:
        return sorted(p.name for p in self.target.parent.iterdir() if ".staging-" in p.name or ".previous-" in p.name)


class InstallerTests(unittest.TestCase):
    def test_versions(self):
        from tools.btga_studio.core.versioning import compare

        self.assertEqual(compare("0.2.0", "0.1.9"), 1)
        self.assertEqual(compare("0.2.0", "0.2"), 0)
        self.assertEqual(compare("0.2.0-rc1", "0.2.0"), -1)
        self.assertEqual(compare("1.10.0", "1.9.0"), 1)
        with self.assertRaises(ValueError):
            compare("latest", "0.1")

    def test_fresh_install_metadata_shortcuts_and_quoting(self):
        from tools.btga_studio.core import installer

        with Sandbox() as box:
            system, payload = box.system(), box.payload("0.2.0")
            steps = []
            plan = installer.install(payload, box.target, system, desktop_shortcut=False, progress=lambda *a: steps.append(a))
            self.assertEqual(plan.kind, "fresh")
            self.assertTrue(steps and steps[-1][0] == "Done")
            record = json.loads((box.target / installer.RECORD).read_text())
            self.assertEqual((record["version"], record["upgrade_code"]), ("0.2.0", installer.UPGRADE_CODE))
            self.assertEqual(set(record["files"]), set(payload.files))
            registration = system.read_registration()
            self.assertEqual(registration["DisplayVersion"], "0.2.0")
            self.assertEqual(registration["Publisher"], installer.PUBLISHER)
            self.assertEqual(registration["UpgradeCode"], installer.UPGRADE_CODE)
            executable = str(box.target / installer.EXECUTABLE)
            self.assertEqual(registration["UninstallString"], f'"{executable}" --uninstall')  # spaces are quoted
            self.assertEqual(registration["QuietUninstallString"], f'"{executable}" --uninstall --quiet')
            self.assertEqual(json.loads(system.start_menu_link().read_text())["target"], executable)
            self.assertFalse(system.desktop_link().exists())
            self.assertEqual(box.leftovers(), [])
            for relative, info in payload.files.items():
                self.assertEqual(installer.sha256(box.target / relative), info["sha256"])

    def test_damaged_payload_is_refused_without_leftovers(self):
        from tools.btga_studio.core import installer

        with Sandbox() as box:
            system, payload = box.system(), box.payload("0.2.0")
            (payload.root / "_internal" / "runtime.dll").write_bytes(b"tampered")
            with self.assertRaises(installer.InstallError):
                installer.install(payload, box.target, system)
            self.assertFalse(box.target.exists())
            self.assertIsNone(system.read_registration())
            self.assertEqual([p.name for p in box.target.parent.iterdir()], [])

    def test_payload_manifest_rejects_unsafe_paths_and_bad_integrity_records(self):
        from tools.btga_studio.core import installer

        bad_entries = {
            "../escape.dll": {"sha256": "0" * 64, "size": 0},
            "/absolute.dll": {"sha256": "0" * 64, "size": 0},
            "C:/drive.dll": {"sha256": "0" * 64, "size": 0},
            "folder\\windows.dll": {"sha256": "0" * 64, "size": 0},
            "safe.dll": {"sha256": "not-a-digest", "size": 0},
            "also-safe.dll": {"sha256": "0" * 64, "size": True},
        }
        for relative, info in bad_entries.items():
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                (root / installer.PAYLOAD_MANIFEST).write_text(json.dumps({"version": "0.2.0", "files": {relative: info}}))
                with self.assertRaises(installer.InstallError):
                    installer.Payload.from_folder(root, "0.2.0")

    def test_reinstall_repairs_missing_files(self):
        from tools.btga_studio.core import installer

        with Sandbox() as box:
            system, payload = box.system(), box.payload("0.2.0")
            installer.install(payload, box.target, system)
            (box.target / "_internal" / "runtime.dll").unlink()
            (box.target / "README.txt").write_text("changed")
            report = installer.verify(box.target)
            self.assertEqual((report["missing"], report["changed"]), (["_internal/runtime.dll"], ["README.txt"]))
            plan = installer.install(payload, box.target, system)
            self.assertEqual(plan.kind, "reinstall")
            self.assertEqual(installer.verify(box.target)["missing"] + installer.verify(box.target)["changed"], [])

    def test_upgrade_keeps_user_files_and_identity(self):
        from tools.btga_studio.core import installer

        with Sandbox() as box:
            system = box.system()
            installer.install(box.payload("0.1.0"), box.target, system, desktop_shortcut=True)
            (box.target / "my notes.txt").write_text("keep me")
            plan = installer.install(box.payload("0.2.0"), box.target, system, desktop_shortcut=True)
            self.assertEqual((plan.kind, plan.current), ("upgrade", "0.1.0"))
            self.assertEqual((box.target / "my notes.txt").read_text(), "keep me")
            self.assertIn(b"0.2.0", (box.target / installer.EXECUTABLE).read_bytes())
            self.assertEqual(system.read_registration()["DisplayVersion"], "0.2.0")
            self.assertEqual(system.read_registration()["UpgradeCode"], installer.UPGRADE_CODE)
            self.assertTrue(system.desktop_link().exists())
            self.assertEqual(box.leftovers(), [])

    def test_downgrade_needs_explicit_choice(self):
        from tools.btga_studio.core import installer

        with Sandbox() as box:
            system = box.system()
            installer.install(box.payload("0.3.0"), box.target, system)
            older = box.payload("0.2.0")
            plan = installer.plan(older, box.target, system)
            self.assertEqual(plan.kind, "downgrade")
            self.assertFalse(plan.ok)
            with self.assertRaises(installer.InstallError):
                installer.install(older, box.target, system)
            self.assertIn(b"0.3.0", (box.target / installer.EXECUTABLE).read_bytes())
            result = installer.install(older, box.target, system, allow_downgrade=True)
            self.assertTrue(result.warnings)
            self.assertIn(b"0.2.0", (box.target / installer.EXECUTABLE).read_bytes())

    def test_failed_upgrade_rolls_back_at_every_step(self):
        from tools.btga_studio.core import installer

        for step in installer.STEPS:
            with self.subTest(step=step), Sandbox() as box:
                system = box.system()
                installer.install(box.payload("0.1.0"), box.target, system)
                before = box.snapshot(system)
                with self.assertRaises(OSError):
                    installer.install(box.payload("0.2.0"), box.target, system, desktop_shortcut=True, fail_at=step)
                self.assertEqual(box.snapshot(system), before)
                self.assertEqual(box.leftovers(), [])

    def test_failed_install_restores_preexisting_shortcut_bytes(self):
        from tools.btga_studio.core import installer

        with Sandbox() as box:
            system = box.system()
            link = system.start_menu_link()
            link.parent.mkdir(parents=True)
            original = b"pre-existing shortcut from another installation"
            link.write_bytes(original)
            with self.assertRaises(OSError):
                installer.install(box.payload("0.2.0"), box.target, system, fail_at="register")
            self.assertEqual(link.read_bytes(), original)
            self.assertFalse(box.target.exists())

    def test_failed_fresh_install_and_failing_shortcut_roll_back(self):
        from tools.btga_studio.core import installer

        with Sandbox() as box:
            system = box.system()

            def broken(link, target, working):
                raise OSError("shortcut service unavailable")

            system.create_shortcut = broken
            with self.assertRaises(OSError):
                installer.install(box.payload("0.2.0"), box.target, system)
            self.assertFalse(box.target.exists())
            self.assertIsNone(system.read_registration())
            self.assertEqual(box.leftovers(), [])

    @unittest.skipIf(sys.platform == "win32" or os.geteuid() == 0, "permission bits are not enforced here")
    def test_unwritable_folder_and_foreign_folder_and_running_app(self):
        from tools.btga_studio.core import installer

        with Sandbox() as box:
            locked = box.root / "locked"
            locked.mkdir()
            os.chmod(locked, stat.S_IRUSR | stat.S_IXUSR)
            plan = installer.plan(box.payload("0.2.0"), locked / "BattleTanx Studio", box.system())
            self.assertTrue(any("permission" in b for b in plan.blockers))
            foreign = box.root / "Documents"
            foreign.mkdir()
            (foreign / "letter.txt").write_text("mine")
            self.assertTrue(any("other files" in b for b in installer.plan(box.payload("0.2.1"), foreign, box.system()).blockers))
            installer.install(box.payload("0.2.2"), box.target, box.system())
            running = box.system(running=[4242])
            self.assertTrue(any("running" in b for b in installer.plan(box.payload("0.2.3"), box.target, running).blockers))
            with self.assertRaises(installer.InstallError):
                installer.uninstall(box.target, running)
            self.assertTrue((box.target / installer.EXECUTABLE).exists())

    def test_uninstall_preserves_user_data_and_user_files(self):
        from tools.btga_studio.core import installer

        with Sandbox() as box:
            system = box.system()
            installer.install(box.payload("0.2.0"), box.target, system, desktop_shortcut=True)
            (box.target / "_internal" / "runtime.dll").unlink()  # missing files are fine
            (box.target / "my map.btgaproj").write_text("{}")
            roaming = box.root / "Roaming" / "BattleTanx Studio"
            local = box.root / "Local" / "BattleTanx Studio"
            for folder in (roaming, local):
                folder.mkdir(parents=True)
                (folder / "preferences.json").write_text("{}")
            result = installer.uninstall(box.target, system, user_data=(roaming, local))
            self.assertEqual(result.kept, ["my map.btgaproj"])
            self.assertFalse((box.target / installer.EXECUTABLE).exists())
            self.assertTrue(roaming.is_dir() and local.is_dir())
            self.assertIsNone(system.read_registration())
            self.assertFalse(system.start_menu_link().exists() or system.desktop_link().exists())
            installer.uninstall(box.target, system, remove_user_data=True, user_data=(roaming, local, box.root / "Documents"))
            self.assertFalse(roaming.exists() or local.exists())

    def test_uninstall_from_inside_and_without_record(self):
        from tools.btga_studio.core import installer

        with Sandbox() as box:
            system = box.system()
            installer.install(box.payload("0.2.0"), box.target, system)
            result = installer.uninstall(box.target, system, running_from_target=True)
            self.assertTrue(result.scheduled_folder_removal)
            self.assertFalse(box.target.exists())  # the sandbox removes it at once; Windows does it after exit
            box.target.mkdir(parents=True)
            (box.target / installer.EXECUTABLE).write_bytes(b"MZ")
            (box.target / "_internal").mkdir()
            (box.target / "_internal" / "x.dll").write_bytes(b"x")
            (box.target / "family photo.jpg").write_bytes(b"jpg")
            result = installer.uninstall(box.target, system)
            self.assertEqual(result.kept, ["family photo.jpg"])  # without a record only program files are removed

    def test_portable_detection_does_not_register(self):
        from tools.btga_studio.core import environment, installer

        with Sandbox() as box:
            portable = box.payload("0.2.0").root
            env = environment.detect(portable, frozen=True)
            self.assertEqual(env.mode, "portable")
            self.assertIsNone(box.system().read_registration())
            installer.install(installer.Payload.from_folder(portable, "0.2.0"), box.target, box.system())
            self.assertEqual(environment.detect(box.target, frozen=True).mode, "installed")
            self.assertEqual(environment.detect(box.target, frozen=False).mode, "source")


if __name__ == "__main__":
    unittest.main()
