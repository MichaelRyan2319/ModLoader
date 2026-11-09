import io
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from modloader.config import Game, Mod, Store
from modloader.github import Asset
from modloader.installer import InstallError, clean_subdir, stage_files
from modloader.manager import ModManager


def make_zip(path: Path, files: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, text in files.items():
            zf.writestr(name, text)
    return path


class FakeClient:
    """Stands in for GitHubClient: releases and downloads come from local files."""

    rate_remaining = None

    def __init__(self):
        self.releases: dict[str, dict] = {}
        self.files: dict[str, Path] = {}

    def publish(self, repo, rid, tag, files: dict[str, Path]):
        self.releases[repo] = {
            "id": rid, "tag_name": tag, "published_at": "2026-11-01T00:00:00Z",
            "zipball_url": f"zipball:{repo}:{tag}",
            "assets": [{"name": n, "size": p.stat().st_size, "browser_download_url": f"dl:{repo}:{n}",
                        "url": "", "state": "uploaded"} for n, p in files.items()],
        }
        for n, p in files.items():
            self.files[f"dl:{repo}:{n}"] = p

    def latest_release(self, repo, include_prereleases=False):
        return self.releases[repo]

    def download(self, asset, dest, progress=None):
        shutil.copyfile(self.files[asset.download_url], dest)
        return dest


class ManagerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.src = self.root / "published"
        self.src.mkdir()
        self.game_dir = self.root / "Game" / "Mods"
        self.game_dir.mkdir(parents=True)
        (self.game_dir / "settings.cfg").write_text("original")
        (self.game_dir / "other.txt").write_text("not ours")

        self.store = Store(self.root / "data")
        self.game = Game(name="Test Game", mods_dir=str(self.game_dir))
        self.store.games.append(self.game)
        self.logs = []
        self.manager = ModManager(self.store, log=lambda m, level="info": self.logs.append((level, m)))
        self.fake = FakeClient()
        self.manager._client, self.manager._client_token = self.fake, ""

    def add_mod(self, **kw) -> Mod:
        mod = Mod(game_id=self.game.id, repo="me/cool-mod", **kw)
        self.store.mods.append(mod)
        return mod

    def test_install_update_disable_enable_remove(self):
        v1 = make_zip(self.src / "CoolMod-1.0.zip", {
            "CoolMod/plugin.dll": "v1", "CoolMod/readme.txt": "hi", "settings.cfg": "modded"})
        self.fake.publish("me/cool-mod", 1, "v1.0", {"CoolMod-1.0.zip": v1})
        mod = self.add_mod(asset_pattern="CoolMod-*.zip")

        self.assertTrue(self.manager.install(mod))
        self.assertEqual((self.game_dir / "CoolMod/plugin.dll").read_text(), "v1")
        self.assertEqual((self.game_dir / "settings.cfg").read_text(), "modded")
        self.assertEqual(mod.backups, ["settings.cfg"])
        self.assertEqual(mod.installed_tag, "v1.0")
        self.assertFalse(self.manager.install(mod))  # already up to date

        # v2 drops readme.txt and no longer replaces settings.cfg.
        v2 = make_zip(self.src / "CoolMod-2.0.zip", {"CoolMod/plugin.dll": "v2"})
        self.fake.publish("me/cool-mod", 2, "v2.0", {"CoolMod-2.0.zip": v2})
        summary = self.manager.check_many([mod], install=True)
        self.assertEqual(summary.updated, ["cool-mod v2.0"])
        self.assertEqual((self.game_dir / "CoolMod/plugin.dll").read_text(), "v2")
        self.assertFalse((self.game_dir / "CoolMod/readme.txt").exists())
        self.assertEqual((self.game_dir / "settings.cfg").read_text(), "original")
        self.assertEqual(mod.backups, [])
        self.assertEqual(mod.installed_files, ["CoolMod/plugin.dll"])

        self.manager.set_enabled(mod, False)
        self.assertFalse((self.game_dir / "CoolMod").exists())
        self.manager.set_enabled(mod, True)
        self.assertEqual((self.game_dir / "CoolMod/plugin.dll").read_text(), "v2")

        self.manager.remove_mod(mod)
        self.assertFalse((self.game_dir / "CoolMod").exists())
        self.assertEqual(sorted(p.name for p in self.game_dir.iterdir()), ["other.txt", "settings.cfg"])
        self.assertEqual((self.game_dir / "settings.cfg").read_text(), "original")
        self.assertEqual(self.store.mods, [])

    def test_backup_restored_when_removed(self):
        v1 = make_zip(self.src / "m.zip", {"settings.cfg": "modded"})
        self.fake.publish("me/cool-mod", 1, "v1", {"m.zip": v1})
        mod = self.add_mod()
        self.manager.install(mod)
        self.assertEqual((self.game_dir / "settings.cfg").read_text(), "modded")
        self.manager.remove_mod(mod)
        self.assertEqual((self.game_dir / "settings.cfg").read_text(), "original")

    def test_auto_update_off_only_reports(self):
        v1 = make_zip(self.src / "m1.zip", {"a.txt": "1"})
        self.fake.publish("me/cool-mod", 1, "v1", {"m.zip": v1})
        mod = self.add_mod(auto_update=False)
        self.manager.install(mod)
        v2 = make_zip(self.src / "m2.zip", {"a.txt": "2"})
        self.fake.publish("me/cool-mod", 2, "v2", {"m.zip": v2})
        summary = self.manager.check_many([mod], install=True, auto_only=True)
        self.assertEqual(summary.available, ["cool-mod"])
        self.assertTrue(mod.update_available)
        self.assertEqual((self.game_dir / "a.txt").read_text(), "1")
        self.manager.check_many([mod], install=True, auto_only=False)  # "Update all"
        self.assertEqual((self.game_dir / "a.txt").read_text(), "2")

    def test_subfolder_and_plain_files(self):
        jar = make_zip(self.src / "cool-1.0.jar", {"META-INF/MANIFEST.MF": "x"})  # a jar is a zip
        self.fake.publish("me/cool-mod", 1, "v1", {"cool-1.0.jar": jar})
        mod = self.add_mod(subdir="plugins\\extra")
        self.manager.install(mod)
        self.assertTrue(zipfile.is_zipfile(self.game_dir / "plugins/extra/cool-1.0.jar"))
        self.assertEqual(mod.installed_files, ["plugins/extra/cool-1.0.jar"])

    def test_bad_release_leaves_game_folder_alone(self):
        good = make_zip(self.src / "good.zip", {"a.txt": "ok"})
        self.fake.publish("me/cool-mod", 1, "v1", {"m.zip": good})
        mod = self.add_mod()
        self.manager.install(mod)
        evil = make_zip(self.src / "evil.zip", {"../../escaped.txt": "gotcha"})
        self.fake.publish("me/cool-mod", 2, "v2", {"m.zip": evil})
        with self.assertRaises(InstallError):
            self.manager.install(mod)
        self.assertFalse((self.root / "Game" / "escaped.txt").exists())
        self.assertFalse((self.root / "escaped.txt").exists())
        self.assertEqual((self.game_dir / "a.txt").read_text(), "ok")
        self.assertEqual(mod.installed_tag, "v1")
        self.assertIn("outside", mod.last_error)

    def test_two_mods_sharing_a_file(self):
        a = make_zip(self.src / "a.zip", {"shared.dll": "A", "a.txt": "a"})
        b = make_zip(self.src / "b.zip", {"shared.dll": "B", "b.txt": "b"})
        self.fake.publish("me/a", 1, "v1", {"a.zip": a})
        self.fake.publish("me/b", 1, "v1", {"b.zip": b})
        mod_a = Mod(game_id=self.game.id, repo="me/a")
        mod_b = Mod(game_id=self.game.id, repo="me/b")
        self.store.mods += [mod_a, mod_b]
        self.manager.install(mod_a)
        self.manager.install(mod_b)
        self.assertEqual((self.game_dir / "shared.dll").read_text(), "B")
        self.assertTrue(any("also comes from" in m for _, m in self.logs))
        self.manager.remove_mod(mod_a)  # b still needs shared.dll
        self.assertTrue((self.game_dir / "shared.dll").exists())
        self.manager.remove_mod(mod_b)
        self.assertFalse((self.game_dir / "shared.dll").exists())

    def test_config_round_trip(self):
        mod = self.add_mod(name="Cool", asset_pattern="*.zip")
        self.store.save()
        again = Store(self.root / "data")
        self.assertEqual(again.games[0].name, "Test Game")
        self.assertEqual(again.mods[0].name, "Cool")
        self.assertEqual(again.mods[0].id, mod.id)

    def test_broken_config_is_kept_aside(self):
        (self.root / "data" / "config.json").write_text("{not json")
        again = Store(self.root / "data")
        self.assertEqual(again.games, [])
        self.assertIn("could not be read", again.load_warning)
        self.assertTrue(list((self.root / "data").glob("config.broken-*.json")))


class StagingTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def stage(self, asset, files, **kw):
        path = make_zip(self.root / "dl.zip", files)
        opts = {"extract": True, "strip_root": False, "folder_name": "cool-mod"} | kw
        return stage_files([(asset, path)], self.root / "out", self.root / "work", **opts)

    def test_strip_single_top_folder(self):
        files = self.stage(Asset("m.zip", ""), {"CoolMod-1.0/a.txt": "", "CoolMod-1.0/sub/b.txt": ""},
                           strip_root=True)
        self.assertEqual(files, ["a.txt", "sub/b.txt"])

    def test_source_zip_folder_named_after_repo(self):
        files = self.stage(Asset("source-v1.zip", "", is_source=True), {"me-cool-mod-abc123/x.lua": ""})
        self.assertEqual(files, ["cool-mod/x.lua"])

    def test_junk_is_skipped(self):
        files = self.stage(Asset("m.zip", ""), {"a.txt": "", "__MACOSX/._a.txt": "", "sub/.DS_Store": ""})
        self.assertEqual(files, ["a.txt"])

    def test_extract_off_copies_zip(self):
        files = self.stage(Asset("m.zip", ""), {"a.txt": ""}, extract=False)
        self.assertEqual(files, ["m.zip"])

    def test_clean_subdir(self):
        self.assertEqual(clean_subdir(" \\plugins\\extra\\ "), "plugins/extra")
        self.assertEqual(clean_subdir(""), "")
        for bad in ("../x", "C:\\x", "a/../../b"):
            with self.assertRaises(InstallError):
                clean_subdir(bad)


if __name__ == "__main__":
    unittest.main()
