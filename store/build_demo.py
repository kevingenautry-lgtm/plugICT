"""
build_demo.py — Build the FREE demo vault (try-before-buy)
===========================================================
Produces a small, watermarked vault with a handful of videos and a bundled
license (no purchase needed), packaged into a folder ready to zip and host on
the landing page.

The demo reuses the REAL pipeline (ict_ingest.py -> build.py), so search,
glossary, reranker and MCP behave exactly like the paid product — the only
differences are the video count and the "DEMO — N/576" watermark stamped into
the vault itself.

Usage (on the seller machine, where the full transcript library lives):

    ICT_SOURCE_DIR="C:/path/to/full/library" \
    python store/build_demo.py --count 5 --cta "https://your-site/#pricing"

    # or hand-pick the demo videos:
    python store/build_demo.py --videos "2022 ICT Mentorship - Ep 04.md" ...

Output: store/demo_build/ict-vault-demo/   (zip this folder and host it)
"""

import os
import sys
import shutil
import argparse
import subprocess
from pathlib import Path

STORE_DIR = Path(__file__).resolve().parent
ROOT = STORE_DIR.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
from generate_key import generate_license  # noqa: E402

FULL_TOTAL = "576"


def build_demo(source_dir, count=5, videos=None, cta="https://YOUR-SITE/#pricing"):
    source_dir = Path(source_dir)
    if not source_dir.exists():
        sys.exit(f"ERROR: source dir not found: {source_dir}\n"
                 "Set ICT_SOURCE_DIR to your full transcript library.")

    stage = STORE_DIR / "demo_build" / "_stage"
    out_dir = STORE_DIR / "demo_build" / "ict-vault-demo"
    for d in (stage, out_dir):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)

    # ── 1) Stage the demo transcripts ──
    if videos:
        picks = [source_dir / v for v in videos]
        missing = [p.name for p in picks if not p.exists()]
        if missing:
            sys.exit(f"ERROR: not found in source dir: {missing}")
    else:
        all_md = [f for f in sorted(source_dir.glob("*.md"))
                  if f.name not in ("index.md", "README.md", "CATALOG.md")]
        # Prefer 2022 Mentorship episodes for the demo; fall back to the first N.
        preferred = [f for f in all_md if "2022 ICT Mentorship" in f.name]
        picks = (preferred or all_md)[:count]
    if not picks:
        sys.exit("ERROR: no transcripts found to stage.")
    for p in picks:
        shutil.copy2(p, stage / p.name)
    print(f"[1/4] Staged {len(picks)} demo transcripts:")
    for p in picks:
        print(f"      - {p.name}")

    env = dict(os.environ, ICT_SOURCE_DIR=str(stage), ICT_DEMO="1",
               ICT_DEMO_TOTAL=FULL_TOTAL, ICT_DEMO_CTA=cta)

    # ── 2) Index (chunks + vectors + KG) then build the encrypted vault ──
    print("[2/4] Indexing demo transcripts (ict_ingest.py)...")
    r = subprocess.run([sys.executable, str(SCRIPTS / "ict_ingest.py")], env=env)
    if r.returncode != 0:
        sys.exit("ERROR: ict_ingest.py failed (is chromadb installed?)")

    print("[3/4] Building encrypted demo vault (build.py)...")
    r = subprocess.run([sys.executable, str(SCRIPTS / "build.py")], env=env)
    if r.returncode != 0:
        sys.exit("ERROR: build.py failed")

    # ── 3) Bundled demo license (same crypto path; not tied to a buyer) ──
    lic_file, lic_id = generate_license("demo@ict-vault.free", "DEMO")

    # ── 4) Assemble the ready-to-zip folder ──
    print("[4/4] Assembling demo package...")
    shutil.move(str(stage / "ict-vault.kevin"), out_dir / "ict-vault.kevin")
    shutil.move(str(lic_file), out_dir / "license.key")
    for name in ("query.py", "mcp_server.py", "vault_core.py"):
        shutil.copy2(SCRIPTS / name, out_dir / name)
    (out_dir / "requirements.txt").write_text(
        "cryptography~=42.0\nchromadb~=0.5.0\nsentence-transformers~=3.0\n"
        "mcp~=1.2\nzstandard~=0.22\nrich~=13.7\n")
    (out_dir / "README.txt").write_text(
        f"ICT VAULT — FREE DEMO ({len(picks)}/{FULL_TOTAL} videos)\n"
        "=================================================\n\n"
        "1. pip install -r requirements.txt\n"
        "2. python query.py \"Fair Value Gap\"\n"
        "3. Optional: python mcp_server.py  (connect Claude Desktop / Cursor / Hermes)\n\n"
        f"This demo searches {len(picks)} videos. The full vault has {FULL_TOTAL}\n"
        f"across 10 playlists, with the same search engine.\n\n"
        f"Unlock everything: {cta}\n")

    # Never ship seller secrets: wipe the staging dir (holds .vault_key).
    shutil.rmtree(stage)

    size = sum(f.stat().st_size for f in out_dir.rglob("*") if f.is_file()) / 1024 / 1024
    print()
    print("=" * 56)
    print(f"DEMO PACKAGE READY: {out_dir}")
    print(f"  {len(picks)} videos · {size:.0f} MB · license bundled (no key needed)")
    print("  Zip this folder and host it behind the 'Try Free Demo' button.")
    print("=" * 56)
    return out_dir


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Build the free watermarked demo vault.")
    ap.add_argument("--count", type=int, default=5, help="Number of demo videos (default 5)")
    ap.add_argument("--videos", nargs="*", help="Explicit transcript filenames to include")
    ap.add_argument("--cta", default=os.environ.get("ICT_DEMO_CTA", "https://YOUR-SITE/#pricing"),
                    help="Buy link shown in the demo watermark")
    ap.add_argument("--source", default=os.environ.get("ICT_SOURCE_DIR"),
                    help="Full transcript library (or set ICT_SOURCE_DIR)")
    a = ap.parse_args()
    if not a.source:
        ap.error("Provide --source or set ICT_SOURCE_DIR")
    build_demo(a.source, a.count, a.videos, a.cta)
