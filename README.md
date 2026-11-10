# Mod Loader

A general-purpose mod manager for any game that loads mods from a folder. You point it at a
game's mods folder, add mods by their GitHub repository, and it installs them from the latest
**GitHub Release** and keeps them updated.

Pure Python 3.10+, standard library only. Nothing to `pip install`.

## Download

Get **`ModLoader.exe`** from the [latest release](https://github.com/MichaelRyan2319/ModLoader/releases/latest)
and run it. There's nothing to install, and Python isn't needed. Mod Loader keeps itself up to date
from then on.

The first time, Windows may say "Windows protected your PC" because the program isn't signed. Click
**More info**, then **Run anyway**.

With Python installed, you can run the source instead: download `ModLoader-source.zip`, unpack it, and
double-click `ModLoader.pyw`.

## Run it

1. **Add game**: pick the game from the list of installed games. Mod Loader fills in the name, the
   launcher, and the **mods folder** (see below). For a game that isn't listed, choose its game folder
   and the mods folder is still detected. You can always pick a different folder.
2. **Add mod**: enter `owner/repo` or paste the repo's GitHub link, then click **Look up**. If the
   release has several files (Windows/Linux builds, for example), choose one under **Release file**.
   Version numbers in the name are swapped for `*` (so `MyMod-1.2.3.zip` becomes `MyMod-*.zip`) and
   future releases still match.
3. Click **Add & install**.

After that, Mod Loader checks for new releases when it opens and every 60 minutes while it's open, and
installs updates automatically. All of this can be changed in **Settings**.

## Collections

A collection is a named set of a game's mods, for example "Co-op with friends" or "Just the basics".
Everything for them is in the **Collection** row above the mod list.

- **Make one**: **Collections ▾ → New collection**, give it a name and tick its mods. It starts with
  the mods that are enabled now, or with the mods you had selected if you selected several. A mod can
  be in any number of collections.
- **See one**: pick it in the **Collection** list and the mod list shows only its mods. **All mods**
  shows everything again. **Add mod** while a collection is shown puts the new mod into it.
- **Use one**: click **Use**. Its mods are enabled and the game's other mods are disabled, after
  showing you which. Nothing is deleted or downloaded again, so switching back is quick. The list marks
  the collection whose mods are the ones enabled right now with **✓ in use**. A collection with no mods
  is a handy "no mods" switch.
- **Edit** and **Delete** are in the same menu. Deleting a collection leaves its mods alone.

If a mod needs another mod and you agree to add it, the needed mod joins the same collections.

### Sharing a collection

**Collections ▾ → Export … as a code** turns the collection that's shown (or all of the game's mods)
into a code: one line of text starting with `MLC1:`. **Copy code** and send it to someone however you
like.

They select their game, choose **Collections ▾ → Import code** and paste it (a code already on the
clipboard is filled in). Mod Loader shows the collection's name and every mod in it, and which ones
they already have. **Import** adds and installs the missing mods and makes the collection. Mods they
already had are left as they were, and nothing is switched off until they click **Use**. Importing a
code with the name of a collection they already have updates that collection.

The code holds each mod's repository, name, release file, install folder and unpack options. It doesn't
hold files, versions (everyone gets each mod's latest release) or your GitHub token. Only import codes
from people you trust: the code can't do anything by itself, but the mods it lists run inside your
game.

## Updates to Mod Loader itself

When Mod Loader opens, it checks [its own releases](https://github.com/MichaelRyan2319/ModLoader/releases)
for a newer version. If there is one, it downloads and installs it (the size and checksum are verified),
and a bar at the top offers **Restart now**. Otherwise the new version starts the next time Mod Loader
opens.

- **`ModLoader.exe`** replaces itself while it runs. The old copy is kept as `ModLoader.exe.old` until the
  next start. Mod Loader needs to be in a folder it can write to, which rules out `Program Files`.
- **Run from source**, the new files are copied over the program folder. A git checkout is never
  changed this way; use `git pull`.

**Settings → Keep Mod Loader itself up to date automatically** can be turned off. Mod Loader then only
offers the update.

## Finding games and their mods folders

Installed games are read from **Steam** (every library), **Epic**, **GOG**, and Minecraft (the official
launcher, plus **Prism Launcher**, **CurseForge** and **Modrinth** instances).

The mods folder is worked out by looking inside the game folder, best match first:

| Found | Mods folder |
|---|---|
| BepInEx | `BepInEx\plugins` |
| MelonLoader | `Mods` |
| UE4SS (Unreal) | `...\Binaries\Win64\ue4ss\Mods` |
| Unreal Engine game | `<Project>\Content\Paks\~mods` |
| Minecraft / instance | `mods` |
| A `Mods` folder | that folder |
| Known games | e.g. Factorio and Project Zomboid (user folder), Skyrim/Fallout 4 `Data`, KSP `GameData`, The Sims 4 (Documents), DayZ/Arma (game folder, `@Mod` folders), RuneScape: Dragonwilds (its inner `RSDragonwilds` folder; mod zips hold `Content\Paks\~mods\...` or `Binaries\Win64\...`) |
| Unity game with no mod loader | the game folder, so BepInEx can be installed as the first mod; other mods then use **Install into** `BepInEx\plugins` |
| Nothing recognised | the game folder |

The dialog says why it picked a folder and lists other likely ones. If the game ships anti-cheat
(Easy Anti-Cheat, BattlEye, EA Anti-Cheat and others), it warns that mods in online games can get an
account banned.

## What it does

- **Updates**: a new release (a new release ID, so re-tagged releases count) means an update. The
  check uses GitHub ETags, so checking a mod that hasn't changed costs almost nothing.
- **Clean installs**: every file a mod installs is recorded. When an update drops a file, the file is
  deleted, and removing a mod leaves nothing behind. Empty folders are tidied up.
- **Backups**: if a mod overwrites a file it didn't create (a game's own `winhttp.dll`, a config),
  the original is backed up and put back when the mod is removed or disabled.
- **Safe downloads**: files are downloaded and unpacked into a staging folder first, so a broken
  download never touches the game folder. Sizes and GitHub's SHA-256 checksums are verified. Zips
  that try to write outside the mods folder are rejected.
- **Archives**: `.zip` and `.tar.gz`-style archives are unpacked. `.jar`, `.pak`, `.pk3`, `.dll`
  and other files are copied as-is. One option drops a zip's single top folder; if a release has no
  files attached, the source-code zip is used.
- **Enable/disable** moves a mod's files out of the game folder and back, without downloading again.
- **Conflicts**: if two mods ship the same file, you're told, and removing one doesn't delete a file
  the other still needs.
- **Mods that need other mods**: a mod can list the mods it needs (see below). When you add it,
  **Look up** shows what it needs, and Mod Loader asks whether to add and install anything that's
  missing (or enable it, if it's disabled). The needed mods are installed first. An update that
  starts needing a new mod asks the same way. If you say no, you aren't asked again until you click
  **Update** or **Reinstall** on that mod; until then its status says **Needs …**. Removing or
  disabling a mod that another one needs asks first. The details under the list show what each mod
  needs and what needs it.
- **Collections**: named sets of a game's mods that you switch between with one click and share as
  a code (see above).
- **Update & play / desktop shortcut**: updates a game's mods, then starts the game. The **Shortcut**
  button puts this on your desktop.

## Publishing a mod so Mod Loader can use it

Create a GitHub Release (`Releases → Draft a new release`), give it a tag such as `v1.2.0`, and attach
the mod file (a `.zip` laid out the way it should land in the mods folder, or the plain file).
Pre-releases are skipped unless the user ticks **Include pre-releases**. To publish files for
several platforms or games, give them distinct names so users can pick one with a pattern.

### Mods that need other mods

Put a `modloader.json` file at the top of the mod's repository listing the repositories it needs:

```json
{
  "requires": [
    "MichaelRyan2319/RSDragonwilds-UE4SS",
    {"repo": "BepInEx/BepInEx", "name": "BepInEx", "file": "BepInEx_win_x64_*.zip", "install_into": ""}
  ]
}
```

Each entry is `owner/name` (or a github.com link), or an object that also says how to add it: `name`
(shown in the list), `file` (which release file, like **Release file** in the Add mod dialog) and
`install_into` (a folder inside the game's mods folder). Mod Loader reads the file **at the release's
tag**, so it has to be committed before the release is made. Changing the list needs a new release.
A needed mod counts as present when the same game has a mod from that repository.

## Settings

- **GitHub token** (optional). Without a token, GitHub allows 60 API requests an hour. With one it
  allows 5,000, and private repositories work. A fine-grained token with read-only access is enough.
  It's stored unencrypted in `config.json`.
- Check on startup, install automatically, and the check interval. Each mod also has its own
  **Update automatically** switch; mods with it off are only flagged, and **Update all** installs
  everything.

## Command line

```
python ModLoader.pyw --update            # install updates for all auto-updating mods, then exit
python ModLoader.pyw --play "Game name"  # update a game's mods, start it, then exit
python ModLoader.pyw --data D:\ModData   # keep data somewhere else (portable use)
```

`--update` exits with code 1 if any mod had a problem, so it works from Task Scheduler.

## Where things are kept

`%APPDATA%\ModLoader\` (or the `--data` folder) holds:

| | |
|---|---|
| `config.json` | games, mods, collections, settings |
| `backups\<mod>\` | original files a mod replaced |
| `disabled\<mod>\` | files of disabled mods |
| `http_cache.json` | cached GitHub responses (safe to delete) |
| `modloader.log` | activity log |

## Releasing a new version of Mod Loader

```
python release.py 1.2.0
```

This sets the version in `modloader/__init__.py` and runs the tests. If they pass, it commits, tags
`v1.2.0` and pushes. GitHub then builds `ModLoader.exe` (`.github/workflows/release.yml`) and publishes
the release with `ModLoader.exe` and `ModLoader-source.zip`, usually within five minutes. Every copy of
Mod Loader picks it up the next time it opens. Progress is under the repository's **Actions** tab.

## Make an .exe yourself

Run `build_exe.bat`. It installs PyInstaller and builds `dist\ModLoader.exe`. Desktop shortcuts made
from the .exe point at the .exe.

## Code layout

| File | Purpose |
|---|---|
| `modloader/detect.py` | finding installed games and their mods folders |
| `modloader/selfupdate.py` | Mod Loader updating itself from its own releases |
| `modloader/github.py` | GitHub Releases API, choosing release files, downloads with checksums |
| `modloader/installer.py` | staging, unpacking, file tracking, backups, enable/disable/uninstall |
| `modloader/manager.py` | check / install / update logic shared by the window and command line |
| `modloader/requires.py` | mods that need other mods: reading `modloader.json`, working out what's missing |
| `modloader/collection.py` | collections of a game's mods, and the codes that share them |
| `modloader/config.py` | saved data (dataclasses to JSON) |
| `modloader/gui.py` | the window, dialogs, and the "Update & play" window |
| `modloader/system.py` | opening folders, starting games, creating shortcuts |
| `modloader/app.py` | command-line entry point |
| `release.py`, `.github/workflows/release.yml` | publishing a new version |

Tests: `python -m unittest discover -s tests`
