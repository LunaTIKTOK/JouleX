"""Ed25519 signing and verification (RFC 8032) — CF-KYA v0.1.

PROD PATH: the `cryptography` library. This module uses it whenever it is
importable (``USES_CRYPTOGRAPHY`` is True) and the sidecar (the prod
entrypoint) refuses to start without it (see sidecar.create_app). Hand-rolled
crypto never runs in production.

FALLBACK: when `cryptography` is unavailable (library tests, offline dev
runs), the pure-python implementation in ``ed25519_pure.py`` is used instead.
Both backends implement the same RFC 8032 algorithm and produce identical,
interoperable signatures, so a signature made by one verifies under the
other. The fallback is NOT a prod path — it exists so the library stays
usable and testable without the dependency installed.

API (identical across backends):
  generate_keypair() -> (seed, public_key)   # 32-byte seed, 32-byte pubkey
  public_key_from_seed(seed) -> bytes
  sign(message, seed, public_key) -> bytes   # 64-byte signature
  verify(signature, message, public_key) -> bool
  public_key_hex_from_seed(seed) -> str
  sign_hex(message, seed, public_key) -> str
  verify_hex(signature_hex, message, public_key_hex) -> bool
"""

from __future__ import annotations

try:  # pragma: no cover - backend selection is environment-dependent
    from cryptography.exceptions import InvalidSignature as _InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey as _EdPrivateKey,
    )
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PublicKey as _EdPublicKey,
    )

    USES_CRYPTOGRAPHY = True
    BACKEND = "cryptography"
except Exception:  # cryptography not installed: pure-python fallback
    from ed25519_pure import (  # type: ignore[no-redef]
        generate_keypair,
        public_key_from_seed,
        public_key_hex_from_seed,
        sign,
        sign_hex,
        verify,
        verify_hex,
    )

    USES_CRYPTOGRAPHY = False
    BACKEND = "ed25519_pure"


if USES_CRYPTOGRAPHY:

    def generate_keypair() -> tuple[bytes, bytes]:
        """Return a fresh (seed, public_key) pair. Seed is the 32-byte
        private key material; the public key is its 32-byte encoding."""
        private = _EdPrivateKey.generate()
        seed = private.private_bytes_raw()
        public_key = private.public_key().public_bytes_raw()
        return seed, public_key

    def public_key_from_seed(seed: bytes) -> bytes:
        if len(seed) != 32:
            raise ValueError("seed must be 32 bytes")
        private = _EdPrivateKey.from_private_bytes(seed)
        return private.public_key().public_bytes_raw()

    def sign(message: bytes, seed: bytes, public_key: bytes) -> bytes:
        """Sign `message` with the private key `seed`. `public_key` is
        accepted for API symmetry and checked against the seed-derived key
        so a mismatched pair fails loudly instead of signing under the
        wrong identity."""
        if len(seed) != 32:
            raise ValueError("seed must be 32 bytes")
        derived = public_key_from_seed(seed)
        if len(public_key) != 32 or public_key != derived:
            raise ValueError("public_key does not match seed")
        private = _EdPrivateKey.from_private_bytes(seed)
        return private.sign(message)

    def verify(signature: bytes, message: bytes, public_key: bytes) -> bool:
        if len(signature) != 64 or len(public_key) != 32:
            return False
        try:
            pub = _EdPublicKey.from_public_bytes(public_key)
            pub.verify(signature, message)
        except Exception:
            return False
        return True

    def public_key_hex_from_seed(seed: bytes) -> str:
        return public_key_from_seed(seed).hex()

    def sign_hex(message: bytes, seed: bytes, public_key: bytes) -> str:
        return sign(message, seed, public_key).hex()

    def verify_hex(signature_hex: str, message: bytes, public_key_hex: str) -> bool:
        try:
            signature = bytes.fromhex(signature_hex)
            public_key = bytes.fromhex(public_key_hex)
        except ValueError:
            return False
        return verify(signature, message, public_key)


def assert_prod_crypto() -> None:
    """Raise unless the prod crypto backend is active.

    Called by the sidecar (the prod entrypoint). Library-level verification
    works with the pure-python fallback for tests and dev, but a deployment
    must never run on hand-rolled crypto.
    """
    if not USES_CRYPTOGRAPHY:
        raise RuntimeError(
            "CF-KYA prod path requires the `cryptography` library; "
            "it is not installed in this environment. Install "
            "requirements-sidecar.txt (which includes cryptography) "
            "and restart."
        )
