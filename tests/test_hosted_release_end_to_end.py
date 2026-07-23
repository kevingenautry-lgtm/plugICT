"""End-to-end proof of the signed-release-manifest promise.

Hermes point 5: package one hosted ZIP through the *real* deliver flow with a
*real* encrypted vault + signed manifest, extract it exactly like a buyer, then
prove a STALE license (one that pins an old, now-wrong VAULT_HASH) still opens
the packaged vault because open_vault trusts the packaged release.sig.json.
Tampering must still fail closed.

This is the strongest test in the suite: it does not hand-build the manifest
next to a bare fixture — it opens the actual bytes that ship in plugict.zip.
"""
import hashlib
import importlib
import json
import sys
import zipfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import vault_core as vc  # noqa: E402
from test_vault_core import _write_fixture_vault  # noqa: E402


def _file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _signed_manifest(key, key_id, vault_hash, *, tag="v3.6.0"):
    manifest = {
        "product": vc.RELEASE_PRODUCT,
        "tag": tag,
        "vault_sha256": vault_hash,
        "key_id": key_id,
        "algo": "ed25519",
    }
    manifest["sig"] = key.sign(vc.release_manifest_payload(manifest)).hex()
    return manifest


def _stale_license(license_file):
    """Rewrite the license so its VAULT_HASH no longer matches the vault —
    simulates a buyer holding a license issued against a previous release."""
    text = license_file.read_text()
    license_file.write_text("\n".join(
        f"VAULT_HASH={'0' * 64}" if line.startswith("VAULT_HASH=") else line
        for line in text.splitlines()) + "\n")


@pytest.fixture
def packaged_buyer_dir(tmp_path, monkeypatch):
    """Run the real deliver_hosted(), then extract plugict.zip like a buyer.

    Returns (buyer_pkg_dir, seller_key, key_id): the extracted plugict/ folder,
    plus the seller keypair whose public half a buyer would pin.
    """
    source = tmp_path / "source"
    source.mkdir()
    # A real AES-CTR encrypted vault + a real (envelope-encrypted) license.
    vault, license_file = _write_fixture_vault(source, compress=True)

    # Seller signs the CURRENT vault hash and drops release.sig.json beside it.
    key = Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes_raw()
    key_id = vc.release_key_id(pub)
    manifest = _signed_manifest(key, key_id, _file_hash(vault))
    (source / vc.RELEASE_MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    monkeypatch.setenv("ICT_SOURCE_DIR", str(source))
    monkeypatch.delenv("ICT_BUILD_DIR", raising=False)
    monkeypatch.setenv("ICT_DELIVERY_DIR", str(tmp_path / "delivery"))
    import deliver
    importlib.reload(deliver)
    zip_path = deliver.deliver_hosted()

    buyer = tmp_path / "buyer-machine"
    with zipfile.ZipFile(zip_path) as archive:
        archive.extractall(buyer)
    importlib.reload(deliver)  # restore module env for other tests

    pkg = buyer / "plugict"
    # The buyer places their emailed license.key next to the extracted files.
    (pkg / "license.key").write_text(license_file.read_text(), encoding="utf-8")
    return pkg, key, key_id


def test_packaged_zip_carries_vault_and_matching_manifest(packaged_buyer_dir):
    pkg, _key, _key_id = packaged_buyer_dir
    assert (pkg / "ict-vault.kevin").is_file()
    assert (pkg / vc.RELEASE_MANIFEST_NAME).is_file()
    manifest = json.loads((pkg / vc.RELEASE_MANIFEST_NAME).read_text())
    assert manifest["vault_sha256"] == _file_hash(pkg / "ict-vault.kevin")


def test_stale_license_opens_packaged_vault_via_signed_manifest(
        packaged_buyer_dir, monkeypatch):
    pkg, key, key_id = packaged_buyer_dir
    vault = pkg / "ict-vault.kevin"
    license_file = pkg / "license.key"
    _stale_license(license_file)  # license pins a now-wrong hash

    # Buyer client pins the seller's public key -> manifest path is enabled.
    manifest = json.loads((pkg / vc.RELEASE_MANIFEST_NAME).read_text())
    assert manifest["key_id"] == key_id
    monkeypatch.setattr(vc, "RELEASE_TRUSTED_KEYS",
                        {key_id: key.public_key().public_bytes_raw().hex()})

    db, _chroma, who = vc.open_vault(vault_file=vault, license_file=license_file)
    assert who == "tester@example.com"
    db.close()


def test_tampered_packaged_vault_fails_closed(packaged_buyer_dir, monkeypatch):
    pkg, key, key_id = packaged_buyer_dir
    vault = pkg / "ict-vault.kevin"
    license_file = pkg / "license.key"
    monkeypatch.setattr(vc, "RELEASE_TRUSTED_KEYS",
                        {key_id: key.public_key().public_bytes_raw().hex()})
    # Corrupt the packaged vault after signing; stale the license too, so neither
    # the license pin nor the manifest hash can authorize these bytes.
    vault.write_bytes(vault.read_bytes() + b"tamper")
    _stale_license(license_file)
    with pytest.raises(vc.VaultError, match="integrity check"):
        vc.open_vault(vault_file=vault, license_file=license_file)


def test_packaged_manifest_disabled_when_buyer_pins_nothing(
        packaged_buyer_dir, monkeypatch):
    # Default shipping state: empty trust store -> a stale license cannot ride
    # the manifest, even though a valid one is packaged.
    pkg, _key, _key_id = packaged_buyer_dir
    vault = pkg / "ict-vault.kevin"
    license_file = pkg / "license.key"
    monkeypatch.setattr(vc, "RELEASE_TRUSTED_KEYS", {})
    _stale_license(license_file)
    with pytest.raises(vc.VaultError, match="integrity check"):
        vc.open_vault(vault_file=vault, license_file=license_file)
