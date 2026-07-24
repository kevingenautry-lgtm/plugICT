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

import hashlib
import hmac
import json
import re
import tempfile
import sys, os, shutil
from pathlib import Path
from datetime import datetime

from artifact_paths import resolve_artifact_dir
import vault_core as vc

# Source transcripts/docs and built encrypted artifacts may live separately.
SOURCE_DIR = Path(os.environ.get("ICT_SOURCE_DIR", r"C:\Users\kevin\Hermes ICT Selling Idea"))
ARTIFACT_DIR = resolve_artifact_dir(SOURCE_DIR)
# The buyer-facing code ships from this repo's scripts/ dir (next to deliver.py).
SCRIPT_DIR = Path(__file__).parent.resolve()
# Buyers get the AI-agent (MCP) product only — no CLI search tool.
CODE_FILES = ["mcp_server.py", "vault_core.py", "metadata_enricher.py"]
ROOT_ASSET_FILES = ["VAULT.md", "setup.py"]
DELIVERY_ROOT = Path(os.environ.get("ICT_DELIVERY_DIR", ARTIFACT_DIR / "delivery"))


# ── shared packaging steps ───────────────────────────────────────────────────


def _parse_license_text(text):
    fields = {}
    for line in text.splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            fields[key.strip()] = value.strip()
    return fields


def _parse_license_fields(license_file):
    return _parse_license_text(Path(license_file).read_text(encoding="utf-8"))


def _file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        while True:
            block = source.read(1024 * 1024)
            if not block:
                return digest.hexdigest()
            digest.update(block)


def _verify_buyer_license(license_file, vault_file, buyer_email, purchase_id):
    license_bytes = Path(license_file).read_bytes()
    fields = _parse_license_text(license_bytes.decode("utf-8"))
    expected_hash = fields.get("VAULT_HASH", "")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", expected_hash):
        raise RuntimeError("selected license has a missing or malformed VAULT_HASH")
    actual_hash = _file_sha256(vault_file)
    if not hmac.compare_digest(expected_hash.lower(), actual_hash):
        raise RuntimeError("selected license VAULT_HASH does not match ict-vault.kevin")
    if fields.get("LICENSED_TO") != buyer_email:
        raise RuntimeError("selected license identity does not match the requested buyer")
    if fields.get("PURCHASE_ID") != purchase_id:
        raise RuntimeError("selected license purchase ID does not match the requested order")
    return fields


def _snapshot_verified_vault(vault_file, expected_hash):
    """Copy the licensed artifact once, then bind the snapshot bytes to its hash."""
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    fd, raw_path = tempfile.mkstemp(prefix=".delivery-vault-", suffix=".tmp",
                                    dir=str(ARTIFACT_DIR))
    snapshot = Path(raw_path)
    digest = hashlib.sha256()
    try:
        with Path(vault_file).open("rb") as source, os.fdopen(fd, "wb") as target:
            while True:
                block = source.read(1024 * 1024)
                if not block:
                    break
                target.write(block)
                digest.update(block)
            target.flush()
            os.fsync(target.fileno())
        if not hmac.compare_digest(expected_hash.lower(), digest.hexdigest()):
            raise RuntimeError(
                "ict-vault.kevin changed while creating the licensed delivery snapshot")
        return snapshot
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        snapshot.unlink(missing_ok=True)
        raise


def _fresh_dir(name):
    d = DELIVERY_ROOT / name
    if d.exists():
        try:
            shutil.rmtree(str(d))
        except PermissionError:
            d = DELIVERY_ROOT / f"{name}_{datetime.now().strftime('%H%M%S')}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _copy_vault(delivery_dir, vault_file=None):
    vault_file = Path(vault_file) if vault_file else ARTIFACT_DIR / "ict-vault.kevin"
    if not vault_file.exists():
        print("ERROR: ict-vault.kevin not found. Run build.py first.")
        sys.exit(1)
    shutil.copy2(vault_file, delivery_dir / "ict-vault.kevin")
    size = (delivery_dir / "ict-vault.kevin").stat().st_size / 1024 / 1024
    print(f"  OK ict-vault.kevin ({size:.0f} MB)")


def _copy_release_manifest(delivery_dir, vault_file=None):
    """Ship the signed release manifest beside the vault — mandatory.

    Without release.sig.json next to ict-vault.kevin, a buyer client can never
    authorize a future updated vault (issued licenses pin one exact hash), so a
    hosted package missing it is broken by construction. Fails closed when the
    manifest is absent or does not describe the exact vault being shipped; when
    the buyer trust store is populated, the signature must also verify.
    """
    vault_file = Path(vault_file) if vault_file else ARTIFACT_DIR / "ict-vault.kevin"
    manifest_src = vault_file.parent / vc.RELEASE_MANIFEST_NAME
    if not manifest_src.is_file():
        print(f"  ERROR: {vc.RELEASE_MANIFEST_NAME} not found beside {vault_file.name}.")
        print("         Sign this release first:")
        print("           python scripts/sign_release.py --tag vX.Y.Z")
        sys.exit(1)

    vault_hash = _file_sha256(vault_file)
    try:
        manifest = json.loads(manifest_src.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"  ERROR: {vc.RELEASE_MANIFEST_NAME} is unreadable: {e} — aborting.")
        sys.exit(1)
    claimed = str(manifest.get("vault_sha256", "")).strip().lower()
    if manifest.get("product") != vc.RELEASE_PRODUCT or claimed != vault_hash:
        print(f"  ERROR: {vc.RELEASE_MANIFEST_NAME} does not describe this exact vault")
        print(f"         (product={manifest.get('product')!r}, manifest hash {claimed[:16]}…,")
        print(f"          actual vault hash {vault_hash[:16]}…). Re-sign the release:")
        print("           python scripts/sign_release.py --tag vX.Y.Z")
        sys.exit(1)
    if vc.RELEASE_TRUSTED_KEYS:
        ok, reason = vc.verify_release_manifest(manifest_src, vault_hash)
        if not ok:
            print(f"  ERROR: release manifest fails buyer-side verification: {reason}")
            print("         Buyers with the pinned trust store would reject this package.")
            sys.exit(1)
    else:
        print("  WARNING: RELEASE_TRUSTED_KEYS is empty — buyers cannot verify this")
        print("           manifest until the seller public key is pinned in vault_core.py")
        print("           (run scripts/sign_release.py --init and commit the snippet).")

    shutil.copy2(manifest_src, delivery_dir / vc.RELEASE_MANIFEST_NAME)
    print(f"  OK {vc.RELEASE_MANIFEST_NAME} (tag {manifest.get('tag', '?')})")


def _copy_code(delivery_dir):
    for name in CODE_FILES:
        src = SCRIPT_DIR / name
        if src.exists():
            shutil.copy2(src, delivery_dir / name)
            print(f"  OK {name}")
        else:
            print(f"  ERROR: {name} not found next to deliver.py — aborting.")
            sys.exit(1)
    for name in ROOT_ASSET_FILES:
        src = SCRIPT_DIR.parent / name
        if not src.is_file():
            print(f"  ERROR: required buyer asset missing: {src}")
            sys.exit(1)
        shutil.copy2(src, delivery_dir / name)
        print(f"  OK {name}")


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
    # setup.bat/setup.sh are only OS wrappers. setup.py is the single canonical
    # installer for both repository clones and downloaded buyer packages.
    setup_bat = delivery_dir / "setup.bat"
    crlf = chr(13) + chr(10)
    setup_bat.write_text(crlf.join([
        "@echo off",
        "title PlugICT Installer",
        "echo Starting PlugICT installer...",
        "python --version >nul 2>&1",
        "if errorlevel 1 (",
        "  echo Python 3.10+ is required: https://www.python.org/downloads/",
        "  pause",
        "  exit /b 1",
        ")",
        "python setup.py",
        "if errorlevel 1 (",
        "  echo Installation encountered an error. See messages above.",
        "  pause",
        "  exit /b 1",
        ")",
        "echo Setup complete. You can close this window.",
        "pause",
    ]) + crlf, encoding="utf-8")
    (delivery_dir / "setup.sh").write_text(
        "#!/usr/bin/env bash\n"
        "set -e\n"
        "cd \"$(dirname \"$0\")\"\n"
        "python3 setup.py\n")
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
    "command": str(VENV_PY), "args": ["-E", str(SERVER)]}}}

(EXAMPLES / 'claude_desktop_config.json').write_text(json.dumps(cfg, indent=2) + "\\n")
(EXAMPLES / 'cursor_mcp.json').write_text(json.dumps(cfg, indent=2) + "\\n")
(EXAMPLES / 'hermes_config.yaml').write_text(
    "# Add to ~/.hermes/profiles/<name>/config.yaml\\n"
    "mcp_servers:\\n"
    "  ict-knowledge-vault:\\n"
    f'    command: "{VENV_PY.as_posix()}"\\n'
    f'    args: ["-E", "{SERVER.as_posix()}"]\\n')

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
    src_docs = SOURCE_DIR / "docs"
    if src_docs.exists():
        for doc in src_docs.glob("*.md"):
            shutil.copy2(doc, docs_dir / doc.name)
    return docs_dir


_README_BODY = """
Your AI agent, upgraded with 775 ICT videos. Ask it anything about ICT and it
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

Your AI searches all 775 videos and answers with cited timestamps.

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

    print("[1/5] Copying encrypted vault + signed release manifest...")
    _copy_vault(delivery_dir)
    _copy_release_manifest(delivery_dir)

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

    license_files = list(ARTIFACT_DIR.glob(f"license_{safe_email}*.key"))
    if not license_files:
        print("ERROR: License key not found. Run generate_key.py first:")
        print(f"   python generate_key.py \"{buyer_email}\" \"{purchase_id}\"")
        sys.exit(1)
    license_file = license_files[0]
    vault_file = ARTIFACT_DIR / "ict-vault.kevin"
    if not vault_file.is_file():
        print("ERROR: ict-vault.kevin not found. Run build.py first.")
        sys.exit(1)
    try:
        license_fields = _verify_buyer_license(
            license_file, vault_file, buyer_email, purchase_id)
        license_bytes = license_file.read_bytes()
        if _parse_license_text(license_bytes.decode("utf-8")) != license_fields:
            raise RuntimeError("selected license changed while creating delivery snapshot")
        vault_snapshot = _snapshot_verified_vault(
            vault_file, license_fields["VAULT_HASH"])
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        print("Refresh or regenerate the buyer license before delivery.")
        sys.exit(1)

    delivery_dir = None
    try:
        delivery_dir = _fresh_dir(safe_email)

        print("[1/6] Copying encrypted vault...")
        _copy_vault(delivery_dir, vault_snapshot)
        copied_hash = _file_sha256(delivery_dir / "ict-vault.kevin")
        if not hmac.compare_digest(
                copied_hash, license_fields["VAULT_HASH"].lower()):
            raise RuntimeError("copied vault bytes do not match the selected license")
    except RuntimeError as exc:
        if delivery_dir is not None:
            shutil.rmtree(delivery_dir, ignore_errors=True)
        print(f"ERROR: {exc}")
        sys.exit(1)
    finally:
        vault_snapshot.unlink(missing_ok=True)

    print("[2/6] Copying license key...")
    (delivery_dir / "license.key").write_bytes(license_bytes)
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

    lic_id = license_fields.get("LICENSE_ID", "unknown")

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
