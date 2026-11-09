import tempfile
import unittest
from pathlib import Path

from modloader.config import Game, Mod, Store
from modloader.manager import ModManager
from modloader.requires import MANIFEST_NAME, decline, dependents, new_mod, parse_manifest, unmet
from test_installer import FakeClient, make_zip


class ParseManifestTests(unittest.TestCase):
    def test_strings_objects_and_links(self):
        found, problems = parse_manifest('''{"requires": [
            "MichaelRyan2319/RSDragonwilds-UE4SS",
            {"repo": "https://github.com/BepInEx/BepInEx", "name": "BepInEx",
             "file": "BepInEx_win_x64_*.zip", "install_into": "\\\\BepInEx\\\\core\\\\"}
        ]}''')
        self.assertEqual(problems, [])
        self.assertEqual(found, [
            {"repo": "MichaelRyan2319/RSDragonwilds-UE4SS", "name": "", "file": "", "install_into": ""},
            {"repo": "BepInEx/BepInEx", "name": "BepInEx", "file": "BepInEx_win_x64_*.zip",
             "install_into": "BepInEx/core"},
        ])

    def test_single_entry_bom_duplicates_and_itself(self):
        found, _ = parse_manifest('﻿{"requires": "a/core"}')
        self.assertEqual([r["repo"] for r in found], ["a/core"])
        found, _ = parse_manifest('{"requires": ["a/core", "A/Core", "me/mod"]}', own_repo="me/mod")
        self.assertEqual([r["repo"] for r in found], ["a/core"])

    def test_no_requires_key(self):
        self.assertEqual(parse_manifest('{"name": "Cool"}'), ([], []))

    def test_bad_entries_are_skipped_and_explained(self):
        found, problems = parse_manifest('{"requires": ["a/core", "not a repo", 5, {"name": "x"}, '
                                         '{"repo": "b/lib", "install_into": "../escape"}]}')
        self.assertEqual([r["repo"] for r in found], ["a/core", "b/lib"])
        self.assertEqual(found[1]["install_into"], "")
        self.assertEqual(len(problems), 4)

    def test_broken_files(self):
        for text in ("{not json", '["a/core"]', '{"requires": 5}'):
            found, problems = parse_manifest(text)
            self.assertEqual(found, [], text)
            self.assertEqual(len(problems), 1, text)


class RequirementTests(unittest.TestCase):
    """Installing, updating and removing mods that need other mods."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.src = self.root / "published"
        self.src.mkdir()
        self.game_dir = self.root / "Game" / "Mods"
        self.game_dir.mkdir(parents=True)
        self.store = Store(self.root / "data")
        self.game = Game(name="Test Game", mods_dir=str(self.game_dir))
        self.other_game = Game(name="Other Game", mods_dir=str(self.root / "Other"))
        self.store.games += [self.game, self.other_game]
        self.logs, self.asked = [], []
        self.manager = ModManager(self.store, log=lambda m, level="info": self.logs.append((level, m)),
                                  on_requirements=lambda mod, again: self.asked.append((mod.repo, again)))
        self.fake = FakeClient()
        self.manager._client, self.manager._client_token = self.fake, ""

    def publish(self, repo, rid, tag, needs=None):
        z = make_zip(self.src / f"{repo.replace('/', '_')}-{rid}.zip", {f"{repo.split('/')[1]}/file.txt": tag})
        self.fake.publish(repo, rid, tag, {"m.zip": z})
        if needs is not None:
            self.fake.repo_files[(repo, MANIFEST_NAME, tag)] = needs

    def add(self, repo, game=None, **kw) -> Mod:
        mod = Mod(game_id=(game or self.game).id, repo=repo, **kw)
        self.store.mods.append(mod)
        return mod

    def test_install_reads_requirements_and_reports_missing(self):
        self.publish("me/lua-mod", 1, "v1", '{"requires": [{"repo": "me/core", "name": "Core"}]}')
        mod = self.add("me/lua-mod")
        self.manager.install(mod)
        self.assertEqual([r["repo"] for r in mod.requires], ["me/core"])
        self.assertEqual(mod.requires_release_id, 1)
        self.assertEqual(self.asked, [("me/lua-mod", False)])
        self.assertTrue(any("needs Core (me/core), which isn't added to Test Game" in m for _, m in self.logs))
        self.assertEqual(self.fake.file_reads, [("me/lua-mod", MANIFEST_NAME, "v1")])

        # Saved and loaded again like any other setting.
        self.store.save()
        again = Store(self.root / "data")
        self.assertEqual(again.mods[0].requires, mod.requires)

    def test_nothing_to_ask_when_the_required_mod_is_there(self):
        self.publish("me/lua-mod", 1, "v1", '{"requires": ["me/core"]}')
        self.add("me/Core")  # same repo, different case
        mod = self.add("me/lua-mod")
        self.manager.install(mod)
        self.assertEqual(self.asked, [])
        self.assertEqual(unmet(self.store, mod), [])

    def test_required_mod_in_another_game_doesnt_count(self):
        self.publish("me/lua-mod", 1, "v1", '{"requires": ["me/core"]}')
        self.add("me/core", game=self.other_game)
        mod = self.add("me/lua-mod")
        self.manager.install(mod)
        self.assertEqual(self.asked, [("me/lua-mod", False)])

    def test_disabled_required_mod_is_reported(self):
        self.publish("me/lua-mod", 1, "v1", '{"requires": ["me/core"]}')
        core = self.add("me/core", enabled=False)
        mod = self.add("me/lua-mod")
        self.manager.install(mod)
        self.assertEqual(unmet(self.store, mod), [(mod.requires[0], core)])
        self.assertTrue(any("which is disabled" in m for _, m in self.logs))

    def test_declined_is_only_asked_again_when_the_player_asks(self):
        self.publish("me/lua-mod", 1, "v1", '{"requires": ["me/core"]}')
        mod = self.add("me/lua-mod")
        self.manager.install(mod)
        decline(mod, "me/core")
        self.asked.clear()
        self.manager.install(mod)  # up to date; nothing new to ask
        self.manager.check_many([mod], install=True)
        self.assertEqual(self.asked, [])
        self.manager.install(mod, force=True, ask_declined=True)  # "Reinstall"
        self.assertEqual(self.asked, [("me/lua-mod", True)])
        self.assertEqual(len(unmet(self.store, mod)), 1)  # the status still shows it's missing

    def test_requirements_only_read_once_per_release(self):
        self.publish("me/lua-mod", 1, "v1", '{"requires": ["me/core"]}')
        mod = self.add("me/lua-mod")
        self.manager.install(mod)
        self.manager.install(mod)
        self.manager.check_many([mod], install=True)
        self.assertEqual(len(self.fake.file_reads), 1)

    def test_update_that_adds_a_requirement(self):
        self.publish("me/lua-mod", 1, "v1")  # no modloader.json
        mod = self.add("me/lua-mod")
        self.manager.install(mod)
        self.assertEqual((mod.requires, self.asked), ([], []))
        self.publish("me/lua-mod", 2, "v2", '{"requires": ["me/core"]}')
        self.manager.check_many([mod], install=True)
        self.assertEqual(mod.installed_tag, "v2")
        self.assertEqual(self.asked, [("me/lua-mod", False)])

    def test_mod_installed_by_an_older_mod_loader_is_read_on_the_next_check(self):
        self.publish("me/lua-mod", 1, "v1", '{"requires": ["me/core"]}')
        mod = self.add("me/lua-mod", installed_release_id=1, installed_tag="v1")  # requires never read
        self.manager.check_many([mod], install=True)
        self.assertEqual([r["repo"] for r in mod.requires], ["me/core"])
        self.assertEqual(self.asked, [("me/lua-mod", False)])

    def test_read_failure_doesnt_stop_the_install(self):
        from modloader.github import GitHubError

        def broken(*args):
            raise GitHubError("GitHub's rate limit was reached")

        self.publish("me/lua-mod", 1, "v1", '{"requires": ["me/core"]}')
        self.fake.read_file = broken
        mod = self.add("me/lua-mod")
        self.assertTrue(self.manager.install(mod))
        self.assertTrue((self.game_dir / "lua-mod/file.txt").exists())
        self.assertIsNone(mod.requires_release_id)  # tried again next time
        self.assertTrue(any("Couldn't check which mods" in m for _, m in self.logs))

    def test_broken_manifest_is_logged_not_fatal(self):
        self.publish("me/lua-mod", 1, "v1", "{oops")
        mod = self.add("me/lua-mod")
        self.assertTrue(self.manager.install(mod))
        self.assertEqual(mod.requires, [])
        self.assertTrue(any("modloader.json: it isn't valid JSON" in m for _, m in self.logs))

    def test_adding_the_required_mod(self):
        self.publish("me/lua-mod", 1, "v1",
                     '{"requires": [{"repo": "me/core", "name": "Core", "install_into": "loader"}]}')
        self.publish("me/core", 1, "v1")
        mod = self.add("me/lua-mod")
        self.manager.install(mod)
        core = new_mod(self.game.id, mod.requires[0])
        self.assertEqual((core.repo, core.name, core.subdir), ("me/core", "Core", "loader"))
        self.store.mods.append(core)
        self.manager.install(core)
        self.assertTrue((self.game_dir / "loader/core/file.txt").exists())
        self.assertEqual(unmet(self.store, mod), [])
        self.assertEqual(dependents(self.store, core), [mod])
        mod.enabled = False
        self.assertEqual(dependents(self.store, core), [])

    def test_enabling_a_mod_reports_what_it_needs(self):
        self.publish("me/lua-mod", 1, "v1", '{"requires": ["me/core"]}')
        mod = self.add("me/lua-mod")
        self.manager.install(mod)
        self.manager.set_enabled(mod, False)
        self.asked.clear()
        self.manager.set_enabled(mod, True)
        self.assertEqual(self.asked, [("me/lua-mod", False)])

    def test_files_are_untouched_by_requirements(self):
        """Install, update, disable/enable and remove still leave the game folder as it was."""
        self.publish("me/lua-mod", 1, "v1", '{"requires": ["me/core"]}')
        mod = self.add("me/lua-mod")
        self.manager.install(mod)
        self.publish("me/lua-mod", 2, "v2", '{"requires": ["me/core", "me/extra"]}')
        self.manager.check_many([mod], install=True)
        self.assertEqual((self.game_dir / "lua-mod/file.txt").read_text(), "v2")
        self.manager.set_enabled(mod, False)
        self.manager.set_enabled(mod, True)
        self.manager.remove_mod(mod)
        self.assertEqual(list(self.game_dir.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
