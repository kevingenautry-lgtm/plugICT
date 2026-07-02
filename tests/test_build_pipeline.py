"""
Seller-pipeline integration test: build.py -> generate_key.py -> open_vault.

Runs the real scripts as subprocesses against a tiny fixture source tree, so a
wiring mistake in the refactor (imports, classify_playlist, pack_and_encrypt)
fails here instead of on the seller's machine at 2am.
"""

import os
import sys
import sqlite3
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
import vault_core as vc  # noqa: E402


def _make_source_tree(src):
    src = Path(src)
    # kg.db with the tables build.py expects to copy forward.
    kg = sqlite3.connect(src / "kg.db")
    kg.execute("CREATE VIRTUAL TABLE transcripts_fts USING fts5("
               "title, video_id, playlist, start_ts, source_file, content, "
               "tokenize='porter unicode61')")
    kg.execute("INSERT INTO transcripts_fts VALUES (?,?,?,?,?,?)",
               ("Order Blocks 101", "vid1", "2022 ICT Mentorship", "0:00", "a.md",
                "An order block is where institutional orders rest."))
    kg.execute("CREATE TABLE entities (id INTEGER PRIMARY KEY, name TEXT UNIQUE, type TEXT,"
               " description TEXT, source_file TEXT, source_count INTEGER)")
    kg.execute("INSERT INTO entities (name,type,description,source_count) VALUES "
               "('OB','ICT Concept','Order Block',9)")
    kg.execute("CREATE TABLE relations (id INTEGER PRIMARY KEY, from_entity TEXT, to_entity TEXT,"
               " relation_type TEXT, evidence TEXT, source_file TEXT)")
    kg.commit()
    kg.close()

    (src / "_vectors").mkdir()
    (src / "_vectors" / "chroma.sqlite3").write_bytes(b"dummy vectors")

    (src / "2022 ICT Mentorship - Order Blocks.md").write_text(
        '---\ntitle: "Order Blocks 101"\nvideo_id: "vid1"\n---\n0:00 An order block is where orders rest.\n')


def test_build_generate_open(tmp_path):
    src = tmp_path / "source"
    src.mkdir()
    _make_source_tree(src)

    env = dict(os.environ, ICT_SOURCE_DIR=str(src))

    r1 = subprocess.run([sys.executable, str(SCRIPTS / "build.py")],
                        env=env, capture_output=True, text=True)
    assert r1.returncode == 0, r1.stdout + r1.stderr
    assert (src / "ict-vault.kevin").exists()
    assert (src / ".vault_key").exists()

    r2 = subprocess.run([sys.executable, str(SCRIPTS / "generate_key.py"), "t@e.com", "ID1"],
                        env=env, capture_output=True, text=True)
    assert r2.returncode == 0, r2.stdout + r2.stderr
    lic = src / "license_t_at_e_com.key"
    assert lic.exists()

    db, chroma_dir, who = vc.open_vault(vault_file=src / "ict-vault.kevin", license_file=lic)
    try:
        assert who == "t@e.com"
        row = db.execute("SELECT title FROM transcripts_fts WHERE content MATCH ?",
                        (vc.sanitize_fts("order block"),)).fetchone()
        assert row and row[0] == "Order Blocks 101"
        assert (Path(chroma_dir) / "chroma.sqlite3").exists()
        n = db.execute("SELECT COUNT(*) FROM transcript_files").fetchone()[0]
        assert n == 1
    finally:
        db.close()
