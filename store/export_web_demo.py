"""
export_web_demo.py — Export the DEMO vault as a JSON index for the landing page
================================================================================
Produces landing/demo-index.json: the 5 demo videos' chunks (title, timestamp,
text, YouTube id) + the ICT acronym map, so the landing page can run a real,
client-side "try it now" search with cited timestamps — no backend, no API key.

Only ever run this on a DEMO vault. The demo content is already public by
design (the free demo zip ships its own license), so a JSON index of the same
5 videos adds zero new exposure. The script refuses to export a non-demo vault.

Usage (after `python store/build_demo.py ...`):

    python store/export_web_demo.py \
        --vault store/demo_build/ict-vault-demo/ict-vault.kevin \
        --license store/demo_build/ict-vault-demo/license.key \
        --out demo-index.json

Then commit demo-index.json at the repo root (next to index.html).
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))


def export_from_db(db, out_path, require_demo=True):
    """Write the web index JSON from an open sqlite connection."""
    import vault_core as vc

    demo = vc.demo_info(db)
    if require_demo and not demo:
        sys.exit("ERROR: this vault is not a demo build — refusing to export "
                 "paid content to a public JSON file.")

    chunks = []
    for title, video_id, playlist, start_ts, content in db.execute(
            "SELECT title, video_id, playlist, start_ts, content FROM transcripts_fts"):
        chunks.append({
            "t": title,
            "v": video_id or "",
            "p": playlist or "",
            "ts": start_ts or "0:00",
            "x": (content or "").strip(),
        })
    if not chunks:
        sys.exit("ERROR: no chunks found in transcripts_fts — wrong database?")

    videos = sorted({(c["t"], c["v"]) for c in chunks})
    index = {
        "demo": {
            "count": demo["count"] if demo else "?",
            "total": demo["total"] if demo else "576",
        },
        "videos": [{"t": t, "v": v} for t, v in videos],
        "shortforms": vc.ICT_SHORTFORMS,
        "chunks": chunks,
    }

    out_path = Path(out_path)
    out_path.write_text(json.dumps(index, ensure_ascii=False, separators=(",", ":")),
                        encoding="utf-8")
    size_kb = out_path.stat().st_size / 1024
    print(f"OK {out_path}  ({len(chunks)} chunks, {len(videos)} videos, {size_kb:.0f} KB)")
    if size_kb > 900:
        print("WARN: index is getting big for a landing page — consider fewer/"
              "shorter chunks.")
    return out_path


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--vault", help="path to the DEMO ict-vault.kevin")
    ap.add_argument("--license", help="path to the demo's bundled license.key")
    ap.add_argument("--db", help="(testing) path to an already-decrypted master.db")
    ap.add_argument("--out", default="demo-index.json")
    ap.add_argument("--allow-non-demo", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.db:
        db = sqlite3.connect(args.db)
    elif args.vault and args.license:
        import vault_core as vc
        db, _chroma, _who = vc.open_vault(vault_file=Path(args.vault),
                                          license_file=Path(args.license))
    else:
        ap.error("need --vault + --license (or --db for testing)")

    export_from_db(db, args.out, require_demo=not args.allow_non_demo)


if __name__ == "__main__":
    main()
