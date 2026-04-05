"""Cryptographic identity utilities for Claw Network.

Provides Ed25519 key validation, did:key derivation, and request signature
verification.  Keeps all crypto logic in one place so the rest of the codebase
never touches raw bytes directly.
"""

from __future__ import annotations

import base64
import hashlib
import struct
from datetime import datetime, timezone

from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Multicodec prefix for Ed25519 public keys (varint 0xed = 0xed01 in unsigned LEB128)
_ED25519_MULTICODEC_PREFIX = b"\xed\x01"

# Multibase prefix for base58btc
_MULTIBASE_BASE58BTC = "z"

# Base58 Bitcoin alphabet
_B58_ALPHABET = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

# Maximum allowed clock skew for signature timestamps (seconds)
SIGNATURE_MAX_SKEW_SECONDS = 300  # 5 minutes


# ---------------------------------------------------------------------------
# Base58 encoding (minimal, no external dependency)
# ---------------------------------------------------------------------------

def _b58encode(data: bytes) -> str:
    """Encode bytes to base58btc string."""
    n = int.from_bytes(data, "big")
    result = []
    while n > 0:
        n, remainder = divmod(n, 58)
        result.append(_B58_ALPHABET[remainder:remainder + 1])
    # Preserve leading zero bytes
    for byte in data:
        if byte == 0:
            result.append(b"1")
        else:
            break
    return b"".join(reversed(result)).decode("ascii")


def _b58decode(s: str) -> bytes:
    """Decode base58btc string to bytes."""
    n = 0
    for ch in s.encode("ascii"):
        n = n * 58 + _B58_ALPHABET.index(ch)
    # Count leading '1's (representing zero bytes)
    leading_zeros = 0
    for ch in s:
        if ch == "1":
            leading_zeros += 1
        else:
            break
    # Convert integer to bytes
    if n == 0:
        return b"\x00" * leading_zeros
    byte_length = (n.bit_length() + 7) // 8
    return b"\x00" * leading_zeros + n.to_bytes(byte_length, "big")


# ---------------------------------------------------------------------------
# Public key validation
# ---------------------------------------------------------------------------

def validate_public_key_b64(public_key_b64: str) -> bytes:
    """Validate a Base64-encoded Ed25519 public key and return raw 32 bytes.

    Raises ``ValueError`` on any format problem.
    """
    try:
        raw = base64.b64decode(public_key_b64, validate=True)
    except Exception as exc:
        raise ValueError(f"Invalid Base64 encoding: {exc}") from exc
    if len(raw) != 32:
        raise ValueError(f"Ed25519 public key must be 32 bytes, got {len(raw)}.")
    # Let PyNaCl verify it's a valid curve point
    try:
        VerifyKey(raw)
    except Exception as exc:
        raise ValueError(f"Invalid Ed25519 public key: {exc}") from exc
    return raw


# ---------------------------------------------------------------------------
# did:key derivation
# ---------------------------------------------------------------------------

def derive_did_key(public_key_bytes: bytes) -> str:
    """Derive a ``did:key`` identifier from raw Ed25519 public key bytes.

    Follows the did:key method specification:
    ``did:key:z<base58btc(multicodec(ed25519-pub, raw_key))>``
    """
    if len(public_key_bytes) != 32:
        raise ValueError("Expected 32-byte Ed25519 public key.")
    multi = _ED25519_MULTICODEC_PREFIX + public_key_bytes
    return f"did:key:{_MULTIBASE_BASE58BTC}{_b58encode(multi)}"


def did_key_to_public_key_bytes(did: str) -> bytes:
    """Extract raw Ed25519 public key bytes from a ``did:key`` string.

    Raises ``ValueError`` if the DID is malformed.
    """
    prefix = "did:key:z"
    if not did.startswith(prefix):
        raise ValueError("Not a valid did:key (must start with 'did:key:z').")
    b58_part = did[len(prefix):]
    decoded = _b58decode(b58_part)
    if not decoded.startswith(_ED25519_MULTICODEC_PREFIX):
        raise ValueError("did:key does not contain an Ed25519 multicodec prefix.")
    raw = decoded[len(_ED25519_MULTICODEC_PREFIX):]
    if len(raw) != 32:
        raise ValueError(f"Decoded key is {len(raw)} bytes, expected 32.")
    return raw


def public_key_b64_to_did(public_key_b64: str) -> str:
    """Convenience: validate a Base64 public key and return its did:key."""
    raw = validate_public_key_b64(public_key_b64)
    return derive_did_key(raw)


# ---------------------------------------------------------------------------
# DID Document generation
# ---------------------------------------------------------------------------

def build_did_document(did: str, public_key_b64: str) -> dict:
    """Build a minimal W3C DID Document for a did:key identity."""
    key_id = f"{did}#keys-1"
    return {
        "@context": [
            "https://www.w3.org/ns/did/v1",
            "https://w3id.org/security/suites/ed25519-2020/v1",
        ],
        "id": did,
        "verificationMethod": [
            {
                "id": key_id,
                "type": "Ed25519VerificationKey2020",
                "controller": did,
                "publicKeyBase64": public_key_b64,
            }
        ],
        "authentication": [key_id],
        "assertionMethod": [key_id],
    }


# ---------------------------------------------------------------------------
# Request signature verification
# ---------------------------------------------------------------------------

def build_signature_payload(method: str, path: str, timestamp: str, body_bytes: bytes) -> bytes:
    """Construct the canonical byte string that must be signed.

    Format: ``METHOD\nPATH\nTIMESTAMP\nSHA256(body)``
    """
    body_hash = hashlib.sha256(body_bytes).hexdigest()
    canonical = f"{method.upper()}\n{path}\n{timestamp}\n{body_hash}"
    return canonical.encode("utf-8")


def verify_request_signature(
    *,
    public_key_b64: str,
    signature_b64: str,
    method: str,
    path: str,
    timestamp: str,
    body_bytes: bytes,
) -> None:
    """Verify an Ed25519 request signature.

    Raises ``ValueError`` with a descriptive message on failure.
    """
    # Validate timestamp freshness
    try:
        ts = datetime.fromisoformat(timestamp)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
    except Exception as exc:
        raise ValueError(f"Invalid timestamp format: {exc}") from exc

    now = datetime.now(timezone.utc)
    skew = abs((now - ts).total_seconds())
    if skew > SIGNATURE_MAX_SKEW_SECONDS:
        raise ValueError(
            f"Timestamp skew too large ({skew:.0f}s > {SIGNATURE_MAX_SKEW_SECONDS}s). "
            "Possible replay attack or clock drift."
        )

    # Decode public key
    try:
        pk_bytes = base64.b64decode(public_key_b64, validate=True)
        vk = VerifyKey(pk_bytes)
    except Exception as exc:
        raise ValueError(f"Cannot load public key: {exc}") from exc

    # Decode signature
    try:
        sig_bytes = base64.b64decode(signature_b64, validate=True)
    except Exception as exc:
        raise ValueError(f"Invalid signature encoding: {exc}") from exc

    if len(sig_bytes) != 64:
        raise ValueError(f"Signature must be 64 bytes, got {len(sig_bytes)}.")

    # Verify
    payload = build_signature_payload(method, path, timestamp, body_bytes)
    try:
        vk.verify(payload, sig_bytes)
    except BadSignatureError:
        raise ValueError("Signature verification failed.")
