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


def test_issue_reads_secrets_but_writes_license_to_work_dir(tmp_path, monkeypatch):
    """Render mounts /etc/secrets read-only; issuance must not write there."""
    src = tmp_path / "secrets"
    src.mkdir()
    (src / ".vault_key").write_bytes(b"k" * 32)
    (src / ".vault_sha256").write_text("abc123")
    issued = tmp_path / "issued"
    work = tmp_path / "work"
    monkeypatch.setattr(issue_license, "SOURCE_DIR", src)
    monkeypatch.setattr(issue_license, "LICENSE_WORK_DIR", work)
    monkeypatch.setattr(issue_license, "ISSUED_DIR", issued)
    monkeypatch.setattr(issue_license, "LEDGER", tmp_path / "ledger.csv")

    license_path = issue_license.issue("Buyer@Example.com", "ORDER-43")

    assert license_path.parent == issued
    assert license_path.exists()
    assert not list(src.glob("license_*.key"))
    assert work.exists()


def test_find_issued_is_idempotency_key(tmp_path, monkeypatch):
    """find_issued() must recognise an order_id already in the ledger, so the
    webhook can skip a duplicate delivery instead of re-issuing + re-emailing."""
    src = tmp_path / "seller"
    src.mkdir()
    _seed_source(src)
    monkeypatch.setattr(issue_license, "SOURCE_DIR", src)
    monkeypatch.setattr(issue_license, "ISSUED_DIR", tmp_path / "issued")
    monkeypatch.setattr(issue_license, "LEDGER", tmp_path / "ledger.csv")

    # Nothing issued yet, and an empty/missing ledger is fine.
    assert issue_license.find_issued("cs_test_123") is None

    issue_license.issue("buyer@example.com", "cs_test_123", method="stripe")

    row = issue_license.find_issued("cs_test_123")
    assert row is not None and row["email"] == "buyer@example.com"
    assert row["method"] == "stripe"
    # A different order is still unseen; a falsy order_id never matches.
    assert issue_license.find_issued("cs_other") is None
    assert issue_license.find_issued("") is None
    assert issue_license.find_issued(None) is None


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
