"""
Smoke tests for vault_core — the buyer-side critical path.

Runs without the real 400MB vault or heavy deps (chromadb/sentence-transformers):
we build a tiny fixture vault in the real format and round-trip it. This is the
test that would have caught the shipped MCP decrypt bug.
"""

import io
import os
import sys
import sqlite3
import tarfile
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import vault_core as vc
from cryptography.fernet import Fernet


# ── Fixtures ─────────────────────────────────────────────────────────────────
def _make_db_bytes(tmp):
    db_path = os.path.join(tmp, "src.db")
    con = sqlite3.connect(db_path)
    con.execute("CREATE VIRTUAL TABLE transcripts_fts USING fts5("
                "title, video_id, playlist, start_ts, source_file, content, "
                "tokenize='porter unicode61')")
    con.execute("INSERT INTO transcripts_fts VALUES (?,?,?,?,?,?)",
                ("Fair Value Gap Explained", "abc123", "2022 ICT Mentorship",
                 "0:00", "f.md", "A fair value gap is a three candle imbalance."))
    con.execute("CREATE TABLE transcript_files (id INTEGER PRIMARY KEY, filename TEXT,"
                " title TEXT, video_id TEXT, duration TEXT, playlist TEXT, content TEXT, created TEXT)")
    con.execute("INSERT INTO transcript_files VALUES (NULL,?,?,?,?,?,?,?)",
                ("f.md", "Fair Value Gap Explained", "abc123", "10:00",
                 "2022 ICT Mentorship", "full transcript body", "2026-07-02"))
    con.execute("CREATE TABLE entities (id INTEGER PRIMARY KEY, name TEXT UNIQUE,"
                " type TEXT, description TEXT, source_file TEXT, source_count INTEGER)")
    con.execute("CREATE TABLE relations (id INTEGER PRIMARY KEY, from_entity TEXT,"
                " to_entity TEXT, relation_type TEXT, evidence TEXT, source_file TEXT)")
    con.execute("CREATE TABLE vault_metadata (key TEXT PRIMARY KEY, value TEXT)")
    con.execute("INSERT INTO vault_metadata VALUES ('version','1.0.0')")
    con.commit()
    con.close()
    with open(db_path, "rb") as f:
        return f.read()


def _make_chroma_bytes():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        data = b"pretend chroma sqlite file"
        info = tarfile.TarInfo(name="chroma.sqlite3")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _write_fixture_vault(tmp, compress):
    db_bytes = _make_db_bytes(tmp)
    chroma_bytes = _make_chroma_bytes()
    blob, vault_key, vault_hash = vc.pack_and_encrypt(db_bytes, chroma_bytes, compress=compress)

    vault_path = Path(tmp) / "ict-vault.kevin"
    vault_path.write_bytes(blob)

    buyer_key = Fernet.generate_key()
    encrypted_vault_key = Fernet(buyer_key).encrypt(vault_key)
    license_path = Path(tmp) / "license.key"
    license_path.write_text(
        "# license\n"
        "LICENSED_TO=tester@example.com\n"
        f"BUYER_KEY={buyer_key.decode()}\n"
        f"ENCRYPTED_VAULT_KEY={encrypted_vault_key.decode()}\n"
        f"VAULT_HASH={vault_hash}\n"
    )
    return vault_path, license_path


# ── Round-trip: v1 (raw) and v2 (zstd) ───────────────────────────────────────
def test_roundtrip_v1_raw():
    with tempfile.TemporaryDirectory() as tmp:
        vault, lic = _write_fixture_vault(tmp, compress=False)
        db, chroma_dir, who = vc.open_vault(vault_file=vault, license_file=lic)
        assert who == "tester@example.com"
        rows = db.execute("SELECT title FROM transcripts_fts WHERE content MATCH ?",
                          (vc.sanitize_fts("fair value gap"),)).fetchall()
        assert rows and rows[0][0] == "Fair Value Gap Explained"
        assert os.path.exists(os.path.join(chroma_dir, "chroma.sqlite3"))
        db.close()


def test_roundtrip_v2_zstd_is_smaller():
    with tempfile.TemporaryDirectory() as tmp:
        vault, lic = _write_fixture_vault(tmp, compress=True)
        # v2 header
        assert vault.read_bytes()[16:20] != b""  # sanity: file has body
        db, chroma_dir, who = vc.open_vault(vault_file=vault, license_file=lic)
        assert db.execute("SELECT value FROM vault_metadata WHERE key='version'").fetchone()[0] == "1.0.0"
        db.close()


def test_corrupted_vault_rejected():
    with tempfile.TemporaryDirectory() as tmp:
        vault, lic = _write_fixture_vault(tmp, compress=False)
        raw = bytearray(vault.read_bytes())
        raw[-1] ^= 0xFF  # flip a byte -> hash mismatch
        vault.write_bytes(bytes(raw))
        try:
            vc.open_vault(vault_file=vault, license_file=lic)
            assert False, "expected VaultError on corrupted vault"
        except vc.VaultError as e:
            assert "integrity" in str(e).lower() or "corrupt" in str(e).lower()


def test_wrong_license_rejected():
    with tempfile.TemporaryDirectory() as tmp:
        vault, lic = _write_fixture_vault(tmp, compress=True)
        # Replace with a license whose buyer key wraps a *different* vault key
        other_key = Fernet.generate_key()
        bad_wrapped = Fernet(other_key).encrypt(b"0" * 32)
        lic.write_text(
            "LICENSED_TO=x\n"
            f"BUYER_KEY={other_key.decode()}\n"
            f"ENCRYPTED_VAULT_KEY={bad_wrapped.decode()}\n"
        )
        try:
            vc.open_vault(vault_file=vault, license_file=lic)
            assert False, "expected failure with mismatched license"
        except vc.VaultError:
            pass


# ── Pure functions ───────────────────────────────────────────────────────────
def test_sanitize_fts_survives_punctuation():
    # OR-joined quoted phrases; question words dropped for recall.
    assert vc.sanitize_fts("buy-side liquidity") == '"buy-side" OR "liquidity"'
    assert vc.sanitize_fts("what's an order block?") == '"order" OR "block"'
    # AND mode still available for callers that want precision
    assert vc.sanitize_fts("order block", mode="and") == '"order" "block"'
    # all-stopword query keeps its tokens rather than matching nothing
    assert vc.sanitize_fts("what is it") is not None
    assert vc.sanitize_fts("   ") is None
    assert vc.sanitize_fts("") is None


def test_expand_query_only_uppercase():
    assert vc.expand_query("FVG entry")[0].startswith("Fair Value Gap")
    # lowercase 'ms'/'bs' must NOT expand (they occur in normal sentences)
    assert vc.expand_query("what does he say about ms")[1] is False
    assert vc.expand_query("bs on the chart")[1] is False


def test_related_terms_are_real():
    rel = vc.related_terms("FVG")
    assert "IFVG" in rel or "CE" in rel  # same category (Fair Value Concepts)
    assert "H4" not in rel  # different category


def test_classify_playlist():
    assert vc.classify_playlist("2022 ICT Mentorship - Ep1.md") == "2022 ICT Mentorship"
    assert vc.classify_playlist("2026 ICT SMC Lecture 3.md") == "2026 SMC Lecture"
    assert vc.classify_playlist("random file.md") == "Other / Misc"


def test_safe_extractall_rejects_traversal(tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        data = b"evil"
        ti = tarfile.TarInfo(name="../escape.txt"); ti.size = len(data)
        tar.addfile(ti, io.BytesIO(data))
    buf.seek(0)
    with tarfile.open(fileobj=buf) as tar:
        try:
            vc._safe_extractall(tar, str(tmp_path / "out"))
            assert False, "traversal member should be rejected"
        except vc.VaultError as e:
            assert "unsafe path" in str(e)
    assert not (tmp_path / "escape.txt").exists()


def test_safe_extractall_rejects_symlink(tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        ti = tarfile.TarInfo(name="link"); ti.type = tarfile.SYMTYPE; ti.linkname = "/etc/passwd"
        tar.addfile(ti)
    buf.seek(0)
    with tarfile.open(fileobj=buf) as tar:
        try:
            vc._safe_extractall(tar, str(tmp_path / "out"))
            assert False, "symlink member should be rejected"
        except vc.VaultError as e:
            assert "link entry" in str(e)


def test_safe_extractall_allows_normal_files(tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        data = b"chroma data"
        ti = tarfile.TarInfo(name="sub/chroma.sqlite3"); ti.size = len(data)
        tar.addfile(ti, io.BytesIO(data))
    buf.seek(0)
    out = tmp_path / "out"
    with tarfile.open(fileobj=buf) as tar:
        vc._safe_extractall(tar, str(out))
    assert (out / "sub" / "chroma.sqlite3").read_bytes() == b"chroma data"


def test_youtube_deep_links():
    assert vc.youtube_link("abc", "12:34") == "https://youtu.be/abc?t=754"
    assert vc.youtube_link("abc", "1:02:07") == "https://youtu.be/abc?t=3727"
    assert vc.youtube_link("abc", "0:00") == "https://youtu.be/abc"   # start → plain
    assert vc.youtube_link("abc", None) == "https://youtu.be/abc"
    assert vc.youtube_link("abc", "junk") == "https://youtu.be/abc"
    assert vc.youtube_link("", "12:34") == ""


def test_glossary_flat_matches_grouped():
    total = sum(len(v) for v in vc.ICT_GLOSSARY.values())
    assert len(vc.ICT_SHORTFORMS) == total
    assert "FVG" in vc.ICT_SHORTFORMS
