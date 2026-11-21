"""The hash formulas of the ledger. Every tag is versioned so that formats can evolve."""

from __future__ import annotations

import hashlib

ENTRY_TAG = b"ledger-entry-v1"
CHAIN_TAG = b"ledger-chain-v1"
GENESIS_TAG = b"ledger-genesis-v1"


def entry_hash(entry_bytes: bytes) -> bytes:
    """SHA-256("ledger-entry-v1" ‖ JCS(entry))."""
    return hashlib.sha256(ENTRY_TAG + entry_bytes).digest()


def payload_commitment(salt: bytes, payload_bytes: bytes) -> bytes:
    """SHA-256(salt ‖ JCS(payload)); the salt keeps short payloads unguessable."""
    return hashlib.sha256(salt + payload_bytes).digest()


def commitment_string(digest: bytes) -> str:
    return "sha256:" + digest.hex()


def chain_hash(previous: bytes, current_entry_hash: bytes) -> bytes:
    """SHA-256("ledger-chain-v1" ‖ chain_hash(n−1) ‖ entry_hash(n))."""
    return hashlib.sha256(CHAIN_TAG + previous + current_entry_hash).digest()


def genesis_hash(public_key: bytes, created_at: str) -> bytes:
    """The chain's starting value: the instance's public key and its creation time."""
    return hashlib.sha256(GENESIS_TAG + public_key + created_at.encode("ascii")).digest()
