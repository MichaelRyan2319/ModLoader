"""Putting mod files into a game's folder, and taking them out again.

Every file a mod installs is recorded (relative to the game's mods folder), so
an update can delete files the new version dropped and removing a mod leaves
nothing behind. If a mod overwrites a file it didn't create, the original is
backed up and put back when the mod is removed or disabled.

Downloads are unpacked into a staging folder first, so a bad download never
touches the game folder.
"""

from __future__ import annotations

import os
import shutil
import stat
import tarfile
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterable

from .util import safe_filename

if TYPE_CHECKING:
    from .config import Game, Mod, Store
    from .github import Asset

Log = Callable[..., None]

TAR_SUFFIXES = (".tar", ".tar.gz", ".tgz", ".tar.xz", ".txz", ".tar.bz2", ".tbz2")
JUNK_DIRS = {"__MACOSX"}
JUNK_FILES = {".DS_Store", "Thumbs.db", "desktop.ini"}


class InstallError(Exception):
    """A problem installing files, with a message fit to show the user."""


# -- Paths ---------------------------------------------------------------------

def clean_subdir(text: str) -> str:
    """Validate the optional 'install into' folder. Returns it as a/b, or ''."""
    s = (text or "").strip().replace("\\", "/").strip("/")
    if not s:
        return ""
    parts = [p for p in s.split("/") if p not in ("", ".")]
    if ":" in s or ".." in parts:
        raise InstallError('“Install into” must be a folder inside the mods folder, like "plugins" or "mods/extra".')
    return "/".join(parts)


def safe_path(base: Path | str, rel: str) -> Path:
    """Join base and rel, refusing anything that would land outside base."""
    base_abs = os.path.abspath(base)
    target = os.path.abspath(os.path.join(base_abs, rel))
    try:
        inside = os.path.commonpath([base_abs, target]) == base_abs
    except ValueError:  # different drives
        inside = False
    if not inside or target == base_abs:
        raise InstallError(f"Refusing to use a path outside the mods folder: {rel}")
    return Path(target)


def _parts(name: str) -> list[str]:
    return [p for p in name.replace("\\", "/").split("/") if p not in ("", ".")]


def _make_writable(path: Path) -> None:
    try:
        if path.exists() and not os.access(path, os.W_OK):
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    except OSError:
        pass


def _move_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    _make_writable(dst)
    try:
        os.replace(src, dst)
    except OSError:  # e.g. across drives
        shutil.copy2(src, dst)
        os.unlink(src)


def _copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    _make_writable(dst)
    shutil.copy2(src, dst)


def _remove_file(base: Path, rel: str) -> bool:
    path = safe_path(base, rel)
    if path.is_file() or path.is_symlink():
        _make_writable(path)
        path.unlink()
        return True
    return False


def _prune_dirs(base: Path, rels: Iterable[str]) -> None:
    """Delete folders that became empty, never going above base."""
    base_abs = Path(os.path.abspath(base))
    for rel in rels:
        try:
            folder = safe_path(base_abs, rel).parent
        except InstallError:
            continue
        while folder != base_abs and base_abs in folder.parents:
            try:
                folder.rmdir()
            except OSError:
                break  # not empty, or already gone
            folder = folder.parent


# -- Unpacking -----------------------------------------------------------------

def archive_kind(name: str) -> str | None:
    """Only real archives are unpacked; .jar, .pk3, .pak etc. are zips but are installed as-is."""
    lower = name.lower()
    if lower.endswith(".zip"):
        return "zip"
    if lower.endswith(TAR_SUFFIXES):
        return "tar"
    return None


def _wanted(parts: list[str]) -> bool:
    return bool(parts) and parts[0] not in JUNK_DIRS and parts[-1] not in JUNK_FILES


def _extract(archive: Path, kind: str, dest: Path, label: str) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    if kind == "zip":
        try:
            zf = zipfile.ZipFile(archive)
        except zipfile.BadZipFile:
            raise InstallError(f"{label} isn't a valid .zip file.") from None
        with zf:
            for info in zf.infolist():
                parts = _parts(info.filename)
                if info.is_dir() or not _wanted(parts):
                    continue
                target = safe_path(dest, "/".join(parts))
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
    else:
        try:
            tf = tarfile.open(archive)
        except tarfile.TarError:
            raise InstallError(f"{label} isn't a valid archive.") from None
        with tf:
            for member in tf.getmembers():
                parts = _parts(member.name)
                if not member.isfile() or not _wanted(parts):
                    continue  # skips folders, links and device files
                target = safe_path(dest, "/".join(parts))
                target.parent.mkdir(parents=True, exist_ok=True)
                src = tf.extractfile(member)
                if src is None:
                    continue
                with src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)


def stage_files(
    downloads: list[tuple["Asset", Path]],
    out_dir: Path,
    work_dir: Path,
    extract: bool,
    strip_root: bool,
    folder_name: str,
) -> list[str]:
    """Lay out everything that will be installed in out_dir; returns relative file paths."""
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True)
    for asset, path in downloads:
        kind = archive_kind(asset.name) if extract else None
        if kind is None:
            _move_file(path, out_dir / safe_filename(asset.name))
            continue
        shutil.rmtree(work_dir, ignore_errors=True)
        _extract(path, kind, work_dir, asset.name)
        root = work_dir
        entries = list(work_dir.iterdir())
        single_folder = len(entries) == 1 and entries[0].is_dir()
        if single_folder and strip_root:
            root = entries[0]
        elif single_folder and asset.is_source:
            # GitHub names it owner-repo-commit; use the repo name instead.
            renamed = work_dir / safe_filename(folder_name)
            if entries[0] != renamed:
                entries[0].rename(renamed)
        for file in sorted(p for p in root.rglob("*") if p.is_file()):
            _move_file(file, out_dir / file.relative_to(root))
        shutil.rmtree(work_dir, ignore_errors=True)
    files = sorted(p.relative_to(out_dir).as_posix() for p in out_dir.rglob("*") if p.is_file())
    if not files:
        raise InstallError("The download didn't contain any files to install.")
    return files


# -- Installing into the game folder ------------------------------------------

def _owned_by_others(store: "Store", mod: "Mod") -> dict[str, str]:
    """Files other enabled mods of the same game have installed: {lowercase path: mod name}."""
    owners = {}
    for other in store.mods_for(mod.game_id):
        if other.id != mod.id and other.enabled:
            for rel in other.installed_files:
                owners.setdefault(rel.lower(), other.display_name)
    return owners


def _back_up(store: "Store", mod: "Mod", mods_dir: Path, rel: str, log: Log) -> None:
    dest = safe_path(mods_dir, rel)
    if dest.is_dir():
        raise InstallError(f"Can't install {rel}: a folder with that name is already there.")
    _copy_file(dest, safe_path(store.mod_dir("backups", mod.id), rel))
    mod.backups.append(rel)
    log(f"  Backed up the existing {rel} (it comes back if you remove this mod)", "dim")


def restore_backups(store: "Store", game: "Game", mod: "Mod", log: Log, only: Iterable[str] | None = None) -> None:
    mods_dir = Path(game.mods_dir)
    backup_dir = store.mod_dir("backups", mod.id)
    others = _owned_by_others(store, mod)
    for rel in list(only if only is not None else mod.backups):
        if rel not in mod.backups:
            continue
        saved = safe_path(backup_dir, rel)
        if saved.is_file() and rel.lower() not in others:
            _copy_file(saved, safe_path(mods_dir, rel))
            log(f"  Restored the original {rel}", "dim")
        saved.unlink(missing_ok=True)
        mod.backups.remove(rel)
    if not mod.backups:
        shutil.rmtree(backup_dir, ignore_errors=True)


def apply_install(
    store: "Store", game: "Game", mod: "Mod", staged_dir: Path, staged: list[str], subdir: str, log: Log
) -> None:
    """Make the game folder hold exactly the staged files for this mod."""
    mods_dir = Path(game.mods_dir)
    if not mods_dir.is_dir():
        if mods_dir.exists():
            raise InstallError(f"{mods_dir} is a file, not a folder.")
        mods_dir.mkdir(parents=True)
        log(f"  Created the folder {mods_dir}", "dim")

    prefix = f"{subdir}/" if subdir else ""
    new = {prefix + rel: staged_dir / rel for rel in staged}
    old = set(mod.installed_files)
    others = _owned_by_others(store, mod)
    removed: list[str] = []
    written: list[str] = []
    try:
        # Files the new version no longer has.
        for rel in sorted(old - new.keys()):
            if rel.lower() not in others:
                _remove_file(mods_dir, rel)
            removed.append(rel)
            if rel in mod.backups:
                restore_backups(store, game, mod, log, only=[rel])
        # Files of the new version.
        for rel, src in sorted(new.items()):
            dest = safe_path(mods_dir, rel)
            if rel not in old and dest.exists():
                owner = others.get(rel.lower())
                if owner:
                    log(f"  Note: {rel} also comes from {owner}; this mod's copy is used.", "warn")
                elif rel not in mod.backups:
                    _back_up(store, mod, mods_dir, rel, log)
            _copy_file(src, dest)
            written.append(rel)
    except BaseException:
        # Remember what's on disk so a later update or removal can clean it up.
        mod.installed_files = sorted((old - set(removed)) | set(written))
        raise
    finally:
        _prune_dirs(mods_dir, removed)
    mod.installed_files = sorted(new)


def uninstall_files(store: "Store", game: "Game", mod: "Mod", log: Log) -> None:
    mods_dir = Path(game.mods_dir)
    if mod.enabled and mods_dir.is_dir():
        others = _owned_by_others(store, mod)
        for rel in mod.installed_files:
            if rel.lower() not in others:
                _remove_file(mods_dir, rel)
        _prune_dirs(mods_dir, mod.installed_files)
        restore_backups(store, game, mod, log)
    shutil.rmtree(store.mod_dir("disabled", mod.id), ignore_errors=True)
    shutil.rmtree(store.mod_dir("backups", mod.id), ignore_errors=True)
    mod.installed_files = []
    mod.backups = []
    mod.installed_release_id = None
    mod.installed_tag = mod.installed_asset = mod.installed_at = ""


def disable_mod(store: "Store", game: "Game", mod: "Mod", log: Log) -> None:
    """Move the mod's files out of the game folder (kept for re-enabling)."""
    mods_dir = Path(game.mods_dir)
    parked = store.mod_dir("disabled", mod.id)
    others = _owned_by_others(store, mod)
    for rel in mod.installed_files:
        src = safe_path(mods_dir, rel)
        if not src.is_file():
            continue
        if rel.lower() in others:
            _copy_file(src, safe_path(parked, rel))  # another mod still needs it here
        else:
            _move_file(src, safe_path(parked, rel))
    _prune_dirs(mods_dir, mod.installed_files)
    restore_backups(store, game, mod, log)


def enable_mod(store: "Store", game: "Game", mod: "Mod", log: Log) -> None:
    """Put a disabled mod's files back into the game folder."""
    mods_dir = Path(game.mods_dir)
    mods_dir.mkdir(parents=True, exist_ok=True)
    parked = store.mod_dir("disabled", mod.id)
    others = _owned_by_others(store, mod)
    for rel in mod.installed_files:
        src = safe_path(parked, rel)
        if not src.is_file():
            continue
        dest = safe_path(mods_dir, rel)
        if dest.exists():
            owner = others.get(rel.lower())
            if owner:
                log(f"  Note: {rel} also comes from {owner}; this mod's copy is used.", "warn")
            elif rel not in mod.backups:
                _back_up(store, mod, mods_dir, rel, log)
        _move_file(src, dest)
    shutil.rmtree(parked, ignore_errors=True)
