"""Publish a new version of Mod Loader:  python release.py 1.2.0

Sets the version in modloader/__init__.py, runs the tests, then commits, tags
and pushes. GitHub builds ModLoader.exe and publishes the release
(.github/workflows/release.yml), and every copy of Mod Loader updates itself
from it.
"""

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
INIT = ROOT / "modloader" / "__init__.py"


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, text=True, capture_output=True).stdout


def main(argv: list[str]) -> int:
    if len(argv) != 1 or not re.fullmatch(r"\d+\.\d+\.\d+", argv[0]):
        print("Usage: python release.py 1.2.0")
        return 2
    version = argv[0]
    text = INIT.read_text(encoding="utf-8")
    current = re.search(r'__version__ = "([^"]+)"', text).group(1)
    if tuple(map(int, version.split("."))) <= tuple(map(int, current.split("."))):
        print(f"{version} has to be newer than the current version, {current}.")
        return 1
    if git("status", "--porcelain").strip():
        print("You have changes that aren't committed. Commit them (or stash them) first.")
        return 1
    if subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"], cwd=ROOT).returncode:
        print("The tests failed, so nothing was released.")
        return 1

    INIT.write_text(text.replace(f'__version__ = "{current}"', f'__version__ = "{version}"'), encoding="utf-8")
    git("commit", "-am", f"Release v{version}")
    git("tag", f"v{version}")
    try:
        git("push", "origin", "HEAD", f"v{version}")
    except subprocess.CalledProcessError as e:
        print(f"Pushing failed:\n{e.stderr}\nThe release commit and tag were made locally; "
              f"run `git push origin HEAD v{version}` once the problem is fixed.")
        return 1
    remote = git("remote", "get-url", "origin").strip().removesuffix(".git")
    print(f"Pushed v{version}. GitHub is building it now; the release appears at {remote}/releases "
          "in a few minutes, and Mod Loader updates itself the next time it opens.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
