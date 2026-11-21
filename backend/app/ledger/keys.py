"""The instance's Ed25519 signing key and the attachment master key, as files."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


class SigningKeyError(RuntimeError):
    """The signing key file is missing or not an Ed25519 key."""


def _write_secret(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


def generate_signing_key(path: Path) -> Ed25519PrivateKey:
    """Create a new Ed25519 key as PKCS#8 PEM with mode 0600. Never overwrites."""
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    _write_secret(Path(path), pem)
    return key


def generate_master_key(path: Path) -> bytes:
    """Create the 32-byte master key that wraps attachment keys. Never overwrites."""
    key = os.urandom(32)
    _write_secret(Path(path), key)
    return key


def load_signing_key(path: Path) -> Ed25519PrivateKey:
    path = Path(path)
    if not path.is_file():
        raise SigningKeyError(
            f"SIGNING_KEY_FILE {path} does not exist; create it with `python -m app keys`"
        )
    try:
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    except (ValueError, TypeError) as exc:
        raise SigningKeyError(f"SIGNING_KEY_FILE {path} is not a readable PEM key: {exc}") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise SigningKeyError(f"SIGNING_KEY_FILE {path} is not an Ed25519 key")
    return key


def public_key_bytes(key: Ed25519PrivateKey) -> bytes:
    """The raw 32-byte public key."""
    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def public_key_pem(key: Ed25519PrivateKey) -> bytes:
    """The public key as SubjectPublicKeyInfo PEM, the form `openssl` reads."""
    return key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )


def key_fingerprint(key: Ed25519PrivateKey) -> str:
    """SHA-256 of the DER SubjectPublicKeyInfo, as `sha256:<hex>`."""
    der = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return "sha256:" + hashlib.sha256(der).hexdigest()
