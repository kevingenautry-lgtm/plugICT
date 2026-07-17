#!/usr/bin/env python3
"""PlugICT Installer — one-command setup for buyers.

Usage:
    # Interactive (prompts for license path):
    python install.py

    # Non-interactive:
    python install.py --license C:/Users/Me/Downloads/license.key
    python install.py --license license.key --target D:/PlugICT --agent claude

    # Headless (no --license = prompts):
    python install.py --target D:/PlugICT --no-agent
"""
import hashlib
import json
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

# ── Constants ──────────────────────────────────────────────────────────
REPO = "godzillacode0000/plugICT"
MANIFEST_URL = f"https://raw.githubusercontent.com/{REPO}/main/release-manifest.json"
RELEASE_BASE = f"https://github.com/{REPO}/releases/download"

STARTUP_HELP = r"""{agent}

You can now ask PlugICT questions through your AI agent.

Quick test prompt for your agent:

    Search the PlugICT vault for "fair value gap explained by ICT"
    and show me the first result with timestamps.

Make sure PlugICT MCP is configured. If not configured, restart your agent
after adding MCP configuration.

Troubleshooting:
  - Run `python mcp_server.py --doctor` inside the vault folder.
  - Check that `license.key` is in the same folder as `mcp_server.py`.
  - If doctor fails, re-run this installer or contact support.
"""


# ── Helpers ────────────────────────────────────────────────────────────

def ansi(s, code):
    """Wrap string in ANSI color if terminal supports it."""
    if sys.stdout.isatty() and os.name != "nt":
        return f"\033[{code}m{s}\033[0m"
    return s


def green(s):    return ansi(s, "32")
def yellow(s):   return ansi(s, "33")
def red(s):      return ansi(s, "31")
def bold(s):     return ansi(s, "1")


def fmt_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# ── Download helpers ───────────────────────────────────────────────────

def _ssl_ctx():
    ctx = ssl.create_default_context()
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


def fetch_json(url):
    with urllib.request.urlopen(url, context=_ssl_ctx(), timeout=30) as r:
        return json.loads(r.read().decode())


def download_file(url, dest, label="Downloading"):
    """Stream download with progress."""
    req = urllib.request.Request(url, headers={"Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, context=_ssl_ctx(), timeout=300) as r:
        total = int(r.headers.get("Content-Length", 0))
        got = 0
        with open(dest, "wb") as f:
            chunk_size = 8192 * 64  # 512 KB
            last_pct = -1
            while True:
                chunk = r.read(chunk_size)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                if total:
                    pct = got * 100 // total
                    if pct != last_pct:
                        last_pct = pct
                        print(f"\r  {label}: {fmt_size(got)} / {fmt_size(total)} ({pct}%)", end="", flush=True)
        if total:
            print(f"\r  {label}: {fmt_size(got)} / {fmt_size(got)} (100%)")
        else:
            print(f"\r  {label}: {fmt_size(got)}")


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(8192 * 64)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def find_license():
    """Find a license.key file interactively."""
    # Check common locations
    candidates = [
        Path.home() / "Downloads" / "license.key",
        Path("license.key"),
        Path.home() / "Desktop" / "license.key",
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


def prompt_license():
    """Ask user where their license.key is."""
    found = find_license()
    while True:
        if found:
            print(f"\n  Found license.key at: {found.resolve()}")
            r = input("  Use this file? [Y/n]: ").strip().lower()
            if r in ("", "y", "yes"):
                return found
        p = input("  Path to your license.key file: ").strip().strip("\"'").strip()
        if p:
            path = Path(p).expanduser().resolve()
            if path.exists():
                return path
            print(red(f"  File not found: {path}"))


def parse_license(path):
    """Parse license.key into a dict."""
    info = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                info[k.strip()] = v.strip()
    return info


def detect_agent():
    """Detect which AI agents are installed."""
    agents = []
    # Codex CLI
    if shutil.which("codex"):
        agents.append("codex")
    # Claude Code
    if shutil.which("claude"):
        agents.append("claude-code")
    # Cursor (check common install paths)
    cursor_paths = [
        Path.home() / "AppData" / "Local" / "Programs" / "cursor" / "Cursor.exe",
        "/usr/bin/cursor",
        "/Applications/Cursor.app/Contents/MacOS/Cursor",
    ]
    if any(p.exists() for p in cursor_paths):
        agents.append("cursor")
    return agents


def configure_mcp_codex(target_dir):
    """Add PlugICT MCP to Codex CLI."""
    python = str(Path(sys.executable))
    server = str(target_dir / "mcp_server.py")
    cmd = f'codex mcp add plugict -- "{python}" "{server}"'
    print(f"\n  Configuring Codex CLI...")
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
    if r.returncode == 0:
        print(green("  ✅ Codex MCP configured. Restart Codex to use PlugICT."))
    else:
        print(yellow(f"  ⚠️  Codex config failed: {r.stderr.strip()}"))
        print(yellow("     Add MCP manually: codex mcp add plugict -- <python> <server>"))


def configure_mcp_claude_code(target_dir):
    """PlugICT works in Claude Code via direct python execution."""
    python = str(Path(sys.executable))
    server = str(target_dir / "mcp_server.py")
    print(f"\n  Claude Code runs MCP servers via config.")
    print(yellow(f"     python \"{server}\""))
    print("  Add to claude_desktop_config.json or run via Codex/CLI session.")


def configure_mcp_cursor(target_dir):
    """Show Cursor instructions."""
    python = str(Path(sys.executable))
    server = str(target_dir / "mcp_server.py")
    print(f"\n  To configure Cursor:")
    print(f"    Open Cursor → Settings → MCP Servers → Add")
    print(f'    Command: "{sys.executable}"')
    print(f'    Args:    ["{server}"]')
    print()

def configure_mcp_hermes(target_dir):
    """Show Hermes Agent instructions."""
    server = str(target_dir / "mcp_server.py")
    print(f"\n  To configure Hermes Agent:")
    print(f"    Add to your profile config.yaml:")
    print(f"      mcp_servers:")
    print(f"        plugict:")
    print(f"          command: {sys.executable}")
    print(f"          args: ['{server}']")
    print()


# ── Main flow ──────────────────────────────────────────────────────────

def main():
    print()
    print(bold("PlugICT — Knowledge Vault Installer"))
    print("=" * 42)
    print()

    # ── Parse args ──────────────────────────────────────────────────
    import argparse
    ap = argparse.ArgumentParser(description="Install PlugICT Knowledge Vault")
    ap.add_argument("--license", help="Path to license.key file")
    ap.add_argument("--target", default=None,
                    help="Install directory (default: current directory)")
    ap.add_argument("--agent", nargs="*", choices=["codex", "claude-code", "cursor", "hermes", "none"],
                    help="Configure MCP for specific AI agents")
    ap.add_argument("--no-agent", action="store_true",
                    help="Skip MCP agent configuration")
    args = ap.parse_args()

    # ── Resolve license ─────────────────────────────────────────────
    license_path = None
    if args.license:
        license_path = Path(args.license).expanduser().resolve()
        if not license_path.exists():
            print(red(f"  ❌ License file not found: {license_path}"))
            sys.exit(1)
    else:
        license_path = prompt_license()

    license_data = parse_license(license_path)
    vault_hash = license_data.get("VAULT_HASH", "").strip().lower()
    buyer = license_data.get("LICENSED_TO", "Unknown")
    license_id = license_data.get("LICENSE_ID", "Unknown")

    if len(vault_hash) != 64:
        print(red(f"  ❌ Invalid VAULT_HASH in license file. Make sure this is a valid PlugICT license."))
        sys.exit(1)

    print(f"  License for: {green(buyer)}")
    print(f"  License ID:  {license_id}")
    print(f"  Vault hash:  {vault_hash[:16]}...{vault_hash[-16:]}")
    print()

    # ── Resolve target directory ────────────────────────────────────
    target = Path(args.target).expanduser().resolve() if args.target else Path.cwd().resolve()
    target.mkdir(parents=True, exist_ok=True)
    vault_file = target / "ict-vault.kevin"

    # ── Fetch manifest from GitHub ──────────────────────────────────
    print(f"  Fetching release info...")
    try:
        manifest = fetch_json(MANIFEST_URL)
    except Exception as e:
        print(red(f"  ❌ Could not fetch release manifest: {e}"))
        print(yellow("  Check your internet connection and try again."))
        sys.exit(1)

    artifacts = manifest.get("artifacts", {})
    artifact = artifacts.get(vault_hash)
    if not artifact:
        print(red(f"  ❌ No vault found matching your license hash."))
        print(yellow("  Your license may be for a different vault version."))
        print(yellow(f"  Supported hashes: {', '.join(artifacts.keys()[:3])}"))
        if len(artifacts) > 3:
            print(yellow(f"    ... and {len(artifacts) - 3} more"))
        sys.exit(1)

    version = artifact.get("version", "unknown")
    dl_url = artifact["url"]
    expected_size = artifact.get("size", 0)
    print(f"  Vault version: {green(f'v{version}')}")
    print(f"  Download size: {fmt_size(expected_size) if expected_size else 'unknown'}")
    print()

    # ── Download vault if needed ────────────────────────────────────
    if vault_file.exists():
        print(f"  Checking existing vault...")
        existing_hash = file_sha256(vault_file)
        if existing_hash == vault_hash:
            print(green(f"  ✅ Vault already exists and matches license."))
        else:
            print(yellow(f"  ⚠️  Existing vault hash does not match license."))
            print(yellow(f"     Expected: {vault_hash[:16]}..."))
            print(yellow(f"     Got:      {existing_hash[:16]}..."))
            vault_file.unlink()
            print(f"\n  Downloading vault ({fmt_size(expected_size)})...")
            download_file(dl_url, vault_file, label="Downloading vault")
    else:
        print(f"  Downloading vault ({fmt_size(expected_size)})...")
        download_file(dl_url, vault_file, label="Downloading vault")

    # ── Verify vault ────────────────────────────────────────────────
    print(f"\n  Verifying vault integrity...")
    actual_hash = file_sha256(vault_file)
    if actual_hash != vault_hash:
        print(red(f"  ❌ Hash mismatch after download: expected {vault_hash[:16]}..., got {actual_hash[:16]}..."))
        print(red(f"     The file may be corrupted. Try running this installer again."))
        sys.exit(1)
    print(green(f"  ✅ SHA-256 verified. Vault is intact."))

    # ── Place license.key ───────────────────────────────────────────
    target_license = target / "license.key"
    if target_license.exists():
        existing_lic = parse_license(target_license)
        if existing_lic.get("LICENSE_ID") == license_id:
            print(green(f"  ✅ License already in place."))
        else:
            backup = target / "license.key.bak"
            shutil.move(str(target_license), str(backup))
            shutil.copy2(str(license_path), str(target_license))
            print(green(f"  ✅ License updated (old one backed up as license.key.bak)."))
    else:
        shutil.copy2(str(license_path), str(target_license))
        print(green(f"  ✅ License placed in vault folder."))

    # ── Install dependencies if needed ──────────────────────────────
    venv_dir = target / ".venv"
    req_file = target / "requirements.txt"
    python_exe = None

    if req_file.exists():
        if (venv_dir / "Scripts" / "python.exe").exists():
            python_exe = str(venv_dir / "Scripts" / "python.exe")
            print(green(f"  ✅ Virtual environment already exists."))
        elif (venv_dir / "bin" / "python").exists():
            python_exe = str(venv_dir / "bin" / "python")
            print(green(f"  ✅ Virtual environment already exists."))

        if not python_exe:
            print(f"\n  Creating virtual environment...")
            import venv
            venv.create(venv_dir, with_pip=True)
            if (venv_dir / "Scripts" / "python.exe").exists():
                python_exe = str(venv_dir / "Scripts" / "python.exe")
            elif (venv_dir / "bin" / "python").exists():
                python_exe = str(venv_dir / "bin" / "python")
            else:
                print(yellow(f"  ⚠️  Could not create virtual environment. Will use system Python."))
                python_exe = sys.executable

            print(f"  Installing dependencies (may take a minute)...")
            subprocess.run(
                [python_exe, "-m", "pip", "install", "-r", str(req_file)],
                capture_output=True, timeout=300,
            )
            print(green(f"  ✅ Dependencies installed."))
    else:
        print(yellow(f"  ⚠️  No requirements.txt found in repo. Buyer may need to install manually."))
        python_exe = sys.executable

    # ── Run doctor ──────────────────────────────────────────────────
    mcp_server = target / "mcp_server.py"
    if mcp_server.exists():
        doctor_cmd = [python_exe, str(mcp_server), "--doctor"]
        print(f"\n  Running doctor check...")
        r = subprocess.run(doctor_cmd, capture_output=True, text=True, timeout=120)
        if r.returncode == 0:
            doctor_out = r.stdout.strip()
            if "FAIL" in doctor_out.upper() or "ERROR" in doctor_out.upper() or not doctor_out:
                print(yellow(f"  ⚠️  Doctor check had issues:"))
                for line in doctor_out.splitlines():
                    print(f"     {line}")
            else:
                last_line = [l for l in doctor_out.splitlines() if l.strip()][-1] if doctor_out else ""
                print(green(f"  ✅ Doctor passed: {last_line}"))
        else:
            print(yellow(f"  ⚠️  Doctor exited with code {r.returncode}:"))
            for line in r.stderr.splitlines():
                print(f"     {line}")
            print(yellow("  This may still work. Proceeding..."))
    else:
        print(yellow(f"  ⚠️  mcp_server.py not found in repo. Skipping doctor."))

    # ── Agent configuration ─────────────────────────────────────────
    if not args.no_agent:
        agents_to_configure = args.agent
        if not agents_to_configure:
            detected = detect_agent()
            if detected:
                print(f"\n  Detected agents: {', '.join(green(a) for a in detected)}")
                agents_to_configure = detected
            else:
                print(yellow(f"\n  ⚠️  No known AI agents detected on this machine."))

        if agents_to_configure:
            print(f"\n  Configuring MCP for your AI agent...")
            for agent in agents_to_configure:
                if agent == "codex":
                    configure_mcp_codex(target)
                elif agent == "claude-code":
                    configure_mcp_claude_code(target)
                elif agent == "cursor":
                    configure_mcp_cursor(target)
                elif agent == "hermes":
                    configure_mcp_hermes(target)
                elif agent == "none":
                    pass

    # ── Done ────────────────────────────────────────────────────────
    print()
    print(green(bold("  ✅ PlugICT installation complete!")))
    print(f"     Vault folder: {target}")
    print(f"     Licensed to:  {buyer}")
    print()
    print(STARTUP_HELP.format(agent=detect_agent() or "Your agent"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
