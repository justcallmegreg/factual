"""FAC-08 (and FAC-17): the instance's Ed25519 key file."""

from __future__ import annotations

import pytest

from app.ledger.keys import (
    SigningKeyError,
    generate_signing_key,
    load_signing_key,
    public_key_bytes,
)


def test_a_generated_key_loads_back_and_has_a_32_byte_public_key(tmp_path):
    path = tmp_path / "signing-key.pem"
    generate_signing_key(path)
    assert path.read_text().startswith("-----BEGIN PRIVATE KEY-----")
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    key = load_signing_key(path)
    assert len(public_key_bytes(key)) == 32
    assert public_key_bytes(load_signing_key(path)) == public_key_bytes(key)


def test_generating_never_overwrites_an_existing_key(tmp_path):
    path = tmp_path / "signing-key.pem"
    generate_signing_key(path)
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        generate_signing_key(path)
    assert path.read_bytes() == before


def test_a_missing_or_wrong_key_file_is_reported_clearly(tmp_path):
    with pytest.raises(SigningKeyError, match="SIGNING_KEY_FILE"):
        load_signing_key(tmp_path / "missing.pem")
    (tmp_path / "rsa.pem").write_text(
        "-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n"
    )
    with pytest.raises(SigningKeyError):
        load_signing_key(tmp_path / "rsa.pem")
