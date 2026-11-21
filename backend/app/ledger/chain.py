"""Chain proofs: everything an outsider needs to tie one entry to a chain head."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from app.ledger.canonical import canonicalize
from app.ledger.hashing import chain_hash, entry_hash


@dataclass
class ChainProof:
    """One disclosed entry, the chain value before it, and the later entries as hashes."""

    seq: int
    entry_bytes: bytes
    chain_before: bytes
    later_entry_hashes: list[bytes]
    chain_head: bytes
    up_to_seq: int = field(default=0)

    def to_json(self) -> dict:
        return {
            "v": 1,
            "seq": self.seq,
            "entry": json.loads(self.entry_bytes),
            "chain_before": "sha256:" + self.chain_before.hex(),
            "later_entry_hashes": ["sha256:" + h.hex() for h in self.later_entry_hashes],
            "chain_head": "sha256:" + self.chain_head.hex(),
            "up_to_seq": self.up_to_seq,
        }


def fold_chain(chain_before: bytes, entry_bytes: bytes, later_entry_hashes: list[bytes]) -> bytes:
    head = chain_hash(chain_before, entry_hash(entry_bytes))
    for later in later_entry_hashes:
        head = chain_hash(head, later)
    return head


def verify_proof(proof: ChainProof) -> bool:
    """True when the entry is canonical, carries its seq, and folds to the chain head."""
    try:
        entry = json.loads(proof.entry_bytes)
    except ValueError:
        return False
    if not isinstance(entry, dict) or entry.get("seq") != proof.seq:
        return False
    if canonicalize(entry) != proof.entry_bytes:
        return False
    return fold_chain(proof.chain_before, proof.entry_bytes, proof.later_entry_hashes) == (
        proof.chain_head
    )
