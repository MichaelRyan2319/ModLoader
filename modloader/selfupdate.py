"""Keeping Mod Loader itself up to date from its own GitHub releases.

Each release has two files: ModLoader.exe (the packaged program) and
ModLoader-source.zip (for people who run it with Python).

The .exe swaps itself for the new one while it runs: Windows lets a running
program's file be renamed, so the old one becomes ModLoader.exe.old (deleted on
the next start) and the new one takes its name. The new version starts the
next time Mod Loader opens. Run from source, the new files are copied over the
program folder, unless it's a git checkout (then `git pull` is the way).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import APP_REPO, __version__, system
from .github import Asset, GitHubError, select_assets
from .installer import InstallError, safe_path, stage_files

EXE_ASSET = "ModLoader.exe"
SOURCE_ASSET = "ModLoader-source.zip"


@dataclass
class AppUpdate:
    version: str
    tag: str
    page: str  # the release on github.com
    asset: Asset


def parse_version(text: str) -> tuple[int, ...] | None:
    match = re.fullmatch(r"[vV]?(\d+(?:\.\d+)*)", (text or "").strip())
    return tuple(int(part) for part in match.group(1).split(".")) if match else None


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def blocked_reason(root: Path = system.ROOT) -> str:
    """Why this copy can't update itself, or '' if it can."""
    if not APP_REPO:
        return "No update repository is set."
    if not is_frozen() and (root / ".git").exists():
        return "This copy runs from a git checkout, so it's updated with git pull instead."
    return ""


def find_update(client, current: str = __version__, repo: str = APP_REPO, frozen: bool | None = None
                ) -> AppUpdate | None:
    """The newer release to install, or None if this is the latest."""
    release = client.latest_release(repo)
    tag = release.get("tag_name") or ""
    new, old = parse_version(tag), parse_version(current)
    if new is None or old is None or new <= old:
        return None
    name = EXE_ASSET if (is_frozen() if frozen is None else frozen) else SOURCE_ASSET
    try:
        asset = select_assets(release, name)[0]
    except GitHubError:
        raise GitHubError(f"Mod Loader {tag} is out, but its release doesn't have {name} yet. "
                          "Try again in a few minutes.") from None
    page = release.get("html_url") or f"https://github.com/{repo}/releases"
    return AppUpdate(tag.lstrip("vV"), tag, page, asset)


def install(client, update: AppUpdate, work_dir: Path, progress: Callable[[int, int], None] | None = None,
            exe: Path | None = None, root: Path | None = None) -> None:
    """Put the new version in place. It runs from the next start."""
    work_dir = Path(work_dir)
    shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True)
    try:
        if exe is not None or (root is None and is_frozen()):
            _swap_exe(client, update.asset, Path(exe or sys.executable), progress)
        else:
            _unpack_source(client, update.asset, Path(root or system.ROOT), work_dir, progress)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _check_writable(folder: Path) -> None:
    probe = folder / f".modloader-write-test-{os.getpid()}"
    try:
        probe.write_bytes(b"")
        probe.unlink()
    except OSError:
        raise InstallError(f"Mod Loader can't update itself because it isn't allowed to change files in "
                           f"{folder}. Move Mod Loader to a folder you own (such as Documents), or download "
                           "the new version from its releases page.") from None


def _swap_exe(client, asset: Asset, exe: Path, progress) -> None:
    _check_writable(exe.parent)
    new = exe.with_name(exe.name + ".new")
    client.download(asset, new, progress)  # checks the size and GitHub's checksum
    old = exe.with_name(exe.name + ".old")
    try:
        old.unlink(missing_ok=True)
    except OSError:  # an older copy is still running from it
        old = exe.with_name(f"{exe.name}.{int(time.time())}.old")
    try:
        os.replace(exe, old)  # allowed even while the program is running
    except OSError as e:
        new.unlink(missing_ok=True)
        raise InstallError(f"Couldn't replace {exe.name} ({e.strerror or e}).") from None
    try:
        os.replace(new, exe)
    except OSError:
        os.replace(old, exe)
        raise


def _unpack_source(client, asset: Asset, root: Path, work_dir: Path, progress) -> None:
    archive = client.download(asset, work_dir / SOURCE_ASSET, progress)
    files = stage_files([(asset, archive)], work_dir / "files", work_dir / "unpack",
                        extract=True, strip_root=True, folder_name="ModLoader")
    if "ModLoader.pyw" not in files or "modloader/__init__.py" not in files:
        raise InstallError("The download doesn't look like Mod Loader, so it wasn't installed.")
    _check_writable(root)
    package = root / "modloader"
    stale = [p for p in package.glob("*.py") if f"modloader/{p.name}" not in files] if package.is_dir() else []
    for rel in files:
        dest = safe_path(root, rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(work_dir / "files" / rel, dest)
    for path in stale:
        path.unlink(missing_ok=True)


def clean_up_old_exe() -> None:
    """Delete the copy left behind by the last update."""
    if not is_frozen():
        return
    exe = Path(sys.executable)
    for old in exe.parent.glob(exe.name + "*.old"):
        try:
            old.unlink()
        except OSError:
            pass  # still running somewhere; next time


def restart() -> None:
    """Start a fresh Mod Loader (the caller then closes itself)."""
    exe, args = system.self_command()
    # A packaged exe started from another copy of itself would otherwise reuse that copy's
    # unpacked files, which are deleted when it closes.
    env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1")
    cwd = str(Path(exe).parent if is_frozen() else system.ROOT)
    subprocess.Popen([exe, *args], env=env, cwd=cwd, close_fds=True)
