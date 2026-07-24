#!/usr/bin/env python3
"""PlugICT — One-command setup for AI agents.

Run: python setup.py
The agent guides the user through license activation, vault download, and MCP config.
"""

import os, sys, json, subprocess, urllib.request, zipfile, shutil, hashlib
from pathlib import Path

REPO = "godzillacode0000/plugICT"
ASSET_NAME = "plugict.zip"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
FALLBACK_URL = f"https://github.com/{REPO}/releases/latest/download/{ASSET_NAME}"
REQUIRED_FILES = ["mcp_server.py", "vault_core.py", "ict-vault.kevin", "license.key"]
HERE = Path(__file__).parent.resolve()


def runtime_python():
    """Return the interpreter dedicated to this buyer installation."""
    if sys.platform == "win32":
        return HERE / ".venv" / "Scripts" / "python.exe"
    return HERE / ".venv" / "bin" / "python"


def create_runtime_environment():
    """Create the buyer-local virtual environment once, never use global pip."""
    python = runtime_python()
    if python.exists():
        print("  Isolated environment already present")
        return
    subprocess.check_call([sys.executable, "-m", "venv", str(HERE / ".venv")])
    if not python.exists():
        print("ERROR: Could not create the isolated Python environment.")
        sys.exit(1)
    print("  Isolated environment created")

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
        str(runtime_python()), "-E", "-X", "utf8", "-m", "pip", "install", "-q", "-r", str(req)
    ])
    print("  Dependencies installed")

def resolve_release():
    """Ask the GitHub API for the latest release so the download URL and its
    SHA-256 digest always track the newest vault — no hard-pinned tag."""
    req = urllib.request.Request(API_LATEST, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "plugict-setup",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.load(r)
    except Exception as e:
        print(f"  ⚠️ GitHub API unavailable ({e})")
        print("     Falling back to the latest-release URL without checksum verification")
        return {"tag": "latest", "url": FALLBACK_URL, "digest": None}

    for asset in data.get("assets", []):
        if asset.get("name") == ASSET_NAME:
            digest = asset.get("digest") or ""
            return {
                "tag": data.get("tag_name", "latest"),
                "url": asset.get("browser_download_url", FALLBACK_URL),
                "digest": digest.split(":", 1)[1] if digest.startswith("sha256:") else None,
            }

    print(f"  ⚠️ {ASSET_NAME} not listed in the latest release — trying fallback URL")
    return {"tag": data.get("tag_name", "latest"), "url": FALLBACK_URL, "digest": None}

def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def download_vault():
    zip_path = HERE / "plugict_download.zip"
    if zip_path.exists():
        zip_path.unlink()

    rel = resolve_release()
    print(f"  Downloading vault {rel['tag']} ({rel['url']})...")
    urllib.request.urlretrieve(rel["url"], zip_path)
    print(f"  Downloaded: {zip_path.stat().st_size / 1024 / 1024:.0f} MB")

    if rel["digest"]:
        print("  Verifying SHA-256...")
        actual = file_sha256(zip_path)
        if actual != rel["digest"]:
            zip_path.unlink()
            print("  ❌ Checksum mismatch — the download is corrupt or was tampered with.")
            print(f"     expected: {rel['digest']}")
            print(f"     got:      {actual}")
            print("     Re-run setup.py; if this persists, contact support.")
            sys.exit(1)
        print("  ✅ Checksum verified")
    else:
        print("  ⚠️ No checksum published for this asset — skipping verification")

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
            [str(runtime_python()), "-E", "-X", "utf8", str(doctor), "--doctor"],
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
    abs_python = runtime_python()

    print("""
=== MCP Configuration ===

Add this to your AI agent's MCP config:

For Claude Desktop:
  Edit %%APPDATA%%\\Claude\\claude_desktop_config.json
  Add under "mcpServers":

  {
    "mcpServers": {
      "plugict": {
        "command": "%s",
        "args": ["-E", "-X", "utf8", "%s"]
      }
    }
  }

For Hermes Agent:
  Add to your profile config.yaml:

  mcp_servers:
    plugict:
      command: "%s"
      args: ["-E", "-X", "utf8", "%s"]

For Cursor:
  Settings → Features → MCP → Add:
  Name: plugict
  Type: command
  Command: "%s" "-E" "-X" "utf8" "%s"

Then restart your AI agent and ask:
  "What is FVG in ICT?"
""" % (abs_python, abs_path, abs_python, abs_path, abs_python, abs_path))

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
            step("Creating isolated environment")
            create_runtime_environment()
            step("Installing dependencies")
            install_deps()
            step("Verifying installation")
            verify()
            print_mcp_config()
            print("\n✅ Already set up. Just paste the MCP config above into your AI agent.")
            return

    step("License key")
    key = None
    if lic.exists():
        reuse = prompt("Found existing license.key — reuse it? (Y/n)").lower()
        if reuse in ("", "y", "yes"):
            print("  Using existing license.key")
        else:
            key = prompt("Enter your PlugICT license key (from purchase email)")
            if not key:
                print("  No key entered — exiting")
                sys.exit(1)
    else:
        key = prompt("Enter your PlugICT license key (from purchase email)")
        if not key:
            print("  No key entered — exiting")
            sys.exit(1)

    step("Downloading vault")
    download_vault()

    if key:
        step("Writing license")
        write_license(key)

    step("Creating isolated environment")
    create_runtime_environment()

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
