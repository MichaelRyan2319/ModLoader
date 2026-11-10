"""Collections: named sets of a game's mods, and the codes that share them.

A collection belongs to one game and lists some of that game's mods. In the
window, picking a collection shows only its mods, and "Use" enables exactly
those mods and disables the game's other ones (ModManager.use_collection).

A collection is shared as a code, a line of text to paste anywhere:

    MLC1:eNqrVspLzE1VslJyztfNL1AoSEzOVtJRSoeIhSXmZKRm5gIFcvNTipWsopXyy/NSi/TBWmJrAUI8E3s=

That is "MLC" + the format number + ":" + base64 of zlib-compressed JSON. The
one above holds {"name": "Co-op pack", "game": "Valheim", "mods": ["owner/name"]}.
A mod that doesn't use the usual settings is an object instead:

    {"repo": "owner/other", "name": "Other", "file": "Other-*.zip", "install_into": "plugins",
     "unpack": false, "strip_root": true, "prereleases": true}

Each mod is listed the way modloader.json lists the mods a mod needs
(requires.parse_entry), plus "unpack", "strip_root" and "prereleases" for mods
that don't use the usual settings. Importing a code adds the mods the game
doesn't have yet and makes a collection of them. A code is only a list of
repositories and settings; nothing in it is run.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import zlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from . import requires
from .config import Collection, Mod

if TYPE_CHECKING:
    from .config import Game, Store

PREFIX = "MLC"
FORMAT = 1  # raise only when older Mod Loaders couldn't read the new codes at all
MAX_BYTES = 1_000_000  # of JSON inside a code
MAX_MODS = 500
NAME_LENGTH = 60

_CODE = re.compile(rf"{PREFIX}(\d+):([A-Za-z0-9+/_=-]+)", re.I)


class CodeError(ValueError):
    """A collection code that can't be used, with a message fit to show the player."""


@dataclass
class Shared:
    """What a collection code holds."""

    name: str
    game: str  # the game it was made for (only shown to the player)
    mods: list[dict]
    problems: list[str] = field(default_factory=list)  # entries that were skipped or changed


def clean_name(name: str) -> str:
    return " ".join(str(name or "").split())[:NAME_LENGTH].strip()


# -- Codes ------------------------------------------------------------------------

def entry_for(mod: Mod) -> str | dict:
    """How a mod is written in a code: just its repository when it uses the usual settings."""
    entry: dict = {"repo": mod.repo}
    if mod.name:
        entry["name"] = mod.name
    if mod.asset_pattern:
        entry["file"] = mod.asset_pattern
    if mod.subdir:
        entry["install_into"] = mod.subdir
    if not mod.extract:
        entry["unpack"] = False
    if mod.strip_root:
        entry["strip_root"] = True
    if mod.include_prereleases:
        entry["prereleases"] = True
    return entry if len(entry) > 1 else mod.repo


def export_code(name: str, game_name: str, mods: list[Mod]) -> str:
    payload = {"name": clean_name(name), "game": game_name, "mods": [entry_for(m) for m in mods]}
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    # Standard base64: chat apps leave + / = alone, but can eat the _ and - of the URL-safe kind.
    return f"{PREFIX}{FORMAT}:" + base64.b64encode(zlib.compress(raw, 9)).decode("ascii")


def looks_like_code(text: str) -> bool:
    return bool(_CODE.search(text or ""))


def parse_code(text: str) -> Shared:
    """Read a pasted code. Raises CodeError with a message for the player."""
    text = (text or "").strip()
    if not text:
        raise CodeError("Paste a collection code.")
    # A code that was wrapped over several lines, or one sitting inside a message.
    candidates = [m for m in (_CODE.search("".join(text.split())), _CODE.search(text)) if m]
    if not candidates:
        raise CodeError(f"That isn't a Mod Loader collection code. Codes start with {PREFIX}{FORMAT}:")
    error: CodeError | None = None
    for match in candidates:
        try:
            return _read(int(match.group(1)), match.group(2))
        except CodeError as e:
            error = error or e
    raise error


def _read(number: int, body: str) -> Shared:
    damaged = CodeError("The code is incomplete or damaged. Copy the whole code and paste it again.")
    if number > FORMAT:
        raise CodeError("This code was made by a newer Mod Loader. Update Mod Loader "
                        "(Settings, then Check now) and try again.")
    body = body.rstrip("=").replace("-", "+").replace("_", "/")
    try:
        packed = base64.b64decode(body + "=" * (-len(body) % 4), validate=True)
        unpacker = zlib.decompressobj()
        raw = unpacker.decompress(packed, MAX_BYTES + 1)
    except (binascii.Error, ValueError, zlib.error):
        raise damaged from None
    if len(raw) > MAX_BYTES or unpacker.unconsumed_tail:
        raise CodeError("That code is too big to be a collection.")
    if not unpacker.eof:
        raise damaged
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError:
        raise damaged from None
    if not isinstance(data, dict) or not isinstance(data.get("mods"), list):
        raise damaged
    if len(data["mods"]) > MAX_MODS:
        raise CodeError(f"That code lists more than {MAX_MODS} mods, which is too many for one collection.")

    shared = Shared(name=clean_name(data.get("name")) or "Imported collection",
                    game=clean_name(data.get("game")), mods=[])
    seen: set[str] = set()
    for raw_entry in data["mods"]:
        entry, problem = requires.parse_entry(raw_entry)
        if entry is None:
            shared.problems.append(problem)
            continue
        if entry["repo"].lower() in seen:
            continue
        seen.add(entry["repo"].lower())
        if problem:
            shared.problems.append(problem)
        if isinstance(raw_entry, dict):
            if raw_entry.get("unpack") is False:
                entry["unpack"] = False
            if raw_entry.get("strip_root") is True:
                entry["strip_root"] = True
            if raw_entry.get("prereleases") is True:
                entry["prereleases"] = True
        shared.mods.append(entry)
    if not shared.mods:
        raise CodeError("That collection has no mods in it.")
    return shared


def new_mod(game_id: str, entry: dict) -> Mod:
    mod = requires.new_mod(game_id, entry)
    mod.extract = entry.get("unpack") is not False
    mod.strip_root = entry.get("strip_root") is True
    mod.include_prereleases = entry.get("prereleases") is True
    return mod


# -- Collections ------------------------------------------------------------------

def members(store: "Store", collection: Collection) -> list[Mod]:
    """The collection's mods, in the order the game lists them."""
    ids = set(collection.mod_ids)
    return [m for m in store.mods_for(collection.game_id) if m.id in ids]


def in_use(store: "Store", collection: Collection) -> bool:
    """True when the game's enabled mods are exactly the collection's mods.

    An empty collection is in use when every mod is disabled (a "no mods" collection).
    """
    ids = set(collection.mod_ids)
    mods = store.mods_for(collection.game_id)
    return bool(mods) and all(m.enabled == (m.id in ids) for m in mods)


def find_by_name(store: "Store", game_id: str, name: str) -> Collection | None:
    key = clean_name(name).casefold()
    return next((c for c in store.collections_for(game_id) if c.name.casefold() == key), None)


def forget_mod(store: "Store", mod_id: str) -> None:
    """Take a mod that's being removed out of every collection."""
    for collection in store.collections:
        if mod_id in collection.mod_ids:
            collection.mod_ids = [i for i in collection.mod_ids if i != mod_id]


def add_alongside(store: "Store", mod: Mod, other: Mod) -> None:
    """Put `other` (a mod that `mod` needs) into every collection `mod` is in."""
    for collection in store.collections_for(mod.game_id):
        if mod.id in collection.mod_ids and other.id not in collection.mod_ids:
            collection.mod_ids.append(other.id)


def import_shared(store: "Store", game: "Game", shared: Shared, name: str = "") -> tuple[Collection, list[Mod], bool]:
    """Add a shared collection to `game`.

    Mods the game doesn't have are added (the caller installs them); mods it has
    are used as they are. A collection of the same name gets the new list of mods.
    Returns (the collection, the mods that were added, whether it replaced one).
    """
    name = clean_name(name) or shared.name
    with store.lock:
        added: list[Mod] = []
        ids: list[str] = []
        for entry in shared.mods:
            mod = requires.find(store, game.id, entry["repo"])
            if mod is None:
                mod = new_mod(game.id, entry)
                store.mods.append(mod)
                added.append(mod)
            if mod.id not in ids:
                ids.append(mod.id)
        collection = find_by_name(store, game.id, name)
        replaced = collection is not None
        if collection is None:
            collection = Collection(game_id=game.id, name=name)
            store.collections.append(collection)
        collection.mod_ids = ids
        store.save()
    return collection, added, replaced
