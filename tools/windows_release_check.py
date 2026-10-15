"""Hidden Windows filesystem-level checks of a built release (developer use; never touches the registry or Start menu).

    python -m tools.windows_release_check --release RELEASE_DIR --dist PROGRAM_DIR --work EMPTY_TEMP_DIR --report OUT.json

Every install below goes through ``BattleTanxStudioSetup.exe --silent --sandbox``,
which records shortcuts and the uninstall entry as files under the work folder.
Processes are started hidden, waited for, and any helper process this script
starts is terminated before it returns.  Windows only.

Checks: setup self-check; portable self-check; fresh install into a path with
spaces, Greek, Cyrillic and Japanese; installed self-check (bundled runtime, no
Python needed by the program); reinstall/repair after deleting a program file;
upgrade from a synthetic older version; downgrade refusal; read-only target
refusal (ACL deny on a temporary folder); running-program detection for install
and uninstall; rollback at every step on the real filesystem; uninstall with
missing files keeping user files and user data; nothing left behind but user content.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.btga_studio.core import installer  # noqa: E402

CREATE_NO_WINDOW = 0x08000000


def run(arguments: list, timeout: int = 600) -> int:
    return subprocess.run([str(a) for a in arguments], creationflags=CREATE_NO_WINDOW, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, timeout=timeout).returncode


def check(release: Path, dist: Path, work: Path) -> dict:
    results: dict = {}
    setup = release / "BattleTanxStudioSetup.exe"
    sandbox = work / "sandbox"
    target = work / "Program Files ü" / "BattleTanx Studio Ελλάδα Кириллица 日本語"
    report = work / "report.json"

    def silent(*extra, where=target) -> dict:
        report.unlink(missing_ok=True)
        code = run([setup, "--silent", "--sandbox", sandbox, "--target", where, "--report", report, *extra])
        data = json.loads(report.read_text(encoding="utf-8")) if report.is_file() else {}
        data["exit"] = code
        return data

    results["setup_self_check"] = run([setup, "--self-check", work / "setup-check.json"]) == 0
    results["portable_self_check"] = run([dist / installer.EXECUTABLE, "--self-check", work / "portable-check.json"]) == 0
    fresh = silent("--desktop")
    system = installer.SandboxSystem(sandbox, running=[])
    registration = system.read_registration() or {}
    results["fresh_install"] = fresh.get("kind") == "fresh" and (target / installer.EXECUTABLE).is_file() \
        and registration.get("UninstallString") == f'"{target / installer.EXECUTABLE}" --uninstall' and system.desktop_link().is_file()
    results["installed_self_check"] = run([target / installer.EXECUTABLE, "--self-check", work / "installed-check.json"]) == 0
    victim = next(p for p in sorted((target / "_internal").rglob("*.pyd")))
    victim.unlink()
    repair = silent()
    results["repair"] = repair.get("kind") == "reinstall" and victim.is_file() and not installer.verify(target)["missing"]
    record = json.loads((target / installer.RECORD).read_text(encoding="utf-8"))
    newer = dict(record, version="99.0.0")
    (target / installer.RECORD).write_text(json.dumps(newer), encoding="utf-8")
    refused = silent()
    results["downgrade_refused"] = refused.get("exit") == 1 and "newer" in refused.get("refused", "")
    record["version"] = "0.0.1"
    (target / installer.RECORD).write_text(json.dumps(record), encoding="utf-8")
    (target / "my notes ü.txt").write_text("keep", encoding="utf-8")
    upgrade = silent()
    results["upgrade"] = upgrade.get("kind") == "upgrade" and (target / "my notes ü.txt").is_file()

    locked = work / "locked folder"
    locked.mkdir()
    user = os.environ.get("USERNAME", "")
    run(["icacls", locked, "/deny", f"{user}:(OI)(CI)(W,WD,AD)"])
    try:
        denied = silent(where=locked / "BattleTanx Studio")
        results["read_only_target_refused"] = denied.get("exit") == 1 and "permission" in denied.get("refused", "")
    finally:
        run(["icacls", locked, "/remove:d", user])

    helper = target / "running-check.exe"
    shutil.copyfile(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "PING.EXE", helper)
    process = subprocess.Popen([str(helper), "-n", "30", "127.0.0.1"], creationflags=CREATE_NO_WINDOW,
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(1.0)
        detected = installer._running_from(target)
        busy = silent()
        results["running_detected_install"] = process.pid in detected and busy.get("exit") == 1 and "running" in busy.get("refused", "")
        try:
            installer.uninstall(target, installer.SandboxSystem(sandbox))
            results["running_detected_uninstall"] = False
        except installer.InstallError:
            results["running_detected_uninstall"] = True
    finally:
        process.kill()
        process.wait(30)
        results["helper_pid"] = process.pid
    helper.unlink(missing_ok=True)

    from tools.audit_studio_installer import scenarios

    rollback = scenarios(installer.Payload.from_folder(dist, "0"))
    results["rollback_real_filesystem"] = all(v is True for v in rollback.values()) or rollback

    (target / "_internal" / "data" / "build.json").unlink()  # uninstall with missing files
    user_data = work / "Roaming" / "BattleTanx Studio"
    user_data.mkdir(parents=True)
    (user_data / "preferences.json").write_text("{}", encoding="utf-8")
    result = installer.uninstall(target, installer.SandboxSystem(sandbox, running=[]), user_data=(user_data,))
    leftovers = sorted(p.relative_to(target).as_posix() for p in target.rglob("*")) if target.exists() else []
    results["uninstall"] = (result.kept == ["my notes ü.txt"] and leftovers == ["my notes ü.txt"] and user_data.is_dir()
                            and system.read_registration() is None and not system.start_menu_link().exists()
                            and not system.desktop_link().exists())
    results["ok"] = all(v is True for k, v in results.items() if k not in ("helper_pid", "ok"))
    return results


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--dist", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        raise SystemExit("Windows only")
    args.work.mkdir(parents=True, exist_ok=True)
    if any(args.work.iterdir()):
        raise SystemExit("the work folder must be empty")
    results = check(args.release, args.dist, args.work)
    args.report.write_text(json.dumps(results, indent=1, sort_keys=True, default=str) + "\n", encoding="utf-8")
    return 0 if results["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
