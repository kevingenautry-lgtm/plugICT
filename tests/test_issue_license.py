"""
Delivery-flow test: a license issued from an 'order' must actually unlock the
vault. This guards the post-purchase pipeline (issue_license -> open_vault).
"""

import io
import sys
import sqlite3
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "store"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import vault_core as vc  # noqa: E402
import issue_license  # noqa: E402
from test_vault_core import _make_db_bytes, _make_chroma_bytes  # noqa: E402


def _seed_source(src):
    """Create .vault_key / .vault_sha256 / ict-vault.kevin like build.py would."""
    db_bytes = _make_db_bytes(str(src))
    chroma_bytes = _make_chroma_bytes()
    blob, vault_key, vault_hash = vc.pack_and_encrypt(db_bytes, chroma_bytes, compress=True)
    (src / "ict-vault.kevin").write_bytes(blob)
    (src / ".vault_key").write_bytes(vault_key)
    (src / ".vault_sha256").write_text(vault_hash)
    return src / "ict-vault.kevin"


def test_issued_license_unlocks_vault(tmp_path, monkeypatch):
    src = tmp_path / "seller"
    src.mkdir()
    vault_file = _seed_source(src)

    issued = tmp_path / "issued"
    monkeypatch.setattr(issue_license, "SOURCE_DIR", src)
    monkeypatch.setattr(issue_license, "ISSUED_DIR", issued)
    monkeypatch.setattr(issue_license, "LEDGER", tmp_path / "ledger.csv")

    license_path = issue_license.issue("Buyer@Example.com", "ORDER-42")
    assert license_path.exists()

    # The issued license must open the very vault it was minted for.
    db, chroma_dir, who = vc.open_vault(vault_file=vault_file, license_file=license_path)
    try:
        assert who == "buyer@example.com"  # normalised to lowercase
        assert db.execute("SELECT COUNT(*) FROM transcript_files").fetchone()[0] == 1
    finally:
        db.close()

    # Ledger recorded the sale.
    ledger = (tmp_path / "ledger.csv").read_text()
    assert "buyer@example.com" in ledger and "ORDER-42" in ledger


def test_two_buyers_get_distinct_licenses(tmp_path, monkeypatch):
    src = tmp_path / "seller"
    src.mkdir()
    _seed_source(src)
    monkeypatch.setattr(issue_license, "SOURCE_DIR", src)
    monkeypatch.setattr(issue_license, "ISSUED_DIR", tmp_path / "issued")
    monkeypatch.setattr(issue_license, "LEDGER", tmp_path / "ledger.csv")

    a = issue_license.issue("a@x.com", "O1").read_text()
    b = issue_license.issue("b@x.com", "O2").read_text()
    # Different buyer keys per license (traceability).
    key_a = [l for l in a.splitlines() if l.startswith("BUYER_KEY=")][0]
    key_b = [l for l in b.splitlines() if l.startswith("BUYER_KEY=")][0]
    assert key_a != key_b
