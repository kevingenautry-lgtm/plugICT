"""Key stability across rebuilds: reusing the vault key lets an already-issued
license open a vault rebuilt with new videos, so buyers get updates without
re-licensing. Rotating the key (deliberately) locks old licenses out."""
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import vault_core as vc


def _pack(text, key=None):
    return vc.pack_and_encrypt(b"DB:" + text.encode(), b"CHROMA", vault_key=key)


def test_reused_key_is_stable():
    _, k1, _ = _pack("576 videos")
    _, k2, _ = _pack("580 videos", key=k1)  # rebuild with new content, same key
    assert k1 == k2


def test_existing_license_opens_rebuilt_vault():
    # First build mints a key; a buyer license envelope-wraps it.
    _, key_v1, _ = _pack("576 videos")
    buyer = Fernet.generate_key()
    wrapped = Fernet(buyer).encrypt(key_v1)

    # Seller rebuilds with 4 new videos, REUSING the key.
    _, key_v2, _ = _pack("580 videos", key=key_v1)

    # The buyer's existing license unwraps to the key that opens the NEW vault.
    assert Fernet(buyer).decrypt(wrapped) == key_v2


def test_rotated_key_locks_out_old_license():
    _, key_v1, _ = _pack("576 videos")
    buyer = Fernet.generate_key()
    wrapped = Fernet(buyer).encrypt(key_v1)

    _, key_rotated, _ = _pack("580 videos", key=None)  # fresh key (rotation)
    assert Fernet(buyer).decrypt(wrapped) != key_rotated


def test_fresh_key_when_none():
    _, k1, _ = _pack("x")
    _, k2, _ = _pack("x")  # two independent builds, no key passed
    assert k1 != k2 and len(k1) == 32


def test_malformed_key_rejected():
    with pytest.raises(ValueError):
        _pack("x", key=b"too-short")
