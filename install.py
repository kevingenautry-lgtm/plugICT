#!/usr/bin/env python3
"""DEPRECATED — PlugICT now installs via setup.py.

This shim exists so older purchase emails that say
    python install.py --license /path/to/license.key
keep working: it copies the license into place, then hands off to setup.py
(which downloads the latest vault from GitHub Releases and verifies its
SHA-256 automatically).
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent.resolve()


def main():
    parser = argparse.ArgumentParser(
        description="Deprecated wrapper — forwards to setup.py")
    parser.add_argument("--license", help="Path to your license.key file")
    args, unknown = parser.parse_known_args()

    print("NOTE: install.py is deprecated — running setup.py instead.")
    if unknown:
        print(f"      (ignoring legacy options: {' '.join(unknown)})")
    print()

    if args.license:
        src = Path(args.license).expanduser()
        if not src.exists():
            print(f"ERROR: license file not found: {src}")
            sys.exit(1)
        dest = HERE / "license.key"
        if src.resolve() != dest.resolve():
            shutil.copyfile(src, dest)
        print(f"  license.key copied from {src}")

    sys.exit(subprocess.call([sys.executable, str(HERE / "setup.py")]))


if __name__ == "__main__":
    main()
