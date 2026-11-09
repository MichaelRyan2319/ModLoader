import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from modloader import selfupdate
from modloader.github import GitHubError
from modloader.installer import InstallError


class FakeClient:
    def __init__(self, tag: str, files: dict[str, Path]):
        self.release = {
            "id": 1, "tag_name": tag, "html_url": f"https://github.com/me/ModLoader/releases/tag/{tag}",
            "assets": [{"name": n, "size": p.stat().st_size, "browser_download_url": f"dl:{n}", "url": "",
                        "state": "uploaded"} for n, p in files.items()],
        }
        self.files = {f"dl:{n}": p for n, p in files.items()}

    def latest_release(self, repo, include_prereleases=False):
        return self.release

    def download(self, asset, dest, progress=None):
        shutil.copyfile(self.files[asset.download_url], dest)
        return Path(dest)


class SelfUpdateTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.published = self.root / "published"
        self.published.mkdir()

    def exe_release(self, tag="v1.2.0", content=b"new exe"):
        exe = self.published / "ModLoader.exe"
        exe.write_bytes(content)
        return FakeClient(tag, {"ModLoader.exe": exe})

    def test_versions(self):
        self.assertEqual(selfupdate.parse_version("v1.10.0"), (1, 10, 0))
        self.assertGreater(selfupdate.parse_version("1.10"), selfupdate.parse_version("1.9.9"))
        self.assertIsNone(selfupdate.parse_version("nightly"))

    def test_only_newer_releases_count(self):
        client = self.exe_release("v1.2.0")
        self.assertIsNone(selfupdate.find_update(client, current="1.2.0", frozen=True))
        self.assertIsNone(selfupdate.find_update(client, current="1.3.0", frozen=True))
        update = selfupdate.find_update(client, current="1.1.9", frozen=True)
        self.assertEqual((update.version, update.asset.name), ("1.2.0", "ModLoader.exe"))

    def test_missing_file_is_explained(self):
        with self.assertRaisesRegex(GitHubError, "doesn't have ModLoader-source.zip"):
            selfupdate.find_update(self.exe_release(), current="1.0.0", frozen=False)

    def test_exe_is_swapped(self):
        app_dir = self.root / "app"
        app_dir.mkdir()
        exe = app_dir / "ModLoader.exe"
        exe.write_bytes(b"old exe")
        client = self.exe_release()
        update = selfupdate.find_update(client, current="1.0.0", frozen=True)
        selfupdate.install(client, update, self.root / "work", exe=exe)
        self.assertEqual(exe.read_bytes(), b"new exe")
        self.assertEqual((app_dir / "ModLoader.exe.old").read_bytes(), b"old exe")
        self.assertFalse((app_dir / "ModLoader.exe.new").exists())
        self.assertFalse((self.root / "work").exists())

        with mock.patch.object(selfupdate.sys, "frozen", True, create=True), \
                mock.patch.object(selfupdate.sys, "executable", str(exe)):
            selfupdate.clean_up_old_exe()
        self.assertFalse((app_dir / "ModLoader.exe.old").exists())

    def test_source_is_unpacked(self):
        app = self.root / "app"
        (app / "modloader").mkdir(parents=True)
        (app / "modloader" / "gone.py").write_text("old module")
        (app / "modloader" / "__init__.py").write_text("old")
        (app / "notes.txt").write_text("the player's own file")
        source = self.published / "ModLoader-source.zip"
        with zipfile.ZipFile(source, "w") as zf:
            zf.writestr("ModLoader/ModLoader.pyw", "entry")
            zf.writestr("ModLoader/modloader/__init__.py", "__version__ = '1.2.0'")
            zf.writestr("ModLoader/modloader/new.py", "new module")
        client = FakeClient("v1.2.0", {"ModLoader-source.zip": source})
        update = selfupdate.find_update(client, current="1.0.0", frozen=False)
        selfupdate.install(client, update, self.root / "work", root=app)
        self.assertEqual((app / "modloader" / "__init__.py").read_text(), "__version__ = '1.2.0'")
        self.assertTrue((app / "modloader" / "new.py").exists())
        self.assertFalse((app / "modloader" / "gone.py").exists())
        self.assertEqual((app / "notes.txt").read_text(), "the player's own file")

    def test_source_zip_must_be_mod_loader(self):
        source = self.published / "ModLoader-source.zip"
        with zipfile.ZipFile(source, "w") as zf:
            zf.writestr("something/else.txt", "x")
        client = FakeClient("v1.2.0", {"ModLoader-source.zip": source})
        update = selfupdate.find_update(client, current="1.0.0", frozen=False)
        app = self.root / "app"
        app.mkdir()
        with self.assertRaisesRegex(InstallError, "doesn't look like Mod Loader"):
            selfupdate.install(client, update, self.root / "work", root=app)
        self.assertEqual(list(app.iterdir()), [])

    def test_git_checkout_is_left_alone(self):
        (self.root / ".git").mkdir()
        self.assertIn("git pull", selfupdate.blocked_reason(self.root))
        self.assertEqual(selfupdate.blocked_reason(self.root / "published"), "")


if __name__ == "__main__":
    unittest.main()
