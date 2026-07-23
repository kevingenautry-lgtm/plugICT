"""The --hosted package must never contain a license key, and its zip must
carry everything a buyer needs — including the signed release manifest that
lets issued licenses survive future vault updates. Guards the public-Release
packaging flow."""
import hashlib
import importlib
import json
import os
import sys
import zipfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import vault_core as vc  # noqa: E402


def _write_release_manifest(dirpath, vault_bytes, *, tag="v1.0.0",
                            product=None, vault_sha256=None):
    """Drop a release.sig.json beside a vault. The trust store is empty in these
    tests, so deliver.py only checks product + vault_sha256; the signature bytes
    need not verify (a dummy key_id/sig is fine for the packaging-path tests)."""
    manifest = {
        "product": vc.RELEASE_PRODUCT if product is None else product,
        "tag": tag,
        "vault_sha256": (hashlib.sha256(vault_bytes).hexdigest()
                         if vault_sha256 is None else vault_sha256),
        "key_id": "0" * 16,
        "algo": "ed25519",
        "sig": "00" * 64,
    }
    (Path(dirpath) / vc.RELEASE_MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


@pytest.fixture
def hosted_zip(tmp_path, monkeypatch):
    # Fake seller content dir: dummy vault + its signed manifest + a doc + a stray
    # license that must NOT ship.
    vault_bytes = b"dummy-encrypted-vault"
    (tmp_path / "ict-vault.kevin").write_bytes(vault_bytes)
    _write_release_manifest(tmp_path, vault_bytes)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "AI-AGENT-GUIDE.md").write_text("# guide")
    (tmp_path / "license_someone_at_x_com.key").write_text("LICENSE_ID=SHOULD-NOT-SHIP")

    monkeypatch.setenv("ICT_SOURCE_DIR", str(tmp_path))
    monkeypatch.delenv("ICT_BUILD_DIR", raising=False)
    monkeypatch.setenv("ICT_DELIVERY_DIR", str(tmp_path / "delivery"))
    # Dummy packaging fixtures are deliberately unsigned; production signature
    # enforcement is covered separately with a pinned throwaway key below.
    monkeypatch.setattr(vc, "RELEASE_TRUSTED_KEYS", {})
    import deliver
    importlib.reload(deliver)  # re-read ICT_SOURCE_DIR
    return deliver.deliver_hosted()


def test_hosted_zip_exists_and_has_no_license(hosted_zip):
    assert hosted_zip.exists()
    names = zipfile.ZipFile(hosted_zip).namelist()
    assert not any(n.lower().endswith(".key") for n in names), names


def test_hosted_zip_contains_buyer_essentials(hosted_zip):
    names = zipfile.ZipFile(hosted_zip).namelist()
    for required in (
        "plugict/ict-vault.kevin",
        "plugict/mcp_server.py",
        "plugict/vault_core.py",
        "plugict/metadata_enricher.py",
        "plugict/VAULT.md",
        "plugict/setup.bat",
        "plugict/setup.sh",
        "plugict/requirements.txt",
        "plugict/examples/make_configs.py",
        "plugict/docs/README.md",
    ):
        assert required in names, f"missing {required}"


def test_hosted_readme_tells_buyer_to_add_license(hosted_zip):
    readme = zipfile.ZipFile(hosted_zip).read("plugict/docs/README.md").decode()
    assert "license.key" in readme
    assert "purchase email" in readme.lower()


def test_make_configs_writes_local_paths(hosted_zip, tmp_path):
    # Simulate a buyer extracting the zip somewhere else and running make_configs.
    buyer_dir = tmp_path / "buyer-machine"
    zipfile.ZipFile(hosted_zip).extractall(buyer_dir)
    pkg = buyer_dir / "plugict"

    import subprocess
    out = subprocess.run([sys.executable, str(pkg / "examples" / "make_configs.py")],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr

    cfg = (pkg / "examples" / "claude_desktop_config.json").read_text()
    assert str(pkg / "mcp_server.py") in cfg.replace("\\\\", "\\")


def test_hosted_zip_uses_exact_isolated_build_artifact_over_source_decoy(tmp_path, monkeypatch):
    import deliver
    source = tmp_path / "source"
    build = tmp_path / "build"
    delivery = tmp_path / "delivery"
    source.mkdir()
    build.mkdir()
    (source / "ict-vault.kevin").write_bytes(b"SOURCE-DECOY")
    _write_release_manifest(source, b"SOURCE-DECOY")
    (build / "ict-vault.kevin").write_bytes(b"ISOLATED-V3")
    _write_release_manifest(build, b"ISOLATED-V3")
    with monkeypatch.context() as env:
        env.setenv("ICT_SOURCE_DIR", str(source))
        env.setenv("ICT_BUILD_DIR", str(build))
        env.setenv("ICT_DELIVERY_DIR", str(delivery))
        # This test isolates artifact selection, not cryptographic signing.
        env.setattr(vc, "RELEASE_TRUSTED_KEYS", {})
        importlib.reload(deliver)
        zip_path = deliver.deliver_hosted()
        with zipfile.ZipFile(zip_path) as archive:
            assert archive.read("plugict/ict-vault.kevin") == b"ISOLATED-V3"
            # The manifest that ships must describe the isolated build vault,
            # not the source decoy.
            manifest = json.loads(archive.read("plugict/release.sig.json"))
            assert manifest["vault_sha256"] == hashlib.sha256(b"ISOLATED-V3").hexdigest()



def test_hosted_zip_contains_release_manifest(hosted_zip):
    names = zipfile.ZipFile(hosted_zip).namelist()
    assert "plugict/release.sig.json" in names, names
    manifest = json.loads(zipfile.ZipFile(hosted_zip).read("plugict/release.sig.json"))
    assert manifest["product"] == vc.RELEASE_PRODUCT
    # The manifest must describe the exact vault bytes that shipped.
    vault_bytes = zipfile.ZipFile(hosted_zip).read("plugict/ict-vault.kevin")
    assert manifest["vault_sha256"] == hashlib.sha256(vault_bytes).hexdigest()


def test_hosted_packaging_fails_when_manifest_missing(tmp_path, monkeypatch):
    # A hosted package with the vault but no release.sig.json is broken by
    # construction (issued licenses could never authorize a future vault), so
    # packaging must fail closed rather than ship it.
    import deliver
    (tmp_path / "ict-vault.kevin").write_bytes(b"dummy-encrypted-vault")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "AI-AGENT-GUIDE.md").write_text("# guide")
    monkeypatch.setenv("ICT_SOURCE_DIR", str(tmp_path))
    monkeypatch.delenv("ICT_BUILD_DIR", raising=False)
    monkeypatch.setenv("ICT_DELIVERY_DIR", str(tmp_path / "delivery"))
    importlib.reload(deliver)
    try:
        with pytest.raises(SystemExit):
            deliver.deliver_hosted()
    finally:
        importlib.reload(deliver)


def test_hosted_packaging_fails_when_manifest_hash_mismatches(tmp_path, monkeypatch):
    # A manifest that describes different vault bytes must never be shipped —
    # it would authorize a vault the seller did not sign for this release.
    import deliver
    (tmp_path / "ict-vault.kevin").write_bytes(b"dummy-encrypted-vault")
    _write_release_manifest(tmp_path, b"a-completely-different-vault")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "AI-AGENT-GUIDE.md").write_text("# guide")
    monkeypatch.setenv("ICT_SOURCE_DIR", str(tmp_path))
    monkeypatch.delenv("ICT_BUILD_DIR", raising=False)
    monkeypatch.setenv("ICT_DELIVERY_DIR", str(tmp_path / "delivery"))
    importlib.reload(deliver)
    try:
        with pytest.raises(SystemExit):
            deliver.deliver_hosted()
    finally:
        importlib.reload(deliver)


def test_hosted_packaging_fails_on_invalid_signature_when_trust_store_pinned(
        tmp_path, monkeypatch):
    # The production posture: buyers pin a seller key. A manifest that matches
    # product + vault hash but carries an INVALID signature must be rejected at
    # packaging time — a buyer with the pinned key would refuse it, so shipping
    # it would hand out an unopenable vault. Guards the populated-trust-store
    # branch of _copy_release_manifest that the empty-store tests never reach.
    import deliver
    vault_bytes = b"dummy-encrypted-vault"
    vault_hash = hashlib.sha256(vault_bytes).hexdigest()
    (tmp_path / "ict-vault.kevin").write_bytes(vault_bytes)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "AI-AGENT-GUIDE.md").write_text("# guide")

    key = Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes_raw()
    key_id = vc.release_key_id(pub)
    manifest = {
        "product": vc.RELEASE_PRODUCT,
        "tag": "v1.0.0",
        "vault_sha256": vault_hash,
        "key_id": key_id,
        "algo": "ed25519",
    }
    # Sign correctly, then corrupt the signature so verification must fail.
    good_sig = bytearray(key.sign(vc.release_manifest_payload(manifest)))
    good_sig[0] ^= 0xFF
    manifest["sig"] = good_sig.hex()
    (tmp_path / vc.RELEASE_MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    monkeypatch.setenv("ICT_SOURCE_DIR", str(tmp_path))
    monkeypatch.delenv("ICT_BUILD_DIR", raising=False)
    monkeypatch.setenv("ICT_DELIVERY_DIR", str(tmp_path / "delivery"))
    # Buyers pin this exact key -> the invalid signature is now a hard failure.
    monkeypatch.setattr(vc, "RELEASE_TRUSTED_KEYS", {key_id: pub.hex()})
    importlib.reload(deliver)
    try:
        with pytest.raises(SystemExit):
            deliver.deliver_hosted()
        # Nothing must have been zipped for shipment.
        assert not (tmp_path / "delivery" / "plugict.zip").exists()
    finally:
        importlib.reload(deliver)


def _write_test_license(path, buyer, purchase, vault_hash):
    path.write_text(
        f"LICENSED_TO={buyer}\n"
        f"PURCHASE_ID={purchase}\n"
        "LICENSE_ID=FIXTURE123\n"
        f"VAULT_HASH={vault_hash}\n",
        encoding="utf-8",
    )


def test_per_buyer_delivery_rejects_stale_license_before_creating_package(tmp_path, monkeypatch):
    import deliver
    source = tmp_path / "source"
    build = tmp_path / "build"
    delivery = tmp_path / "delivery"
    source.mkdir()
    build.mkdir()
    vault = build / "ict-vault.kevin"
    vault.write_bytes(b"current encrypted artifact")
    _write_test_license(
        build / "license_buyer_at_example_com.key",
        "buyer@example.com", "ORDER-1", "0" * 64)

    with monkeypatch.context() as env:
        env.setenv("ICT_SOURCE_DIR", str(source))
        env.setenv("ICT_BUILD_DIR", str(build))
        env.setenv("ICT_DELIVERY_DIR", str(delivery))
        importlib.reload(deliver)
        with pytest.raises(SystemExit):
            deliver.deliver("buyer@example.com", "ORDER-1")
        assert not (delivery / "buyer_at_example_com").exists()
    importlib.reload(deliver)


def test_per_buyer_delivery_accepts_exact_license_artifact_and_order_binding(tmp_path, monkeypatch):
    import deliver
    source = tmp_path / "source"
    build = tmp_path / "build"
    delivery = tmp_path / "delivery"
    source.mkdir()
    build.mkdir()
    vault = build / "ict-vault.kevin"
    vault.write_bytes(b"current encrypted artifact")
    _write_test_license(
        build / "license_buyer_at_example_com.key",
        "buyer@example.com", "ORDER-1", hashlib.sha256(vault.read_bytes()).hexdigest())

    with monkeypatch.context() as env:
        env.setenv("ICT_SOURCE_DIR", str(source))
        env.setenv("ICT_BUILD_DIR", str(build))
        env.setenv("ICT_DELIVERY_DIR", str(delivery))
        importlib.reload(deliver)
        package = deliver.deliver("buyer@example.com", "ORDER-1")
        assert (package / "ict-vault.kevin").read_bytes() == vault.read_bytes()
        assert "ORDER-1" in (package / "docs" / "README.md").read_text()
    importlib.reload(deliver)


def test_per_buyer_delivery_uses_verified_snapshot_if_live_vault_changes_after_check(
        tmp_path, monkeypatch):
    import deliver
    source = tmp_path / "source"
    build = tmp_path / "build"
    delivery = tmp_path / "delivery"
    source.mkdir()
    build.mkdir()
    vault = build / "ict-vault.kevin"
    verified_bytes = b"verified encrypted artifact"
    vault.write_bytes(verified_bytes)
    _write_test_license(
        build / "license_buyer_at_example_com.key",
        "buyer@example.com", "ORDER-RACE",
        hashlib.sha256(verified_bytes).hexdigest())

    with monkeypatch.context() as env:
        env.setenv("ICT_SOURCE_DIR", str(source))
        env.setenv("ICT_BUILD_DIR", str(build))
        env.setenv("ICT_DELIVERY_DIR", str(delivery))
        importlib.reload(deliver)
        original_fresh_dir = deliver._fresh_dir

        def mutate_after_license_check(name):
            vault.write_bytes(b"different artifact after verification")
            return original_fresh_dir(name)

        env.setattr(deliver, "_fresh_dir", mutate_after_license_check)
        package = deliver.deliver("buyer@example.com", "ORDER-RACE")
        packaged_vault = package / "ict-vault.kevin"
        assert packaged_vault.read_bytes() == verified_bytes
        assert hashlib.sha256(packaged_vault.read_bytes()).hexdigest() == (
            deliver._parse_license_fields(package / "license.key")["VAULT_HASH"])
    importlib.reload(deliver)
