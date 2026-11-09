"""Double-click to open Mod Loader. Run `python ModLoader.pyw --help` for command-line options."""

import sys

from modloader.app import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
