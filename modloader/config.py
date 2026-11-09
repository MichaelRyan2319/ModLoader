"""Saved data: settings, games and mods, stored as one JSON file."""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Callable

APP_NAME = "ModLoader"
CONFIG_VERSION = 1


def default_data_dir() -> Path:
    override = os.environ.get("MODLOADER_DATA")
    if override:
        return Path(override)
    base = os.environ.get("APPDATA") or str(Path.home() / ".config")
    return Path(base) / APP_NAME


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def _from_dict(cls, data: dict):
    """Build a dataclass from saved data, ignoring keys from other versions."""
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Settings:
    github_token: str = ""
    check_on_startup: bool = True
    auto_install_updates: bool = True
    check_interval_minutes: int = 60
    update_app_automatically: bool = True  # Mod Loader's own updates


@dataclass
class Game:
    name: str
    mods_dir: str
    launch_path: str = ""
    launch_args: str = ""
    install_dir: str = ""  # where the game itself is installed (used to detect the mods folder)
    id: str = field(default_factory=new_id)


@dataclass
class Mod:
    game_id: str
    repo: str  # "owner/name"
    name: str = ""
    asset_pattern: str = ""  # which release file(s); blank = automatic
    subdir: str = ""  # install into this folder inside the game's mods folder
    include_prereleases: bool = False
    auto_update: bool = True
    extract: bool = True
    strip_root: bool = False
    enabled: bool = True
    id: str = field(default_factory=new_id)

    # What is installed right now.
    installed_release_id: int | None = None
    installed_tag: str = ""
    installed_asset: str = ""
    installed_at: str = ""
    installed_files: list[str] = field(default_factory=list)  # relative to mods_dir
    backups: list[str] = field(default_factory=list)  # files this mod replaced

    # What GitHub said last time we asked.
    latest_release_id: int | None = None
    latest_tag: str = ""
    last_checked: str = ""
    last_error: str = ""

    @property
    def display_name(self) -> str:
        return self.name or self.repo.split("/")[-1]

    @property
    def installed(self) -> bool:
        return self.installed_release_id is not None

    @property
    def update_available(self) -> bool:
        return (
            self.installed
            and self.latest_release_id is not None
            and self.latest_release_id != self.installed_release_id
        )


class Store:
    """Holds all saved data. Use `lock` around changes made from worker threads."""

    def __init__(self, data_dir: Path | str | None = None):
        self.data_dir = Path(data_dir) if data_dir else default_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.settings = Settings()
        self.games: list[Game] = []
        self.mods: list[Mod] = []
        self.listeners: list[Callable[[], None]] = []
        self.load_warning = ""
        self.load()

    @property
    def config_path(self) -> Path:
        return self.data_dir / "config.json"

    @property
    def cache_path(self) -> Path:
        return self.data_dir / "http_cache.json"

    @property
    def log_path(self) -> Path:
        return self.data_dir / "modloader.log"

    def mod_dir(self, kind: str, mod_id: str) -> Path:
        """Per-mod working folders: 'backups', 'disabled' or 'staging'."""
        return self.data_dir / kind / mod_id

    def load(self) -> None:
        path = self.config_path
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            settings = _from_dict(Settings, raw.get("settings", {}))
            games = [_from_dict(Game, g) for g in raw.get("games", [])]
            mods = [_from_dict(Mod, m) for m in raw.get("mods", [])]
        except (ValueError, TypeError, AttributeError) as e:
            broken = path.with_name(f"config.broken-{time.strftime('%Y%m%d-%H%M%S')}.json")
            path.replace(broken)
            self.load_warning = (
                f"Your saved settings could not be read ({e}). They were kept as "
                f"{broken.name} and Mod Loader started fresh."
            )
            return
        self.settings, self.games, self.mods = settings, games, mods

    def save(self) -> None:
        with self.lock:
            data = {
                "version": CONFIG_VERSION,
                "settings": asdict(self.settings),
                "games": [asdict(g) for g in self.games],
                "mods": [asdict(m) for m in self.mods],
            }
            tmp = self.config_path.with_name("config.json.tmp")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            os.replace(tmp, self.config_path)
        self.notify()

    def notify(self) -> None:
        for listener in list(self.listeners):
            listener()

    def game(self, game_id: str) -> Game | None:
        return next((g for g in self.games if g.id == game_id), None)

    def mod(self, mod_id: str) -> Mod | None:
        return next((m for m in self.mods if m.id == mod_id), None)

    def mods_for(self, game_id: str) -> list[Mod]:
        return [m for m in self.mods if m.game_id == game_id]
