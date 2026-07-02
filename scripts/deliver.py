"""
deliver.py — Package vault for buyer delivery
==============================================
Bundles everything into a clean delivery folder ready for zip & send.

Usage:
    python deliver.py "ali@gmail.com" "ICT-2026001"

Output: delivery/{email_safe}/
"""

import sys, os, shutil
from pathlib import Path
from datetime import datetime

# Seller content (vault, licenses, docs) — override with ICT_SOURCE_DIR.
VAULT_DIR = Path(os.environ.get("ICT_SOURCE_DIR", r"C:\Users\kevin\Hermes ICT Selling Idea"))
# The buyer-facing code ships from this repo's scripts/ dir (next to deliver.py).
SCRIPT_DIR = Path(__file__).parent.resolve()
CODE_FILES = ["query.py", "mcp_server.py", "vault_core.py"]
DELIVERY_ROOT = VAULT_DIR / "delivery"

def deliver(buyer_email, purchase_id):
    """Package vault for a specific buyer."""
    
    print("=" * 60)
    print("ICT Knowledge Vault — Delivery Package")
    print("=" * 60)
    print(f"Buyer:    {buyer_email}")
    print(f"Purchase: {purchase_id}")
    print()
    
    safe_email = buyer_email.replace('@', '_at_').replace('.', '_')
    
    # ── Find license file ──
    license_files = list(VAULT_DIR.glob(f"license_{safe_email}*.key"))
    if not license_files:
        print("ERROR: License key not found. Run generate_key.py first:")
        print(f"   python generate_key.py \"{buyer_email}\" \"{purchase_id}\"")
        sys.exit(1)
    license_file = license_files[0]
    
    # ── Verify vault ──
    vault_file = VAULT_DIR / "ict-vault.kevin"
    if not vault_file.exists():
        print("ERROR: ict-vault.kevin not found. Run build.py first.")
        sys.exit(1)
    
    # ── Create delivery folder ──
    delivery_dir = DELIVERY_ROOT / safe_email
    if delivery_dir.exists():
        try:
            shutil.rmtree(str(delivery_dir))
        except PermissionError:
            # Folder locked, use timestamped name
            delivery_dir = DELIVERY_ROOT / f"{safe_email}_{datetime.now().strftime('%H%M%S')}"
    
    delivery_dir.mkdir(parents=True, exist_ok=True)
    
    # ── Copy vault ──
    print("[1/6] Copying encrypted vault...")
    shutil.copy2(vault_file, delivery_dir / "ict-vault.kevin")
    vault_size = (delivery_dir / "ict-vault.kevin").stat().st_size / 1024 / 1024
    print(f"  OK ict-vault.kevin ({vault_size:.0f} MB)")
    
    # ── Copy license ──
    print("[2/6] Copying license key...")
    shutil.copy2(license_file, delivery_dir / "license.key")
    print("  OK license.key")
    
    # ── Copy buyer-facing code (query.py, mcp_server.py, vault_core.py) ──
    print("[3/6] Copying application code...")
    for name in CODE_FILES:
        src = SCRIPT_DIR / name
        if src.exists():
            shutil.copy2(src, delivery_dir / name)
            print(f"  OK {name}")
        else:
            print(f"  ERROR: {name} not found next to deliver.py — aborting.")
            sys.exit(1)

    # ── Write requirements.txt (compatible-release pins, not open >=) ──
    # NOTE: run one clean `setup.bat` install and lock these to a tested
    # lockfile before a public launch.
    print("[4/6] Writing requirements + installers...")
    req_file = delivery_dir / "requirements.txt"
    req_file.write_text(
        "cryptography~=42.0\n"
        "chromadb~=0.5.0\n"
        "sentence-transformers~=3.0\n"
        "mcp~=1.2\n"
        "zstandard~=0.22\n"
        "rich~=13.7\n"
    )
    print("  OK requirements.txt (pinned)")

    # ── setup.bat — isolated venv so we never touch the buyer's global Python ──
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
        "echo Checking your setup...\r\n"
        ".venv\\Scripts\\python query.py --doctor\r\n"
        "echo.\r\n"
        "echo ========================================\r\n"
        "echo Setup complete!  Search with:\r\n"
        "echo   vault.bat \"your question\"\r\n"
        "echo ========================================\r\n"
        "pause\r\n"
    )
    # vault.bat / vault.sh wrappers so buyers never touch the venv directly.
    (delivery_dir / "vault.bat").write_text(
        "@echo off\r\n.venv\\Scripts\\python query.py %*\r\n")
    (delivery_dir / "setup.sh").write_text(
        "#!/usr/bin/env bash\n"
        "set -e\n"
        "cd \"$(dirname \"$0\")\"\n"
        "echo 'Creating isolated environment (.venv)...'\n"
        "python3 -m venv .venv\n"
        ".venv/bin/pip install --upgrade pip\n"
        ".venv/bin/pip install -r requirements.txt\n"
        ".venv/bin/python query.py --doctor\n"
        "echo 'Setup complete. Search with: ./vault.sh \"your question\"'\n")
    (delivery_dir / "vault.sh").write_text(
        "#!/usr/bin/env bash\n"
        "cd \"$(dirname \"$0\")\"\n"
        ".venv/bin/python query.py \"$@\"\n")
    for sh in ("setup.sh", "vault.sh"):
        try:
            os.chmod(delivery_dir / sh, 0o755)
        except OSError:
            pass
    print("  OK setup.bat / vault.bat / setup.sh / vault.sh")
    
    # ── Create examples folder ──
    print("[5/6] Creating example configs...")
    examples_dir = delivery_dir / "examples"
    examples_dir.mkdir(exist_ok=True)
    
    # Use the venv interpreter created by setup.bat (bare "python" lacks deps).
    base = delivery_dir.as_posix()
    venv_py = f"{base}/.venv/Scripts/python.exe"   # Windows; on mac/Linux use .venv/bin/python

    # Claude Desktop config
    claude_config = examples_dir / "claude_desktop_config.json"
    claude_config.write_text(f"""{{
  "mcpServers": {{
    "ict-knowledge-vault": {{
      "command": "{venv_py}",
      "args": ["{base}/mcp_server.py"]
    }}
  }}
}}
""")

    # Cursor config
    cursor_config = examples_dir / "cursor_mcp.json"
    cursor_config.write_text(f"""{{
  "mcpServers": {{
    "ict-knowledge-vault": {{
      "command": "{venv_py}",
      "args": ["{base}/mcp_server.py"]
    }}
  }}
}}
""")

    # Hermes config
    hermes_config = examples_dir / "hermes_config.yaml"
    hermes_config.write_text(f"""# Add to ~/.hermes/profiles/<name>/config.yaml
# On macOS/Linux change the command to: {base}/.venv/bin/python
mcp_servers:
  ict-knowledge-vault:
    command: "{venv_py}"
    args: ["{base}/mcp_server.py"]
""")
    
    print("  OK examples/")
    
    # ── Copy docs ──
    print("[6/6] Copying documentation...")
    docs_dir = delivery_dir / "docs"
    docs_dir.mkdir(exist_ok=True)
    
    src_docs = VAULT_DIR / "docs"
    if src_docs.exists():
        for doc in src_docs.glob("*.md"):
            shutil.copy2(doc, docs_dir / doc.name)
    
    # Extract license_id from key file
    with open(license_file) as f:
        lic_content = f.read()
    lic_id = "unknown"
    for line in lic_content.strip().split('\n'):
        if line.startswith('LICENSE_ID='):
            lic_id = line.split('=', 1)[1].strip()
    
    # Write README
    readme = docs_dir / "README.md"
    readme.write_text(f"""# ICT Knowledge Vault

## Quick Start

```
setup.bat                       # One-time: builds an isolated environment
vault.bat "Fair Value Gap"      # Search the vault
vault.bat                       # Interactive mode (decrypt once, ask many)
```

macOS / Linux: use `./setup.sh` then `./vault.sh "your question"`.

Something not working? Run `vault.bat --doctor` for a health check.

## Connect to AI Agent

```
.venv\\Scripts\\python mcp_server.py   # Start MCP server
```
Then add `examples/claude_desktop_config.json` to your Claude Desktop config.
See `AI-AGENT-GUIDE.md` for the full setup guide.

## License

Licensed to: **{buyer_email}**
Purchase ID: {purchase_id}
License ID: {lic_id}

Do not share. Your license is traceable to you.
""")
    
    # ── Summary ──
    print()
    total_size = sum(f.stat().st_size for f in delivery_dir.rglob('*') if f.is_file()) / 1024 / 1024
    
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
            icon = '🔑' if f.name == 'license.key' else '📦' if f.name.endswith('.kevin') else '🐍' if f.suffix == '.py' else '📄' if f.suffix == '.md' else '⚙️' if f.suffix in ('.json','.yaml','.bat') else '📁'
            print(f"   {icon} {rel} ({size/1024:.0f} KB)")
    print()
    print("Next: Zip this folder and send to buyer.")
    print("  Right-click folder → Send to → Compressed (zipped) folder")
    print("=" * 60)
    
    return delivery_dir

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python deliver.py <buyer_email> [purchase_id]")
        print("Example: python deliver.py ali@gmail.com ICT-2026001")
        sys.exit(1)
    
    buyer_email = sys.argv[1]
    purchase_id = sys.argv[2] if len(sys.argv) > 2 else f"ICT-{datetime.now().strftime('%Y%m%d%H%M')}"
    
    deliver(buyer_email, purchase_id)
