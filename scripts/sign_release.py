#!/usr/bin/env python3
"""sign_release.py — seller-side Ed25519 signing of a vault release.

Why: buyer licenses pin the exact VAULT_HASH they were issued against, so any
vault update would otherwise invalidate every issued license. A signed release
manifest (release.sig.json, shipped next to ict-vault.kevin) lets open_vault()
accept a newer vault whose hash the seller has signed — issued licenses keep
working across releases, and integrity stays intact because only the seller
holds the signing key.

One-time setup (on the seller machine):

    python scripts/sign_release.py --init
        Generates .release_signing_key in the build dir (0600, NEVER commit it)
        and prints the public key + key_id snippet to paste into
        vault_core.RELEASE_TRUSTED_KEYS. Ship that vault_core.py to buyers.

Each release:

    python scripts/sign_release.py --tag v3.6.0
        Hashes ict-vault.kevin in the build dir, signs it, writes
        release.sig.json next to it, and self-verifies the result.
        Include release.sig.json in the buyer ZIP beside ict-vault.kevin.

The private key is read from ICT_BUILD_DIR (same convention as .vault_key) and
must never appear in the repo, the buyer ZIP, or any release asset.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent))
import vault_core as vc  # noqa: E402


from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: E402
    Ed25519PrivateKey,
)

KEY_FILE_NAME = ".release_signing_key"


def _build_dir(arg: str | None) -> Path:
    return Path(arg or os.environ.get("ICT_BUILD_DIR") or ".").resolve()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _secure_windows_private_key_acl(path: Path, *, runner=subprocess.run,
                                    username: str | None = None) -> None:
    """Restrict a newly-created private key to its owner, SYSTEM and admins.

    POSIX mode bits are not an ACL boundary on NTFS; without this, a new key can
    inherit broad access from its parent folder.
    """
    user = username or os.environ.get("USERNAME") or getpass.getuser()
    result = runner(
        ["icacls", str(path), "/inheritance:r", "/grant:r",
         f"{user}:(F)", "BUILTIN\\Administrators:(F)", "NT AUTHORITY\\SYSTEM:(F)"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "could not restrict Windows ACL on release signing key: "
            f"{getattr(result, 'stderr', '').strip()}"
        )


def _write_private_key(path: Path, key: Ed25519PrivateKey) -> None:
    data = key.private_bytes_raw().hex() + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, data.encode("ascii"))
    finally:
        os.close(fd)
    if os.name == "nt":
        try:
            _secure_windows_private_key_acl(path)
        except Exception:
            path.unlink(missing_ok=True)
            raise


def _load_private_key(path: Path) -> Ed25519PrivateKey:
    if not path.exists():
        sys.exit(
            f"ERROR: {path} not found.\n"
            "  Run `python scripts/sign_release.py --init` once on this machine first."
        )
    mode = stat.S_IMODE(path.stat().st_mode)
    if os.name != "nt" and mode & 0o077:
        print(f"  WARNING: {path} is group/world readable (mode {oct(mode)}); "
              "consider chmod 600.", file=sys.stderr)
    raw = bytes.fromhex(path.read_text(encoding="ascii").strip())
    if len(raw) != 32:
        sys.exit(f"ERROR: {path} is malformed (expected 32 raw key bytes as hex).")
    return Ed25519PrivateKey.from_private_bytes(raw)


def cmd_init(build_dir: Path) -> None:
    key_path = build_dir / KEY_FILE_NAME
    if key_path.exists():
        sys.exit(
            f"ERROR: {key_path} already exists. Refusing to overwrite a signing key.\n"
            "  Delete it manually only if you intend to rotate (old manifests then\n"
            "  stop verifying once the pinned public key is replaced)."
        )
    build_dir.mkdir(parents=True, exist_ok=True)
    key = Ed25519PrivateKey.generate()
    _write_private_key(key_path, key)
    pub_hex = key.public_key().public_bytes_raw().hex()
    key_id = vc.release_key_id(bytes.fromhex(pub_hex))
    print(f"  Private key written: {key_path}  (0600 — NEVER commit or ship this)")
    print()
    print("  Pin the public key in scripts/vault_core.py:")
    print()
    print("  RELEASE_TRUSTED_KEYS = {")
    print(f'      "{key_id}": "{pub_hex}",')
    print("  }")
    print()
    print("  Commit that vault_core.py change so buyers can verify signed releases.")


def cmd_sign(build_dir: Path, vault: Path | None, tag: str, out: Path | None) -> None:
    vault_path = (vault or build_dir / "ict-vault.kevin").resolve()
    if not vault_path.is_file():
        sys.exit(f"ERROR: vault not found: {vault_path}")
    key = _load_private_key(build_dir / KEY_FILE_NAME)
    pub_bytes = key.public_key().public_bytes_raw()
    key_id = vc.release_key_id(pub_bytes)

    vault_hash = _sha256_file(vault_path)
    manifest = {
        "product": vc.RELEASE_PRODUCT,
        "tag": tag,
        "vault_sha256": vault_hash,
        "key_id": key_id,
        "algo": "ed25519",
    }
    signature = key.sign(vc.release_manifest_payload(manifest))
    manifest["sig"] = signature.hex()

    out_path = (out or vault_path.parent / vc.RELEASE_MANIFEST_NAME).resolve()
    out_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")

    # Self-verify with the derived public key before declaring success.
    ok, reason = vc.verify_release_manifest(
        out_path, vault_hash, trusted_keys={key_id: pub_bytes.hex()})
    if not ok:
        out_path.unlink(missing_ok=True)
        sys.exit(f"ERROR: self-verification failed ({reason}); manifest removed.")

    print(f"  Vault:    {vault_path}")
    print(f"  SHA-256:  {vault_hash}")
    print(f"  Tag:      {tag}")
    print(f"  Manifest: {out_path}  ✅ self-verified")
    if key_id not in vc.RELEASE_TRUSTED_KEYS:
        print()
        print(f"  WARNING: key_id {key_id} is NOT pinned in vault_core.RELEASE_TRUSTED_KEYS —")
        print("  buyers cannot verify this manifest until that public key is committed.")
    print()
    print("  Ship release.sig.json in the buyer ZIP beside ict-vault.kevin.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--init", action="store_true",
                        help="generate the signing keypair (one-time)")
    parser.add_argument("--build-dir", default=None,
                        help="seller build dir (default: $ICT_BUILD_DIR or .)")
    parser.add_argument("--vault", type=Path, default=None,
                        help="vault file to sign (default: <build-dir>/ict-vault.kevin)")
    parser.add_argument("--tag", default=None, help="release tag, e.g. v3.6.0")
    parser.add_argument("--out", type=Path, default=None,
                        help=f"manifest output path (default: next to the vault as {vc.RELEASE_MANIFEST_NAME})")
    args = parser.parse_args()

    build_dir = _build_dir(args.build_dir)
    if args.init:
        cmd_init(build_dir)
        return
    if not args.tag:
        parser.error("--tag is required when signing (e.g. --tag v3.6.0)")
    cmd_sign(build_dir, args.vault, args.tag, args.out)


if __name__ == "__main__":
    main()
