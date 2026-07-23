"""Signed release manifest — trust-model tests.

The rule under test: open_vault accepts a vault whose hash EITHER matches the
license's pinned VAULT_HASH (legacy) OR is authorized by a valid seller-signed
release manifest. Every other state fails closed: tampered vault, tampered
manifest, wrong signer, unknown key, wrong product, unsigned/malformed
manifest, missing manifest.
"""
import hashlib
import json
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import vault_core as vc  # noqa: E402
from test_vault_core import _write_fixture_vault  # noqa: E402


# ── helpers ──────────────────────────────────────────────────────────────────

def _keypair():
    key = Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes_raw()
    return key, pub, vc.release_key_id(pub)


def _signed_manifest(key, key_id, vault_hash, *, tag="v9.9.9",
                     product=vc.RELEASE_PRODUCT, algo="ed25519"):
    manifest = {
        "product": product,
        "tag": tag,
        "vault_sha256": vault_hash,
        "key_id": key_id,
        "algo": algo,
    }
    manifest["sig"] = key.sign(vc.release_manifest_payload(manifest)).hex()
    return manifest


def _write_manifest(dirpath, manifest):
    path = Path(dirpath) / vc.RELEASE_MANIFEST_NAME
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def _file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ── Production trust-store integrity ──────────────────────────────────────────

def test_production_trusted_key_ids_match_public_keys():
    """A copy-paste mistake in the committed public pin must fail CI, not buyers."""
    assert vc.RELEASE_TRUSTED_KEYS, "production release trust store must be pinned"
    for key_id, public_hex in vc.RELEASE_TRUSTED_KEYS.items():
        public_bytes = bytes.fromhex(public_hex)
        assert len(public_bytes) == 32
        assert vc.release_key_id(public_bytes) == key_id


# ── verify_release_manifest: the fail-closed matrix ──────────────────────────

def test_valid_signature_passes(tmp_path):
    key, pub, key_id = _keypair()
    vault_hash = "ab" * 32
    path = _write_manifest(tmp_path, _signed_manifest(key, key_id, vault_hash))
    ok, reason = vc.verify_release_manifest(path, vault_hash,
                                            trusted_keys={key_id: pub.hex()})
    assert ok, reason
    assert "v9.9.9" in reason


def test_vault_hash_mismatch_fails(tmp_path):
    key, pub, key_id = _keypair()
    path = _write_manifest(tmp_path, _signed_manifest(key, key_id, "ab" * 32))
    ok, reason = vc.verify_release_manifest(path, "cd" * 32,
                                            trusted_keys={key_id: pub.hex()})
    assert not ok
    assert "does not match" in reason


def test_tampered_manifest_field_fails(tmp_path):
    key, pub, key_id = _keypair()
    manifest = _signed_manifest(key, key_id, "ab" * 32, tag="v1.0.0")
    manifest["tag"] = "v2.0.0"  # altered after signing
    path = _write_manifest(tmp_path, manifest)
    ok, reason = vc.verify_release_manifest(path, "ab" * 32,
                                            trusted_keys={key_id: pub.hex()})
    assert not ok
    assert "signature verification failed" in reason


def test_wrong_signer_fails(tmp_path):
    # Signed by attacker's key but claiming the trusted key's key_id.
    _trusted_key, trusted_pub, trusted_id = _keypair()
    attacker_key, _apub, _aid = _keypair()
    manifest = {
        "product": vc.RELEASE_PRODUCT, "tag": "v1.0.0",
        "vault_sha256": "ab" * 32, "key_id": trusted_id, "algo": "ed25519",
    }
    manifest["sig"] = attacker_key.sign(vc.release_manifest_payload(manifest)).hex()
    path = _write_manifest(tmp_path, manifest)
    ok, reason = vc.verify_release_manifest(path, "ab" * 32,
                                            trusted_keys={trusted_id: trusted_pub.hex()})
    assert not ok
    assert "signature verification failed" in reason


def test_unknown_key_id_fails(tmp_path):
    key, _pub, key_id = _keypair()
    path = _write_manifest(tmp_path, _signed_manifest(key, key_id, "ab" * 32))
    ok, reason = vc.verify_release_manifest(path, "ab" * 32, trusted_keys={})
    assert not ok
    assert "unknown key" in reason


def test_wrong_product_fails(tmp_path):
    key, pub, key_id = _keypair()
    path = _write_manifest(
        tmp_path, _signed_manifest(key, key_id, "ab" * 32, product="other-product"))
    ok, reason = vc.verify_release_manifest(path, "ab" * 32,
                                            trusted_keys={key_id: pub.hex()})
    assert not ok
    assert "different product" in reason


def test_unsigned_manifest_fails(tmp_path):
    key, pub, key_id = _keypair()
    manifest = _signed_manifest(key, key_id, "ab" * 32)
    del manifest["sig"]
    path = _write_manifest(tmp_path, manifest)
    ok, reason = vc.verify_release_manifest(path, "ab" * 32,
                                            trusted_keys={key_id: pub.hex()})
    assert not ok
    assert "missing field: sig" in reason


def test_unsupported_algo_fails(tmp_path):
    key, pub, key_id = _keypair()
    path = _write_manifest(
        tmp_path, _signed_manifest(key, key_id, "ab" * 32, algo="rsa"))
    ok, reason = vc.verify_release_manifest(path, "ab" * 32,
                                            trusted_keys={key_id: pub.hex()})
    assert not ok
    assert "unsupported" in reason


def test_missing_manifest_fails(tmp_path):
    ok, reason = vc.verify_release_manifest(
        tmp_path / vc.RELEASE_MANIFEST_NAME, "ab" * 32, trusted_keys={})
    assert not ok
    assert "no release manifest" in reason


def test_malformed_json_fails_closed(tmp_path):
    path = Path(tmp_path) / vc.RELEASE_MANIFEST_NAME
    path.write_text("{not json", encoding="utf-8")
    ok, reason = vc.verify_release_manifest(path, "ab" * 32, trusted_keys={"x": "y"})
    assert not ok
    assert "unreadable" in reason


def test_non_hex_signature_fails(tmp_path):
    key, pub, key_id = _keypair()
    manifest = _signed_manifest(key, key_id, "ab" * 32)
    manifest["sig"] = "zz-not-hex"
    path = _write_manifest(tmp_path, manifest)
    ok, reason = vc.verify_release_manifest(path, "ab" * 32,
                                            trusted_keys={key_id: pub.hex()})
    assert not ok
    assert "not valid hex" in reason


def test_trusted_key_entry_must_match_its_key_id(tmp_path):
    # A trust-store entry whose pubkey doesn't hash to its key_id is rejected
    # (defends against a corrupted/typo'd pin).
    key, _pub, key_id = _keypair()
    _other, other_pub, _other_id = _keypair()
    path = _write_manifest(tmp_path, _signed_manifest(key, key_id, "ab" * 32))
    ok, reason = vc.verify_release_manifest(path, "ab" * 32,
                                            trusted_keys={key_id: other_pub.hex()})
    assert not ok
    assert "does not match its key_id" in reason


# ── open_vault integration: the real acceptance behaviour ────────────────────

def test_legacy_exact_hash_license_still_passes(tmp_path):
    vault, lic = _write_fixture_vault(tmp_path, compress=True)
    db, _chroma, who = vc.open_vault(vault_file=vault, license_file=lic)
    assert who == "tester@example.com"
    db.close()


def test_stale_license_hash_fails_without_manifest(tmp_path):
    vault, lic = _write_fixture_vault(tmp_path, compress=True)
    text = lic.read_text()
    stale = "0" * 64
    lic.write_text("\n".join(
        f"VAULT_HASH={stale}" if line.startswith("VAULT_HASH=") else line
        for line in text.splitlines()) + "\n")
    with pytest.raises(vc.VaultError, match="integrity check"):
        vc.open_vault(vault_file=vault, license_file=lic)


def test_stale_license_plus_signed_manifest_opens(tmp_path, monkeypatch):
    vault, lic = _write_fixture_vault(tmp_path, compress=True)
    # Simulate a vault update: the license still pins an old (now wrong) hash.
    text = lic.read_text()
    lic.write_text("\n".join(
        f"VAULT_HASH={'0' * 64}" if line.startswith("VAULT_HASH=") else line
        for line in text.splitlines()) + "\n")
    # Seller signs the CURRENT vault hash; buyer client pins the seller pubkey.
    key, pub, key_id = _keypair()
    _write_manifest(vault.parent, _signed_manifest(key, key_id, _file_hash(vault)))
    monkeypatch.setattr(vc, "RELEASE_TRUSTED_KEYS", {key_id: pub.hex()})
    db, _chroma, who = vc.open_vault(vault_file=vault, license_file=lic)
    assert who == "tester@example.com"
    db.close()


def test_tampered_vault_fails_even_with_manifest(tmp_path, monkeypatch):
    vault, lic = _write_fixture_vault(tmp_path, compress=True)
    key, pub, key_id = _keypair()
    _write_manifest(vault.parent, _signed_manifest(key, key_id, _file_hash(vault)))
    monkeypatch.setattr(vc, "RELEASE_TRUSTED_KEYS", {key_id: pub.hex()})
    # Corrupt the vault AFTER signing; also stale the license pin so neither
    # acceptance path can match.
    vault.write_bytes(vault.read_bytes() + b"tamper")
    text = lic.read_text()
    lic.write_text("\n".join(
        f"VAULT_HASH={'0' * 64}" if line.startswith("VAULT_HASH=") else line
        for line in text.splitlines()) + "\n")
    with pytest.raises(vc.VaultError, match="integrity check"):
        vc.open_vault(vault_file=vault, license_file=lic)


def test_manifest_ignored_when_trust_store_empty(tmp_path, monkeypatch):
    # Default shipping state: no pinned keys -> manifest path disabled entirely.
    vault, lic = _write_fixture_vault(tmp_path, compress=True)
    key, _pub, key_id = _keypair()
    _write_manifest(vault.parent, _signed_manifest(key, key_id, _file_hash(vault)))
    monkeypatch.setattr(vc, "RELEASE_TRUSTED_KEYS", {})
    text = lic.read_text()
    lic.write_text("\n".join(
        f"VAULT_HASH={'0' * 64}" if line.startswith("VAULT_HASH=") else line
        for line in text.splitlines()) + "\n")
    with pytest.raises(vc.VaultError, match="integrity check"):
        vc.open_vault(vault_file=vault, license_file=lic)


# ── sign_release.py: seller tool round-trip ──────────────────────────────────

def test_sign_release_roundtrip(tmp_path, monkeypatch, capsys):
    import sign_release

    build = tmp_path / "build"
    build.mkdir()
    vault = build / "ict-vault.kevin"
    vault.write_bytes(b"encrypted vault artifact bytes")

    monkeypatch.setattr(sys, "argv", ["sign_release.py", "--init",
                                      "--build-dir", str(build)])
    sign_release.main()
    out = capsys.readouterr().out
    assert "NEVER commit" in out
    key_file = build / sign_release.KEY_FILE_NAME
    assert key_file.exists()

    monkeypatch.setattr(sys, "argv", ["sign_release.py", "--tag", "v9.0.0",
                                      "--build-dir", str(build)])
    sign_release.main()
    out = capsys.readouterr().out
    assert "self-verified" in out

    manifest_path = build / vc.RELEASE_MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text())
    assert manifest["product"] == vc.RELEASE_PRODUCT
    assert manifest["tag"] == "v9.0.0"
    assert manifest["vault_sha256"] == _file_hash(vault)

    # And the produced manifest verifies against the derived public key.
    raw = bytes.fromhex(key_file.read_text().strip())
    pub = Ed25519PrivateKey.from_private_bytes(raw).public_key().public_bytes_raw()
    ok, reason = vc.verify_release_manifest(
        manifest_path, _file_hash(vault),
        trusted_keys={vc.release_key_id(pub): pub.hex()})
    assert ok, reason


def test_sign_release_init_refuses_overwrite(tmp_path, monkeypatch):
    import sign_release

    build = tmp_path / "build"
    build.mkdir()
    (build / sign_release.KEY_FILE_NAME).write_text("aa" * 32)
    monkeypatch.setattr(sys, "argv", ["sign_release.py", "--init",
                                      "--build-dir", str(build)])
    with pytest.raises(SystemExit, match="Refusing to overwrite"):
        sign_release.main()


def test_windows_private_key_acl_is_restrictive(tmp_path):
    """Windows must not inherit broad ACLs when it creates a signing key."""
    import sign_release

    calls = []

    class Result:
        returncode = 0
        stderr = ""

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return Result()

    key_path = tmp_path / sign_release.KEY_FILE_NAME
    sign_release._secure_windows_private_key_acl(
        key_path, runner=fake_run, username="kevin")

    assert calls == [(
        ["icacls", str(key_path), "/inheritance:r", "/grant:r",
         "kevin:(F)", "BUILTIN\\Administrators:(F)", "NT AUTHORITY\\SYSTEM:(F)"],
        {"capture_output": True, "text": True},
    )]
