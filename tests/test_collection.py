import base64
import json
import tempfile
import unittest
import zlib
from pathlib import Path

from modloader import collection
from modloader.collection import CodeError, export_code, import_shared, in_use, members, parse_code
from modloader.config import Collection, Game, Mod, Store
from modloader.manager import ModManager
from modloader.requires import MANIFEST_NAME
from test_installer import FakeClient, make_zip


def code_for(payload, number=1) -> str:
    """A code holding any JSON, to test what parse_code does with odd ones."""
    packed = zlib.compress(json.dumps(payload).encode("utf-8"))
    return f"MLC{number}:" + base64.b64encode(packed).decode("ascii")


class CodeTests(unittest.TestCase):
    def mods(self) -> list[Mod]:
        return [
            Mod(game_id="g", repo="me/plain"),
            Mod(game_id="g", repo="BepInEx/BepInEx", name="BepInEx", asset_pattern="BepInEx_win_x64_*.zip",
                subdir="loader/core", extract=False, strip_root=True, include_prereleases=True,
                auto_update=False, enabled=False, installed_tag="v5", installed_files=["a.dll"]),
        ]

    def test_round_trip_keeps_how_each_mod_is_added(self):
        code = export_code("  Co-op   pack ", "Valheim", self.mods())
        self.assertRegex(code, r"^MLC1:[A-Za-z0-9+/]+=*$")
        shared = parse_code(code)
        self.assertEqual((shared.name, shared.game, shared.problems), ("Co-op pack", "Valheim", []))
        self.assertEqual(shared.mods, [
            {"repo": "me/plain", "name": "", "file": "", "install_into": ""},
            {"repo": "BepInEx/BepInEx", "name": "BepInEx", "file": "BepInEx_win_x64_*.zip",
             "install_into": "loader/core", "unpack": False, "strip_root": True, "prereleases": True},
        ])
        plain, bepinex = (collection.new_mod("other", e) for e in shared.mods)
        self.assertEqual((plain.repo, plain.extract, plain.strip_root, plain.include_prereleases),
                         ("me/plain", True, False, False))
        self.assertEqual((bepinex.name, bepinex.asset_pattern, bepinex.subdir), ("BepInEx", "BepInEx_win_x64_*.zip",
                                                                                 "loader/core"))
        self.assertEqual((bepinex.extract, bepinex.strip_root, bepinex.include_prereleases), (False, True, True))
        # Nothing about this player's own copy travels with the code.
        self.assertEqual((bepinex.game_id, bepinex.enabled, bepinex.auto_update, bepinex.installed_tag,
                          bepinex.installed_files), ("other", True, True, "", []))

    def test_code_holds_no_token_or_paths(self):
        raw = zlib.decompress(base64.b64decode(export_code("P", "Game", self.mods()).split(":")[1]))
        data = json.loads(raw)
        self.assertEqual(sorted(data), ["game", "mods", "name"])
        self.assertEqual(data["mods"][0], "me/plain")  # the usual settings: just the repository
        self.assertEqual(sorted(data["mods"][1]), ["file", "install_into", "name", "prereleases", "repo",
                                                   "strip_root", "unpack"])

    def test_pasted_with_wrapping_quotes_or_a_message_around_it(self):
        code = export_code("Pack", "Game", self.mods())
        wrapped = "\n".join(code[i:i + 20] for i in range(0, len(code), 20))
        for text in (f"  {code}\n", f"`{code}`", f'"{code}"', wrapped, code.rstrip("="), code.lower()[:4] + code[4:],
                     f"here's my pack: {code} enjoy!", f"try this\n{code}\nhave fun"):
            self.assertEqual(parse_code(text).name, "Pack", text)

    def test_url_safe_alphabet_is_read_too(self):
        code = export_code("Pack", "Game", [Mod(game_id="g", repo=f"owner/mod-{i}") for i in range(40)])
        body = code.split(":")[1]
        self.assertEqual(len(parse_code("MLC1:" + body.replace("+", "-").replace("/", "_")).mods), 40)

    def test_bad_codes_are_explained(self):
        good = export_code("Pack", "Game", self.mods())
        for text, expected in (
            ("", "Paste a collection code"),
            ("hello there", "isn't a Mod Loader collection code"),
            ("https://github.com/me/mod", "isn't a Mod Loader collection code"),
            (good[:-12], "incomplete or damaged"),
            (good[:9] + "AAAA" + good[13:], "incomplete or damaged"),
            ("MLC1:!!!!", "isn't a Mod Loader collection code"),
            ("MLC1:" + base64.b64encode(b"not zlib").decode(), "incomplete or damaged"),
            (code_for(["me/mod"]), "incomplete or damaged"),
            (code_for({"name": "x", "mods": "me/mod"}), "incomplete or damaged"),
            ("MLC1:" + base64.b64encode(zlib.compress(b"{oops")).decode(), "incomplete or damaged"),
            (code_for({"name": "x", "mods": []}), "no mods in it"),
            (code_for({"name": "x", "mods": ["not a repo", 5]}), "no mods in it"),
            (code_for({"name": "x", "mods": ["me/mod"]}, number=2), "newer Mod Loader"),
            (code_for({"mods": [f"me/mod{i}" for i in range(501)]}), "too many"),
            (code_for({"mods": ["me/mod"], "junk": "x" * 1_100_000}), "too big"),
        ):
            with self.assertRaises(CodeError, msg=text[:40]) as caught:
                parse_code(text)
            self.assertIn(expected, str(caught.exception), text[:40])

    def test_odd_entries_are_skipped_or_made_safe(self):
        shared = parse_code(code_for({"name": "  ", "game": 7, "mods": [
            "me/core", "ME/Core", "https://github.com/me/linked", "not a repo", 5, {"name": "no repo"},
            {"repo": "me/escape", "install_into": "..\\..\\Windows", "name": "A\nB   C", "unpack": 0,
             "strip_root": "yes", "prereleases": 1},
        ], "future_key": True}))
        self.assertEqual(shared.name, "Imported collection")
        self.assertEqual(shared.game, "7")
        self.assertEqual([e["repo"] for e in shared.mods], ["me/core", "me/linked", "me/escape"])
        self.assertEqual(shared.mods[2], {"repo": "me/escape", "name": "A B C", "file": "", "install_into": ""})
        self.assertEqual(len(shared.problems), 4)
        mod = collection.new_mod("g", shared.mods[2])
        self.assertEqual((mod.subdir, mod.extract, mod.strip_root, mod.include_prereleases), ("", True, False, False))

    def test_long_names_are_cut(self):
        shared = parse_code(code_for({"name": "n" * 500, "game": "g" * 500, "mods": [{"repo": "a/b", "name": "m" * 500}]}))
        self.assertEqual((len(shared.name), len(shared.game), len(shared.mods[0]["name"])), (60, 60, 80))


class CollectionTests(unittest.TestCase):
    """Collections of a game's mods: saving, switching, importing, and tidying up."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.src = self.root / "published"
        self.src.mkdir()
        self.game_dir = self.root / "Game" / "Mods"
        self.game_dir.mkdir(parents=True)
        (self.game_dir / "settings.cfg").write_text("original")
        self.store = Store(self.root / "data")
        self.game = Game(name="Test Game", mods_dir=str(self.game_dir))
        self.other_game = Game(name="Other Game", mods_dir=str(self.root / "Other"))
        self.store.games += [self.game, self.other_game]
        self.logs, self.asked = [], []
        self.manager = ModManager(self.store, log=lambda m, level="info": self.logs.append((level, m)),
                                  on_requirements=lambda mod, again: self.asked.append((mod.repo, again)))
        self.fake = FakeClient()
        self.manager._client, self.manager._client_token = self.fake, ""

    def publish(self, repo, files=None, needs=None, tag="v1"):
        name = repo.split("/")[1]
        z = make_zip(self.src / f"{repo.replace('/', '_')}.zip", files or {f"{name}/file.txt": name})
        self.fake.publish(repo, 1, tag, {"m.zip": z})
        if needs is not None:
            self.fake.repo_files[(repo, MANIFEST_NAME, tag)] = needs

    def add(self, repo, game=None, install=True, **kw) -> Mod:
        mod = Mod(game_id=(game or self.game).id, repo=repo, **kw)
        self.store.mods.append(mod)
        if install:
            self.publish(repo)
            self.manager.install(mod)
        return mod

    def collect(self, name, *mods, game=None) -> Collection:
        coll = Collection(game_id=(game or self.game).id, name=name, mod_ids=[m.id for m in mods])
        self.store.collections.append(coll)
        return coll

    def folder(self) -> list[str]:
        return sorted(p.relative_to(self.game_dir).as_posix() for p in self.game_dir.rglob("*") if p.is_file())

    def test_saved_and_loaded(self):
        a, b = self.add("me/a"), self.add("me/b")
        coll = self.collect("Co-op", b, a)
        self.store.save()
        again = Store(self.root / "data")
        self.assertEqual([(c.id, c.game_id, c.name, c.mod_ids) for c in again.collections],
                         [(coll.id, self.game.id, "Co-op", [b.id, a.id])])
        self.assertEqual(again.collections_for(self.other_game.id), [])
        self.assertEqual(again.collection(coll.id).name, "Co-op")

    def test_config_from_before_collections_still_loads(self):
        self.add("me/a")
        self.store.save()
        path = self.root / "data" / "config.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        del data["collections"]
        path.write_text(json.dumps(data), encoding="utf-8")
        again = Store(self.root / "data")
        self.assertEqual((len(again.mods), again.collections, again.load_warning), (1, [], ""))

    def test_members_and_in_use(self):
        a, b, c = self.add("me/a"), self.add("me/b"), self.add("me/c")
        elsewhere = self.add("me/a", game=self.other_game)
        coll = self.collect("Pair", b, a)
        coll.mod_ids.append("gone")  # a mod that no longer exists
        self.assertEqual(members(self.store, coll), [a, b])  # the game's order
        self.assertFalse(in_use(self.store, coll))  # c is enabled too
        self.manager.set_enabled(c, False)
        self.assertTrue(in_use(self.store, coll))
        self.manager.set_enabled(elsewhere, False)  # another game's mods don't matter
        self.assertTrue(in_use(self.store, coll))
        self.manager.set_enabled(a, False)
        self.assertFalse(in_use(self.store, coll))

    def test_an_empty_collection_turns_every_mod_off(self):
        a, b = self.add("me/a"), self.add("me/b")
        vanilla = self.collect("No mods")
        self.assertFalse(in_use(self.store, vanilla))
        self.manager.use_collection(vanilla)
        self.assertEqual((a.enabled, b.enabled), (False, False))
        self.assertEqual(self.folder(), ["settings.cfg"])
        self.assertTrue(in_use(self.store, vanilla))
        self.assertFalse(in_use(self.store, self.collect("Empty", game=self.other_game)))  # a game with no mods

    def test_using_a_collection_switches_mods_and_back(self):
        a, b, c = self.add("me/a"), self.add("me/b"), self.add("me/c")
        self.assertEqual(self.folder(), ["a/file.txt", "b/file.txt", "c/file.txt", "settings.cfg"])
        first, second = self.collect("First", a, b), self.collect("Second", c)

        self.assertEqual(self.manager.use_collection(first), 0)
        self.assertEqual((a.enabled, b.enabled, c.enabled), (True, True, False))
        self.assertEqual(self.folder(), ["a/file.txt", "b/file.txt", "settings.cfg"])
        self.assertTrue(in_use(self.store, first))

        self.manager.use_collection(second)
        self.assertEqual((a.enabled, b.enabled, c.enabled), (False, False, True))
        self.assertEqual(self.folder(), ["c/file.txt", "settings.cfg"])
        self.assertTrue(in_use(self.store, second) and not in_use(self.store, first))

        self.logs.clear()
        self.manager.use_collection(second)  # nothing to do
        self.assertTrue(any("already in use" in m for _, m in self.logs))
        self.assertEqual(self.folder(), ["c/file.txt", "settings.cfg"])

        # Saved, so the next start sees the same mods enabled.
        again = Store(self.root / "data")
        self.assertEqual([m.enabled for m in again.mods], [False, False, True])

    def test_other_games_are_left_alone(self):
        a = self.add("me/a")
        elsewhere = self.add("me/b", game=self.other_game)
        self.manager.use_collection(self.collect("Only a", a))
        self.assertTrue(elsewhere.enabled)
        self.assertTrue((self.root / "Other" / "b" / "file.txt").exists())

    def test_using_a_collection_installs_a_mod_that_never_was(self):
        a = self.add("me/a")
        late = self.add("me/late", install=False, enabled=False)
        self.publish("me/late")
        self.manager.use_collection(self.collect("Both", a, late))
        self.assertTrue(late.enabled and late.installed)
        self.assertEqual(self.folder(), ["a/file.txt", "late/file.txt", "settings.cfg"])

    def test_a_mod_that_cannot_be_switched_is_reported(self):
        from modloader.github import GitHubError

        a = self.add("me/a")
        broken = self.add("me/broken", install=False, enabled=False)

        def no_release(repo, include_prereleases=False):
            if repo == "me/broken":
                raise GitHubError("me/broken has no releases yet.")
            return self.fake.releases[repo]

        self.fake.latest_release = no_release
        self.assertEqual(self.manager.use_collection(self.collect("Both", a, broken)), 1)
        self.assertTrue(any(level == "error" and "couldn't be changed" in m for level, m in self.logs))
        self.assertTrue(a.enabled)

    def test_needed_mods_are_asked_about_once_the_switch_is_done(self):
        self.publish("me/core")
        self.publish("me/lua", needs='{"requires": ["me/core"]}')
        self.publish("me/solo", needs='{"requires": ["me/core"]}')
        core = self.add("me/core", install=False)
        lua = self.add("me/lua", install=False)
        solo = self.add("me/solo", install=False)
        for mod in (core, lua, solo):
            self.manager.install(mod)
        self.manager.use_collection(self.collect("Nothing", ))
        self.assertEqual([m.enabled for m in (core, lua, solo)], [False, False, False])

        # The collection has what lua needs: no question, whichever is enabled first.
        self.asked.clear()
        self.manager.use_collection(self.collect("With core", lua, core))
        self.assertEqual(self.asked, [])
        # This one leaves out what solo needs: asked, once, after the switch.
        self.manager.use_collection(self.collect("Without core", solo))
        self.assertEqual(self.asked, [("me/solo", False)])
        self.assertFalse(core.enabled)

    def test_replaced_files_come_back_when_their_mod_is_switched_off(self):
        self.publish("me/cfg", files={"settings.cfg": "modded", "cfg/readme.txt": "hi"})
        cfg = self.add("me/cfg", install=False)
        self.manager.install(cfg)
        plain = self.add("me/plain")
        self.assertEqual((self.game_dir / "settings.cfg").read_text(), "modded")
        with_cfg, without = self.collect("With", cfg, plain), self.collect("Without", plain)
        for _ in range(2):
            self.manager.use_collection(without)
            self.assertEqual((self.game_dir / "settings.cfg").read_text(), "original")
            self.assertEqual(self.folder(), ["plain/file.txt", "settings.cfg"])
            self.manager.use_collection(with_cfg)
            self.assertEqual((self.game_dir / "settings.cfg").read_text(), "modded")
            self.assertEqual(self.folder(), ["cfg/readme.txt", "plain/file.txt", "settings.cfg"])
        self.manager.remove_mod(cfg)
        self.manager.remove_mod(plain)
        self.assertEqual(self.folder(), ["settings.cfg"])
        self.assertEqual((self.game_dir / "settings.cfg").read_text(), "original")

    def test_removing_a_mod_or_game_tidies_collections(self):
        a, b = self.add("me/a"), self.add("me/b")
        elsewhere = self.add("me/a", game=self.other_game)
        mine, theirs = self.collect("Mine", a, b), self.collect("Theirs", elsewhere, game=self.other_game)
        self.manager.remove_mod(a)
        self.assertEqual((mine.mod_ids, theirs.mod_ids), ([b.id], [elsewhere.id]))
        self.manager.remove_game(self.game)
        self.assertEqual(self.store.collections, [theirs])
        self.assertEqual(Store(self.root / "data").collections[0].name, "Theirs")

    def test_importing_adds_what_is_missing_and_reuses_the_rest(self):
        have = self.add("me/Have", name="Mine", subdir="kept")
        off = self.add("me/off")
        self.manager.set_enabled(off, False)
        elsewhere = self.add("me/new", game=self.other_game)  # another game's copy doesn't count
        friend = [Mod(game_id="x", repo="me/have", name="Theirs", subdir="different"),
                  Mod(game_id="x", repo="me/off"),
                  Mod(game_id="x", repo="me/new", name="New one", subdir="plugins", strip_root=True)]
        shared = parse_code(export_code("Friends", "Test Game", friend))

        coll, added, replaced = import_shared(self.store, self.game, shared)
        self.assertEqual((coll.name, coll.game_id, replaced), ("Friends", self.game.id, False))
        self.assertEqual([(m.repo, m.name, m.subdir, m.strip_root, m.game_id, m.installed) for m in added],
                         [("me/new", "New one", "plugins", True, self.game.id, False)])
        self.assertEqual(coll.mod_ids, [have.id, off.id, added[0].id])
        self.assertEqual((have.name, have.subdir, off.enabled), ("Mine", "kept", False))  # theirs are left as they are
        self.assertEqual(len(self.store.mods_for(self.other_game.id)), 1)
        self.assertIsNot(added[0], elsewhere)
        self.assertEqual(self.folder(), ["kept/Have/file.txt", "settings.cfg"])  # nothing installed by importing itself

        self.publish("me/new")
        self.manager.install(added[0])
        self.assertEqual(self.folder(), ["kept/Have/file.txt", "plugins/file.txt", "settings.cfg"])
        self.assertFalse(in_use(self.store, coll))  # me/off is still disabled
        self.manager.use_collection(coll)
        self.assertTrue(in_use(self.store, coll))
        self.assertEqual(Store(self.root / "data").collections[0].mod_ids, coll.mod_ids)

    def test_importing_again_updates_the_collection_of_that_name(self):
        a = self.add("me/a")
        first = parse_code(export_code("Pack", "Test Game", [Mod(game_id="x", repo="me/a")]))
        coll, added, replaced = import_shared(self.store, self.game, first)
        self.assertEqual((coll.mod_ids, added, replaced), ([a.id], [], False))

        second = parse_code(export_code("pack", "Test Game", [Mod(game_id="x", repo="me/b")]))
        again, added, replaced = import_shared(self.store, self.game, second)
        self.assertIs(again, coll)
        self.assertEqual((replaced, coll.name, coll.mod_ids), (True, "Pack", [added[0].id]))
        self.assertIn(a, self.store.mods)  # no mod is ever removed by importing

        other, _, replaced = import_shared(self.store, self.game, second, "  My   own name ")
        self.assertEqual((other.name, replaced, len(self.store.collections)), ("My own name", False, 2))
        elsewhere, _, replaced = import_shared(self.store, self.other_game, second)  # names are per game
        self.assertEqual((elsewhere.game_id, replaced), (self.other_game.id, False))

    def test_a_needed_mod_joins_the_collections_of_the_mod_that_needs_it(self):
        lua, core, solo = (self.add(r, install=False) for r in ("me/lua", "me/core", "me/solo"))
        with_lua, without = self.collect("With lua", lua), self.collect("Without", solo)
        collection.add_alongside(self.store, lua, core)
        collection.add_alongside(self.store, lua, core)  # once only
        self.assertEqual((with_lua.mod_ids, without.mod_ids), ([lua.id, core.id], [solo.id]))


if __name__ == "__main__":
    unittest.main()
