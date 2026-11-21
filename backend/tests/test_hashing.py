"""FAC-08: entry hash, salted payload commitment, rolling chain hash and genesis."""

from __future__ import annotations

import hashlib
import os

from app.ledger.hashing import (
    chain_hash,
    commitment_string,
    entry_hash,
    genesis_hash,
    payload_commitment,
)


def test_entry_hash_is_sha256_over_the_tag_and_the_canonical_bytes():
    entry = b'{"seq":1,"type":"record.created","v":1}'
    assert entry_hash(entry) == hashlib.sha256(b"ledger-entry-v1" + entry).digest()
    assert len(entry_hash(entry)) == 32


def test_payload_commitment_is_sha256_over_salt_and_payload():
    salt, payload = os.urandom(32), b'{"text":"ok"}'
    assert payload_commitment(salt, payload) == hashlib.sha256(salt + payload).digest()
    assert commitment_string(payload_commitment(salt, payload)).startswith("sha256:")
    assert len(commitment_string(payload_commitment(salt, payload))) == len("sha256:") + 64


def test_a_short_message_cannot_be_guessed_without_the_salt():
    payload = b'{"text":"ok"}'
    first, second = (
        payload_commitment(os.urandom(32), payload),
        payload_commitment(os.urandom(32), payload),
    )
    assert first != second
    assert first != hashlib.sha256(payload).digest()


def test_chain_hash_folds_the_previous_value_and_the_entry_hash():
    previous, current = os.urandom(32), os.urandom(32)
    assert (
        chain_hash(previous, current)
        == hashlib.sha256(b"ledger-chain-v1" + previous + current).digest()
    )


def test_genesis_depends_on_the_public_key_and_the_creation_time():
    key = os.urandom(32)
    assert genesis_hash(key, "2026-11-21T00:00:00Z") != genesis_hash(key, "2026-11-22T00:00:00Z")
    assert genesis_hash(key, "2026-11-21T00:00:00Z") != genesis_hash(
        os.urandom(32), "2026-11-21T00:00:00Z"
    )
    assert (
        genesis_hash(key, "2026-11-21T00:00:00Z")
        == hashlib.sha256(b"ledger-genesis-v1" + key + b"2026-11-21T00:00:00Z").digest()
    )
