import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from modloader.detect import (anti_cheat, find_epic_games, find_minecraft, find_steam_games, parse_vdf,
                              steam_app_id, suggest_mods_folders)


def touch(path: Path, text: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def acf(app_id: str, name: str, folder: str) -> str:
    return f'"AppState"\n{{\n\t"appid"\t\t"{app_id}"\n\t"name"\t\t"{name}"\n\t"installdir"\t\t"{folder}"\n}}\n'


class TempDirTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def best(self, game_dir: Path, steam_id: str = ""):
        return suggest_mods_folders(str(game_dir), steam_id)[0]


class LauncherTests(TempDirTest):
    def test_parse_vdf(self):
        data = parse_vdf('"libraryfolders"\n{\n "0"\n {\n  "path"  "D:\\\\Steam"\n  "apps" { "10" "123" }\n }\n}')
        self.assertEqual(data["libraryfolders"]["0"]["path"], "D:\\Steam")
        self.assertEqual(data["libraryfolders"]["0"]["apps"], {"10": "123"})

    def test_steam_libraries_and_tools(self):
        steam, extra = self.root / "Steam", self.root / "Games2"
        escaped = [str(p).replace("\\", "\\\\") for p in (steam, extra)]  # VDF escapes backslashes
        touch(steam / "steamapps" / "libraryfolders.vdf",
              '"libraryfolders" { "0" { "path" "%s" } "1" { "path" "%s" } }' % tuple(escaped))
        touch(steam / "steamapps" / "appmanifest_892970.acf", acf("892970", "Valheim", "Valheim"))
        (steam / "steamapps" / "common" / "Valheim").mkdir(parents=True)
        touch(steam / "steamapps" / "appmanifest_228980.acf", acf("228980", "Steamworks Common Redistributables", "SR"))
        (steam / "steamapps" / "common" / "SR").mkdir(parents=True)
        touch(steam / "steamapps" / "appmanifest_1.acf", acf("1", "Uninstalled", "Gone"))
        touch(extra / "steamapps" / "appmanifest_108600.acf", acf("108600", "Project Zomboid", "ProjectZomboid"))
        (extra / "steamapps" / "common" / "ProjectZomboid").mkdir(parents=True)

        games = find_steam_games([steam])
        self.assertEqual(sorted(g.name for g in games), ["Project Zomboid", "Valheim"])
        valheim = next(g for g in games if g.name == "Valheim")
        self.assertEqual(valheim.launch, "steam://rungameid/892970")
        self.assertEqual(steam_app_id(valheim.launch), "892970")

    def test_epic_skips_damaged_and_engine_manifests(self):
        manifests = self.root / "Epic" / "EpicGamesLauncher" / "Data" / "Manifests"
        game_dir, engine_dir, orphan_dir = self.root / "Fortnite", self.root / "UE_5.7", self.root / "Satisfactory"
        for d in (game_dir, engine_dir, orphan_dir):
            d.mkdir()
        touch(manifests / "a.item", json.dumps({"DisplayName": "Fortnite", "InstallLocation": str(game_dir),
                                                "CatalogNamespace": "ns", "CatalogItemId": "id", "AppName": "app"}))
        touch(manifests / "b.item", json.dumps({"DisplayName": "Unreal Engine", "InstallLocation": str(engine_dir),
                                                "AppCategories": ["engines/ue5", "engines"]}))
        (manifests / "c.item").write_bytes(b"\x00" * 2000)  # damaged, as found on a real PC
        touch(self.root / "Epic" / "UnrealEngineLauncher" / "LauncherInstalled.dat", json.dumps({"InstallationList": [
            {"InstallLocation": str(game_dir), "NamespaceId": "ns", "ItemId": "id", "AppName": "app"},
            {"InstallLocation": str(engine_dir), "NamespaceId": "ue", "ItemId": "x", "AppName": "UE_5.7"},
            {"InstallLocation": str(orphan_dir), "NamespaceId": "n2", "ItemId": "i2", "AppName": "a2"},
        ]}))
        games = find_epic_games(self.root)
        self.assertEqual([g.name for g in games], ["Fortnite", "Satisfactory"])
        self.assertIn("apps/ns%3Aid%3Aapp", games[0].launch)

    def test_minecraft_launchers(self):
        appdata, home = self.root / "AppData", self.root / "home"
        (appdata / ".minecraft").mkdir(parents=True)
        (appdata / "PrismLauncher" / "instances" / "Create" / "minecraft").mkdir(parents=True)
        (appdata / "PrismLauncher" / "instances" / "_LAUNCHER_TEMP").mkdir(parents=True)
        (home / "curseforge" / "minecraft" / "Instances" / "ATM10").mkdir(parents=True)
        names = [(g.name, g.source) for g in find_minecraft(appdata, home)]
        self.assertEqual(names, [("Minecraft", "Minecraft Launcher"), ("Minecraft: Create", "Prism Launcher"),
                                 ("Minecraft: ATM10", "CurseForge")])


class SuggestionTests(TempDirTest):
    def test_bepinex(self):
        (self.root / "BepInEx" / "plugins").mkdir(parents=True)
        touch(self.root / "UnityPlayer.dll")
        best = self.best(self.root)
        self.assertEqual(best.path, str(self.root / "BepInEx" / "plugins"))
        self.assertIn("BepInEx is installed", best.reason)

    def test_melonloader(self):
        (self.root / "MelonLoader").mkdir()
        self.assertEqual(self.best(self.root).path, str(self.root / "Mods"))

    def test_unity_without_loader_offers_game_folder_and_plugins(self):
        (self.root / "Game_Data" / "Managed").mkdir(parents=True)
        paths = [s.path for s in suggest_mods_folders(str(self.root))]
        self.assertEqual(paths[0], str(self.root))
        self.assertIn(str(self.root / "BepInEx" / "plugins"), paths)

    def test_unreal_pak_and_ue4ss(self):
        project = self.root / "MyGame"
        (project / "Content" / "Paks").mkdir(parents=True)
        (self.root / "Engine" / "Content" / "Paks").mkdir(parents=True)  # the engine's, never a mods folder
        best = self.best(self.root)
        self.assertEqual(best.path, str(project / "Content" / "Paks" / "~mods"))
        (project / "Binaries" / "Win64" / "ue4ss" / "Mods").mkdir(parents=True)
        self.assertEqual(self.best(self.root).path, str(project / "Binaries" / "Win64" / "ue4ss" / "Mods"))
        self.assertNotIn("Engine", " ".join(s.path for s in suggest_mods_folders(str(self.root))))

    def test_minecraft_folder(self):
        (self.root / "versions").mkdir()
        touch(self.root / "options.txt")
        best = self.best(self.root)
        self.assertEqual(best.path, str(self.root / "mods"))
        self.assertIn("Minecraft", best.reason)

    def test_plain_mods_folder_and_picking_it_directly(self):
        (self.root / "Mods").mkdir()
        self.assertEqual(self.best(self.root).path, str(self.root / "Mods"))
        self.assertEqual(self.best(self.root / "Mods").path, str(self.root / "Mods"))

    def test_known_games(self):
        appdata = self.root / "AppData"
        with mock.patch.dict(os.environ, {"APPDATA": str(appdata)}):
            best = self.best(self.root, steam_id="427520")
        self.assertEqual(best.path, str(appdata / "Factorio" / "mods"))
        dayz = self.root / "DayZ"
        (dayz / "Addons").mkdir(parents=True)  # the game's own data: must not be suggested
        self.assertEqual(self.best(dayz).path, str(dayz))
        self.assertNotIn(str(dayz / "Addons"), [s.path for s in suggest_mods_folders(str(dayz))])

    def test_nothing_found_falls_back_to_game_folder(self):
        touch(self.root / "game.exe")
        best = self.best(self.root)
        self.assertEqual(best.path, str(self.root))
        self.assertLess(best.score, 50)
        self.assertEqual(suggest_mods_folders(str(self.root / "missing")), [])

    def test_anti_cheat(self):
        self.assertEqual(anti_cheat(str(self.root)), "")
        touch(self.root / "EAAntiCheat.cfg")
        self.assertEqual(anti_cheat(str(self.root)), "EA Anti-Cheat")
        other = self.root / "other"
        (other / "Game" / "EasyAntiCheat").mkdir(parents=True)
        self.assertEqual(anti_cheat(str(other)), "Easy Anti-Cheat")


if __name__ == "__main__":
    unittest.main()
