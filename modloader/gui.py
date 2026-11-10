"""The Mod Loader window, its dialogs, and the small "Update & Play" window."""

from __future__ import annotations

import logging
import os
import queue
import subprocess
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from . import __version__, collection, requires, selfupdate, system
from .config import Collection, Game, Mod, Store
from .detect import (FoundGame, Suggestion, anti_cheat, find_installed_games, steam_app_id,
                     suggest_mods_folders)
from .github import GitHubError, is_side_file, parse_repo, select_assets, version_glob
from .installer import InstallError, clean_subdir
from .manager import ModManager, Summary
from .util import friendly_time

logger = logging.getLogger("modloader")

RED, AMBER, GREEN, GREY, BLUE = "#b03a2e", "#8a6100", "#1e7e34", "#6c757d", "#1a5fb4"
AUTO_LABEL = "(automatic)"
LEVELS = {"error": logging.ERROR, "warn": logging.WARNING}
TOKEN_URL = "https://github.com/settings/personal-access-tokens/new"


def _scale_for(widget: tk.Misc) -> float:
    return max(1.0, widget.winfo_fpixels("1i") / 96.0)


def _apply_theme(root: tk.Misc, scale: float) -> None:
    style = ttk.Style(root)
    if "vista" in style.theme_names():
        style.theme_use("vista")
    elif "clam" in style.theme_names():
        style.theme_use("clam")
    style.configure("Treeview", rowheight=int(24 * scale))
    style.configure("Head.TLabel", font=("Segoe UI", 11, "bold"))


class Worker:
    """Runs one task at a time on a background thread so the window never freezes."""

    def __init__(self, post):
        self._post = post
        self._tasks: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._pending = 0
        self.current: str | None = None
        threading.Thread(target=self._run, name="modloader-worker", daemon=True).start()

    @property
    def pending(self) -> int:
        with self._lock:
            return self._pending

    @property
    def busy(self) -> bool:
        return self.pending > 0

    def submit(self, label: str, fn) -> None:
        with self._lock:
            self._pending += 1
        self._tasks.put((label, fn))
        self._post(("refresh",))

    def _run(self) -> None:
        while True:
            label, fn = self._tasks.get()
            self.current = label
            self._post(("refresh",))
            try:
                fn()
            except (GitHubError, InstallError) as e:
                self._post(("log", str(e), "error"))
            except Exception as e:  # a bug; keep the window alive and record it
                logger.exception("Task %r crashed", label)
                self._post(("log", f"{label} failed unexpectedly: {e!r}. Details are in modloader.log.", "error"))
            finally:
                self.current = None
                with self._lock:
                    self._pending -= 1
                self._post(("refresh",))


# -- Main window ------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self, store: Store):
        super().__init__()
        self.store = store
        self.events: queue.Queue = queue.Queue()
        self.manager = ModManager(store, log=self._log_async, progress=self._progress_async,
                                  on_requirements=lambda mod, again: self.events.put(("requires", mod.id, again)))
        store.listeners.append(lambda: self.events.put(("refresh",)))
        self.worker = Worker(self.events.put)
        self._timer: str | None = None
        self._requirement_asks: list[tuple[str, bool]] = []  # (mod id, ask_declined) waiting to be asked
        self._asking = False
        self.shown_collection: dict[str, str] = {}  # game id -> the collection its mod list is showing
        self._collection_values: list[str] = []  # what the collection list holds right now
        self.app_update: selfupdate.AppUpdate | None = None
        self.app_update_ready = False  # downloaded and in place; runs after a restart

        self.title("Mod Loader")
        self.scale = _scale_for(self)
        self.geometry(f"{self.px(1100)}x{self.px(700)}")
        self.minsize(self.px(820), self.px(500))
        _apply_theme(self, self.scale)
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.refresh()
        self.after(100, self._pump)
        if store.load_warning:
            self.log(store.load_warning, "warn")
        self.log(f"Mod Loader {__version__} is ready. Data is kept in {store.data_dir}", "dim")
        if store.settings.check_on_startup and store.mods:
            self.after(800, lambda: self.check_all(auto=True))
        if not selfupdate.blocked_reason():
            self.after(1500, lambda: self.check_app(auto=True))
        self._schedule_periodic()

    def px(self, value: float) -> int:
        return int(value * self.scale)

    # -- Layout -----------------------------------------------------------------

    def _build(self) -> None:
        px = self.px
        toolbar = ttk.Frame(self, padding=(px(10), px(10), px(10), px(6)))
        toolbar.pack(fill="x")
        ttk.Button(toolbar, text="Check for updates", command=self.check_all).pack(side="left")
        ttk.Button(toolbar, text="Update all", command=self.update_all).pack(side="left", padx=(px(6), 0))
        self.summary_var = tk.StringVar()
        ttk.Label(toolbar, textvariable=self.summary_var, foreground=GREY).pack(side="left", padx=px(14))
        ttk.Button(toolbar, text="Settings", command=self.open_settings).pack(side="right")

        # Shown when a new Mod Loader is available or ready.
        self.banner = ttk.Frame(self, padding=(px(10), px(2), px(10), px(8)))
        self.banner_var = tk.StringVar()
        ttk.Label(self.banner, textvariable=self.banner_var, foreground=BLUE).pack(side="left")
        self.banner_button = ttk.Button(self.banner, command=self._banner_action)
        self.banner_button.pack(side="right")
        self.banner_notes = ttk.Button(self.banner, text="What's new",
                                       command=lambda: self.app_update and webbrowser.open(self.app_update.page))
        self.banner_notes.pack(side="right", padx=(0, px(6)))

        vpane = self.vpane = ttk.Panedwindow(self, orient="vertical")
        vpane.pack(fill="both", expand=True, padx=px(10))
        self.hpane = ttk.Panedwindow(vpane, orient="horizontal")
        vpane.add(self.hpane, weight=4)
        self._build_games(self.hpane)
        self._build_mods(self.hpane)

        log_frame = ttk.Labelframe(vpane, text="Activity", padding=px(4))
        vpane.add(log_frame, weight=1)
        self.log_text = ScrolledText(log_frame, height=7, wrap="word", state="disabled",
                                     relief="flat", borderwidth=0, font=("Segoe UI", 9))
        self.log_text.pack(fill="both", expand=True)
        for tag, color in (("error", RED), ("warn", AMBER), ("ok", GREEN), ("dim", GREY),
                           ("update", BLUE), ("time", GREY)):
            self.log_text.tag_configure(tag, foreground=color)

        status = ttk.Frame(self, padding=(px(10), px(6)))
        status.pack(fill="x")
        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(status, textvariable=self.status_var).pack(side="left")
        self.progress = ttk.Progressbar(status, length=px(240), mode="determinate", maximum=100)
        self.progress.pack(side="right")
        self.after(60, lambda: self.hpane.sashpos(0, px(290)))

    def _build_games(self, parent) -> None:
        px = self.px
        frame = ttk.Labelframe(parent, text="Games", padding=px(8))
        parent.add(frame, weight=1)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        self.games_tree = ttk.Treeview(frame, show="tree", selectmode="browse")
        self.games_tree.grid(row=0, column=0, sticky="nsew")
        self.games_tree.tag_configure("updates", foreground=BLUE)
        self.games_tree.bind("<<TreeviewSelect>>", lambda e: self.refresh_mods())
        self.games_tree.bind("<Double-1>", lambda e: self.edit_game())

        buttons = ttk.Frame(frame)
        buttons.grid(row=1, column=0, sticky="ew", pady=(px(8), 0))
        for col in range(3):
            buttons.columnconfigure(col, weight=1, uniform="g")
        ttk.Button(buttons, text="Add game", command=self.add_game).grid(row=0, column=0, sticky="ew")
        self.btn_game_edit = ttk.Button(buttons, text="Edit", command=self.edit_game)
        self.btn_game_edit.grid(row=0, column=1, sticky="ew", padx=px(4))
        self.btn_game_remove = ttk.Button(buttons, text="Remove", command=self.remove_game)
        self.btn_game_remove.grid(row=0, column=2, sticky="ew")
        self.btn_folder = ttk.Button(buttons, text="Open mods folder", command=self.open_game_folder)
        self.btn_folder.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(px(4), 0))
        self.btn_play = ttk.Button(buttons, text="▶  Update & play", command=self.play_game)
        self.btn_play.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(px(4), 0), padx=(0, px(4)))
        self.btn_shortcut = ttk.Button(buttons, text="Shortcut", command=self.make_shortcut)
        self.btn_shortcut.grid(row=2, column=2, sticky="ew", pady=(px(4), 0))

    def _build_mods(self, parent) -> None:
        px = self.px
        self.mods_frame = ttk.Labelframe(parent, text="Mods", padding=px(8))
        parent.add(self.mods_frame, weight=3)
        self.mods_frame.columnconfigure(0, weight=1)
        self.mods_frame.rowconfigure(2, weight=1)

        bar = ttk.Frame(self.mods_frame)
        bar.grid(row=0, column=0, sticky="ew", pady=(0, px(8)))
        ttk.Label(bar, text="Collection").pack(side="left")
        self.collection_box = ttk.Combobox(bar, state="disabled", width=34, height=16)
        self.collection_box.pack(side="left", padx=(px(8), 0))
        self.collection_box.bind("<<ComboboxSelected>>", self._on_collection_picked)
        self.btn_use = ttk.Button(bar, text="Use", command=self.use_collection)
        self.btn_use.pack(side="left", padx=(px(6), 0))
        self.btn_collections = ttk.Button(bar, text="Collections  ▾", command=self._open_collection_menu)
        self.btn_collections.pack(side="right")
        self.collection_menu = tk.Menu(self, tearoff=False)
        self.collection_menu.add_command(label="New collection…", command=self.new_collection)
        self.collection_menu.add_command(label="Edit…", command=self.edit_collection)
        self.collection_menu.add_command(label="Delete", command=self.delete_collection)
        self.collection_menu.add_separator()
        self.collection_menu.add_command(label="Export code…", command=self.export_collection)
        self.collection_menu.add_command(label="Import code…", command=self.import_collection)

        self.hint_var = tk.StringVar()
        self.hint = ttk.Label(self.mods_frame, textvariable=self.hint_var, foreground=GREY,
                              wraplength=px(600), justify="left")
        self.hint.grid(row=1, column=0, sticky="ew", pady=(0, px(8)))
        self.hint.bind("<Configure>", lambda e: self.hint.configure(wraplength=max(200, e.width - 4)))

        tree_box = ttk.Frame(self.mods_frame)
        tree_box.grid(row=2, column=0, sticky="nsew")
        tree_box.rowconfigure(0, weight=1)
        tree_box.columnconfigure(0, weight=1)
        columns = ("name", "repo", "installed", "latest", "status")
        self.mods_tree = ttk.Treeview(tree_box, columns=columns, show="headings", selectmode="extended")
        for col, title, width, stretch in (
            ("name", "Mod", 170, True), ("repo", "GitHub repository", 190, True),
            ("installed", "Installed", 90, False), ("latest", "Latest", 90, False),
            ("status", "Status", 200, True),
        ):
            self.mods_tree.heading(col, text=title, anchor="w")
            self.mods_tree.column(col, width=px(width), minwidth=px(60), stretch=stretch, anchor="w")
        scroll = ttk.Scrollbar(tree_box, orient="vertical", command=self.mods_tree.yview)
        self.mods_tree.configure(yscrollcommand=scroll.set)
        self.mods_tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        self.mods_tree.tag_configure("error", foreground=RED)
        self.mods_tree.tag_configure("update", foreground=BLUE)
        self.mods_tree.tag_configure("disabled", foreground=GREY)
        self.mods_tree.tag_configure("busy", foreground=AMBER)
        self.mods_tree.tag_configure("warn", foreground=AMBER)
        self.mods_tree.bind("<<TreeviewSelect>>", lambda e: self._selection_changed())
        self.mods_tree.bind("<Double-1>", self._on_mod_double_click)
        self.mods_tree.bind("<Button-3>", self._on_mod_right_click)
        self.mods_tree.bind("<Delete>", lambda e: self.remove_mods())

        buttons = ttk.Frame(self.mods_frame)
        buttons.grid(row=3, column=0, sticky="ew", pady=(px(8), 0))
        self.btn_add_mod = ttk.Button(buttons, text="Add mod", command=self.add_mod)
        self.btn_add_mod.pack(side="left")
        self.btn_edit_mod = ttk.Button(buttons, text="Edit", command=self.edit_mod)
        self.btn_edit_mod.pack(side="left", padx=(px(4), 0))
        self.btn_remove_mod = ttk.Button(buttons, text="Remove", command=self.remove_mods)
        self.btn_remove_mod.pack(side="left", padx=(px(4), 0))
        self.btn_releases = ttk.Button(buttons, text="Releases page", command=self.open_releases)
        self.btn_releases.pack(side="right")
        self.btn_toggle = ttk.Button(buttons, text="Disable", command=self.toggle_mods)
        self.btn_toggle.pack(side="right", padx=(0, px(4)))
        self.btn_reinstall = ttk.Button(buttons, text="Reinstall", command=self.reinstall_mods)
        self.btn_reinstall.pack(side="right", padx=(0, px(4)))
        self.btn_update = ttk.Button(buttons, text="Update", command=self.update_mods)
        self.btn_update.pack(side="right", padx=(0, px(4)))

        self.details_var = tk.StringVar()
        details = ttk.Label(self.mods_frame, textvariable=self.details_var, foreground=GREY, justify="left")
        details.grid(row=4, column=0, sticky="ew", pady=(px(8), 0))
        details.bind("<Configure>", lambda e: details.configure(wraplength=max(200, e.width - 4)))

        self.menu = tk.Menu(self, tearoff=False)
        self.menu.add_command(label="Update", command=self.update_mods)
        self.menu.add_command(label="Reinstall", command=self.reinstall_mods)
        self.menu.add_command(label="Disable", command=self.toggle_mods)
        self.menu.add_separator()
        self.menu.add_command(label="Edit…", command=self.edit_mod)
        self.menu.add_command(label="Open releases page", command=self.open_releases)
        self.menu.add_separator()
        self.menu.add_command(label="Remove", command=self.remove_mods)

    # -- Events from the worker ---------------------------------------------------

    def _log_async(self, message: str, level: str = "info") -> None:
        self.events.put(("log", message, level))

    def _progress_async(self, done: int, total: int) -> None:
        self.events.put(("progress", done, total))

    def _pump(self) -> None:
        refresh = False
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == "log":
                    self.log(event[1], event[2])
                elif event[0] == "progress":
                    done, total = event[1], event[2]
                    self.progress["value"] = (done * 100 / total) if total else 0
                elif event[0] == "app_update":
                    self.app_update, self.app_update_ready = event[1], event[2]
                    self._show_banner()
                elif event[0] == "requires":
                    if (event[1], event[2]) not in self._requirement_asks:
                        self._requirement_asks.append((event[1], event[2]))
                    self.after(200, self._next_requirement_ask)
                else:
                    refresh = True
        except queue.Empty:
            pass
        try:
            if refresh:
                self.refresh()
        except Exception:
            logger.exception("Refreshing the window failed")
        finally:
            self.after(100, self._pump)

    def log(self, message: str, level: str = "info") -> None:
        logger.log(LEVELS.get(level, logging.INFO), message)
        text = self.log_text
        at_bottom = text.yview()[1] > 0.98
        text.configure(state="normal")
        text.insert("end", time.strftime("%H:%M  "), "time")
        text.insert("end", message + "\n", level)
        lines = int(text.index("end-1c").split(".")[0])
        if lines > 1500:
            text.delete("1.0", f"{lines - 1200}.0")
        text.configure(state="disabled")
        if at_bottom:
            text.see("end")

    # -- Refreshing ----------------------------------------------------------------

    def selected_game(self) -> Game | None:
        selection = self.games_tree.selection()
        return self.store.game(selection[0]) if selection else None

    def selected_mods(self) -> list[Mod]:
        return [m for iid in self.mods_tree.selection() if (m := self.store.mod(iid))]

    def selected_collection(self) -> Collection | None:
        """The collection the mod list is showing, or None when it shows all the game's mods."""
        game = self.selected_game()
        coll = self.store.collection(self.shown_collection.get(game.id, "")) if game else None
        return coll if coll and coll.game_id == game.id else None

    def refresh(self) -> None:
        self.refresh_games()
        self.refresh_mods()
        mods = list(self.store.mods)
        updates = sum(m.update_available and m.enabled for m in mods)
        checked = [m.last_checked for m in mods if m.last_checked]
        parts = [f"{len(self.store.games)} game{'s' * (len(self.store.games) != 1)}",
                 f"{len(mods)} mod{'s' * (len(mods) != 1)}"]
        if updates:
            parts.append(f"{updates} update{'s' * (updates != 1)} available")
        if checked:
            parts.append(f"last checked {friendly_time(max(checked))}")
        self.summary_var.set("  ·  ".join(parts))
        if self.worker.current:
            waiting = self.worker.pending - 1
            self.status_var.set(f"{self.worker.current}…" + (f"  ({waiting} more queued)" if waiting > 0 else ""))
        else:
            self.status_var.set("Ready")
            self.progress["value"] = 0

    def refresh_games(self) -> None:
        tree = self.games_tree
        games = list(self.store.games)
        ids = [g.id for g in games]
        for iid in tree.get_children():
            if iid not in ids:
                tree.delete(iid)
        for index, game in enumerate(games):
            n = sum(m.update_available and m.enabled for m in self.store.mods_for(game.id))
            text = game.name + (f"   ({n} update{'s' * (n != 1)})" if n else "")
            tags = ("updates",) if n else ()
            if tree.exists(game.id):
                tree.item(game.id, text=text, tags=tags)
                tree.move(game.id, "", index)
            else:
                tree.insert("", index, iid=game.id, text=text, tags=tags)
        if games and not tree.selection():
            tree.selection_set(games[0].id)
            tree.focus(games[0].id)

    def _mod_status(self, mod: Mod) -> tuple[str, str]:
        if self.manager.busy_mod_id == mod.id:
            return "Working…", "busy"
        if mod.last_error:
            return "⚠ " + mod.last_error.splitlines()[0], "error"
        if not mod.enabled:
            return "Disabled", "disabled"
        if not mod.installed:
            return "Not installed", ""
        missing = requires.unmet(self.store, mod)
        if missing:
            return "Needs " + ", ".join(requires.label(r) for r, _ in missing), "warn"
        if mod.update_available:
            return f"Update available ({mod.latest_tag})", "update"
        if mod.last_checked:
            return f"Up to date · checked {friendly_time(mod.last_checked)}", ""
        return "Installed", ""

    def refresh_mods(self) -> None:
        game = self.selected_game()
        coll = self.selected_collection()
        tree = self.mods_tree
        all_mods = self.store.mods_for(game.id) if game else []
        mods = collection.members(self.store, coll) if coll else all_mods
        self.mods_frame.configure(text=f"Mods for {game.name}" if game else "Mods")
        self._refresh_collections(game, coll, len(all_mods))

        if not self.store.games:
            self.hint_var.set("Start by adding a game: click “Add game” and pick it from your installed "
                              "games. Mod Loader works out where its mods go.")
        elif game and not all_mods:
            self.hint_var.set(f"No mods yet. Click “Add mod” and enter the GitHub repository the mod is "
                              f"released from, or use Collections → Import code if someone sent you a "
                              f"collection. Files will go into {game.mods_dir}")
        elif coll and not mods:
            self.hint_var.set(f"“{coll.name}” has no mods yet. Use Collections → Edit to pick some of "
                              f"{game.name}'s mods, or click “Add mod” to add a new one straight into it.")
        else:
            self.hint_var.set("")
        if self.hint_var.get():
            self.hint.grid()
        else:
            self.hint.grid_remove()

        ids = [m.id for m in mods]
        for iid in tree.get_children():
            if iid not in ids:
                tree.delete(iid)
        for index, mod in enumerate(mods):
            status, tag = self._mod_status(mod)
            values = (mod.display_name, mod.repo, mod.installed_tag or "—", mod.latest_tag or "—", status)
            if tree.exists(mod.id):
                tree.item(mod.id, values=values, tags=(tag,) if tag else ())
                tree.move(mod.id, "", index)
            else:
                tree.insert("", index, iid=mod.id, values=values, tags=(tag,) if tag else ())
        self._selection_changed()

    def _refresh_collections(self, game: Game | None, coll: Collection | None, mod_count: int) -> None:
        """The row above the mod list: which collection is shown, and what can be done with it."""
        box = self.collection_box
        colls = self.store.collections_for(game.id) if game else []
        values = [f"All mods ({mod_count})"] if game else []
        for c in colls:
            count = len(collection.members(self.store, c))
            values.append(f"{c.name} ({count})" + ("   ✓ in use" if collection.in_use(self.store, c) else ""))
        index = colls.index(coll) + 1 if coll else 0
        if values != self._collection_values:  # only when it changed, so an open list isn't disturbed
            self._collection_values = values
            box.configure(values=values)
        box.configure(state="readonly" if game else "disabled")
        if not game:
            box.set("")
        elif box.current() != index or box.get() != values[index]:
            box.current(index)
        self.btn_use.state(["!disabled"] if coll and not collection.in_use(self.store, coll) else ["disabled"])
        self.btn_collections.state(["!disabled"] if game else ["disabled"])
        shown = len(collection.members(self.store, coll)) if coll else mod_count
        menu = self.collection_menu
        menu.entryconfigure(1, label=f"Edit “{coll.name}”…" if coll else "Edit…",
                            state="normal" if coll else "disabled")
        menu.entryconfigure(2, label=f"Delete “{coll.name}”" if coll else "Delete",
                            state="normal" if coll else "disabled")
        menu.entryconfigure(4, label=f"Export “{coll.name}” as a code…" if coll else "Export all mods as a code…",
                            state="normal" if shown else "disabled")

    def _open_collection_menu(self) -> None:
        button = self.btn_collections
        self.collection_menu.tk_popup(button.winfo_rootx(), button.winfo_rooty() + button.winfo_height())

    def _on_collection_picked(self, _event=None) -> None:
        game = self.selected_game()
        if not game:
            return
        index = self.collection_box.current()
        colls = self.store.collections_for(game.id)
        if 1 <= index <= len(colls):
            self.shown_collection[game.id] = colls[index - 1].id
        else:
            self.shown_collection.pop(game.id, None)
        self.collection_box.selection_clear()
        self.refresh_mods()

    def show_collection(self, coll: Collection | None) -> None:
        game = self.selected_game()
        if not game:
            return
        if coll is not None and coll.game_id == game.id:
            self.shown_collection[game.id] = coll.id
        else:
            self.shown_collection.pop(game.id, None)
        self.refresh()

    def _selection_changed(self) -> None:
        game = self.selected_game()
        mods = self.selected_mods()
        on = lambda b, cond: b.state(["!disabled"] if cond else ["disabled"])  # noqa: E731
        for b in (self.btn_game_edit, self.btn_game_remove, self.btn_folder, self.btn_add_mod):
            on(b, game is not None)
        on(self.btn_play, game is not None and bool(game and game.launch_path))
        on(self.btn_shortcut, game is not None and bool(game and game.launch_path))
        on(self.btn_edit_mod, len(mods) == 1)
        on(self.btn_releases, len(mods) == 1)
        for b in (self.btn_remove_mod, self.btn_update, self.btn_reinstall, self.btn_toggle):
            on(b, bool(mods))
        toggle_text = "Enable" if mods and not mods[0].enabled else "Disable"
        self.btn_toggle.configure(text=toggle_text)
        self.menu.entryconfigure(2, label=toggle_text)
        self.details_var.set(self._details(mods[0], game) if len(mods) == 1 and game else "")

    def _details(self, mod: Mod, game: Game) -> str:
        target = os.path.join(game.mods_dir, *mod.subdir.split("/")) if mod.subdir else game.mods_dir
        lines = [f"Release file: {mod.asset_pattern or 'automatic'}  ·  Installs into: {target}"
                 f"  ·  {'Updates automatically' if mod.auto_update else 'Updates only when you click Update'}"]
        if mod.installed:
            lines.append(f"Installed {mod.installed_tag} ({mod.installed_asset}) {friendly_time(mod.installed_at)}"
                         f"  ·  {len(mod.installed_files)} file{'s' * (len(mod.installed_files) != 1)}"
                         + (f"  ·  {len(mod.backups)} original file(s) backed up" if mod.backups else ""))
        if mod.requires:
            parts = []
            for req in mod.requires:
                have = requires.find(self.store, mod.game_id, req["repo"])
                state = "added" if have and have.enabled else "disabled" if have else "not added"
                parts.append(f"{requires.label(req)} ({state})")
            lines.append("Needs: " + ", ".join(parts))
        needed_by = requires.dependents(self.store, mod)
        if needed_by:
            lines.append("Needed by: " + ", ".join(m.display_name for m in needed_by))
        if mod.last_error:
            lines.append(f"Last problem: {mod.last_error}")
        return "\n".join(lines)

    def _on_mod_double_click(self, event) -> None:
        if self.mods_tree.identify_row(event.y):
            self.edit_mod()

    def _on_mod_right_click(self, event) -> None:
        row = self.mods_tree.identify_row(event.y)
        if not row:
            return
        if row not in self.mods_tree.selection():
            self.mods_tree.selection_set(row)
        self._selection_changed()
        single = "normal" if len(self.mods_tree.selection()) == 1 else "disabled"
        self.menu.entryconfigure(4, state=single)
        self.menu.entryconfigure(5, state=single)
        self.menu.tk_popup(event.x_root, event.y_root)

    # -- Games ---------------------------------------------------------------------

    def add_game(self) -> None:
        dialog = GameDialog(self, "Add a game")
        if dialog.result is None:
            return
        with self.store.lock:
            game = Game(**dialog.result)
            self.store.games.append(game)
            self.store.save()
        self.refresh()
        self.games_tree.selection_set(game.id)
        self.games_tree.see(game.id)
        self.log(f"Added {game.name}. Mods will be installed into {game.mods_dir}", "ok")

    def edit_game(self) -> None:
        game = self.selected_game()
        if not game:
            return
        dialog = GameDialog(self, f"Edit {game.name}", game)
        if dialog.result is None:
            return
        new = dialog.result
        moved = os.path.normcase(os.path.abspath(new["mods_dir"])) != os.path.normcase(os.path.abspath(game.mods_dir))
        installed = [m for m in self.store.mods_for(game.id) if m.enabled and m.installed_files]
        if moved and installed and not messagebox.askyesno(
            "Change the mods folder?",
            f"{len(installed)} installed mod(s) will be removed from\n{game.mods_dir}\n\n"
            f"and installed again into\n{new['mods_dir']}\n\nContinue?", parent=self):
            return
        with self.store.lock:
            game.name, game.launch_path, game.launch_args = new["name"], new["launch_path"], new["launch_args"]
            game.install_dir = new["install_dir"]
            if moved and not installed:
                game.mods_dir = new["mods_dir"]
            self.store.save()
        if moved and installed:
            self.worker.submit(f"Moving {game.name}'s mods",
                               lambda: self.manager.move_game_folder(game, new["mods_dir"]))
        self.refresh()

    def remove_game(self) -> None:
        game = self.selected_game()
        if not game:
            return
        mods = self.store.mods_for(game.id)
        if mods:
            answer = messagebox.askyesnocancel(
                "Remove game",
                f"Remove {game.name} from Mod Loader?\n\n"
                f"Yes: also delete the files of its {len(mods)} mod(s) from the game folder "
                f"(any files they replaced are put back).\n"
                f"No: leave the mod files where they are.", parent=self)
            if answer is None:
                return
        else:
            if not messagebox.askyesno("Remove game", f"Remove {game.name} from Mod Loader?", parent=self):
                return
            answer = False
        self.worker.submit(f"Removing {game.name}", lambda: self.manager.remove_game(game, delete_files=answer))

    def open_game_folder(self) -> None:
        game = self.selected_game()
        if not game:
            return
        if not os.path.isdir(game.mods_dir):
            messagebox.showinfo("Mod Loader", f"{game.mods_dir}\n\ndoesn't exist yet. It will be created "
                                              "when the first mod is installed.", parent=self)
            return
        system.open_path(game.mods_dir)

    def play_game(self) -> None:
        game = self.selected_game()
        if not game:
            return
        if not game.launch_path:
            messagebox.showinfo("Mod Loader", "Set the game's launcher first (Edit the game).", parent=self)
            return

        def task():
            summary = self.manager.update_for_play(game)
            if summary.failed:
                self.events.put(("log", "Starting the game anyway; see the problems above.", "warn"))
            self.manager.launch(game)

        self.worker.submit(f"Updating {game.name}'s mods", task)

    def make_shortcut(self) -> None:
        game = self.selected_game()
        if not game:
            return
        try:
            name = system.create_play_shortcut(game)
        except (OSError, subprocess.SubprocessError) as e:
            messagebox.showerror("Mod Loader", f"Couldn't create the shortcut: {e}", parent=self)
            return
        self.log(f"Created “{name}” on your desktop. It updates {game.name}'s mods, then starts the game.", "ok")
        messagebox.showinfo("Shortcut created", f"“{name}” is on your desktop.\n\nOpening it checks GitHub, "
                                                f"installs any mod updates, then starts {game.name}.", parent=self)

    # -- Mods ----------------------------------------------------------------------

    def add_mod(self) -> None:
        game = self.selected_game()
        if not game:
            return
        dialog = ModDialog(self, game)
        if dialog.result is None:
            return
        coll = self.selected_collection()
        with self.store.lock:
            mod = Mod(game_id=game.id, **dialog.result)
            self.store.mods.append(mod)
            if coll is not None:  # added while a collection is shown: it goes into that collection
                coll.mod_ids.append(mod.id)
            self.store.save()
        self.refresh()
        self.mods_tree.selection_set(mod.id)
        self.mods_tree.see(mod.id)
        self.ask_requirements(mod)  # their installs are queued before this mod's
        self.worker.submit(f"Installing {mod.display_name}", lambda: self.manager.install(mod))

    def edit_mod(self) -> None:
        mods = self.selected_mods()
        game = self.selected_game()
        if len(mods) != 1 or not game:
            return
        mod = mods[0]
        dialog = ModDialog(self, game, mod)
        if dialog.result is None:
            return
        changes = dialog.result
        affects_files = any(getattr(mod, k) != v for k, v in changes.items()
                            if k not in ("name", "auto_update"))
        with self.store.lock:
            if changes["repo"].lower() != mod.repo.lower():
                mod.requires, mod.requires_release_id, mod.declined_requires = [], None, []
            for key, value in changes.items():
                setattr(mod, key, value)
            mod.last_error = ""
            self.store.save()
        if affects_files and mod.installed and mod.enabled:
            if messagebox.askyesno("Reinstall?", f"Reinstall {mod.display_name} now with the new settings?",
                                   parent=self):
                self.worker.submit(f"Reinstalling {mod.display_name}", lambda: self.manager.install(mod, force=True))
        elif not mod.installed and mod.enabled:
            self.worker.submit(f"Installing {mod.display_name}", lambda: self.manager.install(mod))

    def remove_mods(self) -> None:
        mods = self.selected_mods()
        if not mods:
            return
        what = mods[0].display_name if len(mods) == 1 else f"{len(mods)} mods"
        needed = self._needed_by_others(mods)
        coll = self.selected_collection()
        only_collection = (f"\n\nThis removes {'it' if len(mods) == 1 else 'them'} from the game, not just from "
                           f"“{coll.name}”. To only take {'it' if len(mods) == 1 else 'them'} out of the "
                           "collection, use Collections → Edit.") if coll else ""
        if not messagebox.askyesno("Remove", f"Remove {what}?\n\nTheir files are deleted from the game folder, "
                                             "and any files they replaced are put back."
                                             + only_collection + self._needed_warning(needed), parent=self):
            return
        self._decline_needed(needed)
        for mod in mods:
            self.worker.submit(f"Removing {mod.display_name}", lambda m=mod: self.manager.remove_mod(m))

    def update_mods(self) -> None:
        for mod in self.selected_mods():
            self.worker.submit(f"Updating {mod.display_name}",
                               lambda m=mod: self.manager.install(m, ask_declined=True))

    def reinstall_mods(self) -> None:
        for mod in self.selected_mods():
            self.worker.submit(f"Reinstalling {mod.display_name}",
                               lambda m=mod: self.manager.install(m, force=True, ask_declined=True))

    def toggle_mods(self) -> None:
        mods = self.selected_mods()
        if not mods:
            return
        enable = not mods[0].enabled
        if not enable:
            needed = self._needed_by_others(mods)
            if needed and not messagebox.askyesno(
                    "Disable", "Disable anyway?" + self._needed_warning(needed), parent=self):
                return
            self._decline_needed(needed)
        for mod in mods:
            verb = "Enabling" if enable else "Disabling"
            self.worker.submit(f"{verb} {mod.display_name}", lambda m=mod: self.manager.set_enabled(m, enable))

    def open_releases(self) -> None:
        mods = self.selected_mods()
        if len(mods) == 1:
            webbrowser.open(f"https://github.com/{mods[0].repo}/releases")

    # -- Collections -----------------------------------------------------------------

    def new_collection(self) -> None:
        game = self.selected_game()
        if not game:
            return
        picked = self.selected_mods()
        # Several mods selected: start with those. Otherwise start with what's enabled right now.
        ticked = picked if len(picked) > 1 else [m for m in self.store.mods_for(game.id) if m.enabled]
        dialog = CollectionDialog(self, game, ticked=[m.id for m in ticked])
        if dialog.result is None:
            return
        with self.store.lock:
            coll = Collection(game_id=game.id, **dialog.result)
            self.store.collections.append(coll)
            self.store.save()
        self.show_collection(coll)
        count = len(coll.mod_ids)
        self.log(f"Made the collection “{coll.name}” for {game.name} with {count} mod{'s' * (count != 1)}.", "ok")

    def edit_collection(self) -> None:
        game, coll = self.selected_game(), self.selected_collection()
        if not game or not coll:
            return
        dialog = CollectionDialog(self, game, coll)
        if dialog.result is None or self.store.collection(coll.id) is None:
            return
        with self.store.lock:
            coll.name, coll.mod_ids = dialog.result["name"], dialog.result["mod_ids"]
            self.store.save()
        self.refresh()

    def delete_collection(self) -> None:
        game, coll = self.selected_game(), self.selected_collection()
        if not game or not coll:
            return
        count = len(collection.members(self.store, coll))
        kept = (f"\n\nIts {count} mods stay added to {game.name}." if count > 1 else
                f"\n\nIts mod stays added to {game.name}." if count else "")
        if not messagebox.askyesno("Delete collection", f"Delete the collection “{coll.name}”?{kept}", parent=self):
            return
        with self.store.lock:
            self.store.collections = [c for c in self.store.collections if c.id != coll.id]
            self.store.save()
        self.show_collection(None)
        self.log(f"Deleted the collection “{coll.name}”. Its mods weren't touched.", "ok")

    def use_collection(self) -> None:
        game, coll = self.selected_game(), self.selected_collection()
        if not game or not coll:
            return
        wanted = set(coll.mod_ids)
        mods = self.store.mods_for(game.id)
        turn_on = [m for m in mods if m.id in wanted and not m.enabled]
        turn_off = [m for m in mods if m.id not in wanted and m.enabled]
        if not turn_on and not turn_off:
            return
        parts = []
        if turn_on:
            parts.append("Enable:  " + _name_list(turn_on))
        if turn_off:
            parts.append("Disable:  " + _name_list(turn_off))
        if not messagebox.askyesno(
                "Use collection", f"Use “{coll.name}” for {game.name}?\n\n" + "\n\n".join(parts)
                + "\n\nDisabled mods stay in Mod Loader, so you can enable them again at any time.", parent=self):
            return
        self.worker.submit(f"Switching {game.name} to {coll.name}", lambda: self.manager.use_collection(coll))

    def export_collection(self) -> None:
        game, coll = self.selected_game(), self.selected_collection()
        if not game:
            return
        mods = collection.members(self.store, coll) if coll else self.store.mods_for(game.id)
        if not mods:
            messagebox.showinfo("Mod Loader", "There are no mods here to export yet.", parent=self)
            return
        ExportDialog(self, coll.name if coll else f"{game.name} mods", game, mods)

    def import_collection(self) -> None:
        game = self.selected_game()
        if not game:
            return
        dialog = ImportDialog(self, game)
        if dialog.result is None or self.store.game(game.id) is None:
            return
        shared, name = dialog.result
        coll, added, replaced = collection.import_shared(self.store, game, shared, name)
        self.show_collection(coll)
        already = len(coll.mod_ids) - len(added)
        parts = []
        if added:
            parts.append(f"{len(added)} mod{'s' * (len(added) != 1)} added and being installed")
        if already:
            parts.append(f"{already} already added")
        self.log(f"{'Updated' if replaced else 'Imported'} the collection “{coll.name}” for {game.name}: "
                 + ", ".join(parts) + ".", "ok")
        for problem in shared.problems:
            self.log(f"  The code {problem}.", "warn")
        for mod in added:
            self.worker.submit(f"Installing {mod.display_name}", lambda m=mod: self.manager.install(m))
        if not collection.in_use(self.store, coll):
            self.log("Click “Use” to enable exactly this collection's mods and disable the others.", "dim")

    # -- Mods that need other mods -------------------------------------------------

    def _next_requirement_ask(self) -> None:
        if self._asking or not self._requirement_asks:
            return
        try:
            dialog_open = self.grab_current() is not None
        except KeyError:  # Tk's own widget (e.g. a drop-down list) has the grab
            dialog_open = True
        if dialog_open:  # ask once it's closed
            self.after(500, self._next_requirement_ask)
            return
        mod_id, again = self._requirement_asks.pop(0)
        mod = self.store.mod(mod_id)
        if mod is not None:
            self.ask_requirements(mod, again)
        if self._requirement_asks:
            self.after(200, self._next_requirement_ask)

    def ask_requirements(self, mod: Mod, ask_declined: bool = False) -> None:
        """Offer to add (or enable) the mods `mod` needs. Their installs are queued on the worker."""
        game = self.store.game(mod.game_id)
        missing = requires.unmet(self.store, mod, include_declined=ask_declined)
        if game is None or not missing or not mod.enabled or self._asking:
            return
        lines = [f"\u2022  {requires.label(req)}   ({req['repo']})" + ("   is added but disabled" if have else "")
                 for req, have in missing]
        one = len(missing) == 1
        if all(have is None for _, have in missing):
            action = f"Add {'it' if one else 'them'} to {game.name} and install {'it' if one else 'them'} now?"
        elif all(have is not None for _, have in missing):
            action = f"Enable {'it' if one else 'them'} now?"
        else:
            action = "Add and enable them now?"
        self._asking = True
        try:
            yes = messagebox.askyesno(
                "Required mods", f"{mod.display_name} needs {'this mod' if one else 'these mods'} to work:\n\n"
                + "\n".join(lines) + f"\n\n{action}", parent=self)
        finally:
            self._asking = False
        if self.store.mod(mod.id) is None:
            return  # removed while the question was open
        if not yes:
            with self.store.lock:
                for req, _ in missing:
                    requires.decline(mod, req["repo"])
                self.store.save()
            self.log(f"Not adding what {mod.display_name} needs. Update or reinstall {mod.display_name} "
                     "to be asked again, or add the mods yourself with \u201cAdd mod\u201d.", "dim")
            return
        for req, have in requires.unmet(self.store, mod):  # it may have changed while the question was open
            if have is not None:
                with self.store.lock:
                    collection.add_alongside(self.store, mod, have)
                    self.store.save()
                self.worker.submit(f"Enabling {have.display_name}", lambda m=have: self.manager.set_enabled(m, True))
                continue
            with self.store.lock:
                new = requires.new_mod(game.id, req)
                self.store.mods.append(new)
                collection.add_alongside(self.store, mod, new)  # collections with the mod get what it needs
                self.store.save()
            self.log(f"Added {new.display_name} ({new.repo}), which {mod.display_name} needs.", "ok")
            self.worker.submit(f"Installing {new.display_name}", lambda m=new: self.manager.install(m))
        self.refresh()

    def _needed_by_others(self, mods: list[Mod]) -> list[tuple[Mod, list[Mod]]]:
        """For mods about to be removed or disabled: which other enabled mods need each of them."""
        leaving = {m.id for m in mods}
        needed = []
        for mod in mods:
            others = [d for d in requires.dependents(self.store, mod) if d.id not in leaving]
            if others:
                needed.append((mod, others))
        return needed

    @staticmethod
    def _needed_warning(needed: list[tuple[Mod, list[Mod]]]) -> str:
        lines = [f"{', '.join(d.display_name for d in others)} need{'s' * (len(others) == 1)} {mod.display_name} "
                 "and may stop working." for mod, others in needed]
        return "\n\n" + "\n".join(lines) if lines else ""

    def _decline_needed(self, needed: list[tuple[Mod, list[Mod]]]) -> None:
        """The player chose to remove or disable a mod others need: don't offer it back to them."""
        if not needed:
            return
        with self.store.lock:
            for mod, others in needed:
                for other in others:
                    requires.decline(other, mod.repo)
            self.store.save()

    # -- Checking ------------------------------------------------------------------

    def check_all(self, auto: bool = False) -> None:
        if not self.store.mods:
            if not auto:
                self.log("There are no mods to check yet.", "dim")
            return
        install = auto and self.store.settings.auto_install_updates
        self.worker.submit("Checking for updates",
                           lambda: self.manager.check_many(self.store.mods, install=install, auto_only=True))

    def update_all(self) -> None:
        if not self.store.mods:
            self.log("There are no mods to update yet.", "dim")
            return
        self.worker.submit("Updating all mods",
                           lambda: self.manager.check_many(self.store.mods, install=True, auto_only=False))

    def _schedule_periodic(self) -> None:
        if self._timer:
            self.after_cancel(self._timer)
            self._timer = None
        minutes = self.store.settings.check_interval_minutes
        if minutes and minutes > 0:
            self._timer = self.after(int(minutes * 60_000), self._periodic)

    def _periodic(self) -> None:
        self._timer = None
        if not self.worker.busy:
            self.check_all(auto=True)
            if not self.app_update_ready and not selfupdate.blocked_reason():
                self.check_app(auto=True)
        self._schedule_periodic()

    # -- Mod Loader's own updates ----------------------------------------------------

    def check_app(self, auto: bool = False) -> None:
        """Look for a new Mod Loader; when checking by itself, also install it if allowed."""
        if self.app_update_ready:
            if not auto:
                self.log(f"Mod Loader {self.app_update.version} is already downloaded. Restart to use it.", "update")
            return

        def task():
            try:
                update = selfupdate.find_update(self.manager.client)
            except GitHubError as e:
                self._log_async(f"Couldn't check for a new Mod Loader: {e}", "dim" if auto else "error")
                return
            if update is None:
                if not auto:
                    self._log_async(f"Mod Loader {__version__} is the latest version.", "ok")
                return
            self.events.put(("app_update", update, False))
            if auto and self.store.settings.update_app_automatically:
                self._install_app(update)
            else:
                self._log_async(f"Mod Loader {update.version} is available (you have {__version__}).", "update")

        self.worker.submit("Checking for a new Mod Loader", task)

    def install_app_update(self) -> None:
        update = self.app_update
        if update and not self.app_update_ready:
            self.worker.submit(f"Downloading Mod Loader {update.version}", lambda: self._install_app(update))

    def _install_app(self, update: selfupdate.AppUpdate) -> None:
        """Runs on the worker thread."""
        self._log_async(f"Downloading Mod Loader {update.version}…")
        try:
            selfupdate.install(self.manager.client, update, self.store.mod_dir("staging", "mod-loader"),
                               self._progress_async)
        except (GitHubError, InstallError, OSError) as e:
            self._log_async(f"Couldn't update Mod Loader: {e}", "error")
            return
        finally:
            self._progress_async(0, 0)
        self.events.put(("app_update", update, True))
        self._log_async(f"Mod Loader {update.version} is installed. It starts the next time Mod Loader opens, "
                        "or click Restart now.", "ok")

    def _show_banner(self) -> None:
        update = self.app_update
        if update is None:
            self.banner.pack_forget()
            return
        if self.app_update_ready:
            self.banner_var.set(f"Mod Loader {update.version} is installed. Restart to start using it.")
            self.banner_button.configure(text="Restart now")
        else:
            self.banner_var.set(f"Mod Loader {update.version} is available. You have {__version__}.")
            self.banner_button.configure(text="Update now")
        self.banner.pack(fill="x", before=self.vpane)

    def _banner_action(self) -> None:
        if not self.app_update_ready:
            self.install_app_update()
            return
        if self.worker.busy and not messagebox.askyesno(
                "Restart?", f"Mod Loader is still busy ({self.worker.current or 'working'}). Restarting now can "
                            "leave a mod half-installed. Restart anyway?", parent=self):
            return
        try:
            selfupdate.restart()
        except OSError as e:
            messagebox.showerror("Mod Loader", f"Couldn't restart Mod Loader: {e}\n\nClose it and open it again.",
                                 parent=self)
            return
        self.destroy()

    # -- Settings and closing --------------------------------------------------------

    def open_settings(self) -> None:
        dialog = SettingsDialog(self)
        if dialog.result is None:
            return
        with self.store.lock:
            for key, value in dialog.result.items():
                setattr(self.store.settings, key, value)
            self.store.save()
        self._schedule_periodic()
        self.log("Settings saved.", "ok")

    def _on_close(self) -> None:
        if self.worker.busy and not messagebox.askyesno(
                "Quit?", f"Mod Loader is still busy ({self.worker.current or 'working'}). "
                         "Quitting now can leave a mod half-installed. Quit anyway?", parent=self):
            return
        self.destroy()


# -- Dialogs ----------------------------------------------------------------------

class Dialog(tk.Toplevel):
    def __init__(self, app: App, title: str):
        super().__init__(app)
        self.app = app
        self.result = None
        self.withdraw()
        self.title(title)
        self.transient(app)
        self.resizable(False, False)
        self.body = ttk.Frame(self, padding=app.px(16))
        self.body.pack(fill="both", expand=True)
        self.body.columnconfigure(1, weight=1)
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.bind("<Escape>", lambda e: self.cancel())

    def label(self, row: int, text: str, sticky: str = "w") -> None:
        ttk.Label(self.body, text=text).grid(row=row, column=0, sticky=sticky, padx=(0, self.app.px(12)),
                                             pady=(self.app.px(4), 0))

    def hint(self, row: int, text: str = "", var: tk.StringVar | None = None) -> ttk.Label:
        label = ttk.Label(self.body, text=text, textvariable=var, foreground=GREY,
                          wraplength=self.app.px(430), justify="left")
        label.grid(row=row, column=1, columnspan=2, sticky="w", pady=(self.app.px(1), self.app.px(6)))
        return label

    def show(self, focus: tk.Widget, ok_text: str = "OK", cancel_text: str = "Cancel") -> None:
        px = self.app.px
        buttons = ttk.Frame(self, padding=(px(16), 0, px(16), px(16)))
        buttons.pack(fill="x")
        ttk.Button(buttons, text=cancel_text, command=self.cancel).pack(side="right")
        ttk.Button(buttons, text=ok_text, command=self.ok, default="active").pack(side="right", padx=(0, px(6)))
        self.bind("<Return>", lambda e: self.ok())
        self.update_idletasks()
        width, height = self.winfo_reqwidth(), self.winfo_reqheight()
        x = self.app.winfo_rootx() + max(0, (self.app.winfo_width() - width) // 2)
        y = self.app.winfo_rooty() + max(0, (self.app.winfo_height() - height) // 3)
        self.geometry(f"+{x}+{y}")
        self.deiconify()
        self.grab_set()
        focus.focus_set()
        self.wait_window()

    def error(self, message: str) -> None:
        messagebox.showerror("Mod Loader", message, parent=self)

    def ok(self) -> None:
        raise NotImplementedError

    def cancel(self) -> None:
        self.result = None
        self.destroy()


class GameDialog(Dialog):
    """Add or edit a game. Lists installed games and works out the mods folder."""

    def __init__(self, app: App, title: str, game: Game | None = None):
        super().__init__(app, title)
        px = app.px
        self.editing = game
        self.name_var = tk.StringVar(value=game.name if game else "")
        self.install_var = tk.StringVar(value=game.install_dir if game else "")
        self.dir_var = tk.StringVar(value=game.mods_dir if game else "")
        self.launch_var = tk.StringVar(value=game.launch_path if game else "")
        self.args_var = tk.StringVar(value=game.launch_args if game else "")
        self.found: list[FoundGame] = []
        self.suggestions: list[Suggestion] = []
        self._auto_mods = ""  # the mods folder we filled in, so we know we may replace it
        self._detect_job: str | None = None
        self._scan_results: queue.Queue = queue.Queue()
        b = self.body

        self.label(0, "Installed game")
        self.pick_var = tk.StringVar(value="Looking for installed games…")
        self.pick = ttk.Combobox(b, textvariable=self.pick_var, state="disabled", width=48, height=18)
        self.pick.grid(row=0, column=1, columnspan=2, sticky="ew", pady=(px(4), 0))
        self.pick.bind("<<ComboboxSelected>>", self._on_pick)
        self.hint(1, "Found from Steam, Epic, GOG and Minecraft launchers. Pick one to fill in everything "
                     "below, or fill it in yourself.")

        self.label(2, "Name")
        name_entry = ttk.Entry(b, textvariable=self.name_var, width=50)
        name_entry.grid(row=2, column=1, columnspan=2, sticky="ew", pady=(px(4), px(8)))

        self.label(3, "Game folder")
        ttk.Entry(b, textvariable=self.install_var, width=50).grid(row=3, column=1, sticky="ew", pady=(px(4), 0))
        ttk.Button(b, text="Browse…", command=self._browse_install).grid(row=3, column=2, padx=(px(6), 0),
                                                                        pady=(px(4), 0))
        self.hint(4, "Where the game is installed. Mod Loader looks inside it to find the mods folder.")

        self.label(5, "Mods folder")
        self.mods_box = ttk.Combobox(b, textvariable=self.dir_var, width=48, height=10)
        self.mods_box.grid(row=5, column=1, sticky="ew", pady=(px(4), 0))
        ttk.Button(b, text="Browse…", command=self._browse_dir).grid(row=5, column=2, padx=(px(6), 0),
                                                                    pady=(px(4), 0))
        self.reason_var = tk.StringVar()
        self.reason = self.hint(6, var=self.reason_var)
        self.warn_var = tk.StringVar()
        self.warn = ttk.Label(b, textvariable=self.warn_var, foreground=RED, wraplength=px(430), justify="left")
        self.warn.grid(row=7, column=1, columnspan=2, sticky="w", pady=(0, px(6)))
        self.warn.grid_remove()

        self.label(8, "Launcher")
        ttk.Entry(b, textvariable=self.launch_var, width=50).grid(row=8, column=1, sticky="ew", pady=(px(4), 0))
        ttk.Button(b, text="Browse…", command=self._browse_launcher).grid(row=8, column=2, padx=(px(6), 0),
                                                                         pady=(px(4), 0))
        self.hint(9, "Optional. The game's .exe or shortcut, or a link like steam://rungameid/892970. "
                     "Needed for “Update & play” and desktop shortcuts.")

        self.label(10, "Arguments")
        ttk.Entry(b, textvariable=self.args_var, width=50).grid(row=10, column=1, columnspan=2, sticky="ew",
                                                                pady=(px(4), 0))
        self.hint(11, "Optional. Extra command-line options for the game.")

        self.install_var.trace_add("write", lambda *_: self._schedule_detect())
        self.dir_var.trace_add("write", lambda *_: self._update_reason())
        if self.install_var.get():
            self._detect()
        else:
            self._update_reason()

        threading.Thread(target=lambda: self._scan_results.put(find_installed_games()), daemon=True).start()
        self.after(100, self._poll_scan)
        self.show(name_entry, "Save" if game else "Add game")

    # -- Installed games -----------------------------------------------------------

    def _poll_scan(self) -> None:
        if not self.winfo_exists():
            return
        try:
            self.found = self._scan_results.get_nowait()
        except queue.Empty:
            self.after(100, self._poll_scan)
            return
        if not self.found:
            self.pick_var.set("No installed games found. Fill in the details below.")
            return
        added = [g.install_dir for g in self.app.store.games if g.install_dir]
        labels = [g.label + ("   ✓ added" if any(_same_path(g.install_dir, a) for a in added) else "")
                  for g in self.found]
        self.pick.configure(values=labels, state="readonly")
        current = self.install_var.get().strip()
        match = next((i for i, g in enumerate(self.found) if current and _same_path(g.install_dir, current)), None)
        if match is not None:
            self.pick.current(match)
        else:
            self.pick_var.set(f"Choose one of {len(self.found)} installed games…")

    def _on_pick(self, _event=None) -> None:
        index = self.pick.current()
        if index < 0:
            return
        found = self.found[index]
        self.name_var.set(found.name)
        self.launch_var.set(found.launch)  # blank for games without one, so an earlier pick doesn't linger
        self.install_var.set(found.install_dir)
        self._detect()

    # -- Mods folder detection -------------------------------------------------------

    def _schedule_detect(self) -> None:
        if self._detect_job:
            self.after_cancel(self._detect_job)
        self._detect_job = self.after(400, self._detect)

    def _detect(self) -> None:
        if self._detect_job:
            self.after_cancel(self._detect_job)
            self._detect_job = None
        folder = os.path.expandvars(self.install_var.get().strip().strip('"'))
        self.suggestions = suggest_mods_folders(folder, steam_app_id(self.launch_var.get())) if folder else []
        self.mods_box.configure(values=[s.path for s in self.suggestions])
        current = self.dir_var.get().strip()
        if self.suggestions and (not current or current == self._auto_mods):
            self._auto_mods = self.suggestions[0].path
            self.dir_var.set(self._auto_mods)
        self._update_reason()
        cheat = anti_cheat(folder) if folder else ""
        if cheat:
            self.warn_var.set(f"⚠ This game uses {cheat}. Mods in online games can get your account banned; "
                              "only use them where the game allows it.")
            self.warn.grid()
        else:
            self.warn_var.set("")
            self.warn.grid_remove()

    def _update_reason(self) -> None:
        value = os.path.expandvars(self.dir_var.get().strip().strip('"'))
        if not value:
            self.reason_var.set("Pick an installed game or a game folder and this fills in by itself, "
                                "or choose the folder the game loads mods from.")
            self.reason.configure(foreground=GREY)
            return
        match = next((s for s in self.suggestions if _same_path(s.path, value)), None)
        if match is None:
            text, color = "Your own choice.", GREY
        elif match.score >= 85:
            text, color = "✓ " + match.reason, GREEN
        elif match.score >= 50:
            text, color = match.reason, GREY
        else:
            text, color = ("No mod loader or mods folder found, so this is the game's own folder. "
                           "Check the mod's instructions for where its files go."), AMBER
        if not os.path.isdir(value):
            text += " It doesn't exist yet; it'll be created when the first mod is installed."
        others = sum(1 for s in self.suggestions if s.score >= 50 and not _same_path(s.path, value))
        if others:
            text += f" {others} other likely folder{'s' * (others != 1)} in the list."
        self.reason_var.set(text)
        self.reason.configure(foreground=color)

    # -- Browsing ----------------------------------------------------------------------

    def _browse_install(self) -> None:
        start = self.install_var.get().strip()
        folder = filedialog.askdirectory(parent=self, title="Choose the folder the game is installed in",
                                         initialdir=start if os.path.isdir(start) else None)
        if folder:
            self.install_var.set(os.path.normpath(folder))
            if not self.name_var.get().strip():
                self.name_var.set(os.path.basename(os.path.normpath(folder)))
            self._detect()

    def _browse_dir(self) -> None:
        start = self.dir_var.get().strip() or self.install_var.get().strip()
        folder = filedialog.askdirectory(parent=self, title="Choose the folder the game loads mods from",
                                         initialdir=start if os.path.isdir(start) else None, mustexist=False)
        if folder:
            self.dir_var.set(os.path.normpath(folder))

    def _browse_launcher(self) -> None:
        path = filedialog.askopenfilename(parent=self, title="Choose how to start the game", filetypes=[
            ("Programs and shortcuts", "*.exe *.lnk *.url *.bat *.cmd"), ("All files", "*.*")])
        if not path:
            return
        path = os.path.normpath(path)
        self.launch_var.set(path)
        if path.lower().endswith(".exe"):
            if not self.name_var.get().strip():
                self.name_var.set(os.path.splitext(os.path.basename(path))[0])
            if not self.install_var.get().strip():
                self.install_var.set(os.path.dirname(path))
                self._detect()

    def ok(self) -> None:
        name = self.name_var.get().strip()
        folder = os.path.expandvars(self.dir_var.get().strip().strip('"'))
        install = os.path.expandvars(self.install_var.get().strip().strip('"'))
        if not name:
            return self.error("Give the game a name.")
        if not folder:
            return self.error("Choose the folder the game loads mods from, or pick the game folder "
                              "so Mod Loader can find it.")
        if not os.path.isabs(folder):
            return self.error("Use the full path of the mods folder, e.g. D:\\Games\\MyGame\\Mods.")
        if os.path.isfile(folder):
            return self.error("That's a file. Choose the folder that holds the mods.")
        self.result = {
            "name": name,
            "mods_dir": os.path.normpath(folder),
            "install_dir": os.path.normpath(install) if install else "",
            "launch_path": self.launch_var.get().strip().strip('"'),
            "launch_args": self.args_var.get().strip(),
        }
        self.destroy()


def _same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


class ModDialog(Dialog):
    def __init__(self, app: App, game: Game, mod: Mod | None = None):
        super().__init__(app, f"Edit {mod.display_name}" if mod else f"Add a mod to {game.name}")
        px = app.px
        self.game = game
        self.repo_var = tk.StringVar(value=mod.repo if mod else "")
        self.name_var = tk.StringVar(value=mod.name if mod else "")
        self.pattern_var = tk.StringVar(value=mod.asset_pattern if mod else "")
        self.subdir_var = tk.StringVar(value=mod.subdir if mod else "")
        self.extract_var = tk.BooleanVar(value=mod.extract if mod else True)
        self.strip_var = tk.BooleanVar(value=mod.strip_root if mod else False)
        self.pre_var = tk.BooleanVar(value=mod.include_prereleases if mod else False)
        self.auto_var = tk.BooleanVar(value=mod.auto_update if mod else True)
        self.editing = mod
        self.release: dict | None = None
        self.release_repo = ""
        self.requires: list[dict] | None = None  # what the looked-up release needs (None = unknown)
        self._results: queue.Queue = queue.Queue()
        b = self.body

        self.label(0, "GitHub repository")
        repo_entry = ttk.Entry(b, textvariable=self.repo_var, width=50)
        repo_entry.grid(row=0, column=1, sticky="ew", pady=(px(4), 0))
        self.lookup_button = ttk.Button(b, text="Look up", command=self.lookup)
        self.lookup_button.grid(row=0, column=2, padx=(px(6), 0), pady=(px(4), 0))
        self.lookup_var = tk.StringVar(
            value=(f"Latest known release: {mod.latest_tag}." if mod and mod.latest_tag else
                   "owner/name, or paste the repository's github.com link. "
                   "“Look up” shows its latest release and files."))
        self.lookup_label = self.hint(1, var=self.lookup_var)

        self.label(2, "Name")
        ttk.Entry(b, textvariable=self.name_var, width=50).grid(row=2, column=1, columnspan=2, sticky="ew",
                                                                pady=(px(4), 0))
        self.hint(3, "Optional. Shown in the mod list.")

        self.label(4, "Release file")
        self.pattern_box = ttk.Combobox(b, textvariable=self.pattern_var, values=[AUTO_LABEL], width=48)
        self.pattern_box.grid(row=4, column=1, columnspan=2, sticky="ew", pady=(px(4), 0))
        self.pattern_box.bind("<<ComboboxSelected>>", self._on_pick)
        self.pattern_hint = tk.StringVar(value="Blank picks automatically. Wildcards work (MyMod-*.zip); separate "
                                               "several files with commas; “source” means the source code zip.")
        self.hint(5, var=self.pattern_hint)

        self.label(6, "Install into")
        ttk.Entry(b, textvariable=self.subdir_var, width=50).grid(row=6, column=1, columnspan=2, sticky="ew",
                                                                  pady=(px(4), 0))
        self.hint(7, f"Optional subfolder inside {game.mods_dir}")

        options = ttk.Frame(b)
        options.grid(row=8, column=1, columnspan=2, sticky="w", pady=(px(4), 0))
        for row, (text, var) in enumerate((
            ("Unpack .zip files (a .jar, .pak or .dll is always copied as-is)", self.extract_var),
            ("If a zip holds a single folder, install what's inside it", self.strip_var),
            ("Include pre-releases", self.pre_var),
            ("Update automatically", self.auto_var),
        )):
            ttk.Checkbutton(options, text=text, variable=var).grid(row=row, column=0, sticky="w", pady=px(1))
        self.show(repo_entry, "Save" if mod else "Add & install")

    def lookup(self) -> None:
        try:
            repo = parse_repo(self.repo_var.get())
        except ValueError as e:
            self.lookup_var.set(str(e))
            self.lookup_label.configure(foreground=RED)
            return
        self.repo_var.set(repo)
        self.lookup_button.state(["disabled"])
        self.lookup_label.configure(foreground=GREY)
        self.lookup_var.set(f"Looking up {repo}…")
        client, pre = self.app.manager.client, self.pre_var.get()

        def work():
            try:
                release = client.latest_release(repo, pre)
                try:
                    text = client.read_file(repo, requires.MANIFEST_NAME, release.get("tag_name") or "")
                    needs = requires.parse_manifest(text, repo)[0] if text else []
                except GitHubError:
                    needs = None  # not known yet; checked again when it installs
                self._results.put(("ok", repo, (release, needs)))
            except GitHubError as e:
                self._results.put(("error", repo, str(e)))
            except Exception as e:  # noqa: BLE001
                self._results.put(("error", repo, f"Look-up failed: {e!r}"))

        threading.Thread(target=work, daemon=True).start()
        self.after(100, self._poll)

    def _poll(self) -> None:
        if not self.winfo_exists():
            return
        try:
            kind, repo, payload = self._results.get_nowait()
        except queue.Empty:
            self.after(100, self._poll)
            return
        self.lookup_button.state(["!disabled"])
        if kind == "error":
            self.release = None
            self.lookup_var.set(payload)
            self.lookup_label.configure(foreground=RED)
            return
        self.lookup_label.configure(foreground=GREEN)
        release, self.requires = payload
        self.release = release
        self.release_repo = repo
        names = [a["name"] for a in release.get("assets") or []]
        files = [n for n in names if not is_side_file(n)]
        tag = release.get("tag_name") or "?"
        when = friendly_time(release.get("published_at"))
        if not files:
            message = f"Latest release: {tag} ({when}). It has no attached files, so the source code zip will be used."
        elif len(files) == 1:
            message = f"Latest release: {tag} ({when}), with one file: {files[0]}"
        else:
            message = (f"Latest release: {tag} ({when}), with {len(files)} files. "
                       "Pick the one to install under “Release file”.")
            if not self.pattern_var.get().strip():
                self.lookup_label.configure(foreground=AMBER)
                self.pattern_box.focus_set()
        if self.requires:
            store, parts, missing = self.app.store, [], False
            for req in self.requires:
                have = requires.find(store, self.game.id, req["repo"])
                if have and have.enabled:
                    parts.append(f"{requires.label(req)} (already added)")
                else:
                    parts.append(requires.label(req) + (" (disabled)" if have else ""))
                    missing = True
            message += "\nNeeds " + ", ".join(parts) + "."
            if missing:
                message += " You'll be asked to add it." if len(self.requires) == 1 else \
                    " You'll be asked to add what's missing."
        self.lookup_var.set(message)
        self.pattern_box.configure(values=[AUTO_LABEL, *names, "source"])
        if not self.name_var.get().strip():
            self.name_var.set(repo.split("/")[1])

    def _on_pick(self, _event=None) -> None:
        value = self.pattern_var.get()
        if value == AUTO_LABEL:
            self.pattern_var.set("")
            return
        names = [a["name"] for a in (self.release or {}).get("assets") or []]
        if value in names:
            glob = version_glob(value, names, (self.release or {}).get("tag_name") or "")
            self.pattern_var.set(glob)
            if glob != value:
                self.pattern_hint.set(f"Using “{glob}” so future versions of this file still match. "
                                      "You can edit it.")
            else:
                self.pattern_hint.set("Using the exact file name. If future versions rename the file, "
                                      "replace the changing part with *.")

    def ok(self) -> None:
        try:
            repo = parse_repo(self.repo_var.get())
            subdir = clean_subdir(self.subdir_var.get())
        except (ValueError, InstallError) as e:
            return self.error(str(e))
        pattern = self.pattern_var.get().strip()
        if pattern == AUTO_LABEL:
            pattern = ""
        if self.release is not None and self.release_repo == repo:
            try:
                select_assets(self.release, pattern)
            except GitHubError as e:
                return self.error(str(e))
        self.result = {
            "repo": repo,
            "name": self.name_var.get().strip(),
            "asset_pattern": pattern,
            "subdir": subdir,
            "extract": self.extract_var.get(),
            "strip_root": self.strip_var.get(),
            "include_prereleases": self.pre_var.get(),
            "auto_update": self.auto_var.get(),
        }
        if self.editing is None and self.release is not None and self.release_repo == repo \
                and self.requires is not None:
            self.result["requires"] = self.requires
            self.result["requires_release_id"] = self.release.get("id")
        self.destroy()


def _name_list(mods: list[Mod], limit: int = 8) -> str:
    names = [m.display_name for m in mods[:limit]]
    more = len(mods) - len(names)
    return ", ".join(names) + (f" and {more} more" if more > 0 else "")


def _mod_table(parent: tk.Misc, px, columns: tuple[tuple[str, str, int], ...], rows: int) -> ttk.Treeview:
    """A small scrolling table of mods for a dialog. Columns are (id, title, width)."""
    box = ttk.Frame(parent)
    box.columnconfigure(0, weight=1)
    tree = ttk.Treeview(box, columns=[c[0] for c in columns], show="headings", selectmode="browse", height=rows)
    for col, title, width in columns:
        tree.heading(col, text=title, anchor="w")
        tree.column(col, width=px(width), minwidth=px(24), stretch=col == columns[-1][0], anchor="w")
    scroll = ttk.Scrollbar(box, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=scroll.set)
    tree.grid(row=0, column=0, sticky="nsew")
    scroll.grid(row=0, column=1, sticky="ns")
    tree.tag_configure("dim", foreground=GREY)
    tree.tag_configure("new", foreground=BLUE)
    tree.box = box  # what the dialog places
    return tree


class CollectionDialog(Dialog):
    """Name a collection and tick the mods that are in it."""

    TICKED, UNTICKED = "☑", "☐"

    def __init__(self, app: App, game: Game, coll: Collection | None = None, ticked: list[str] | tuple = ()):
        super().__init__(app, f"Edit {coll.name}" if coll else f"New collection for {game.name}")
        px = app.px
        self.game, self.editing = game, coll
        self.mods = app.store.mods_for(game.id)
        self.ticked = set(coll.mod_ids if coll else ticked) & {m.id for m in self.mods}
        self.name_var = tk.StringVar(value=coll.name if coll else "")
        self.count_var = tk.StringVar()
        b = self.body

        self.label(0, "Name")
        name_entry = ttk.Entry(b, textvariable=self.name_var, width=60)
        name_entry.grid(row=0, column=1, sticky="ew", pady=(px(4), 0))
        self.hint(1, "For example “Co-op with friends” or “Just the basics”.")

        self.label(2, "Mods", sticky="nw")
        self.tree = _mod_table(b, px, (("tick", "", 30), ("name", "Mod", 170), ("repo", "GitHub repository", 220)),
                               rows=min(12, max(6, len(self.mods))))
        self.tree.box.grid(row=2, column=1, sticky="nsew", pady=(px(4), 0))
        for mod in self.mods:
            self.tree.insert("", "end", iid=mod.id, values=self._row(mod), tags=() if mod.enabled else ("dim",))
        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<space>", lambda e: self._toggle(self.tree.focus()))

        under = ttk.Frame(b)
        under.grid(row=3, column=1, sticky="ew", pady=(px(6), 0))
        ttk.Button(under, text="Tick all", command=lambda: self._set_all(True)).pack(side="left")
        ttk.Button(under, text="Untick all", command=lambda: self._set_all(False)).pack(side="left", padx=(px(6), 0))
        ttk.Label(under, textvariable=self.count_var, foreground=GREY).pack(side="right")
        self.hint(4, f"Tick the mods that belong in this collection. A mod can be in several collections. "
                     f"“Use” then enables the ticked mods and disables {game.name}'s other mods."
                  if self.mods else
                  f"{game.name} has no mods yet. You can still make the collection: while it's shown, "
                  f"“Add mod” adds new mods straight into it.")
        self._update_count()
        self.show(name_entry, "Save" if coll else "Create")

    def _row(self, mod: Mod) -> tuple[str, str, str]:
        name = mod.display_name + ("" if mod.enabled else "  (disabled)")
        return (self.TICKED if mod.id in self.ticked else self.UNTICKED, name, mod.repo)

    def _on_click(self, event) -> None:
        if self.tree.identify_region(event.x, event.y) == "cell":
            self._toggle(self.tree.identify_row(event.y))

    def _toggle(self, mod_id: str) -> None:
        mod = next((m for m in self.mods if m.id == mod_id), None)
        if mod is None:
            return
        self.ticked.symmetric_difference_update({mod.id})
        self.tree.item(mod.id, values=self._row(mod))
        self._update_count()

    def _set_all(self, ticked: bool) -> None:
        self.ticked = {m.id for m in self.mods} if ticked else set()
        for mod in self.mods:
            self.tree.item(mod.id, values=self._row(mod))
        self._update_count()

    def _update_count(self) -> None:
        self.count_var.set(f"{len(self.ticked)} of {len(self.mods)} ticked" if self.mods else "")

    def ok(self) -> None:
        name = collection.clean_name(self.name_var.get())
        if not name:
            return self.error("Give the collection a name.")
        same = collection.find_by_name(self.app.store, self.game.id, name)
        if same is not None and same is not self.editing:
            return self.error(f"{self.game.name} already has a collection called “{same.name}”. "
                              "Pick another name.")
        self.result = {"name": name, "mod_ids": [m.id for m in self.mods if m.id in self.ticked]}
        self.destroy()


class ExportDialog(Dialog):
    """Shows a set of mods as a code to copy and send to someone."""

    def __init__(self, app: App, name: str, game: Game, mods: list[Mod]):
        super().__init__(app, f"Export {name}")
        px = app.px
        self.code = collection.export_code(name, game.name, mods)
        b = self.body
        count = len(mods)
        ttk.Label(b, text=f"“{name}”  ·  {count} mod{'s' * (count != 1)} for {game.name}",
                  style="Head.TLabel").grid(row=0, column=0, columnspan=3, sticky="w")
        self.text = ScrolledText(b, width=66, height=7, wrap="char", font=("Consolas", 9),
                                 relief="solid", borderwidth=1)
        self.text.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(px(8), px(6)))
        self.text.insert("1.0", self.code)
        self.text.configure(state="disabled")
        self.text.bind("<Button-1>", lambda e: self.text.focus_set())  # so it can be selected and copied by hand

        note = ("Send this code to someone. In Mod Loader they select their game, choose Collections → "
                "Import code, and paste it; their copy then adds and installs these mods. The code holds "
                "which mods and their settings, not the files or your GitHub token.")
        if len(self.code) > 2000:
            note += (f" It's {len(self.code):,} characters long, more than a Discord message holds, "
                     "so Discord sends it as a text file.")
        ttk.Label(b, text=note, foreground=GREY, wraplength=px(480), justify="left"
                  ).grid(row=2, column=0, columnspan=3, sticky="w")
        self.copied_var = tk.StringVar()
        ttk.Label(b, textvariable=self.copied_var, foreground=GREEN).grid(row=3, column=0, columnspan=3,
                                                                          sticky="w", pady=(px(6), 0))
        self.show(self.text, "Copy code", "Close")

    def ok(self) -> None:
        self.clipboard_clear()
        self.clipboard_append(self.code)
        self.update_idletasks()
        self.copied_var.set("✓ Copied. Paste it into a message to whoever you're sharing with.")


class ImportDialog(Dialog):
    """Paste a collection code, see what it holds, and add it to a game."""

    def __init__(self, app: App, game: Game):
        super().__init__(app, f"Import a collection into {game.name}")
        px = app.px
        self.game = game
        self.shared: collection.Shared | None = None
        self._job: str | None = None
        self._auto_name = ""  # the name we filled in, so we know we may replace it
        self.name_var = tk.StringVar()
        self.status_var = tk.StringVar()
        self.name_note_var = tk.StringVar()
        b = self.body

        self.label(0, "Code", sticky="nw")
        self.text = ScrolledText(b, width=60, height=5, wrap="char", font=("Consolas", 9),
                                 relief="solid", borderwidth=1)
        self.text.grid(row=0, column=1, sticky="ew", pady=(px(4), 0))
        ttk.Button(b, text="Paste", command=self._paste).grid(row=0, column=2, sticky="n", padx=(px(6), 0),
                                                               pady=(px(4), 0))
        self.status = self.hint(1, var=self.status_var)

        self.label(2, "Name")
        ttk.Entry(b, textvariable=self.name_var, width=50).grid(row=2, column=1, columnspan=2, sticky="ew",
                                                                pady=(px(4), 0))
        self.name_note = self.hint(3, var=self.name_note_var)

        self.label(4, "Mods", sticky="nw")
        self.tree = _mod_table(b, px, (("name", "Mod", 150), ("repo", "GitHub repository", 200),
                                       ("state", "Status", 130)), rows=8)
        self.tree.box.grid(row=4, column=1, columnspan=2, sticky="nsew", pady=(px(4), 0))
        self.hint(5, "Mods are programs that run inside your game. Only import codes from people you trust.")

        self.text.bind("<<Modified>>", self._on_modified)
        self.name_var.trace_add("write", lambda *_: self._update_name_note())
        try:
            waiting = self.clipboard_get()
        except tk.TclError:  # nothing on the clipboard, or not text
            waiting = ""
        if collection.looks_like_code(waiting):
            self.text.insert("1.0", waiting.strip())
        self._parse()
        self.show(self.text, "Import")

    def _paste(self) -> None:
        try:
            pasted = self.clipboard_get()
        except tk.TclError:
            pasted = ""
        if pasted.strip():
            self.text.delete("1.0", "end")
            self.text.insert("1.0", pasted.strip())
        self._parse()

    def _on_modified(self, _event=None) -> None:
        if not self.text.edit_modified():
            return
        self.text.edit_modified(False)
        if self._job:
            self.after_cancel(self._job)
        self._job = self.after(250, self._parse)

    def _parse(self) -> None:
        if self._job:
            self.after_cancel(self._job)
            self._job = None
        self.tree.delete(*self.tree.get_children())
        self.shared = None
        text = self.text.get("1.0", "end").strip()
        if not text:
            self.status_var.set(f"Paste the code someone sent you. It starts with {collection.PREFIX}"
                                f"{collection.FORMAT}: and is one long line of letters and numbers.")
            self.status.configure(foreground=GREY)
            self._update_name_note()
            return
        try:
            shared = collection.parse_code(text)
        except collection.CodeError as e:
            self.status_var.set(str(e))
            self.status.configure(foreground=RED)
            self._update_name_note()
            return
        self.shared = shared
        store, new = self.app.store, 0
        for index, entry in enumerate(shared.mods):
            have = requires.find(store, self.game.id, entry["repo"])
            new += have is None
            state = "Will be added" if have is None else "Already added" if have.enabled else "Added, disabled"
            self.tree.insert("", "end", iid=str(index), values=(requires.label(entry), entry["repo"], state),
                             tags=("new",) if have is None else ("dim",))
        count = len(shared.mods)
        message = (f"✓ “{shared.name}”: {count} mod{'s' * (count != 1)}"
                   + (f", made for {shared.game}" if shared.game else "") + ". "
                   + (f"{new} will be added to {self.game.name} and installed." if new else
                      f"{self.game.name} already has {'it' if count == 1 else 'all of them'}."))
        color = GREEN
        if shared.game and shared.game.casefold() != self.game.name.casefold():
            message += f" You're adding it to {self.game.name}, so check these mods are meant for that game."
            color = AMBER
        if shared.problems:
            n = len(shared.problems)
            message += f" {n} entr{'y' if n == 1 else 'ies'} in the code couldn't be used; the log will say why."
            color = AMBER
        self.status_var.set(message)
        self.status.configure(foreground=color)
        if not self.name_var.get().strip() or self.name_var.get() == self._auto_name:
            self._auto_name = shared.name
            self.name_var.set(shared.name)
        self._update_name_note()

    def _update_name_note(self) -> None:
        same = collection.find_by_name(self.app.store, self.game.id, self.name_var.get())
        if same is not None and self.shared is not None:
            self.name_note_var.set(f"{self.game.name} already has “{same.name}”. Importing replaces its list of "
                                   "mods (no mod is removed). Change the name to keep both.")
            self.name_note.configure(foreground=AMBER)
        else:
            self.name_note_var.set("What the collection is called in your list.")
            self.name_note.configure(foreground=GREY)

    def ok(self) -> None:
        self._parse()
        if self.shared is None:
            return self.error(self.status_var.get() if self.text.get("1.0", "end").strip()
                              else "Paste a collection code first.")
        self.result = (self.shared, collection.clean_name(self.name_var.get()) or self.shared.name)
        self.destroy()


class SettingsDialog(Dialog):
    def __init__(self, app: App):
        super().__init__(app, "Settings")
        px = app.px
        s = app.store.settings
        self.token_var = tk.StringVar(value=s.github_token)
        self.reveal_var = tk.BooleanVar(value=False)
        self.startup_var = tk.BooleanVar(value=s.check_on_startup)
        self.auto_var = tk.BooleanVar(value=s.auto_install_updates)
        self.interval_var = tk.StringVar(value=str(s.check_interval_minutes))
        self.app_auto_var = tk.BooleanVar(value=s.update_app_automatically)
        b = self.body

        self.label(0, "GitHub token")
        self.token_entry = ttk.Entry(b, textvariable=self.token_var, width=46, show="•")
        self.token_entry.grid(row=0, column=1, sticky="ew", pady=(px(4), 0))
        ttk.Checkbutton(b, text="Show", variable=self.reveal_var,
                        command=lambda: self.token_entry.configure(show="" if self.reveal_var.get() else "•")
                        ).grid(row=0, column=2, padx=(px(6), 0), pady=(px(4), 0))
        self.hint(1, "Optional. Without a token GitHub allows 60 checks an hour; with one, 5,000, and private "
                     "repositories work. A fine-grained token with read-only access is enough. It's stored "
                     "unencrypted in your data folder.")
        link = ttk.Label(b, text="Create a token on github.com", foreground=BLUE, cursor="hand2")
        link.grid(row=2, column=1, sticky="w", pady=(0, px(10)))
        link.bind("<Button-1>", lambda e: webbrowser.open(TOKEN_URL))

        ttk.Separator(b).grid(row=3, column=0, columnspan=3, sticky="ew", pady=px(6))
        ttk.Checkbutton(b, text="Check for updates when Mod Loader opens", variable=self.startup_var
                        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=px(2))
        ttk.Checkbutton(b, text="Install updates automatically (for mods set to update automatically)",
                        variable=self.auto_var).grid(row=5, column=0, columnspan=3, sticky="w", pady=px(2))
        interval = ttk.Frame(b)
        interval.grid(row=6, column=0, columnspan=3, sticky="w", pady=(px(4), px(2)))
        ttk.Label(interval, text="While open, check again every").pack(side="left")
        ttk.Spinbox(interval, from_=0, to=1440, increment=15, width=6, textvariable=self.interval_var
                    ).pack(side="left", padx=px(6))
        ttk.Label(interval, text="minutes (0 = never)").pack(side="left")

        ttk.Separator(b).grid(row=7, column=0, columnspan=3, sticky="ew", pady=px(8))
        self.label(8, "Mod Loader")
        ttk.Label(b, text=f"Version {__version__}", foreground=GREY).grid(row=8, column=1, sticky="w",
                                                                         pady=(px(4), 0))
        check = ttk.Button(b, text="Check now", command=lambda: app.check_app(auto=False))
        check.grid(row=8, column=2, padx=(px(6), 0), pady=(px(4), 0))
        blocked = selfupdate.blocked_reason()
        auto_box = ttk.Checkbutton(b, text="Keep Mod Loader itself up to date automatically",
                                   variable=self.app_auto_var)
        auto_box.grid(row=9, column=1, columnspan=2, sticky="w", pady=(px(4), 0))
        self.hint(10, blocked or "Mod Loader checks for its own new versions when it opens and installs them; "
                                 "they start the next time it opens.")
        if blocked:
            check.state(["disabled"])
            auto_box.state(["disabled"])

        ttk.Separator(b).grid(row=11, column=0, columnspan=3, sticky="ew", pady=px(8))
        self.label(12, "Data folder")
        ttk.Label(b, text=str(app.store.data_dir), foreground=GREY).grid(row=12, column=1, sticky="w",
                                                                         pady=(px(4), 0))
        ttk.Button(b, text="Open", command=lambda: system.open_path(app.store.data_dir)
                   ).grid(row=12, column=2, padx=(px(6), 0), pady=(px(4), 0))
        self.hint(13, "Settings, backups of files mods replaced, disabled mods, and modloader.log live here.")
        self.show(self.token_entry, "Save")

    def ok(self) -> None:
        try:
            minutes = int(self.interval_var.get().strip() or "0")
            if minutes < 0:
                raise ValueError
        except ValueError:
            return self.error("Enter how many minutes to wait between checks (0 turns it off).")
        self.result = {
            "github_token": self.token_var.get().strip(),
            "check_on_startup": self.startup_var.get(),
            "auto_install_updates": self.auto_var.get(),
            "check_interval_minutes": minutes,
            "update_app_automatically": self.app_auto_var.get(),
        }
        self.destroy()


# -- "Update & play" window (used by desktop shortcuts) ----------------------------

class PlayWindow(tk.Tk):
    def __init__(self, store: Store, game: Game):
        super().__init__()
        self.store, self.game = store, game
        self.events: queue.Queue = queue.Queue()
        self.manager = ModManager(store, log=lambda m, level="info": self.events.put(("log", m, level)),
                                  progress=lambda d, t: self.events.put(("progress", d, t)))
        self.scale = _scale_for(self)
        px = lambda v: int(v * self.scale)  # noqa: E731
        _apply_theme(self, self.scale)
        self.title(f"{game.name} · Mod Loader")
        self.resizable(False, False)

        frame = ttk.Frame(self, padding=px(18))
        frame.pack(fill="both", expand=True)
        self.head_var = tk.StringVar(value=f"Updating mods for {game.name}…")
        ttk.Label(frame, textvariable=self.head_var, style="Head.TLabel").pack(anchor="w")
        self.line_var = tk.StringVar(value="Checking GitHub for new releases")
        self.line = ttk.Label(frame, textvariable=self.line_var, foreground=GREY, wraplength=px(420),
                              justify="left")
        self.line.pack(anchor="w", fill="x", pady=(px(6), px(10)))
        self.bar = ttk.Progressbar(frame, length=px(420), mode="indeterminate", maximum=100)
        self.bar.pack(fill="x")
        self.bar.start(15)
        self.buttons = ttk.Frame(frame)
        self.buttons.pack(fill="x", pady=(px(12), 0))

        self.update_idletasks()
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        self.geometry(f"+{(self.winfo_screenwidth() - w) // 2}+{(self.winfo_screenheight() - h) // 3}")
        threading.Thread(target=self._work, daemon=True).start()
        self.after(100, self._pump)

    def _work(self) -> None:
        try:
            summary = self.manager.update_for_play(self.game)
        except Exception as e:  # noqa: BLE001
            logger.exception("Update before playing failed")
            summary = Summary(failed=[("Mod Loader", str(e))])
        self.events.put(("done", summary))

    def _pump(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == "log":
                    logger.log(LEVELS.get(event[2], logging.INFO), event[1])
                    if event[2] in ("info", "dim", "ok"):
                        self.line_var.set(event[1].strip())
                elif event[0] == "progress":
                    done, total = event[1], event[2]
                    if total:
                        self.bar.stop()
                        self.bar.configure(mode="determinate", value=done * 100 / total)
                    else:
                        self.bar.configure(mode="indeterminate")
                        self.bar.start(15)
                elif event[0] == "done":
                    self._finish(event[1])
                    return
        except queue.Empty:
            pass
        self.after(100, self._pump)

    def _button(self, text: str, command) -> None:
        ttk.Button(self.buttons, text=text, command=command).pack(side="right", padx=(int(6 * self.scale), 0))

    def _finish(self, summary: Summary) -> None:
        self.bar.stop()
        self.bar.configure(mode="determinate", value=100)
        if summary.failed:
            self.head_var.set("Some mods couldn't be updated")
            problems = "\n".join(f"• {name}: {error}" for name, error in summary.failed[:4])
            self.line_var.set(problems)
            self.line.configure(foreground=RED)
            self._button("Cancel", self.destroy)
            self._button("Play anyway", self._launch)
            return
        self._launch()

    def _launch(self) -> None:
        for child in self.buttons.winfo_children():
            child.destroy()
        if not self.game.launch_path:
            self.head_var.set("Mods are up to date")
            self.line_var.set("No launcher is set for this game. Open Mod Loader and edit the game to add one.")
            self._button("Close", self.destroy)
            return
        try:
            self.manager.launch(self.game)
        except (InstallError, OSError) as e:
            self.head_var.set("Couldn't start the game")
            self.line_var.set(str(e))
            self.line.configure(foreground=RED)
            self._button("Close", self.destroy)
            return
        self.head_var.set(f"Starting {self.game.name}…")
        self.line.configure(foreground=GREY)
        self.line_var.set("Mods are up to date.")
        self.after(1200, self.destroy)
