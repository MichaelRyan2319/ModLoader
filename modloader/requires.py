"""Mods that need other mods.

A mod lists the mods it needs in a `modloader.json` file at the root of its
repository. Mod Loader reads it at the tag of the release it installs:

    {
      "requires": [
        "MichaelRyan2319/RSDragonwilds-UE4SS",
        {"repo": "BepInEx/BepInEx", "name": "BepInEx", "file": "BepInEx_win_x64_*.zip"}
      ]
    }

Each entry is a repository (owner/name or a github.com link), or an object
with the repository and how to add it: "name", "file" (which release file, as
in the Add mod dialog) and "install_into" (a folder inside the mods folder).

When a mod needs one that isn't added to the same game, or is disabled, the
window offers to add or enable it. A player who says no isn't asked again for
that mod until they update or reinstall it themselves.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from .config import Mod
from .github import parse_repo
from .installer import InstallError, clean_subdir

if TYPE_CHECKING:
    from .config import Store

MANIFEST_NAME = "modloader.json"


def parse_manifest(text: str, own_repo: str = "") -> tuple[list[dict], list[str]]:
    """Read modloader.json. Returns (requirements, problems); bad entries are skipped."""
    try:
        data = json.loads(text.lstrip("﻿"))
    except ValueError as e:
        return [], [f"it isn't valid JSON ({e})"]
    if not isinstance(data, dict):
        return [], ['it should hold an object, like {"requires": ["owner/name"]}']
    entries = data.get("requires", [])
    if isinstance(entries, (str, dict)):
        entries = [entries]
    if not isinstance(entries, list):
        return [], ['"requires" should be a list of repositories']

    found: list[dict] = []
    problems: list[str] = []
    seen = {own_repo.lower()}
    for entry in entries:
        if isinstance(entry, str):
            entry = {"repo": entry}
        if not isinstance(entry, dict) or not isinstance(entry.get("repo"), str):
            problems.append(f'skipped {json.dumps(entry)}: each entry needs a "repo"')
            continue
        try:
            repo = parse_repo(entry["repo"])
        except ValueError:
            problems.append(f'skipped "{entry["repo"]}": it isn\'t a GitHub repository')
            continue
        if repo.lower() in seen:
            continue
        seen.add(repo.lower())
        try:
            subdir = clean_subdir(str(entry.get("install_into") or ""))
        except InstallError:
            problems.append(f'ignored the "install_into" of {repo}: it must be a folder inside the mods folder')
            subdir = ""
        found.append({
            "repo": repo,
            "name": str(entry.get("name") or "").strip(),
            "file": str(entry.get("file") or "").strip(),
            "install_into": subdir,
        })
    return found, problems


def label(req: dict) -> str:
    return req.get("name") or str(req.get("repo", "")).split("/")[-1]


def find(store: "Store", game_id: str, repo: str) -> Mod | None:
    """The game's mod installed from this repository, if any."""
    key = repo.lower()
    return next((m for m in store.mods_for(game_id) if m.repo.lower() == key), None)


def unmet(store: "Store", mod: Mod, include_declined: bool = True) -> list[tuple[dict, Mod | None]]:
    """What `mod` needs but doesn't have: (requirement, the disabled mod, or None if it isn't added)."""
    declined = {r.lower() for r in mod.declined_requires}
    missing = []
    for req in mod.requires:
        repo = str(req.get("repo") or "")
        if not repo or (not include_declined and repo.lower() in declined):
            continue
        have = find(store, mod.game_id, repo)
        if have is None or not have.enabled:
            missing.append((req, have))
    return missing


def dependents(store: "Store", mod: Mod) -> list[Mod]:
    """Enabled mods of the same game that need `mod`."""
    key = mod.repo.lower()
    return [m for m in store.mods_for(mod.game_id)
            if m.id != mod.id and m.enabled and any(str(r.get("repo", "")).lower() == key for r in m.requires)]


def decline(mod: Mod, repo: str) -> None:
    """Remember that the player doesn't want `repo` added for `mod`."""
    if repo.lower() not in (r.lower() for r in mod.declined_requires):
        mod.declined_requires.append(repo.lower())


def new_mod(game_id: str, req: dict) -> Mod:
    return Mod(game_id=game_id, repo=req["repo"], name=req.get("name") or "",
               asset_pattern=req.get("file") or "", subdir=req.get("install_into") or "")
