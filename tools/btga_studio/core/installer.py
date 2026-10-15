"""Transactional per-user installation, upgrade, repair and uninstall.

Used by ``BattleTanxStudioSetup.exe`` and by the editor's "Install for this
user" and ``--uninstall`` modes.  Everything that touches the operating system
outside the install folder goes through a ``System`` adapter:

* ``WindowsSystem``: HKCU uninstall entry (``winreg``), Start menu / desktop
  shortcuts (hidden PowerShell with ``CREATE_NO_WINDOW``), running-process
  detection (``psapi`` via ``ctypes``), delayed folder removal.
* ``SandboxSystem``: the same effects recorded as JSON and placeholder files
  beneath a chosen root, for hidden filesystem-level tests on Windows and Linux
  that never touch the real registry or Start menu.
* ``MemorySystem`` (tests): in-memory, with failure injection.

Install sequence (each completed step is journalled and undone in reverse on
any failure, so a failed install or upgrade leaves the previous state):

    check     target writable, not running, not a foreign folder, version policy
    stage     copy payload to ``<target>.staging-*``, verify every SHA-256
    swap      move an existing install to ``<target>.previous-*``, move staging into place
    carry     copy files the user added to the old install folder (not in its record)
    record    write ``install-record.json`` (version, upgrade code, file hashes, options)
    shortcuts Start menu (always), desktop (optional)
    register  uninstall entry (stable registry key; upgrade code never changes)
    commit    delete the previous install

User data (settings, catalogs, caches, logs, projects) lives outside the
install folder and is never touched unless ``uninstall(..., remove_user_data=True)``
names those folders explicitly.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .atomic import atomic_write_text
from .versioning import compare

APP_NAME = "BattleTanx Studio"
PUBLISHER = "BattleTanx Studio contributors"
EXECUTABLE = "BattleTanxStudio.exe"
UPGRADE_CODE = "{6B1F2A3E-4C5D-4E6F-8A9B-0C1D2E3F4A5B}"  # identifies the product across versions; never change it
REGISTRY_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\BattleTanxStudio"
RECORD = "install-record.json"
PAYLOAD_MANIFEST = "payload-manifest.json"
EXCLUDED = {"BattleTanxStudioData", "portable.txt", RECORD}
CREATE_NO_WINDOW = 0x08000000
STEPS = ("check", "stage", "swap", "carry", "record", "shortcuts", "register", "commit")


class InstallError(RuntimeError):
    """A plain-language reason the operation cannot continue; nothing was changed."""


def default_target() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    return base / "Programs" / APP_NAME


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def quote_argument(path: Path | str) -> str:
    """Windows command-line quoting for a single path argument (no shell involved)."""

    return subprocess.list2cmdline([str(path)])


# ------------------------------------------------------------------ payload --

@dataclass
class Payload:
    root: Path
    version: str
    files: dict[str, dict]  # relative POSIX path -> {"sha256", "size"}

    @classmethod
    def from_folder(cls, root: Path, version: str) -> "Payload":
        root = Path(root)
        manifest = root / PAYLOAD_MANIFEST
        if manifest.is_file():
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
                files = data["files"]
                payload_version = data["version"]
            except (OSError, ValueError, KeyError, TypeError) as error:
                raise InstallError(f"The installer payload manifest is invalid: {error}") from error
            if not isinstance(files, dict) or not isinstance(payload_version, str):
                raise InstallError("The installer payload manifest has an invalid shape.")
            checked = {}
            for relative, info in files.items():
                _validate_payload_entry(relative, info)
                checked[relative] = dict(info)
            return cls(root, payload_version, checked)
        files = {}
        for path in sorted(p for p in root.rglob("*") if p.is_file()):
            relative = path.relative_to(root).as_posix()
            if relative.split("/", 1)[0] in EXCLUDED or relative == PAYLOAD_MANIFEST:
                continue
            files[relative] = {"sha256": sha256(path), "size": path.stat().st_size}
        return cls(root, version, files)

    def manifest(self) -> dict:
        return {"product": APP_NAME, "version": self.version, "upgrade_code": UPGRADE_CODE, "files": self.files}


def _validate_payload_entry(relative: object, info: object) -> None:
    """Reject paths that could escape the staging directory and malformed integrity records."""

    if not isinstance(relative, str) or not relative or "\\" in relative or "\0" in relative:
        raise InstallError("The installer payload contains an invalid file path.")
    path = PurePosixPath(relative)
    if path.is_absolute() or any(part in ("", ".", "..") or ":" in part for part in path.parts):
        raise InstallError(f"The installer payload contains an unsafe file path: {relative!r}")
    if not isinstance(info, dict):
        raise InstallError(f"The installer payload entry for {relative!r} is invalid.")
    digest, size = info.get("sha256"), info.get("size")
    if (not isinstance(digest, str) or len(digest) != 64 or
            any(character not in "0123456789abcdef" for character in digest) or
            isinstance(size, bool) or not isinstance(size, int) or size < 0):
        raise InstallError(f"The installer payload integrity record for {relative!r} is invalid.")


# ------------------------------------------------------------------ adapters --

class WindowsSystem:
    """Real Windows effects (per user).  Never used by automated tests."""

    def start_menu_link(self) -> Path:
        return Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / f"{APP_NAME}.lnk"

    def desktop_link(self) -> Path:
        return Path(os.environ.get("USERPROFILE") or Path.home()) / "Desktop" / f"{APP_NAME}.lnk"

    def create_shortcut(self, link: Path, target: Path, working: Path) -> None:
        script = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:BTS_LINK);$s.TargetPath=$env:BTS_TARGET;"
                  "$s.WorkingDirectory=$env:BTS_WORK;$s.Description='BattleTanx Studio';$s.Save()")
        env = dict(os.environ, BTS_LINK=str(link), BTS_TARGET=str(target), BTS_WORK=str(working))
        Path(link).parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", script], env=env, check=True,
                       creationflags=CREATE_NO_WINDOW, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def remove_shortcut(self, link: Path) -> None:
        Path(link).unlink(missing_ok=True)

    def shortcut_exists(self, link: Path) -> bool:
        return Path(link).is_file()

    def read_registration(self) -> dict | None:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY_KEY) as key:
                values, index = {}, 0
                while True:
                    try:
                        name, value, _ = winreg.EnumValue(key, index)
                    except OSError:
                        return values
                    values[name] = value
                    index += 1
        except FileNotFoundError:
            return None

    def write_registration(self, values: dict) -> None:
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, REGISTRY_KEY) as key:
            for name, value in values.items():
                winreg.SetValueEx(key, name, 0, winreg.REG_DWORD if isinstance(value, int) else winreg.REG_SZ, value)

    def remove_registration(self) -> None:
        import winreg

        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, REGISTRY_KEY)
        except FileNotFoundError:
            pass

    def running_processes(self, folder: Path) -> list[int]:
        return _running_from(folder)

    def remove_folder_later(self, folder: Path) -> None:
        script = ("Start-Sleep -Seconds 2; "
                  "Remove-Item -LiteralPath $env:BTS_REMOVE_DIR -Recurse -Force -ErrorAction SilentlyContinue")
        environment = dict(os.environ, BTS_REMOVE_DIR=str(folder))
        subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", script],
                         env=environment, creationflags=CREATE_NO_WINDOW | 0x00000008,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)


class SandboxSystem:
    """Records shortcuts and the uninstall entry as files under ``root`` (no registry, no real Start menu)."""

    def __init__(self, root: Path, running: list[int] | None = None):
        self.root = Path(root)
        self.running = running

    def start_menu_link(self) -> Path:
        return self.root / "Start Menu" / f"{APP_NAME}.lnk.json"

    def desktop_link(self) -> Path:
        return self.root / "Desktop" / f"{APP_NAME}.lnk.json"

    def create_shortcut(self, link: Path, target: Path, working: Path) -> None:
        atomic_write_text(Path(link), json.dumps({"target": str(target), "working": str(working)}, sort_keys=True))

    def remove_shortcut(self, link: Path) -> None:
        Path(link).unlink(missing_ok=True)

    def shortcut_exists(self, link: Path) -> bool:
        return Path(link).is_file()

    @property
    def registry_file(self) -> Path:
        return self.root / "registry" / "BattleTanxStudio.json"

    def read_registration(self) -> dict | None:
        try:
            return json.loads(self.registry_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def write_registration(self, values: dict) -> None:
        atomic_write_text(self.registry_file, json.dumps(values, sort_keys=True, indent=1))

    def remove_registration(self) -> None:
        self.registry_file.unlink(missing_ok=True)

    def running_processes(self, folder: Path) -> list[int]:
        return list(self.running) if self.running is not None else _running_from(folder)

    def remove_folder_later(self, folder: Path) -> None:
        shutil.rmtree(folder, ignore_errors=True)


def _running_from(folder: Path) -> list[int]:
    """Process IDs whose executable lives in ``folder`` (Windows only; [] elsewhere)."""

    if sys.platform != "win32":
        return []
    from ctypes import wintypes

    psapi, kernel32 = ctypes.WinDLL("psapi"), ctypes.WinDLL("kernel32")
    ids = (wintypes.DWORD * 4096)()
    needed = wintypes.DWORD()
    if not psapi.EnumProcesses(ids, ctypes.sizeof(ids), ctypes.byref(needed)):
        return []
    prefix = os.path.normcase(str(Path(folder).resolve())) + os.sep
    found = []
    for pid in ids[: needed.value // ctypes.sizeof(wintypes.DWORD)]:
        if not pid or pid == os.getpid():
            continue
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            continue
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buffer))
            if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                if os.path.normcase(buffer.value).startswith(prefix):
                    found.append(int(pid))
        finally:
            kernel32.CloseHandle(handle)
    return found


# -------------------------------------------------------------------- state --

@dataclass
class Existing:
    target: Path
    record: dict | None
    registration: dict | None

    @property
    def version(self) -> str | None:
        return (self.record or {}).get("version") or (self.registration or {}).get("DisplayVersion")


def inspect(target: Path, system) -> Existing:
    record = None
    try:
        record = json.loads((Path(target) / RECORD).read_text(encoding="utf-8"))
        if record.get("upgrade_code") != UPGRADE_CODE:
            record = None
    except (OSError, ValueError, AttributeError):
        record = None
    return Existing(Path(target), record, system.read_registration())


@dataclass
class Plan:
    kind: str  # fresh | upgrade | reinstall | downgrade
    current: str | None
    new: str
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.blockers


def _writable(folder: Path) -> bool:
    folder = Path(folder)
    probe_dir = folder if folder.is_dir() else next((p for p in folder.parents if p.is_dir()), None)
    if probe_dir is None:
        return False
    probe = probe_dir / f".write-test-{secrets.token_hex(4)}"
    try:
        probe.write_bytes(b"")
        probe.unlink()
        return True
    except OSError:
        return False


def plan(payload: Payload, target: Path, system, allow_downgrade: bool = False) -> Plan:
    target = Path(target)
    existing = inspect(target, system)
    current = existing.version
    if existing.record is None:
        kind = "fresh"
        if target.is_dir() and any(target.iterdir()):
            result = Plan(kind, None, payload.version, ["The chosen folder already contains other files. Choose an empty folder or "
                                                        "a new folder name."])
            return result
    else:
        order = compare(payload.version, current)
        kind = "upgrade" if order > 0 else ("reinstall" if order == 0 else "downgrade")
    result = Plan(kind, current, payload.version)
    if kind == "downgrade":
        message = f"Version {current} is installed, which is newer than {payload.version}."
        (result.warnings if allow_downgrade else result.blockers).append(
            message + (" It will be replaced by the older version." if allow_downgrade else " Choose \"Install older version anyway\" to continue."))
    if not _writable(target.parent if not target.exists() else target):
        result.blockers.append("You do not have permission to write to that folder. Choose a folder inside your user profile.")
    running = system.running_processes(target) if target.exists() else []
    if running:
        result.blockers.append("BattleTanx Studio is running from this folder. Close it, then try again.")
    reg = existing.registration
    if reg and existing.record is None and reg.get("InstallLocation") and Path(reg["InstallLocation"]) != target:
        result.warnings.append("Another copy is registered elsewhere; this installation will replace its Start menu and Apps entry.")
    return result


# ------------------------------------------------------------------ install --

def registration_values(target: Path, version: str, record_size: int) -> dict:
    executable = Path(target) / EXECUTABLE
    return {"DisplayName": APP_NAME, "DisplayVersion": version, "Publisher": PUBLISHER, "InstallLocation": str(target),
            "DisplayIcon": str(executable), "UninstallString": f"{quote_argument(executable)} --uninstall",
            "QuietUninstallString": f"{quote_argument(executable)} --uninstall --quiet", "NoModify": 1, "NoRepair": 1,
            "EstimatedSize": int(record_size // 1024), "UpgradeCode": UPGRADE_CODE,
            "URLInfoAbout": "https://github.com/TheGh0stShip/BattleTanx-Studio",
            "HelpLink": "https://github.com/TheGh0stShip/BattleTanx-Studio/issues"}


def install(payload: Payload, target: Path, system, desktop_shortcut: bool = False, allow_downgrade: bool = False,
            progress=None, fail_at: str | None = None) -> Plan:
    """Install, upgrade or repair transactionally.  Raises InstallError (nothing changed) or the original error after rollback."""

    target = Path(target)
    report = progress or (lambda stage, done, total: None)
    decision = plan(payload, target, system, allow_downgrade)
    if not decision.ok:
        raise InstallError(" ".join(decision.blockers))
    journal: list[tuple] = []
    token = secrets.token_hex(4)
    staging = target.with_name(f"{target.name}.staging-{token}")
    previous = target.with_name(f"{target.name}.previous-{token}")
    old = inspect(target, system)

    def step(name: str) -> None:
        if fail_at == name:
            raise OSError(f"simulated failure during {name}")

    try:
        step("check")
        report("Copying files", 0, len(payload.files))
        staging.mkdir(parents=True)
        journal.append(("rmtree", staging))
        for number, (relative, info) in enumerate(sorted(payload.files.items())):
            source, destination = payload.root / relative, staging / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            if sha256(destination) != info["sha256"]:
                raise InstallError(f"The installer's copy of {relative} is damaged. Download BattleTanx Studio again.")
            report("Copying files", number + 1, len(payload.files))
        step("stage")
        if target.exists():
            os.replace(target, previous)
            journal.append(("restore", previous, target))
        os.replace(staging, target)
        journal = [entry for entry in journal if entry != ("rmtree", staging)] + [("rmtree", target)]
        step("swap")
        old_files = set((old.record or {}).get("files", {}))
        if previous.exists():
            for path in sorted(p for p in previous.rglob("*") if p.is_file()):
                relative = path.relative_to(previous).as_posix()
                if relative == RECORD or relative in old_files or relative in payload.files:
                    continue
                destination = target / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, destination)  # files the user put beside the program survive upgrades
        step("carry")
        record = {**payload.manifest(), "options": {"desktop_shortcut": bool(desktop_shortcut)}, "installed_from_upgrade": old.version}
        atomic_write_text(target / RECORD, json.dumps(record, indent=1, sort_keys=True) + "\n")
        step("record")
        executable = target / EXECUTABLE
        links = [(system.start_menu_link(), True), (system.desktop_link(), bool(desktop_shortcut))]
        for link, wanted in links:
            existed = system.shortcut_exists(link)
            previous_bytes = Path(link).read_bytes() if existed else None
            journal.append(("shortcut", link, previous_bytes))
            if wanted:
                system.create_shortcut(link, executable, target)
            elif existed and old.record is not None:
                system.remove_shortcut(link)
        step("shortcuts")
        journal.append(("registration", old.registration))
        size = sum(info["size"] for info in payload.files.values())
        system.write_registration(registration_values(target, payload.version, size))
        step("register")
        step("commit")
    except BaseException:
        _rollback(journal, system)
        raise
    # Committed: the new install is complete, so the previous copy is no longer needed (never rolled back past this point).
    shutil.rmtree(previous, ignore_errors=True)
    report("Done", 1, 1)
    return decision


def _rollback(journal: list, system) -> None:
    for entry in reversed(journal):
        try:
            if entry[0] == "rmtree":
                shutil.rmtree(entry[1], ignore_errors=True)
            elif entry[0] == "restore":
                if entry[1].exists():
                    os.replace(entry[1], entry[2])
            elif entry[0] == "shortcut":
                _, link, previous_bytes = entry
                if previous_bytes is None:
                    system.remove_shortcut(link)
                else:
                    Path(link).parent.mkdir(parents=True, exist_ok=True)
                    Path(link).write_bytes(previous_bytes)
            elif entry[0] == "registration":
                if entry[1] is None:
                    system.remove_registration()
                else:
                    system.write_registration(entry[1])
        except OSError:
            continue  # best effort: keep undoing the remaining steps


def verify(target: Path) -> dict:
    """Missing and changed program files according to the install record (used by repair guidance)."""

    record = inspect(Path(target), _NullSystem()).record
    if record is None:
        return {"installed": False, "missing": [], "changed": []}
    missing, changed = [], []
    for relative, info in sorted(record["files"].items()):
        path = Path(target) / relative
        if not path.is_file():
            missing.append(relative)
        elif path.stat().st_size != info["size"] or sha256(path) != info["sha256"]:
            changed.append(relative)
    return {"installed": True, "version": record["version"], "missing": missing, "changed": changed}


class _NullSystem:
    def read_registration(self):
        return None


# ---------------------------------------------------------------- uninstall --

PROGRAM_FILES = (EXECUTABLE, "README.txt", "COPYING.txt", "THIRD-PARTY-NOTICES.txt", PAYLOAD_MANIFEST)
PROGRAM_FOLDERS = ("_internal/", "samples/", "licenses/")


def _program_file(relative: str) -> bool:
    """Without an install record only files the installer itself writes are removed; anything else is the user's."""

    return relative in PROGRAM_FILES or relative.startswith(PROGRAM_FOLDERS)


@dataclass
class UninstallResult:
    removed_files: int
    kept: list[str]
    removed_user_data: list[str]
    scheduled_folder_removal: bool


def uninstall(target: Path, system, remove_user_data: bool = False, user_data: tuple = (), running_from_target: bool = False,
              ignore_running: bool = False) -> UninstallResult:
    """Remove the program, its shortcuts and Apps entry.  User data is kept unless ``remove_user_data`` names it."""

    target = Path(target)
    existing = inspect(target, system)
    running = [pid for pid in system.running_processes(target) if pid != os.getpid()] if target.exists() else []
    if running and not ignore_running:
        raise InstallError("BattleTanx Studio is still running. Close every BattleTanx Studio window, then uninstall again.")
    record_files = set((existing.record or {}).get("files", {}))
    system.remove_shortcut(system.start_menu_link())
    system.remove_shortcut(system.desktop_link())
    registration = existing.registration or {}
    if not registration or Path(registration.get("InstallLocation", target)) == target:
        system.remove_registration()
    removed = 0
    kept = []
    if target.is_dir():
        for path in sorted((p for p in target.rglob("*") if p.is_file()), reverse=True):
            relative = path.relative_to(target).as_posix()
            known = relative in record_files or relative == RECORD or (not record_files and _program_file(relative))
            if known:
                if running_from_target and relative == EXECUTABLE:
                    continue  # the running uninstaller; removed with the folder after exit
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    kept.append(relative)
            else:
                kept.append(relative)
        for folder in sorted((p for p in target.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
            try:
                folder.rmdir()
            except OSError:
                pass
    scheduled = False
    if running_from_target and target.exists() and not kept:
        system.remove_folder_later(target)
        scheduled = True
    elif target.exists() and not any(target.iterdir()):
        target.rmdir()
    removed_data = []
    if remove_user_data:
        for folder in user_data:
            folder = Path(folder)
            if folder.is_dir() and folder.name in ("BattleTanx Studio", "battletanx-studio"):
                shutil.rmtree(folder, ignore_errors=True)
                removed_data.append(folder.name)
    return UninstallResult(removed, kept, removed_data, scheduled)
