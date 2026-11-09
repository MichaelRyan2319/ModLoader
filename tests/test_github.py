import base64
import hashlib
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from modloader.github import (Asset, GitHubClient, GitHubError, NotFoundError, parse_repo,
                              select_assets, version_glob)


def release(tag="v1.0", rid=1, names=(), prerelease=False):
    return {
        "id": rid, "tag_name": tag, "prerelease": prerelease, "draft": False,
        "published_at": "2026-11-01T10:00:00Z", "zipball_url": f"https://api.github.com/zipball/{tag}",
        "assets": [{"name": n, "size": 10, "browser_download_url": f"https://dl/{n}", "url": f"https://api/{n}",
                    "state": "uploaded"} for n in names],
    }


class ParseRepoTests(unittest.TestCase):
    def test_accepts_common_forms(self):
        for text in ("BepInEx/BepInEx", " BepInEx/BepInEx ", "https://github.com/BepInEx/BepInEx",
                     "github.com/BepInEx/BepInEx/", "https://github.com/BepInEx/BepInEx.git",
                     "https://github.com/BepInEx/BepInEx/releases/tag/v5.4", "http://www.github.com/BepInEx/BepInEx?tab=x"):
            self.assertEqual(parse_repo(text), "BepInEx/BepInEx", text)

    def test_rejects_other_things(self):
        for text in ("", "BepInEx", "https://gitlab.com/a/b", "a b/c", "-bad/repo"):
            with self.assertRaises(ValueError, msg=text):
                parse_repo(text)


class VersionGlobTests(unittest.TestCase):
    def test_versions_become_wildcards(self):
        self.assertEqual(version_glob("MyMod-1.2.3.zip"), "MyMod-*.zip")
        self.assertEqual(version_glob("BepInEx_win_x64_5.4.23.2.zip"), "BepInEx_win_x64_*.zip")
        self.assertEqual(version_glob("mod_v2.zip"), "mod_*.zip")
        self.assertEqual(version_glob("build-2026-11-01.zip"), "build-*.zip")
        self.assertEqual(version_glob("thing-r42.zip", tag="r42"), "thing-*.zip")

    def test_keeps_names_without_versions(self):
        self.assertEqual(version_glob("MyMod.dll"), "MyMod.dll")

    def test_falls_back_when_ambiguous(self):
        names = ["Mod-1.0-win.zip", "Mod-1.0-linux.zip"]
        self.assertEqual(version_glob("Mod-1.0-win.zip", names), "Mod-*-win.zip")
        names = ["Mod-1.0.zip", "Mod-2.0.zip"]
        self.assertEqual(version_glob("Mod-1.0.zip", names), "Mod-1.0.zip")


class SelectAssetTests(unittest.TestCase):
    def test_single_file_is_automatic(self):
        assets = select_assets(release(names=["MyMod.zip", "MyMod.zip.sha256", "checksums.txt"]))
        self.assertEqual([a.name for a in assets], ["MyMod.zip"])

    def test_several_files_need_a_choice(self):
        with self.assertRaises(GitHubError) as ctx:
            select_assets(release(names=["a-win.zip", "a-linux.zip"]))
        self.assertIn("several files", str(ctx.exception))

    def test_pattern_picks_one(self):
        rel = release(names=["a-win-1.0.zip", "a-linux-1.0.zip"])
        self.assertEqual(select_assets(rel, "a-win-*.zip")[0].name, "a-win-1.0.zip")
        self.assertEqual(select_assets(rel, "A-WIN-*.ZIP")[0].name, "a-win-1.0.zip")

    def test_pattern_problems_are_explained(self):
        rel = release(names=["a-win.zip", "a-linux.zip"])
        with self.assertRaisesRegex(GitHubError, "No file"):
            select_assets(rel, "b*.zip")
        with self.assertRaisesRegex(GitHubError, "matches several"):
            select_assets(rel, "a-*.zip")

    def test_multiple_patterns(self):
        rel = release(names=["core.dll", "extra.dll", "readme.md"])
        self.assertEqual([a.name for a in select_assets(rel, "core.dll, extra.dll")], ["core.dll", "extra.dll"])

    def test_source_zip(self):
        self.assertTrue(select_assets(release(names=[]))[0].is_source)
        self.assertTrue(select_assets(release(names=["x.zip"]), "source")[0].is_source)


class FakeGitHub(BaseHTTPRequestHandler):
    """Just enough of the GitHub API for the client tests."""

    payload = b"hello mod"
    requests: list = []

    def log_message(self, *args):
        pass

    def _json(self, code, body, etag=None):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-RateLimit-Remaining", "42")
        if etag:
            self.send_header("ETag", etag)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        FakeGitHub.requests.append((self.path, self.headers.get("If-None-Match"), self.headers.get("Authorization")))
        if self.path == "/repos/o/stable/releases/latest":
            if self.headers.get("If-None-Match") == '"v1"':
                self.send_response(304)
                self.end_headers()
                return
            return self._json(200, release(names=["m.zip"]) | {"body": "long notes"}, etag='"v1"')
        if self.path.startswith("/repos/o/pre/releases/latest") or self.path.startswith("/repos/o/none/releases/latest"):
            return self._json(404, {"message": "Not Found"})
        if self.path.startswith("/repos/o/pre/releases?"):
            return self._json(200, [release("v2-beta", 2, prerelease=True)])
        if self.path.startswith("/repos/o/none/releases?"):
            return self._json(200, [])
        if self.path.startswith("/repos/o/limited/"):
            self.send_response(403)
            self.send_header("X-RateLimit-Remaining", "0")
            self.send_header("X-RateLimit-Reset", "1999999999")
            self.end_headers()
            self.wfile.write(b'{"message": "API rate limit exceeded"}')
            return
        if self.path == "/repos/o/stable/contents/modloader.json?ref=v1.0%2Bbuild":
            text = base64.b64encode(b'{"requires": ["o/core"]}').decode()
            content = text[:10] + "\n" + text[10:]  # GitHub wraps the base64 in lines
            return self._json(200, {"type": "file", "encoding": "base64", "content": content})
        if self.path == "/file":
            self.send_response(200)
            self.send_header("Content-Length", str(len(self.payload)))
            self.end_headers()
            self.wfile.write(self.payload)
            return
        self._json(404, {"message": "Not Found"})


class ClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeGitHub)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.root = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        FakeGitHub.requests.clear()

    def client(self, token=""):
        return GitHubClient(token, Path(self.tmp.name) / "cache.json", api_root=self.root)

    def test_etag_cache_survives_restart(self):
        first = self.client().latest_release("o/stable")
        self.assertEqual(first["tag_name"], "v1.0")
        self.assertNotIn("body", first)  # release notes aren't cached
        second = self.client().latest_release("o/stable")  # new client, same cache file
        self.assertEqual(second, first)
        self.assertEqual(FakeGitHub.requests[-1][1], '"v1"')

    def test_prerelease_and_missing_release_messages(self):
        with self.assertRaisesRegex(NotFoundError, "only has pre-releases"):
            self.client().latest_release("o/pre")
        self.assertEqual(self.client().latest_release("o/pre", include_prereleases=True)["tag_name"], "v2-beta")
        with self.assertRaisesRegex(NotFoundError, "hasn't published"):
            self.client().latest_release("o/none")
        with self.assertRaisesRegex(NotFoundError, "Couldn't find the repository"):
            self.client().latest_release("o/missing")

    def test_rate_limit_message(self):
        with self.assertRaisesRegex(GitHubError, "rate limit.*token in Settings"):
            self.client().latest_release("o/limited")

    def test_token_only_sent_to_api(self):
        self.client("secret").latest_release("o/stable")
        self.assertEqual(FakeGitHub.requests[-1][2], "Bearer secret")

    def test_read_file_from_a_tag(self):
        self.assertEqual(self.client().read_file("o/stable", "modloader.json", "v1.0+build"),
                         '{"requires": ["o/core"]}')
        self.assertIsNone(self.client().read_file("o/stable", "modloader.json", "v2.0"))  # not there

    def test_download_checks_size_and_checksum(self):
        dest = Path(self.tmp.name) / "m.zip"
        good = hashlib.sha256(FakeGitHub.payload).hexdigest()
        asset = Asset("m.zip", self.root + "/file", size=len(FakeGitHub.payload), digest=f"sha256:{good}")
        seen = []
        self.client().download(asset, dest, lambda d, t: seen.append((d, t)))
        self.assertEqual(dest.read_bytes(), FakeGitHub.payload)
        self.assertEqual(seen[-1], (len(FakeGitHub.payload), len(FakeGitHub.payload)))

        bad = Asset("m.zip", self.root + "/file", digest="sha256:" + "0" * 64)
        with self.assertRaisesRegex(GitHubError, "checksum"):
            self.client().download(bad, Path(self.tmp.name) / "bad.zip")
        self.assertFalse((Path(self.tmp.name) / "bad.zip").exists())
        self.assertFalse((Path(self.tmp.name) / "bad.zip.part").exists())


if __name__ == "__main__":
    unittest.main()
