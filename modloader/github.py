"""A small GitHub Releases client built only on the standard library."""

from __future__ import annotations

import base64
import fnmatch
import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

API_ROOT = "https://api.github.com"
USER_AGENT = "ModLoader/1.0"
SOURCE_PATTERN = "source"

# Files published next to a release that are never the mod itself.
_SIDE_SUFFIXES = (
    ".sha256", ".sha512", ".sha1", ".md5", ".sig", ".asc", ".minisig", ".pem",
    ".sbom", ".sbom.json", ".spdx.json", ".intoto.jsonl",
)
_SIDE_NAMES = {"checksums.txt", "sha256sums", "sha256sums.txt", "sha512sums", "sha512sums.txt"}

# Only these release fields are kept (and cached).
_RELEASE_KEYS = ("id", "tag_name", "name", "prerelease", "draft", "published_at",
                 "created_at", "html_url", "zipball_url")
_ASSET_KEYS = ("name", "size", "browser_download_url", "url", "digest", "state")


class GitHubError(Exception):
    """A problem talking to GitHub, with a message fit to show the user."""


class NotFoundError(GitHubError):
    pass


class RateLimitError(GitHubError):
    pass


@dataclass
class Asset:
    """One file to download for a release."""

    name: str
    download_url: str
    api_url: str = ""
    size: int = 0
    digest: str = ""
    is_source: bool = False


def parse_repo(text: str) -> str:
    """Turn 'owner/name' or any github.com link to a repo into 'owner/name'."""
    s = (text or "").strip()
    s = re.sub(r"^(?:https?://)?(?:www\.)?github\.com/", "", s, flags=re.I)
    s = s.split("?")[0].split("#")[0]
    parts = [p for p in s.split("/") if p]
    if len(parts) >= 2:
        owner, name = parts[0], parts[1]
        if name.lower().endswith(".git"):
            name = name[:-4]
        if (re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", owner)
                and re.fullmatch(r"[A-Za-z0-9._-]+", name)):
            return f"{owner}/{name}"
    raise ValueError("Enter the repository as owner/name (for example BepInEx/BepInEx) "
                     "or paste its github.com link.")


def is_side_file(name: str) -> bool:
    lower = name.lower()
    return lower in _SIDE_NAMES or lower.endswith(_SIDE_SUFFIXES)


def _matches(name: str, pattern: str) -> bool:
    return fnmatch.fnmatchcase(name.lower(), pattern.lower())


def version_glob(name: str, all_names: list[str] = (), tag: str = "") -> str:
    """Make a file name match future versions: 'MyMod-1.2.3.zip' -> 'MyMod-*.zip'.

    Falls back to the exact name if the wildcard would match more than one
    file in the current release.
    """
    glob = name
    for t in {tag, tag.lstrip("vV")}:
        if len(t) >= 2:
            glob = glob.replace(t, "*")
    glob = re.sub(r"\d{4}[-_.]\d{2}[-_.]\d{2}|v?\d+(?:\.\d+)+|v\d+(?![A-Za-z])", "*", glob)
    glob = re.sub(r"\*+", "*", glob)
    if glob == name:
        return name
    if all_names and sum(_matches(n, glob) for n in all_names) != 1:
        return name
    return glob


def _list_names(names: list[str]) -> str:
    if not names:
        return "none"
    shown = ", ".join(names[:8])
    return shown + (f" and {len(names) - 8} more" if len(names) > 8 else "")


def _to_asset(a: dict) -> Asset:
    return Asset(
        name=a["name"],
        download_url=a["browser_download_url"],
        api_url=a.get("url") or "",
        size=a.get("size") or 0,
        digest=a.get("digest") or "",
    )


def source_asset(release: dict) -> Asset:
    tag = release.get("tag_name") or "latest"
    return Asset(name=f"source-{tag}.zip", download_url=release["zipball_url"], is_source=True)


def select_assets(release: dict, pattern: str = "") -> list[Asset]:
    """Decide which file(s) of a release to install.

    pattern: blank for automatic, otherwise comma-separated wildcards, each of
    which must match exactly one file. "source" means the source code zip.
    """
    assets = [a for a in release.get("assets") or [] if a.get("state", "uploaded") == "uploaded"]
    names = [a["name"] for a in assets]
    tag = release.get("tag_name") or "the latest release"
    patterns = [p.strip() for p in (pattern or "").split(",") if p.strip()]

    if patterns:
        chosen: list[Asset] = []
        for pat in patterns:
            if pat.lower() in (SOURCE_PATTERN, "source code", "zipball"):
                chosen.append(source_asset(release))
                continue
            hits = [a for a in assets if _matches(a["name"], pat)]
            if not hits:
                raise GitHubError(f'No file in {tag} matches "{pat}". '
                                  f"Files in that release: {_list_names(names)}.")
            if len(hits) > 1:
                raise GitHubError(f'"{pat}" matches several files in {tag} '
                                  f"({_list_names([a['name'] for a in hits])}). "
                                  "Make the release file pattern more specific.")
            if all(c.name != hits[0]["name"] for c in chosen):
                chosen.append(_to_asset(hits[0]))
        return chosen

    candidates = [a for a in assets if not is_side_file(a["name"])]
    if not candidates:
        return [source_asset(release)]
    if len(candidates) == 1:
        return [_to_asset(candidates[0])]
    raise GitHubError(f"{tag} has several files ({_list_names([a['name'] for a in candidates])}). "
                      "Edit the mod and choose which one to install under “Release file”.")


def _slim_release(release: dict) -> dict:
    slim = {k: release.get(k) for k in _RELEASE_KEYS}
    slim["assets"] = [{k: a.get(k) for k in _ASSET_KEYS} for a in release.get("assets") or []]
    return slim


def _slim_releases(data):
    return [_slim_release(r) for r in data] if isinstance(data, list) else _slim_release(data)


def _file_text(data) -> str | None:
    """The text of a file from the contents API (None for a folder or an oversized file)."""
    if not isinstance(data, dict) or data.get("type") != "file" or data.get("encoding") != "base64":
        return None
    try:
        return base64.b64decode(data.get("content") or "").decode("utf-8", "replace")
    except ValueError:
        return None


class GitHubClient:
    def __init__(self, token: str = "", cache_path: Path | str | None = None, api_root: str = ""):
        self.token = (token or "").strip()
        # MODLOADER_API_ROOT points Mod Loader at a stand-in server (used for testing).
        self.api_root = (api_root or os.environ.get("MODLOADER_API_ROOT") or API_ROOT).rstrip("/")
        self.cache_path = Path(cache_path) if cache_path else None
        self._lock = threading.Lock()
        self._cache: dict = self._load_cache()
        self.rate_remaining: int | None = None
        self.rate_reset: float | None = None

    # -- HTTP ---------------------------------------------------------------

    def _open(self, url: str, accept: str, etag: str | None = None, timeout: float = 30):
        headers = {"User-Agent": USER_AGENT, "Accept": accept}
        is_api = url.startswith(self.api_root)
        if is_api:
            headers["X-GitHub-Api-Version"] = "2022-11-28"
        if etag:
            headers["If-None-Match"] = etag
        request = urllib.request.Request(url, headers=headers)
        if self.token and is_api:
            # "Unredirected" so the token isn't sent on to the download CDN.
            request.add_unredirected_header("Authorization", f"Bearer {self.token}")
        return urllib.request.urlopen(request, timeout=timeout)

    def _note_rate(self, headers) -> None:
        if headers is None:
            return
        try:
            if headers.get("X-RateLimit-Remaining") is not None:
                self.rate_remaining = int(headers["X-RateLimit-Remaining"])
            if headers.get("X-RateLimit-Reset") is not None:
                self.rate_reset = float(headers["X-RateLimit-Reset"])
        except ValueError:
            pass

    def _error_from(self, e: urllib.error.HTTPError) -> GitHubError:
        try:
            message = json.loads(e.read().decode("utf-8", "replace")).get("message", "")
        except Exception:
            message = ""
        finally:
            e.close()
        if e.code == 401:
            return GitHubError("GitHub rejected the access token. Check it, or clear it, in Settings.")
        if e.code in (403, 429) and (self.rate_remaining == 0 or "rate limit" in message.lower()):
            when = time.strftime("%H:%M", time.localtime(self.rate_reset)) if self.rate_reset else "within the hour"
            hint = "" if self.token else (" Adding a GitHub token in Settings raises the limit "
                                          "from 60 to 5,000 checks an hour.")
            return RateLimitError(f"GitHub's rate limit was reached; it resets at {when}.{hint}")
        if e.code == 404:
            return NotFoundError(message or "Not found on GitHub.")
        return GitHubError(f"GitHub returned an error ({e.code}{': ' + message if message else ''}).")

    def get_json(self, path: str, transform: Callable | None = None):
        url = path if path.startswith("http") else self.api_root + path
        with self._lock:
            cached = self._cache.get(url)
        try:
            with self._open(url, "application/vnd.github+json", etag=cached and cached.get("etag")) as resp:
                self._note_rate(resp.headers)
                body = json.loads(resp.read().decode("utf-8"))
                etag = resp.headers.get("ETag")
        except urllib.error.HTTPError as e:
            self._note_rate(e.headers)
            if e.code == 304 and cached:
                e.close()
                return cached["body"]
            raise self._error_from(e) from None
        except urllib.error.URLError as e:
            raise GitHubError(f"Couldn't reach GitHub ({e.reason}). Check your internet connection.") from None
        except TimeoutError:
            raise GitHubError("GitHub took too long to answer. Try again in a moment.") from None
        except OSError as e:
            raise GitHubError(f"The connection to GitHub failed ({e}). Try again in a moment.") from None
        except ValueError:
            raise GitHubError("GitHub sent an answer Mod Loader couldn't read. Try again in a moment.") from None
        if transform:
            body = transform(body)
        if etag:
            with self._lock:
                self._cache[url] = {"etag": etag, "body": body}
                self._save_cache()
        return body

    # -- Releases -----------------------------------------------------------

    def _list_releases(self, repo: str) -> list[dict]:
        try:
            data = self.get_json(f"/repos/{repo}/releases?per_page=15", _slim_releases)
        except NotFoundError:
            hint = "" if self.token else " If it's private, add a GitHub token in Settings."
            raise NotFoundError(f"Couldn't find the repository {repo} on GitHub. Check the name.{hint}") from None
        return [r for r in data if not r.get("draft")]

    def latest_release(self, repo: str, include_prereleases: bool = False) -> dict:
        if not include_prereleases:
            try:
                return self.get_json(f"/repos/{repo}/releases/latest", _slim_releases)
            except NotFoundError:
                pass  # no stable release, or no such repo; find out which below
        releases = self._list_releases(repo)
        if not include_prereleases:
            stable = [r for r in releases if not r.get("prerelease")]
            if not stable and releases:
                raise NotFoundError(f"{repo} only has pre-releases. Edit the mod and tick "
                                    "“Include pre-releases” to use them.")
            releases = stable
        if not releases:
            raise NotFoundError(f"{repo} hasn't published any releases yet.")
        return max(releases, key=lambda r: r.get("published_at") or r.get("created_at") or "")

    def read_file(self, repo: str, path: str, ref: str) -> str | None:
        """A small text file from the repository at `ref` (a tag), or None if it isn't there."""
        url = f"/repos/{repo}/contents/{path}?ref={urllib.parse.quote(ref, safe='')}"
        try:
            return self.get_json(url, _file_text)
        except NotFoundError:
            return None

    # -- Downloads ----------------------------------------------------------

    def download(self, asset: Asset, dest: Path, progress: Callable[[int, int], None] | None = None) -> Path:
        dest = Path(dest)
        use_api = bool(self.token and asset.api_url)  # needed for private repos
        url = asset.api_url if use_api else asset.download_url
        accept = "*/*" if asset.is_source else "application/octet-stream"
        part = dest.with_name(dest.name + ".part")
        sha = hashlib.sha256()
        done = 0
        try:
            with self._open(url, accept, timeout=60) as resp:
                total = int(resp.headers.get("Content-Length") or 0) or asset.size
                with open(part, "wb") as out:
                    while chunk := resp.read(256 * 1024):
                        out.write(chunk)
                        sha.update(chunk)
                        done += len(chunk)
                        if progress:
                            progress(done, total)
        except urllib.error.HTTPError as e:
            part.unlink(missing_ok=True)
            self._note_rate(e.headers)
            raise self._error_from(e) from None
        except (urllib.error.URLError, OSError) as e:
            part.unlink(missing_ok=True)
            raise GitHubError(f"Downloading {asset.name} failed ({getattr(e, 'reason', e)}).") from None
        if asset.size and done != asset.size:
            part.unlink(missing_ok=True)
            raise GitHubError(f"Downloading {asset.name} stopped early ({done:,} of {asset.size:,} bytes).")
        if asset.digest.lower().startswith("sha256:"):
            if sha.hexdigest() != asset.digest.split(":", 1)[1].strip().lower():
                part.unlink(missing_ok=True)
                raise GitHubError(f"{asset.name} doesn't match the checksum GitHub published, "
                                  "so it wasn't installed. Try again.")
        os.replace(part, dest)
        return dest

    # -- Cache --------------------------------------------------------------

    def _load_cache(self) -> dict:
        if not self.cache_path or not self.cache_path.exists():
            return {}
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_cache(self) -> None:
        if not self.cache_path:
            return
        try:
            tmp = self.cache_path.with_name(self.cache_path.name + ".tmp")
            tmp.write_text(json.dumps(self._cache), encoding="utf-8")
            os.replace(tmp, self.cache_path)
        except OSError:
            pass  # the cache only saves requests; never fail because of it
