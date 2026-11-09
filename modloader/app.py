"""Command-line entry: opens the window, or runs headless with --update / --play."""

from __future__ import annotations

import argparse
import logging
import sys
from logging.handlers import RotatingFileHandler

from .config import Store

logger = logging.getLogger("modloader")


def _setup_logging(store: Store, to_console: bool) -> None:
    logger.setLevel(logging.INFO)
    handler = RotatingFileHandler(store.log_path, maxBytes=1_000_000, backupCount=1, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S"))
    logger.addHandler(handler)
    if to_console and sys.stdout is not None:
        try:
            sys.stdout.reconfigure(errors="replace")  # consoles that can't show "→" etc.
        except (AttributeError, ValueError):
            pass
        logger.addHandler(logging.StreamHandler(sys.stdout))


def _dpi_aware() -> None:
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:  # noqa: BLE001 - older Windows; the window is just a bit blurry
            pass


def _find_game(store: Store, key: str):
    key = key.strip()
    return store.game(key) or next((g for g in store.games if g.name.lower() == key.lower()), None)


def run_update(store: Store) -> int:
    from .manager import ModManager

    def log(message: str, level: str = "info") -> None:
        logger.log(logging.ERROR if level == "error" else logging.INFO, message)

    summary = ModManager(store, log=log).check_many(store.mods, install=True, auto_only=True)
    return 1 if summary.failed else 0


def run_play(store: Store, key: str) -> int:
    from tkinter import messagebox

    from .gui import PlayWindow

    game = _find_game(store, key)
    if game is None:
        messagebox.showerror("Mod Loader", f"There's no game called “{key}” in Mod Loader. "
                                           "It may have been removed or renamed.")
        return 2
    PlayWindow(store, game).mainloop()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ModLoader", description="Install mods from GitHub releases and keep them updated.")
    parser.add_argument("--update", action="store_true", help="install updates for every auto-updating mod, then exit")
    parser.add_argument("--play", metavar="GAME", help="update a game's mods, start it, then exit (name or id)")
    parser.add_argument("--data", metavar="FOLDER", help="keep settings in this folder instead of %%APPDATA%%\\ModLoader")
    args = parser.parse_args(argv)

    store = Store(args.data)
    _setup_logging(store, to_console=args.update)
    _dpi_aware()
    from .selfupdate import clean_up_old_exe

    clean_up_old_exe()
    if args.update:
        return run_update(store)
    if args.play:
        return run_play(store, args.play)
    from .gui import App

    App(store).mainloop()
    return 0
