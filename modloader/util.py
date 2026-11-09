"""Small helpers shared by the rest of the package."""

from __future__ import annotations

import re
from datetime import datetime


def safe_filename(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return cleaned or "download"


def human_size(n: int) -> str:
    if not n:
        return "size unknown"
    size = float(n)
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} {unit}" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} bytes"


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def friendly_time(iso: str | None) -> str:
    """'just now', '5 min ago', 'today 14:02', '3 Nov 2026'."""
    if not iso:
        return "never"
    try:
        then = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    if then.tzinfo is None:
        then = then.astimezone()
    now = datetime.now(then.tzinfo)
    seconds = (now - then).total_seconds()
    if 0 <= seconds < 60:
        return "just now"
    if 0 <= seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    local = then.astimezone()
    if local.date() == datetime.now().date():
        return f"today {local:%H:%M}"
    return f"{local.day} {local:%b %Y}"
