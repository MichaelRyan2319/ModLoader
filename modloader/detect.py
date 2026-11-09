"""Finding installed games, and working out where their mods go.

find_installed_games() reads what Steam, Epic, GOG and the Minecraft launchers
record about installed games. suggest_mods_folders() looks inside a game's
folder for mod loaders and engine layouts and ranks the likely mods folders.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class FoundGame:
    name: str
    install_dir: str
    source: str  # "Steam", "Epic", "GOG", "Prism Launcher", ...
    launch: str = ""

    @property
    def label(self) -> str:
        return f"{self.name}  ({self.source})"


@dataclass
class Suggestion:
    path: str
    reason: str
    score: int

    @property
    def exists(self) -> bool:
        return os.path.isdir(self.path)


# -- Small readers ------------------------------------------------------------------

_VDF_TOKEN = re.compile(r'"((?:[^"\\]|\\.)*)"|([{}])')


def parse_vdf(text: str) -> dict:
    """Parse Valve's KeyValues text format (libraryfolders.vdf, *.acf). Keys are lower-cased."""
    root: dict = {}
    stack = [root]
    key: str | None = None
    for match in _VDF_TOKEN.finditer(text):
        string, brace = match.group(1), match.group(2)
        if brace == "{":
            child: dict = {}
            if key is not None:
                stack[-1][key] = child
            stack.append(child)
            key = None
        elif brace == "}":
            if len(stack) > 1:
                stack.pop()
            key = None
        elif key is None:
            key = string.lower()
        else:
            stack[-1][key] = string.replace("\\\\", "\\")
            key = None
    return root


def _read_text(path: Path) -> str:
    try:
        return path.read_bytes().decode("utf-8-sig", "replace")
    except OSError:
        return ""


def _read_json(path: Path):
    """JSON from a launcher file, or None if it's missing or damaged."""
    text = _read_text(path).strip("\x00 \r\n\t")
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _reg_value(hive_name: str, key: str, value: str) -> str:
    if sys.platform != "win32":
        return ""
    import winreg
    try:
        with winreg.OpenKey(getattr(winreg, hive_name), key) as k:
            return str(winreg.QueryValueEx(k, value)[0])
    except OSError:
        return ""


def _env_path(name: str, fallback: str = "") -> Path | None:
    value = os.environ.get(name) or fallback
    return Path(value) if value else None


def documents_dir() -> Path:
    """The real Documents folder, even when OneDrive or the user moved it."""
    value = _reg_value("HKEY_CURRENT_USER",
                       r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders", "Personal")
    if value:
        return Path(os.path.expandvars(value))
    return Path.home() / "Documents"


def _same(a: str, b: str) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


# -- Steam ----------------------------------------------------------------------------

_STEAM_TOOL_IDS = {"228980", "1070560", "1391110", "1628350", "1493710", "250820", "1826330"}
_STEAM_TOOL_PREFIXES = ("steamworks common", "steam linux runtime", "proton", "steamvr")


def steam_roots() -> list[Path]:
    candidates = [
        _reg_value("HKEY_CURRENT_USER", r"Software\Valve\Steam", "SteamPath"),
        _reg_value("HKEY_LOCAL_MACHINE", r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
        _reg_value("HKEY_LOCAL_MACHINE", r"SOFTWARE\Valve\Steam", "InstallPath"),
    ]
    if sys.platform == "win32":
        candidates.append(os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "Steam"))
    else:
        candidates += [os.path.expanduser("~/.steam/steam"), os.path.expanduser("~/.local/share/Steam")]
    roots: list[Path] = []
    for c in candidates:
        if c and os.path.isdir(c) and not any(_same(c, str(r)) for r in roots):
            roots.append(Path(os.path.realpath(c)))  # realpath also fixes the letter case on Windows
    return roots


def find_steam_games(roots: list[Path] | None = None) -> list[FoundGame]:
    libraries: list[Path] = []

    def add_library(path: str) -> None:
        if os.path.isdir(path) and not any(_same(path, str(lib)) for lib in libraries):
            libraries.append(Path(os.path.normpath(path)))

    for root in roots if roots is not None else steam_roots():
        folders = parse_vdf(_read_text(root / "steamapps" / "libraryfolders.vdf")).get("libraryfolders", {})
        for entry in folders.values():
            path = entry.get("path") if isinstance(entry, dict) else entry  # old files list bare paths
            if isinstance(path, str):
                add_library(path)
        add_library(str(root))
    games: list[FoundGame] = []
    seen: set[str] = set()
    for library in libraries:
        steamapps = library / "steamapps"
        for manifest in sorted(steamapps.glob("appmanifest_*.acf")):
            state = parse_vdf(_read_text(manifest)).get("appstate", {})
            app_id, name, folder = state.get("appid", ""), state.get("name", ""), state.get("installdir", "")
            if not (app_id and name and folder) or app_id in seen:
                continue
            if app_id in _STEAM_TOOL_IDS or name.lower().startswith(_STEAM_TOOL_PREFIXES):
                continue
            install_dir = steamapps / "common" / folder
            if install_dir.is_dir():
                seen.add(app_id)
                games.append(FoundGame(name, str(install_dir), "Steam", f"steam://rungameid/{app_id}"))
    return games


def steam_app_id(launch: str) -> str:
    match = re.search(r"steam://(?:rungameid|run)/(\d+)", launch or "")
    return match.group(1) if match else ""


# -- Epic -------------------------------------------------------------------------------

def _epic_launch(namespace: str, item_id: str, app_name: str) -> str:
    if not (namespace and item_id and app_name):
        return ""
    return f"com.epicgames.launcher://apps/{namespace}%3A{item_id}%3A{app_name}?action=launch&silent=true"


def find_epic_games(program_data: Path | None = None) -> list[FoundGame]:
    base = program_data or _env_path("PROGRAMDATA", r"C:\ProgramData")
    if base is None:
        return []
    games: list[FoundGame] = []
    for item in sorted((base / "Epic" / "EpicGamesLauncher" / "Data" / "Manifests").glob("*.item")):
        data = _read_json(item)  # some launchers leave damaged, all-zero files behind
        if not isinstance(data, dict) or data.get("bIsIncompleteInstall"):
            continue
        if any(str(c).startswith("engines") for c in data.get("AppCategories") or []):
            continue  # Unreal Engine itself
        folder = data.get("InstallLocation") or ""
        if os.path.isdir(folder):
            games.append(FoundGame(
                data.get("DisplayName") or os.path.basename(folder), os.path.normpath(folder), "Epic",
                _epic_launch(data.get("CatalogNamespace", ""), data.get("CatalogItemId", ""), data.get("AppName", ""))))
    # Epic's second list catches games whose manifest is damaged (it has no display names).
    installed = _read_json(base / "Epic" / "UnrealEngineLauncher" / "LauncherInstalled.dat")
    entries = installed.get("InstallationList") if isinstance(installed, dict) else None
    for entry in entries or []:
        folder = entry.get("InstallLocation") or ""
        if (entry.get("NamespaceId") == "ue" or not os.path.isdir(folder)
                or any(_same(folder, g.install_dir) for g in games)):
            continue
        games.append(FoundGame(os.path.basename(os.path.normpath(folder)), os.path.normpath(folder), "Epic",
                               _epic_launch(entry.get("NamespaceId", ""), entry.get("ItemId", ""),
                                            entry.get("AppName", ""))))
    return games


# -- GOG ---------------------------------------------------------------------------------

def find_gog_games() -> list[FoundGame]:
    if sys.platform != "win32":
        return []
    import winreg
    games: list[FoundGame] = []
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\GOG.com\Games")
    except OSError:
        return []
    with root:
        index = 0
        while True:
            try:
                sub = winreg.EnumKey(root, index)
            except OSError:
                break
            index += 1
            try:
                with winreg.OpenKey(root, sub) as k:
                    values = {}
                    for name in ("gameName", "path", "exe", "dependsOn"):
                        try:
                            values[name] = str(winreg.QueryValueEx(k, name)[0])
                        except OSError:
                            values[name] = ""
            except OSError:
                continue
            if values["dependsOn"] or not os.path.isdir(values["path"]):
                continue  # DLC, or gone
            games.append(FoundGame(values["gameName"] or os.path.basename(values["path"]),
                                   os.path.normpath(values["path"]), "GOG", values["exe"]))
    return games


# -- Minecraft ---------------------------------------------------------------------------

def find_minecraft(appdata: Path | None = None, home: Path | None = None) -> list[FoundGame]:
    appdata = appdata or _env_path("APPDATA")
    home = home or Path.home()
    found: list[FoundGame] = []
    if appdata and (appdata / ".minecraft").is_dir():
        found.append(FoundGame("Minecraft", str(appdata / ".minecraft"), "Minecraft Launcher"))
    launchers = []
    if appdata:
        launchers += [("Prism Launcher", appdata / "PrismLauncher" / "instances"),
                      ("Modrinth App", appdata / "ModrinthApp" / "profiles")]
    launchers.append(("CurseForge", home / "curseforge" / "minecraft" / "Instances"))
    for launcher, base in launchers:
        if not base.is_dir():
            continue
        for instance in sorted(p for p in base.iterdir() if p.is_dir() and not p.name.startswith((".", "_"))):
            game_dir = next((d for d in (instance / ".minecraft", instance / "minecraft") if d.is_dir()), instance)
            found.append(FoundGame(f"Minecraft: {instance.name}", str(game_dir), launcher))
    return found


def find_installed_games() -> list[FoundGame]:
    """Every game the known launchers say is installed, sorted by name."""
    games: list[FoundGame] = []
    for finder in (find_steam_games, find_epic_games, find_gog_games, find_minecraft):
        try:
            games += finder()
        except Exception:  # noqa: BLE001 - one broken launcher shouldn't hide the others
            continue
    unique: list[FoundGame] = []
    for game in games:
        if not any(_same(game.install_dir, other.install_dir) for other in unique):
            unique.append(game)
    return sorted(unique, key=lambda g: g.name.casefold())


# -- Where do the mods go? -------------------------------------------------------------

# Games whose mods folder can't be found by looking: (Steam app ids, install folder names, path, title).
# Paths start with the game folder unless they begin with {documents}, {appdata}, {localappdata} or {home}.
KNOWN_GAMES = [
    ({"489830", "72850"}, {"skyrim special edition", "skyrim"}, "Data", "Skyrim"),
    ({"377160"}, {"fallout 4"}, "Data", "Fallout 4"),
    ({"220200"}, {"kerbal space program"}, "GameData", "Kerbal Space Program"),
    ({"261550"}, {"mount & blade ii bannerlord"}, "Modules", "Mount & Blade II: Bannerlord"),
    ({"4000"}, {"garrysmod"}, "garrysmod/addons", "Garry's Mod"),
    ({"294100"}, {"rimworld"}, "Mods", "RimWorld"),
    ({"251570"}, {"7 days to die"}, "Mods", "7 Days to Die"),
    ({"413150"}, {"stardew valley"}, "Mods", "Stardew Valley (with SMAPI)"),
    ({"108600"}, {"projectzomboid"}, "{home}/Zomboid/mods", "Project Zomboid"),
    ({"427520"}, {"factorio"}, "{appdata}/Factorio/mods", "Factorio"),
    ({"1222670"}, {"the sims 4"}, "{documents}/Electronic Arts/The Sims 4/Mods", "The Sims 4"),
    ({"1086940"}, {"baldurs gate 3"}, "{localappdata}/Larian Studios/Baldur's Gate 3/Mods", "Baldur's Gate 3"),
    ({"255710"}, {"cities_skylines"}, "{localappdata}/Colossal Order/Cities_Skylines/Addons/Mods", "Cities: Skylines"),
    ({"227300"}, {"euro truck simulator 2"}, "{documents}/Euro Truck Simulator 2/mod", "Euro Truck Simulator 2"),
    ({"270880"}, {"american truck simulator"}, "{documents}/American Truck Simulator/mod", "American Truck Simulator"),
    ({"1167630"}, {"teardown"}, "{documents}/Teardown/mods", "Teardown"),
    ({"221100", "1024020"}, {"dayz", "dayz exp"}, ".", "DayZ (each mod is an @ModName folder here)"),
    ({"107410"}, {"arma 3"}, ".", "Arma 3 (each mod is an @ModName folder here)"),
]

# File or folder names that mean the game runs anti-cheat.
_ANTI_CHEAT = (("easyanticheat", "Easy Anti-Cheat"), ("eaanticheat", "EA Anti-Cheat"), ("battleye", "BattlEye"),
               ("beservice", "BattlEye"), ("vanguard", "Vanguard"), ("xigncode", "XIGNCODE"),
               ("gameguard", "GameGuard"), ("anticheat", "anti-cheat"))

_SKIP_DIRS = {"engine", "_commonredist", "commonredist", "redist", "redistributables", "directx", "vcredist",
              "support", "__installer", "logs", "crashreportclient", "binaries"}
_MODS_NAMES = {"mods", "mod", "~mods", "plugins", "addons", "gamedata", "modules"}


def _known_path(template: str, game_dir: Path) -> Path:
    places = {
        "{documents}": documents_dir(),
        "{appdata}": _env_path("APPDATA") or Path.home(),
        "{localappdata}": _env_path("LOCALAPPDATA") or Path.home(),
        "{home}": Path.home(),
    }
    for marker, place in places.items():
        if template.startswith(marker):
            return place.joinpath(*template[len(marker):].strip("/").split("/"))
    return game_dir.joinpath(*[part for part in template.split("/") if part != "."])


def _children(folder: Path) -> list[Path]:
    try:
        return [p for p in folder.iterdir() if p.is_dir()]
    except OSError:
        return []


def _child(folder: Path, name: str) -> Path | None:
    """A subfolder by name, ignoring case (matters outside Windows)."""
    exact = folder / name
    if exact.is_dir():
        return exact
    return next((p for p in _children(folder) if p.name.lower() == name.lower()), None)


def _is_unity(folder: Path) -> bool:
    return (folder / "UnityPlayer.dll").exists() or any(
        p.name.endswith("_Data") and (p / "Managed").is_dir() for p in _children(folder))


def _looks_like_minecraft(folder: Path) -> bool:
    if (folder / "versions").is_dir() and ((folder / "saves").is_dir() or (folder / "options.txt").exists()):
        return True  # the official launcher's .minecraft
    # Prism, CurseForge and Modrinth instances
    return (folder / "options.txt").exists() and any(
        (folder / d).is_dir() for d in ("mods", "saves", "config", "resourcepacks"))


def _scan(base: Path, penalty: int) -> list[Suggestion]:
    out: list[Suggestion] = []

    def add(path: Path, reason: str, score: int) -> None:
        out.append(Suggestion(os.path.normpath(str(path)), reason, score - penalty))

    bepinex = _child(base, "BepInEx")
    if bepinex:
        plugins = bepinex / "plugins"
        add(plugins, "BepInEx is installed. BepInEx mods go in its plugins folder.", 100 if plugins.is_dir() else 96)
    if _child(base, "MelonLoader"):
        add(_child(base, "Mods") or base / "Mods", "MelonLoader is installed. Its mods go in the Mods folder.", 97)

    win64 = base / "Binaries" / "Win64"
    if (win64 / "ue4ss" / "Mods").is_dir():
        add(win64 / "ue4ss" / "Mods", "UE4SS is installed. Lua and C++ mods go in its Mods folder.", 93)
    elif (win64 / "UE4SS.dll").exists() and (win64 / "Mods").is_dir():
        add(win64 / "Mods", "UE4SS is installed. Lua and C++ mods go in its Mods folder.", 93)
    paks = base / "Content" / "Paks"
    if paks.is_dir():
        tilde = paks / "~mods"
        add(tilde, "Unreal Engine game. .pak mods usually go in Content\\Paks\\~mods.", 90 if tilde.is_dir() else 76)

    if _looks_like_minecraft(base):
        add(_child(base, "mods") or base / "mods", "Minecraft. Fabric, Forge and Quilt mods go in the mods folder.",
            99 if _child(base, "mods") else 94)

    for child in _children(base):
        if child.name.lower() == "mods":
            add(child, "The game has a Mods folder.", 88)

    if not bepinex and not _child(base, "MelonLoader") and _is_unity(base):
        add(base, "Unity game with no mod loader yet. Most Unity mods need BepInEx: keep the game folder "
                  "here, add BepInEx/BepInEx as the first mod, then set other mods to install into "
                  "BepInEx\\plugins.", 62)
        add(base / "BepInEx" / "plugins", "Where BepInEx mods go once BepInEx is installed.", 55)

    if any(base.glob("*.pck")) and not _child(base, "mods"):
        add(base / "mods", "Godot game. Mod .pck files often go in a mods folder, but it depends on the game.", 60)
    return out


def suggest_mods_folders(game_dir: str, steam_id: str = "") -> list[Suggestion]:
    """Likely mods folders for a game, best first."""
    if not game_dir or not os.path.isdir(game_dir):
        return []
    root = Path(os.path.normpath(game_dir))
    suggestions: list[Suggestion] = []

    if root.name.lower() in _MODS_NAMES:
        suggestions.append(Suggestion(str(root), "This looks like a mods folder already.", 99))

    folder_name = root.name.lower()
    for ids, names, template, title in KNOWN_GAMES:
        if steam_id in ids or folder_name in names:
            path = _known_path(template, root)
            suggestions.append(Suggestion(os.path.normpath(str(path)), f"The usual mods folder for {title}.", 98))

    suggestions += _scan(root, 0)
    for child in _children(root):
        if child.name.lower() not in _SKIP_DIRS:
            suggestions += _scan(child, 2)  # Unreal games keep their content one level down
    suggestions.append(Suggestion(str(root), "The game's own folder, for mods that unpack straight into it.", 10))

    best: dict[str, Suggestion] = {}
    for s in suggestions:
        key = os.path.normcase(s.path)
        if key not in best or s.score > best[key].score:
            best[key] = s
    return sorted(best.values(), key=lambda s: -s.score)


def anti_cheat(game_dir: str) -> str:
    """The name of the game's anti-cheat, if it ships one, else ''."""
    if not game_dir or not os.path.isdir(game_dir):
        return ""
    root = Path(game_dir)
    for folder in [root, *_children(root)]:
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for name in names:
            lower = name.lower()
            for prefix, label in _ANTI_CHEAT:
                if lower.startswith(prefix):
                    return label
    return ""
