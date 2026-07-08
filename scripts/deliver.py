"""
deliver.py — Package vault for delivery
========================================
Two modes:

  Hosted (public download, license emailed separately — the webhook flow):
      python deliver.py --hosted
      → delivery/plugict/ + delivery/plugict.zip  (NO license.key inside!)
        Upload plugict.zip to a GitHub Release. Every buyer downloads the
        same zip; their personal license.key arrives by email.

  Per-buyer (manual delivery — DuitNow / USDT orders):
      python deliver.py "ali@gmail.com" "ICT-2026001"
      → delivery/{email_safe}/  (includes that buyer's license.key)

Never upload a per-buyer folder publicly: it contains a real license.key.
"""

import sys, os, shutil
from pathlib import Path
from datetime import datetime

# Seller content (vault, licenses, docs) — override with ICT_SOURCE_DIR.
VAULT_DIR = Path(os.environ.get("ICT_SOURCE_DIR", r"C:\Users\kevin\Hermes ICT Selling Idea"))
# The buyer-facing code ships from this repo's scripts/ dir (next to deliver.py).
SCRIPT_DIR = Path(__file__).parent.resolve()
# Buyers get the AI-agent (MCP) product only — no CLI search tool.
CODE_FILES = ["mcp_server.py", "vault_core.py"]
DELIVERY_ROOT = VAULT_DIR / "delivery"


# ── shared packaging steps ───────────────────────────────────────────────────

def _fresh_dir(name):
    d = DELIVERY_ROOT / name
    if d.exists():
        try:
            shutil.rmtree(str(d))
        except PermissionError:
            d = DELIVERY_ROOT / f"{name}_{datetime.now().strftime('%H%M%S')}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _copy_vault(delivery_dir):
    vault_file = VAULT_DIR / "ict-vault.kevin"
    if not vault_file.exists():
        print("ERROR: ict-vault.kevin not found. Run build.py first.")
        sys.exit(1)
    shutil.copy2(vault_file, delivery_dir / "ict-vault.kevin")
    size = (delivery_dir / "ict-vault.kevin").stat().st_size / 1024 / 1024
    print(f"  OK ict-vault.kevin ({size:.0f} MB)")


def _copy_code(delivery_dir):
    for name in CODE_FILES:
        src = SCRIPT_DIR / name
        if src.exists():
            shutil.copy2(src, delivery_dir / name)
            print(f"  OK {name}")
        else:
            print(f"  ERROR: {name} not found next to deliver.py — aborting.")
            sys.exit(1)


def _write_requirements(delivery_dir):
    # NOTE: run one clean `setup.bat` install and lock these to a tested
    # lockfile before a public launch.
    (delivery_dir / "requirements.txt").write_text(
        "cryptography~=42.0\n"
        "chromadb~=0.5.0\n"
        "sentence-transformers~=3.0\n"
        "mcp~=1.2\n"
        "zstandard~=0.22\n"
    )
    print("  OK requirements.txt (pinned)")


def _write_installers(delivery_dir):
    # setup regenerates the example configs ON THE BUYER'S MACHINE via
    # examples/make_configs.py, so the paths inside them are always correct
    # no matter where the buyer extracted the folder.
    setup_bat = delivery_dir / "setup.bat"
    setup_bat.write_text(
        "@echo off\r\n"
        "echo ========================================\r\n"
        "echo ICT Knowledge Vault - Setup\r\n"
        "echo ========================================\r\n"
        "echo.\r\n"
        "echo Creating isolated environment (.venv)...\r\n"
        "py -m venv .venv || python -m venv .venv\r\n"
        "echo Installing dependencies (first run downloads ~1 min)...\r\n"
        ".venv\\Scripts\\python -m pip install --upgrade pip\r\n"
        ".venv\\Scripts\\pip install -r requirements.txt\r\n"
        "echo.\r\n"
        "echo Writing AI-agent configs for this folder...\r\n"
        ".venv\\Scripts\\python examples\\make_configs.py\r\n"
        "echo.\r\n"
        "echo Verifying your vault...\r\n"
        ".venv\\Scripts\\python mcp_server.py --doctor\r\n"
        "echo.\r\n"
        "echo ========================================\r\n"
        "echo Setup complete!\r\n"
        "echo Now connect your AI agent:\r\n"
        "echo   add examples\\claude_desktop_config.json to Claude Desktop\r\n"
        "echo   (see docs\\AI-AGENT-GUIDE.md)\r\n"
        "echo Then just ask your AI about any ICT concept.\r\n"
        "echo ========================================\r\n"
        "pause\r\n"
    )
    (delivery_dir / "setup.sh").write_text(
        "#!/usr/bin/env bash\n"
        "set -e\n"
        "cd \"$(dirname \"$0\")\"\n"
        "echo 'Creating isolated environment (.venv)...'\n"
        "python3 -m venv .venv\n"
        ".venv/bin/pip install --upgrade pip\n"
        ".venv/bin/pip install -r requirements.txt\n"
        "echo 'Writing AI-agent configs for this folder...'\n"
        ".venv/bin/python examples/make_configs.py\n"
        "echo 'Verifying your vault...'\n"
        ".venv/bin/python mcp_server.py --doctor\n"
        "echo 'Setup complete. Connect your AI agent — see docs/AI-AGENT-GUIDE.md'\n")
    try:
        os.chmod(delivery_dir / "setup.sh", 0o755)
    except OSError:
        pass
    print("  OK setup.bat / setup.sh")


_MAKE_CONFIGS = '''"""Write AI-agent config examples with THIS folder's real paths.

Runs automatically from setup.bat / setup.sh. Safe to re-run any time —
for example after moving this folder somewhere else.
"""
import json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV_PY = ROOT / ('.venv/Scripts/python.exe' if sys.platform == 'win32' else '.venv/bin/python')
SERVER = ROOT / 'mcp_server.py'
EXAMPLES = Path(__file__).resolve().parent

cfg = {"mcpServers": {"ict-knowledge-vault": {
    "command": str(VENV_PY), "args": [str(SERVER)]}}}

(EXAMPLES / 'claude_desktop_config.json').write_text(json.dumps(cfg, indent=2) + "\\n")
(EXAMPLES / 'cursor_mcp.json').write_text(json.dumps(cfg, indent=2) + "\\n")
(EXAMPLES / 'hermes_config.yaml').write_text(
    "# Add to ~/.hermes/profiles/<name>/config.yaml\\n"
    "mcp_servers:\\n"
    "  ict-knowledge-vault:\\n"
    f'    command: "{VENV_PY.as_posix()}"\\n'
    f'    args: ["{SERVER.as_posix()}"]\\n')

print(f"AI-agent configs written for: {ROOT}")
'''


def _write_examples(delivery_dir):
    examples_dir = delivery_dir / "examples"
    examples_dir.mkdir(exist_ok=True)

    # The real configs are generated on the buyer's machine by make_configs.py
    # (called from setup). Ship placeholders that say exactly that, so anyone
    # opening them before running setup isn't misled by someone else's paths.
    placeholder = (
        "Run setup.bat (Windows) or ./setup.sh (macOS/Linux) first —\n"
        "it rewrites this file with the correct paths for YOUR computer.\n"
    )
    (examples_dir / "claude_desktop_config.json").write_text(
        '{\n  "_note": "Run setup.bat / setup.sh first — it fills this file '
        'with the correct paths for your computer."\n}\n')
    (examples_dir / "cursor_mcp.json").write_text(
        '{\n  "_note": "Run setup.bat / setup.sh first — it fills this file '
        'with the correct paths for your computer."\n}\n')
    (examples_dir / "hermes_config.yaml").write_text("# " + placeholder.replace("\n", "\n# ").rstrip("# "))
    (examples_dir / "make_configs.py").write_text(_MAKE_CONFIGS)
    print("  OK examples/ (configs auto-generated on the buyer's machine at setup)")


def _copy_docs(delivery_dir):
    docs_dir = delivery_dir / "docs"
    docs_dir.mkdir(exist_ok=True)
    src_docs = VAULT_DIR / "docs"
    if src_docs.exists():
        for doc in src_docs.glob("*.md"):
            shutil.copy2(doc, docs_dir / doc.name)
    return docs_dir


_README_BODY = """
Your AI agent, upgraded with 576 ICT videos. Ask it anything about ICT and it
answers with exact video sources and timestamps.

## 1. Set up (one time)

{license_step}```
setup.bat            # Windows  — builds an isolated environment + verifies
./setup.sh           # macOS / Linux
```

Something off? Re-run and read the check, or:
`.venv\\Scripts\\python mcp_server.py --doctor`

## 2. Connect your AI agent

Add the config from `examples/` to your agent, then restart it:
- **Claude Desktop** → `examples/claude_desktop_config.json`
- **Cursor** → `examples/cursor_mcp.json`
- **Hermes** → `examples/hermes_config.yaml`

(These are written for your computer when setup runs. Moved the folder?
Just run setup again.)

Your agent now has ICT tools (search_ict, explore_concept, glossary_lookup…).
Full walkthrough: `docs/AI-AGENT-GUIDE.md`.

## 3. Ask

> "How does ICT teach the Silver Bullet entry?"

Your AI searches all 576 videos and answers with cited timestamps.

## License

{license_note}
"""


def _print_summary(delivery_dir, extra=""):
    total_size = sum(f.stat().st_size for f in delivery_dir.rglob('*') if f.is_file()) / 1024 / 1024
    print()
    print("=" * 60)
    print("DELIVERY PACKAGE READY")
    print(f"   Folder: {delivery_dir}")
    print(f"   Size:   {total_size:.0f} MB")
    print()
    print("Contents:")
    for f in sorted(delivery_dir.rglob('*')):
        if f.is_file():
            rel = f.relative_to(delivery_dir)
            size = f.stat().st_size
            icon = '🔑' if f.name == 'license.key' else '📦' if f.name.endswith('.kevin') else '🐍' if f.suffix == '.py' else '📄' if f.suffix == '.md' else '⚙️' if f.suffix in ('.json', '.yaml', '.bat') else '📁'
            print(f"   {icon} {rel} ({size/1024:.0f} KB)")
    if extra:
        print()
        print(extra)
    print("=" * 60)


# ── hosted mode: one public zip, NO license inside ──────────────────────────

def deliver_hosted():
    """Package the public download (GitHub Release). Contains NO license.key —
    each buyer's key is emailed by the webhook / issue_license.py."""
    print("=" * 60)
    print("ICT Knowledge Vault — HOSTED Package (public download)")
    print("=" * 60)
    print()

    delivery_dir = _fresh_dir("plugict")

    print("[1/5] Copying encrypted vault...")
    _copy_vault(delivery_dir)

    print("[2/5] Copying application code...")
    _copy_code(delivery_dir)

    print("[3/5] Writing requirements + installers...")
    _write_requirements(delivery_dir)
    _write_installers(delivery_dir)

    print("[4/5] Creating example configs...")
    _write_examples(delivery_dir)

    print("[5/5] Copying documentation...")
    docs_dir = _copy_docs(delivery_dir)
    (docs_dir / "README.md").write_text("# ICT Knowledge Vault\n" + _README_BODY.format(
        license_step=("**First**: put your personal `license.key` (from your purchase\n"
                      "email) in this folder, next to `setup.bat`. Then run:\n\n"),
        license_note=("This vault only opens with the `license.key` from your purchase email.\n"
                      "Didn't get one? Check spam, or contact support with your payment receipt.\n\n"
                      "Do not share your license — it is tied to you and traceable."),
    ))
    print("  OK docs/README.md (hosted)")

    # sanity guard: a public zip must never contain a license
    leaked = list(delivery_dir.rglob("*license*.key")) + list(delivery_dir.rglob("license.key"))
    if leaked:
        print(f"  ERROR: license key found in hosted package: {leaked} — aborting.")
        sys.exit(1)

    print()
    print("Zipping...")
    zip_base = DELIVERY_ROOT / "plugict"
    zip_path = shutil.make_archive(str(zip_base), "zip",
                                   root_dir=str(DELIVERY_ROOT), base_dir=delivery_dir.name)
    print(f"  OK {zip_path}")

    _print_summary(delivery_dir, extra=(
        f"Next: upload {Path(zip_path).name} to a GitHub Release (tag v1.0).\n"
        "  It becomes 'latest' — the download link in the license email.\n"
        "  This zip contains NO license.key; buyers get theirs by email."))
    return Path(zip_path)


# ── per-buyer mode (manual DuitNow / USDT orders) ────────────────────────────

def deliver(buyer_email, purchase_id):
    """Package vault for a specific buyer (their license.key included)."""
    print("=" * 60)
    print("ICT Knowledge Vault — Delivery Package")
    print("=" * 60)
    print(f"Buyer:    {buyer_email}")
    print(f"Purchase: {purchase_id}")
    print()

    safe_email = buyer_email.replace('@', '_at_').replace('.', '_')

    license_files = list(VAULT_DIR.glob(f"license_{safe_email}*.key"))
    if not license_files:
        print("ERROR: License key not found. Run generate_key.py first:")
        print(f"   python generate_key.py \"{buyer_email}\" \"{purchase_id}\"")
        sys.exit(1)
    license_file = license_files[0]

    delivery_dir = _fresh_dir(safe_email)

    print("[1/6] Copying encrypted vault...")
    _copy_vault(delivery_dir)

    print("[2/6] Copying license key...")
    shutil.copy2(license_file, delivery_dir / "license.key")
    print("  OK license.key")

    print("[3/6] Copying application code...")
    _copy_code(delivery_dir)

    print("[4/6] Writing requirements + installers...")
    _write_requirements(delivery_dir)
    _write_installers(delivery_dir)

    print("[5/6] Creating example configs...")
    _write_examples(delivery_dir)

    print("[6/6] Copying documentation...")
    docs_dir = _copy_docs(delivery_dir)

    lic_id = "unknown"
    for line in license_file.read_text().strip().split('\n'):
        if line.startswith('LICENSE_ID='):
            lic_id = line.split('=', 1)[1].strip()

    (docs_dir / "README.md").write_text("# ICT Knowledge Vault\n" + _README_BODY.format(
        license_step="",
        license_note=(f"Licensed to: **{buyer_email}**\n"
                      f"Purchase ID: {purchase_id}\n"
                      f"License ID: {lic_id}\n\n"
                      "Do not share. Your license is traceable to you."),
    ))

    _print_summary(delivery_dir, extra=(
        "Next: Zip this folder and send to buyer.\n"
        "  Right-click folder → Send to → Compressed (zipped) folder\n"
        "  (Contains this buyer's license.key — never upload publicly.)"))
    return delivery_dir


if __name__ == "__main__":
    if "--hosted" in sys.argv:
        deliver_hosted()
    elif len(sys.argv) >= 2:
        buyer_email = sys.argv[1]
        purchase_id = sys.argv[2] if len(sys.argv) > 2 else f"ICT-{datetime.now().strftime('%Y%m%d%H%M')}"
        deliver(buyer_email, purchase_id)
    else:
        print("Usage:")
        print("  python deliver.py --hosted                      # public zip for GitHub Release (no license)")
        print("  python deliver.py <buyer_email> [purchase_id]   # per-buyer folder (license included)")
        sys.exit(1)
