"""Audit BattleTanxStudioSetup.exe and the program folder it embeds, without touching the host system.

    python tools/audit_studio_installer.py --setup SETUP.exe --dist PROGRAM_DIR [--previous-manifest OLD.json]
                                           [--forbid TEXT ...] [--report OUT.json]

Checks (each reported by name; exit status 1 if any fails):

* setup_subsystem / setup_version: the setup is a Windows GUI program whose version
  resource names it ``BattleTanxStudioSetup.exe``, the publisher and the product version;
* payload_manifest: every file in ``payload-manifest.json`` exists with its SHA-256,
  the version matches the program, and the upgrade code is the product's constant;
* upgrade_code_stable: with ``--previous-manifest`` (an earlier build-manifest.json or
  payload manifest), the upgrade code is unchanged;
* payload_clean: delegated to ``audit_studio_package.audit`` (GUI subsystem, required
  files, no proprietary or private files, no forbidden text such as build paths);
* sandbox scenarios using the real payload and ``SandboxSystem`` (a temporary folder,
  never the registry or Start menu): fresh install with a path containing spaces and
  non-ASCII characters, registration metadata, shortcut targets, argument quoting,
  upgrade from a synthetic older version, rollback at every step, repair, and uninstall
  preserving user data and user files by default.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.audit_studio_package import audit as audit_package  # noqa: E402
from tools.audit_studio_package import pe_subsystem, version_strings  # noqa: E402
from tools.btga_studio.core import installer  # noqa: E402

SETUP_NAME = "BattleTanxStudioSetup.exe"


def _older_payload(source: installer.Payload, root: Path, version: str) -> installer.Payload:
    """A synthetic older release: the same files with a different program binary and version."""

    root.mkdir(parents=True)
    for relative in source.files:
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if relative == installer.EXECUTABLE:
            destination.write_bytes(b"MZ synthetic older build " + version.encode())
        else:
            shutil.copyfile(source.root / relative, destination)
    return installer.Payload.from_folder(root, version)


def scenarios(payload: installer.Payload) -> dict[str, bool | str]:
    results: dict[str, bool | str] = {}
    with tempfile.TemporaryDirectory(prefix="setup audit ") as temp:
        temp = Path(temp)
        system = installer.SandboxSystem(temp / "system", running=[])
        target = temp / "Local App Data" / "Programs" / "BattleTanx Studio Ελλάδα 日本"
        older = _older_payload(payload, temp / "older", "0.0.1")
        installer.install(older, target, system, desktop_shortcut=True)
        (target / "notes I added.txt").write_text("mine", encoding="utf-8")
        before = _snapshot(target, system)
        for step in installer.STEPS:
            try:
                installer.install(payload, target, system, desktop_shortcut=True, fail_at=step)
            except OSError:
                pass
            if _snapshot(target, system) != before or _leftovers(target):
                results[f"rollback_{step}"] = "state changed after a failed upgrade"
            else:
                results[f"rollback_{step}"] = True
        plan = installer.install(payload, target, system, desktop_shortcut=True)
        results["upgrade"] = plan.kind == "upgrade" and (target / "notes I added.txt").is_file() or "upgrade lost files or was not detected"
        registration = system.read_registration() or {}
        executable = target / installer.EXECUTABLE
        expected = {"DisplayName": installer.APP_NAME, "DisplayVersion": payload.version, "Publisher": installer.PUBLISHER,
                    "InstallLocation": str(target), "UpgradeCode": installer.UPGRADE_CODE,
                    "UninstallString": f'"{executable}" --uninstall'}
        wrong = [k for k, v in expected.items() if registration.get(k) != v]
        results["registration"] = not wrong or f"wrong values: {wrong}"
        links = [system.start_menu_link(), system.desktop_link()]
        targets = [json.loads(link.read_text(encoding="utf-8"))["target"] if link.is_file() else None for link in links]
        results["shortcuts"] = targets == [str(executable)] * 2 or f"shortcut targets {targets}"
        results["quoting"] = registration.get("UninstallString", "").startswith('"') or "uninstall command is not quoted"
        (target / "_internal").exists() and shutil.rmtree(target / "_internal")
        repaired = installer.install(payload, target, system)
        report = installer.verify(target)
        results["repair"] = repaired.kind == "reinstall" and not report["missing"] and not report["changed"] or "repair left damage"
        data = temp / "Roaming" / "BattleTanx Studio"
        data.mkdir(parents=True)
        (data / "preferences.json").write_text("{}", encoding="utf-8")
        result = installer.uninstall(target, system, user_data=(data,))
        results["uninstall"] = (not (target / installer.EXECUTABLE).exists() and system.read_registration() is None
                                and not any(link.exists() for link in links)) or "uninstall left program files or entries"
        results["user_data_preserved"] = data.is_dir() and result.kept == ["notes I added.txt"] or "user data or files were removed"
    return results


def _snapshot(target: Path, system) -> dict:
    files = {p.relative_to(target).as_posix(): installer.sha256(p) for p in target.rglob("*") if p.is_file()} if target.exists() else {}
    links = {p.name: p.read_text(encoding="utf-8") for p in system.root.rglob("*.lnk.json")}
    return {"files": files, "registration": system.read_registration(), "links": links}


def _leftovers(target: Path) -> list[str]:
    return [p.name for p in target.parent.iterdir() if ".staging-" in p.name or ".previous-" in p.name]


def audit(setup: Path | None, dist: Path, forbid: list[str], previous: Path | None = None) -> dict:
    checks: dict[str, bool | str] = {}
    if setup is not None:
        checks["setup_subsystem"] = pe_subsystem(setup) == "windows-gui" or "setup is not a GUI program"
        version = version_strings(setup)
        checks["setup_version"] = (version.get("OriginalFilename") == SETUP_NAME and version.get("CompanyName") == installer.PUBLISHER
                                   and bool(version.get("ProductVersion"))) or f"version resource {version}"
    manifest_path = Path(dist) / installer.PAYLOAD_MANIFEST
    manifest, payload = {}, None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError("manifest is not an object")
        payload = installer.Payload.from_folder(Path(dist), manifest.get("version", ""))
        damaged = [r for r, info in payload.files.items()
                   if not (payload.root / r).is_file() or installer.sha256(payload.root / r) != info["sha256"]]
        checks["payload_manifest"] = (not damaged and manifest.get("upgrade_code") == installer.UPGRADE_CODE
                                      and installer.EXECUTABLE in payload.files) or f"damaged {damaged[:5]}"
    except (OSError, ValueError, TypeError, installer.InstallError) as error:
        checks["payload_manifest"] = f"invalid payload manifest: {error}"
    if previous is not None:
        old = json.loads(Path(previous).read_text(encoding="utf-8"))
        checks["upgrade_code_stable"] = old.get("upgrade_code") == installer.UPGRADE_CODE or "upgrade code changed"
    package = audit_package(Path(dist), forbid)
    checks["payload_clean"] = package["ok"] or "; ".join(package["problems"][:5])
    if checks["payload_manifest"] is True and payload is not None:
        try:
            checks.update(scenarios(payload))
        except (OSError, installer.InstallError, ValueError) as error:
            checks["scenarios"] = f"scenario run failed: {error}"
    else:
        checks["scenarios"] = "not run: the payload is damaged"
    return {"ok": all(v is True for v in checks.values()), "version": manifest.get("version"), "checks": checks}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--setup", type=Path)
    parser.add_argument("--dist", type=Path, required=True)
    parser.add_argument("--previous-manifest", type=Path)
    parser.add_argument("--forbid", action="append", default=[])
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    result = audit(args.setup, args.dist, args.forbid, args.previous_manifest)
    text = json.dumps(result, indent=1, sort_keys=True)
    if args.report:
        args.report.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
