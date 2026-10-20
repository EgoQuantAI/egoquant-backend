"""Salted commitment and the transaction data that carries it.

    salt        = 32 random bytes, one per record (deleting it makes the on-chain value unlinkable forever)
    commitment  = SHA-256(salt || canonical record bytes)
    tx data     = b"EGOQ1" + commitment                     (37 bytes; nothing else goes on-chain)
"""

from __future__ import annotations

import hashlib
import re
import secrets

from egoquant.anchoring.config import TX_PREFIX

HEX64 = re.compile(r"[0-9a-f]{64}")


def new_salt() -> bytes:
    return secrets.token_bytes(32)


def commitment(salt: bytes, canonical: bytes) -> bytes:
    if len(salt) != 32:
        raise ValueError("the salt must be 32 bytes")
    return hashlib.sha256(salt + canonical).digest()


def tx_data(commit: bytes) -> bytes:
    if len(commit) != 32:
        raise ValueError("the commitment must be 32 bytes")
    return TX_PREFIX + commit


def parse_tx_data(data: bytes) -> bytes | None:
    """The 32-byte commitment inside a seal's data, or None if the data is not exactly prefix + 32 bytes."""
    if len(data) != len(TX_PREFIX) + 32 or not data.startswith(TX_PREFIX):
        return None
    return data[len(TX_PREFIX):]


def intrinsic_gas(data: bytes) -> int:
    """21 000 + 16 per non-zero byte + 4 per zero byte (no access list, not a contract creation)."""
    return 21_000 + sum(16 if b else 4 for b in data)


def hex_to_bytes(value: str, n: int | None = None) -> bytes:
    raw = bytes.fromhex(value.removeprefix("0x"))
    if n is not None and len(raw) != n:
        raise ValueError(f"expected {n} bytes")
    return raw
