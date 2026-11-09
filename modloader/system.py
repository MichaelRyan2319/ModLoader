"""Operating-system helpers: opening folders, starting games, desktop shortcuts."""

from __future__ import annotations

import base64
import os
import shlex
import subprocess
import sys
from pathlib import Path

from .util import safe_filename

ROOT = Path(__file__).resolve().parent.parent
ENTRY_SCRIPT = ROOT / "ModLoader.pyw"
CREATE_NO_WINDOW = 0x08000000


def open_path(path: Path | str) -> None:
    if sys.platform == "win32":
        os.startfile(str(path))
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def launch(target: str, args: str = "") -> None:
    """Start a game: an .exe, a shortcut, or a link such as steam://rungameid/123."""
    target = os.path.expandvars(target.strip().strip('"'))
    if not target:
        raise OSError("No launcher is set for this game.")
    is_link = "://" in target
    if not is_link and not Path(target).exists():
        raise OSError(f"The game launcher wasn't found: {target}")
    if sys.platform == "win32":
        cwd = None if is_link else str(Path(target).parent)
        os.startfile(target, arguments=args or "", cwd=cwd)
    elif is_link:
        open_path(target)
    else:
        subprocess.Popen([target, *shlex.split(args or "")], cwd=str(Path(target).parent))


def self_command() -> tuple[str, list[str]]:
    """How to start Mod Loader again without a console window."""
    if getattr(sys, "frozen", False):  # packaged with PyInstaller
        return sys.executable, []
    exe = Path(sys.executable)
    windowless = exe.with_name("pythonw.exe")
    return str(windowless if windowless.exists() else exe), [str(ENTRY_SCRIPT)]


def create_play_shortcut(game) -> str:
    """Put a desktop shortcut that updates the game's mods, then starts it. Returns its name."""
    if sys.platform != "win32":
        raise OSError("Desktop shortcuts are only supported on Windows.")
    exe, prefix = self_command()
    args = subprocess.list2cmdline(prefix + ["--play", game.id])
    launcher = os.path.expandvars(game.launch_path or "")
    icon = launcher if launcher.lower().endswith(".exe") and Path(launcher).exists() else exe
    workdir = str(Path(prefix[0]).parent if prefix else Path(exe).parent)
    name = safe_filename(f"{game.name} (modded)") + ".lnk"

    def q(s: str) -> str:
        return "'" + s.replace("'", "''") + "'"

    script = (
        "$ErrorActionPreference='Stop';"
        "$desk=[Environment]::GetFolderPath('Desktop');"
        f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $desk {q(name)}));"
        f"$s.TargetPath={q(exe)};$s.Arguments={q(args)};$s.WorkingDirectory={q(workdir)};"
        f"$s.IconLocation={q(icon + ',0')};"
        f"$s.Description={q('Update mods with Mod Loader, then start ' + game.name)};$s.Save()"
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        capture_output=True, text=True, timeout=60, creationflags=CREATE_NO_WINDOW,
    )
    if result.returncode != 0:
        raise OSError((result.stderr or "PowerShell couldn't create the shortcut.").strip().splitlines()[0])
    return name
