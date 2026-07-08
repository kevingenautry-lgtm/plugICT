"""
build.py — Build encrypted ICT Knowledge Vault
================================================
Envelope encryption:
  vault_key → encrypts the vault (zstd-compressed, AES-256-CTR)
  .vault_key → saved for generate_key.py to wrap per-buyer

Key stability (so you can ship new videos to existing buyers):
  If .vault_key already exists, it is REUSED, so the rebuilt vault opens with
  every license already issued. Buyers just re-download and keep their key.
  Pass --rotate-key to force a NEW key (security rotation) — this invalidates
  all previously issued licenses, so only do it deliberately.

Output: ict-vault.kevin + .vault_key (keep both secret)
"""

import os, sys, sqlite3, shutil, io, tarfile, struct
from pathlib import Path
from datetime import datetime

import vault_core as vc

ROTATE_KEY = "--rotate-key" in sys.argv

# Source content lives on the seller machine; override with ICT_SOURCE_DIR.
VAULT_DIR = Path(os.environ.get("ICT_SOURCE_DIR", r"C:\Users\kevin\Hermes ICT Selling Idea"))
OUTPUT_FILE = VAULT_DIR / "ict-vault.kevin"
VAULT_KEY_FILE = VAULT_DIR / ".vault_key"  # Keep secret! Used by generate_key.py

print("=" * 60)
print("ICT Knowledge Vault — Encrypted Build")
print("=" * 60)

# ── Step 1: Verify ──
print("\n[1/5] Verifying source files...")
vectors_dir = VAULT_DIR / "_vectors"
kg_db_path = VAULT_DIR / "kg.db"

for p in [vectors_dir, kg_db_path]:
    if not p.exists():
        print(f"  ERROR: {p.name} missing. Run ict_ingest.py first.")
        sys.exit(1)
    size = sum(f.stat().st_size for f in p.rglob('*') if f.is_file()) if p.is_dir() else p.stat().st_size
    print(f"  OK {p.name} ({size/1024/1024:.0f} MB)")

md_count = len([f for f in VAULT_DIR.glob("*.md") if f.name not in ('index.md','README.md','CATALOG.md')])
print(f"  OK {md_count} transcripts")

# ── Step 2: Build master SQLite ──
print("\n[2/5] Building master database...")

master_db = VAULT_DIR / "_build_master.db"
if master_db.exists():
    master_db.unlink()

src = sqlite3.connect(str(kg_db_path))
dst = sqlite3.connect(str(master_db))
src.backup(dst)

dst.execute("""
    CREATE TABLE IF NOT EXISTS transcript_files (
        id INTEGER PRIMARY KEY, filename TEXT, title TEXT,
        video_id TEXT, duration TEXT, playlist TEXT, content TEXT, created TEXT
    )
""")

transcripts = [f for f in sorted(VAULT_DIR.glob("*.md")) 
               if f.name not in ('index.md','README.md','CATALOG.md')]

for fp in transcripts:
    with open(fp, encoding='utf-8', errors='replace') as f:
        content = f.read()
    
    title = fp.stem; video_id = ''; duration = ''
    if content.startswith('---'):
        parts = content.split('---', 2)
        if len(parts) >= 3:
            for line in parts[1].strip().split('\n'):
                if ':' in line:
                    k, v = line.split(':', 1)
                    k, v = k.strip(), v.strip().strip('"')
                    if k == 'title': title = v
                    elif k == 'video_id': video_id = v
                    elif k == 'duration': duration = v
    
    playlist = vc.classify_playlist(fp.name)

    dst.execute(
        "INSERT INTO transcript_files VALUES (NULL, ?, ?, ?, ?, ?, ?, ?)",
        (fp.name, title, video_id, duration, playlist, content, datetime.now().isoformat())
    )

dst.execute("CREATE TABLE IF NOT EXISTS vault_metadata (key TEXT PRIMARY KEY, value TEXT)")
dst.execute("INSERT OR REPLACE INTO vault_metadata VALUES ('version', '1.0.0')")
dst.execute("INSERT OR REPLACE INTO vault_metadata VALUES ('build_date', ?)", (datetime.now().isoformat(),))
dst.execute("INSERT OR REPLACE INTO vault_metadata VALUES ('total_transcripts', ?)", (str(len(transcripts)),))

# Demo builds (store/build_demo.py) stamp a watermark into the vault itself.
if os.environ.get("ICT_DEMO") == "1":
    dst.execute("INSERT OR REPLACE INTO vault_metadata VALUES ('demo', '1')")
    dst.execute("INSERT OR REPLACE INTO vault_metadata VALUES ('demo_count', ?)", (str(len(transcripts)),))
    dst.execute("INSERT OR REPLACE INTO vault_metadata VALUES ('demo_total', ?)",
                (os.environ.get("ICT_DEMO_TOTAL", "576"),))
    dst.execute("INSERT OR REPLACE INTO vault_metadata VALUES ('demo_cta', ?)",
                (os.environ.get("ICT_DEMO_CTA", "https://YOUR-SITE/#pricing"),))
    print(f"  DEMO BUILD — watermarked {len(transcripts)} videos")

dst.commit()
src.close()
dst.close()

db_size = master_db.stat().st_size / 1024 / 1024
print(f"  OK Master DB: {db_size:.0f} MB")

with open(master_db, 'rb') as f:
    db_bytes = f.read()

# ── Step 3: Package ChromaDB ──
print("\n[3/5] Packaging ChromaDB vectors...")

chroma_tar_io = io.BytesIO()
with tarfile.open(fileobj=chroma_tar_io, mode='w') as tar:
    for root, dirs, files in os.walk(vectors_dir):
        for file in files:
            full_path = os.path.join(root, file)
            arcname = os.path.relpath(full_path, vectors_dir)
            tar.add(full_path, arcname=arcname)

chroma_bytes = chroma_tar_io.getvalue()
chroma_size = len(chroma_bytes) / 1024 / 1024
print(f"  OK ChromaDB tar: {chroma_size:.0f} MB")

# ── Step 4: Compress (zstd) + encrypt with vault key ──
print("\n[4/5] Compressing + encrypting vault...")

raw_size = (len(db_bytes) + len(chroma_bytes)) / 1024 / 1024

# Reuse the existing key (default) so already-issued licenses keep working with
# the rebuilt vault. --rotate-key forces a fresh key (invalidates old licenses).
existing_key = None
if VAULT_KEY_FILE.exists() and not ROTATE_KEY:
    existing_key = VAULT_KEY_FILE.read_bytes()
    if len(existing_key) != 32:
        sys.exit(f"ERROR: {VAULT_KEY_FILE} is {len(existing_key)} bytes, expected 32. "
                 "Refusing to build with a malformed key. Restore the real .vault_key "
                 "or run with --rotate-key to mint a new one (invalidates old licenses).")
    print(f"  Reusing existing .vault_key — existing licenses stay valid.")
elif ROTATE_KEY and VAULT_KEY_FILE.exists():
    print("  --rotate-key: minting a NEW key. WARNING: all previously issued "
          "licenses will STOP working with this vault.")

final_encrypted, vault_key, vault_hash = vc.pack_and_encrypt(
    db_bytes, chroma_bytes, compress=True, level=19, vault_key=existing_key)

with open(OUTPUT_FILE, 'wb') as f:
    f.write(final_encrypted)

# SHA-256 for integrity verification (matches what the license embeds).
with open(VAULT_DIR / ".vault_sha256", 'w') as f:
    f.write(vault_hash)

# Save vault key (raw 32 bytes, for generate_key.py to wrap per-buyer).
with open(VAULT_KEY_FILE, 'wb') as f:
    f.write(vault_key)
os.chmod(VAULT_KEY_FILE, 0o600)

# Cleanup
master_db.unlink()

vault_size = OUTPUT_FILE.stat().st_size / 1024 / 1024
ratio = (1 - vault_size / raw_size) * 100 if raw_size else 0
print(f"  OK Vault: {vault_size:.0f} MB (was {raw_size:.0f} MB raw — {ratio:.0f}% smaller, zstd v2)")
print()
print("=" * 60)
print("BUILD COMPLETE")
print(f"   {OUTPUT_FILE.name}: {vault_size:.0f} MB")
print(f"   .vault_key: KEEP SECRET (used by generate_key.py)")
print("=" * 60)
