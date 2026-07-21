#!/usr/bin/env python3
"""PlugICT — One-command setup for AI agents.

Run: python setup.py
The agent guides the user through license activation, vault download, and MCP config.
"""

import os, sys, json, subprocess, urllib.request, zipfile, shutil, hashlib
from pathlib import Path

REPO = "godzillacode0000/plugICT"
RELEASE_TAG = "v3.4.0"
VAULT_URL = f"https://github.com/{REPO}/releases/download/{RELEASE_TAG}/plugict.zip"
REQUIRED_FILES = ["mcp_server.py", "vault_core.py", "ict-vault.kevin", "license.key"]
HERE = Path(__file__).parent.resolve()

def step(msg):
    print(f"\n=== {msg} ===")

def prompt(msg):
    return input(f"\n{msg}: ").strip()

def check_python():
    v = sys.version_info
    if v.major < 3 or (v.major == 3 and v.minor < 10):
        print(f"ERROR: Python 3.10+ required. You have {v.major}.{v.minor}.{v.micro}")
        sys.exit(1)
    print(f"  Python {v.major}.{v.minor}.{v.micro} — OK")

def install_deps():
    req = HERE / "requirements.txt"
    if not req.exists():
        print("  No requirements.txt — skipping")
        return
    subprocess.check_call([
        sys.executable, "-m", "pip", "install", "-q", "-r", str(req)
    ])
    print("  Dependencies installed")

def download_vault():
    zip_path = HERE / "plugict_download.zip"
    if zip_path.exists():
        zip_path.unlink()

    print(f"  Downloading vault ({VAULT_URL})...")
    urllib.request.urlretrieve(VAULT_URL, zip_path)
    print(f"  Downloaded: {zip_path.stat().st_size / 1024 / 1024:.0f} MB")

    print("  Extracting...")
    with zipfile.ZipFile(zip_path, 'r') as z:
        z.extractall(HERE)

    zip_path.unlink()
    print("  Extracted")

    # Move files from subfolder if vault was inside plugict/
    for item in HERE.iterdir():
        if item.is_dir() and item.name == "plugict":
            for f in item.iterdir():
                dest = HERE / f.name
                if not dest.exists():
                    shutil.move(str(f), str(dest))
            shutil.rmtree(str(item))
            break

def write_license(key):
    lic_path = HERE / "license.key"
    lic_path.write_text(key.strip(), encoding='utf-8')
    print(f"  license.key written")

def verify():
    doctor = HERE / "mcp_server.py"
    if not doctor.exists():
        print("  WARNING: mcp_server.py not found — doctor unavailable")
        return

    try:
        result = subprocess.run(
            [sys.executable, str(doctor), "--doctor"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            print("  ✅ Doctor check passed")
        else:
            print(f"  ⚠️ Doctor check: {result.stdout.strip()[:200]}")
    except Exception as e:
        print(f"  ⚠️ Doctor check unavailable: {e}")

def print_mcp_config():
    abs_path = HERE / "mcp_server.py"

    print("""
=== MCP Configuration ===

Add this to your AI agent's MCP config:

For Claude Desktop:
  Edit %%APPDATA%%\\Claude\\claude_desktop_config.json
  Add under "mcpServers":

  {
    "mcpServers": {
      "plugict": {
        "command": "python",
        "args": ["%s"]
      }
    }
  }

For Hermes Agent:
  Add to your profile config.yaml:

  mcp_servers:
    plugict:
      command: python
      args: ["%s"]

For Cursor:
  Settings → Features → MCP → Add:
  Name: plugict
  Type: command
  Command: python "%s"

Then restart your AI agent and ask:
  "What is FVG in ICT?"
""" % (abs_path, abs_path, abs_path))

def main():
    print("=" * 50)
    print("  PlugICT — ICT Evidence Vault Setup")
    print("=" * 50)

    step("Checking Python")
    check_python()

    # Check if already installed
    vault = HERE / "ict-vault.kevin"
    lic = HERE / "license.key"
    already = vault.exists() and lic.exists()
    if already:
        print("  Vault + license already present")
        reinstall = prompt("Reinstall? (y/N)").lower()
        if reinstall != 'y':
            print_mcp_config()
            print("\n✅ Already set up. Just paste the MCP config above into your AI agent.")
            return

    step("License key")
    key = prompt("Enter your PlugICT license key (from purchase email)")
    if not key:
        print("  No key entered — exiting")
        sys.exit(1)

    step("Downloading vault")
    download_vault()

    step("Writing license")
    write_license(key)

    step("Installing dependencies")
    install_deps()

    step("Verifying installation")
    verify()

    step("MCP Configuration")
    print_mcp_config()

    print()
    print("=" * 50)
    print("  ✅ PlugICT is ready!")
    print("  Paste the MCP config into your AI agent and restart it.")
    print("  Then ask: \"What is FVG?\"")
    print("=" * 50)

if __name__ == "__main__":
    main()
