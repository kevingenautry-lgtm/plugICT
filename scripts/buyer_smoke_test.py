#!/usr/bin/env python3
"""buyer_smoke_test.py — prove the whole buyer journey on one real artifact.

Run this against an EXTRACTED hosted package (the plugict/ folder a buyer gets
after unzipping the GitHub Release) plus a real buyer license.key. It walks the
exact path a paying customer takes and reports a single PASS/FAIL, then prints
the real cited answer so you can screen-record it as proof:

    plugict.zip extracted  →  add license  →  configs  →  open vault  →
    ask one real question  →  get a sourced answer (title + timestamp + link)

Usage (on a machine where `setup.bat`/`setup.sh` deps are installed):

    python scripts/buyer_smoke_test.py \
        --package /path/to/extracted/plugict \
        --license /path/to/buyer/license.key \
        --query "What is an order block?"

Exit code 0 = every step passed. Non-zero = the buyer journey is broken; the
failing step is printed. This is a real integration test: it imports the SHIPPED
code from --package and decrypts the SHIPPED vault, so missing dependencies or a
bad vault surface here exactly as they would for a buyer.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

PASS, FAIL = "\033[32mPASS\033[0m", "\033[31mFAIL\033[0m"


class Smoke:
    def __init__(self) -> None:
        self.failures: list[str] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        print(f"  [{PASS if ok else FAIL}] {name}" + (f" — {detail}" if detail else ""))
        if not ok:
            self.failures.append(name)
        return ok


def _load_shipped(package: Path):
    """Import the vault_core + mcp_server that ship INSIDE the package, so the
    test exercises the buyer's actual code, not this repo's working copy."""
    sys.path.insert(0, str(package))
    import vault_core as vc  # noqa: E402
    return vc


def step_structure(s: Smoke, package: Path) -> None:
    print("[1/5] Package structure")
    required = [
        "ict-vault.kevin", "mcp_server.py", "vault_core.py",
        "metadata_enricher.py", "release.sig.json",
        "docs/README.md", "examples/make_configs.py",
    ]
    for rel in required:
        s.check(f"has {rel}", (package / rel).is_file())
    # A public package must never contain a license key.
    stray = list(package.rglob("*.key"))
    s.check("no *.key bundled in package", not stray,
            f"found {[p.name for p in stray]}" if stray else "")


def step_manifest(s: Smoke, vc, package: Path) -> None:
    print("[2/5] Signed release manifest")
    vault = package / "ict-vault.kevin"
    manifest = package / vc.RELEASE_MANIFEST_NAME
    vault_hash = __import__("hashlib").sha256(vault.read_bytes()).hexdigest()
    ok, reason = vc.verify_release_manifest(manifest, vault_hash)
    if vc.RELEASE_TRUSTED_KEYS:
        s.check("manifest verifies against pinned trust key", ok, reason)
    else:
        # No pinned key in this build: the manifest cannot be cryptographically
        # trusted, but it must at least describe this exact vault.
        try:
            m = json.loads(manifest.read_text())
            describes = (m.get("product") == vc.RELEASE_PRODUCT
                         and str(m.get("vault_sha256", "")).lower() == vault_hash)
        except (OSError, ValueError):
            describes = False
        s.check("manifest describes this exact vault (trust store empty)", describes)


def step_configs(s: Smoke, package: Path) -> None:
    print("[3/5] Config generation (examples/make_configs.py)")
    out = subprocess.run([sys.executable, str(package / "examples" / "make_configs.py")],
                         capture_output=True, text=True)
    ok = out.returncode == 0
    s.check("make_configs.py runs", ok, out.stderr.strip()[:200])
    cfg = package / "examples" / "claude_desktop_config.json"
    if cfg.is_file():
        s.check("config points at this machine's mcp_server.py",
                str(package / "mcp_server.py") in cfg.read_text().replace("\\\\", "\\"))


def step_open_vault(s: Smoke, package: Path):
    """Open the vault the way a buyer's agent does — through the MCP server's
    own ensure_vault(). We never call vault_core.open_vault directly: the vault
    intentionally rejects direct access, and going through mcp_server is exactly
    the path the product uses in production."""
    print("[4/5] Decrypt vault via the MCP server (the real buyer path)")
    try:
        import mcp_server as server  # SHIPPED server; needs setup deps installed
        server.ensure_vault()
        who = getattr(server, "_licensed_to", None)
        s.check("MCP server opens the vault", True,
                f"licensed to {who}" if who else "")
        return server
    except Exception as exc:  # noqa: BLE001 - report any failure as the buyer sees it
        s.check("MCP server opens the vault", False, f"{type(exc).__name__}: {exc}")
        return None


def step_first_answer(s: Smoke, server, query: str, top_k: int) -> None:
    print(f"[5/5] Ask first question: {query!r}")
    try:
        results = server.search_vault(query, top_k=top_k)
    except Exception as exc:  # noqa: BLE001
        s.check("query returns a sourced answer", False, f"{type(exc).__name__}: {exc}")
        return

    cited = [r for r in (results or [])
             if r.get("title") and (r.get("video_url") or r.get("video_id"))]
    s.check("query returns a sourced answer", bool(cited),
            f"{len(cited)} cited result(s)")
    if not cited:
        return
    # The proof capture — screen-record THIS block for social content.
    print("\n  ── Cited answer (real output; safe to screen-record) ─────────────")
    for r in cited[:3]:
        ts = r.get("timestamp") or "0:00"
        link = r.get("video_url") or r.get("video_id")
        print(f"   • {r['title']}  @ {ts}")
        print(f"     {link}")
        snip = (r.get("snippet") or "").strip().replace("\n", " ")
        if snip:
            print(f"     “{snip[:180]}”")
    print("  ──────────────────────────────────────────────────────────────────\n")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--package", type=Path, required=True,
                    help="extracted plugict/ folder (the unzipped hosted package)")
    ap.add_argument("--license", type=Path, required=True,
                    help="buyer license.key to open the vault with")
    ap.add_argument("--query", default="What is an order block?",
                    help="the first question to ask (default: a real ICT concept)")
    ap.add_argument("--top-k", type=int, default=5)
    args = ap.parse_args()

    package = args.package.resolve()
    license_file = args.license.resolve()
    if not package.is_dir():
        sys.exit(f"ERROR: --package is not a directory: {package}")
    if not license_file.is_file():
        sys.exit(f"ERROR: --license not found: {license_file}")

    # Point the SHIPPED code at this package's vault + the buyer's license.
    # vault_core reads these at import time, so they must be set before we load
    # the package modules.
    import os
    os.environ["ICT_VAULT_FILE"] = str(package / "ict-vault.kevin")
    os.environ["ICT_VAULT_LICENSE"] = str(license_file)

    print("=" * 66)
    print("PlugICT buyer-journey smoke test")
    print(f"  package: {package}")
    print(f"  license: {license_file}")
    print("=" * 66)

    s = Smoke()
    vc = _load_shipped(package)
    step_structure(s, package)
    step_manifest(s, vc, package)
    step_configs(s, package)
    server = step_open_vault(s, package)
    if server is not None:
        step_first_answer(s, server, args.query, args.top_k)

    print("=" * 66)
    if s.failures:
        print(f"RESULT: {FAIL} — {len(s.failures)} step(s) failed: {', '.join(s.failures)}")
        print("The buyer journey is NOT clean yet. Fix the above before promoting.")
        sys.exit(1)
    print(f"RESULT: {PASS} — clean buyer journey. Safe to soft-launch this artifact.")


if __name__ == "__main__":
    main()
