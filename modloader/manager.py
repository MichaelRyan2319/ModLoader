"""Ties GitHub and the installer together: check, install, update, enable, remove."""

from __future__ import annotations

import shutil
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Iterable

from . import system
from .config import Game, Mod, Store
from .github import GitHubClient, GitHubError, RateLimitError, select_assets
from .installer import (InstallError, apply_install, clean_subdir, disable_mod, enable_mod,
                        stage_files, uninstall_files)
from .util import human_size, now_iso, safe_filename


@contextmanager
def file_errors():
    """Turn low-level file errors into messages a player can act on."""
    try:
        yield
    except PermissionError as e:
        what = Path(e.filename).name if e.filename else "a file"
        raise InstallError(f"Couldn't change {what}: it's in use or read-only. "
                           "Close the game and try again.") from None
    except OSError as e:
        raise InstallError(f"File error: {e}") from None


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


@dataclass
class Summary:
    checked: int = 0
    updated: list[str] = field(default_factory=list)
    available: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)

    def text(self) -> str:
        if not self.checked and not self.failed:
            return "No mods to check."
        parts = []
        if self.updated:
            parts.append(f"{len(self.updated)} updated")
        if self.available:
            parts.append(f"{_plural(len(self.available), 'update')} waiting")
        if self.failed:
            parts.append(_plural(len(self.failed), "problem"))
        detail = ", ".join(parts) if parts else "everything is up to date"
        return f"Checked {_plural(self.checked, 'mod')}: {detail}."


class ModManager:
    def __init__(self, store: Store, log: Callable[..., None] | None = None,
                 progress: Callable[[int, int], None] | None = None):
        self.store = store
        self.log = log or (lambda message, level="info": print(message))
        self.progress = progress or (lambda done, total: None)
        self.busy_mod_id: str | None = None
        self._client: GitHubClient | None = None
        self._client_token: str | None = None

    @property
    def client(self) -> GitHubClient:
        token = self.store.settings.github_token
        if self._client is None or token != self._client_token:
            self._client = GitHubClient(token, self.store.cache_path)
            self._client_token = token
        return self._client

    def _busy(self, mod: Mod | None) -> None:
        self.busy_mod_id = mod.id if mod else None
        self.store.notify()

    def _fail(self, mod: Mod, error: Exception) -> Exception:
        with self.store.lock:
            mod.last_error = str(error)
            self.store.save()
        return error

    # -- Checking -------------------------------------------------------------

    def check(self, mod: Mod) -> dict:
        """Ask GitHub for the mod's latest release and remember it."""
        try:
            release = self.client.latest_release(mod.repo, mod.include_prereleases)
        except GitHubError as e:
            with self.store.lock:
                mod.last_checked = now_iso()
            raise self._fail(mod, e) from None
        with self.store.lock:
            mod.latest_release_id = release.get("id")
            mod.latest_tag = release.get("tag_name") or ""
            mod.last_checked = now_iso()
            mod.last_error = ""
            self.store.save()
        return release

    def check_many(self, mods: Iterable[Mod], install: bool = False, auto_only: bool = True) -> Summary:
        """Check mods and (optionally) install what's new.

        auto_only: only install mods that have "Update automatically" ticked.
        """
        summary = Summary()
        for mod in list(mods):
            if self.store.mod(mod.id) is None:
                continue  # removed while we were busy
            self._busy(mod)
            summary.checked += 1
            try:
                try:
                    release = self.check(mod)
                except GitHubError as e:
                    summary.failed.append((mod.display_name, str(e)))
                    self.log(f"{mod.display_name}: {e}", "error")
                    if isinstance(e, RateLimitError):
                        break
                    continue
                if not mod.enabled or mod.installed_release_id == release.get("id"):
                    continue
                if install and (mod.auto_update or not auto_only):
                    game = self.store.game(mod.game_id)
                    if game is None:
                        continue
                    try:
                        self._install_release(game, mod, release)
                        summary.updated.append(f"{mod.display_name} {mod.installed_tag}")
                    except (GitHubError, InstallError) as e:
                        summary.failed.append((mod.display_name, str(e)))
                        self.log(f"  {e}", "error")
                elif mod.installed:
                    summary.available.append(mod.display_name)
                    self.log(f"Update available: {mod.display_name} {mod.installed_tag} → {mod.latest_tag}", "update")
                else:
                    summary.available.append(mod.display_name)
                    self.log(f"{mod.display_name} isn't installed yet (latest is {mod.latest_tag}).", "update")
            finally:
                self._busy(None)
        self.log(summary.text(), "error" if summary.failed else "ok")
        remaining = self.client.rate_remaining
        if remaining is not None and remaining < 10 and not self.store.settings.github_token:
            self.log(f"Only {remaining} GitHub checks left this hour. A token in Settings raises the limit.", "warn")
        return summary

    # -- Installing -----------------------------------------------------------

    def install(self, mod: Mod, force: bool = False) -> bool:
        """Install the latest release (or reinstall it with force). Returns True if files changed."""
        game = self.store.game(mod.game_id)
        if game is None:
            raise InstallError(f"{mod.display_name} belongs to a game that was removed.")
        if not mod.enabled:
            self.log(f"{mod.display_name} is disabled; enable it to install updates.", "warn")
            return False
        self._busy(mod)
        try:
            release = self.check(mod)
            if not force and mod.installed_release_id == release.get("id"):
                self.log(f"{mod.display_name} is up to date ({mod.installed_tag}).", "ok")
                return False
            self._install_release(game, mod, release)
            return True
        finally:
            self._busy(None)

    def _install_release(self, game: Game, mod: Mod, release: dict) -> None:
        tag = release.get("tag_name") or "?"
        if mod.installed and mod.installed_release_id != release.get("id"):
            self.log(f"Updating {mod.display_name} {mod.installed_tag} → {tag}…")
        else:
            self.log(f"Installing {mod.display_name} {tag}…")
        staging = self.store.mod_dir("staging", mod.id)
        try:
            with file_errors():
                assets = select_assets(release, mod.asset_pattern)
                subdir = clean_subdir(mod.subdir)
                shutil.rmtree(staging, ignore_errors=True)
                download_dir = staging / "download"
                download_dir.mkdir(parents=True)
                downloads = []
                for asset in assets:
                    self.log(f"  Downloading {asset.name} ({human_size(asset.size)})", "dim")
                    path = self.client.download(asset, download_dir / safe_filename(asset.name), self.progress)
                    downloads.append((asset, path))
                files = stage_files(downloads, staging / "files", staging / "unpack",
                                    mod.extract, mod.strip_root, mod.repo.split("/")[-1])
                with self.store.lock:
                    try:
                        apply_install(self.store, game, mod, staging / "files", files, subdir, self.log)
                    finally:
                        self.store.save()  # the file list is updated even if this failed part-way
                    mod.installed_release_id = release.get("id")
                    mod.installed_tag = tag
                    mod.installed_asset = ", ".join(a.name for a in assets)
                    mod.installed_at = now_iso()
                    mod.last_error = ""
                    self.store.save()
        except (GitHubError, InstallError) as e:
            raise self._fail(mod, e) from None
        finally:
            shutil.rmtree(staging, ignore_errors=True)
            self.progress(0, 0)
        target = Path(game.mods_dir, *subdir.split("/")) if subdir else Path(game.mods_dir)
        self.log(f"  {mod.display_name} {tag}: {_plural(len(files), 'file')} in {target}", "ok")

    # -- Enable, disable, remove ----------------------------------------------

    def set_enabled(self, mod: Mod, enabled: bool) -> None:
        if mod.enabled == enabled:
            return
        game = self.store.game(mod.game_id)
        with self.store.lock, file_errors():
            if game and mod.installed_files:
                (enable_mod if enabled else disable_mod)(self.store, game, mod, self.log)
            mod.enabled = enabled
            self.store.save()
        self.log(f"{'Enabled' if enabled else 'Disabled'} {mod.display_name}.", "ok")
        if enabled and not mod.installed:
            self.install(mod)

    def remove_mod(self, mod: Mod, delete_files: bool = True) -> None:
        game = self.store.game(mod.game_id)
        with self.store.lock, file_errors():
            if delete_files and game:
                uninstall_files(self.store, game, mod, self.log)
            self.store.mods = [m for m in self.store.mods if m.id != mod.id]
            for kind in ("backups", "disabled", "staging"):
                shutil.rmtree(self.store.mod_dir(kind, mod.id), ignore_errors=True)
            self.store.save()
        self.log(f"Removed {mod.display_name}{'' if delete_files else ' (its files were left in place)'}.", "ok")

    def remove_game(self, game: Game, delete_files: bool = True) -> None:
        for mod in self.store.mods_for(game.id):
            self.remove_mod(mod, delete_files)
        with self.store.lock:
            self.store.games = [g for g in self.store.games if g.id != game.id]
            self.store.save()
        self.log(f"Removed {game.name}.", "ok")

    def move_game_folder(self, game: Game, new_dir: str) -> None:
        """Take installed mods out of the old folder and install them into the new one."""
        old = replace(game)
        with self.store.lock, file_errors():
            for mod in self.store.mods_for(game.id):
                if mod.enabled and mod.installed_files:
                    uninstall_files(self.store, old, mod, self.log)
            game.mods_dir = new_dir
            self.store.save()
        self.log(f"{game.name} now uses {new_dir}. Reinstalling its mods…")
        for mod in self.store.mods_for(game.id):
            if mod.enabled:
                try:
                    self.install(mod, force=True)
                except (GitHubError, InstallError) as e:
                    self.log(f"{mod.display_name}: {e}", "error")

    # -- Playing --------------------------------------------------------------

    def update_for_play(self, game: Game) -> Summary:
        self.log(f"Checking {game.name}'s mods for updates…")
        return self.check_many(self.store.mods_for(game.id), install=True, auto_only=True)

    def launch(self, game: Game) -> None:
        with file_errors():
            system.launch(game.launch_path, game.launch_args)
        self.log(f"Started {game.name}.", "ok")
