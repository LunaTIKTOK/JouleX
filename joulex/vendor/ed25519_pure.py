"""Pure-python Ed25519 implementation (RFC 8032) — FALLBACK ONLY.

Used only when the `cryptography` library is unavailable. The prod path
(sidecar, KYA verification in deployment) requires `cryptography`; see
ed25519.py and kya.py. This module exists so library tests and offline
dev runs still work without the dependency installed.

Cognitive Firewall v0.1 Bind layer uses Ed25519 so a delegation chain can be
verified offline against public keys, with no new runtime dependency.

Implementation note: point arithmetic uses extended twisted-Edwards
coordinates (Hisil et al.), so scalar multiplication needs no modular
inverse per addition — only the final encoding does, and verification
compares cross-multiplied coordinates with no inversion at all. The naive
affine version measured ~386 ms per verify; this version is ~two orders of
magnitude faster while producing identical signatures. Correctness is pinned
by the RFC 8032 test vector in tests/test_bind_ed25519.py: any regression
there fails the suite.

Do NOT use this module for anything other than Bind-layer signatures
without a fresh review: it is a minimal implementation, not a
general-purpose cryptographic library.
"""
from __future__ import annotations

import hashlib
import secrets

_Q = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493


def _inv(x: int) -> int:
    return pow(x, _Q - 2, _Q)


_D = (-121665 * _inv(121666)) % _Q
_I = pow(2, (_Q - 1) // 4, _Q)


def _xrecover(y: int) -> int:
    xx = (y * y - 1) * _inv(_D * y * y + 1) % _Q
    x = pow(xx, (_Q + 3) // 8, _Q)
    if (x * x - xx) % _Q != 0:
        x = (x * _I) % _Q
    if x % 2 != 0:
        x = _Q - x
    return x


_BY = (4 * _inv(5)) % _Q
_BX = _xrecover(_BY)
# Extended coordinates: (X, Y, T, Z) with x = X/Z, y = Y/Z, T = X*Y/Z.
_IDENTITY = (0, 1, 0, 1)
_BASE_EXT = (_BX, _BY, _BX * _BY % _Q, 1)


def _ext_add(p: tuple[int, int, int, int], q: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    x1, y1, t1, z1 = p
    x2, y2, t2, z2 = q
    a = x1 * x2 % _Q
    b = y1 * y2 % _Q
    c = _D * t1 * t2 % _Q
    d = z1 * z2 % _Q
    e = (x1 + y1) * (x2 + y2) % _Q
    e = (e - a - b) % _Q
    f = (d - c) % _Q
    g = (d + c) % _Q
    h = (b + a) % _Q  # a_curve = -1, so H = B - a*A = B + A
    return (e * f % _Q, g * h % _Q, e * h % _Q, f * g % _Q)


def _ext_double(p: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    x1, y1, _, z1 = p
    a = x1 * x1 % _Q
    b = y1 * y1 % _Q
    c = 2 * z1 * z1 % _Q
    d = (-a) % _Q  # a_curve = -1
    e = (x1 + y1) * (x1 + y1) % _Q
    e = (e - a - b) % _Q
    g = (d + b) % _Q
    f = (g - c) % _Q
    h = (d - b) % _Q
    return (e * f % _Q, g * h % _Q, e * h % _Q, f * g % _Q)


def _scalarmult_ext(p: tuple[int, int, int, int], e: int) -> tuple[int, int, int, int]:
    q = _IDENTITY
    for i in range(e.bit_length() - 1, -1, -1):
        q = _ext_double(q)
        if (e >> i) & 1:
            q = _ext_add(q, p)
    return q


def _encode_ext(p: tuple[int, int, int, int]) -> bytes:
    x, y, _, z = p
    inv_z = _inv(z)
    ax = x * inv_z % _Q
    ay = y * inv_z % _Q
    bits = ay | ((ax & 1) << 255)
    return bits.to_bytes(32, "little")


def _to_ext(affine: tuple[int, int]) -> tuple[int, int, int, int]:
    x, y = affine
    return (x, y, x * y % _Q, 1)


def _bit(h: bytes, i: int) -> int:
    return (h[i // 8] >> (i % 8)) & 1


def _decodeint(s: bytes) -> int:
    return int.from_bytes(s, "little")


def _decodepoint(s: bytes) -> tuple[int, int] | None:
    y = _decodeint(s) & ((1 << 255) - 1)
    if y >= _Q:
        return None
    x = _xrecover(y)
    if x & 1 != _bit(s, 255):
        x = _Q - x
    return (x, y)


def _hint(m: bytes) -> int:
    return int.from_bytes(hashlib.sha512(m).digest(), "little")


def _clamp(h: bytes) -> int:
    a = bytearray(h[:32])
    a[0] &= 248
    a[31] &= 127
    a[31] |= 64
    return int.from_bytes(a, "little")


def generate_keypair() -> tuple[bytes, bytes]:
    """Return (seed, public_key_bytes); the seed is the private key material."""
    seed = secrets.token_bytes(32)
    return seed, public_key_from_seed(seed)


def public_key_from_seed(seed: bytes) -> bytes:
    if len(seed) != 32:
        raise ValueError("Ed25519 seed must be 32 bytes")
    h = hashlib.sha512(seed).digest()
    a = _clamp(h)
    return _encode_ext(_scalarmult_ext(_BASE_EXT, a))


def sign(message: bytes, seed: bytes, public_key: bytes) -> bytes:
    if len(seed) != 32:
        raise ValueError("Ed25519 seed must be 32 bytes")
    h = hashlib.sha512(seed).digest()
    a = _clamp(h)
    r = _hint(h[32:] + message) % _L
    r_enc = _encode_ext(_scalarmult_ext(_BASE_EXT, r))
    s = (r + _hint(r_enc + public_key + message) * a) % _L
    return r_enc + s.to_bytes(32, "little")


def verify(signature: bytes, message: bytes, public_key: bytes) -> bool:
    if len(signature) != 64 or len(public_key) != 32:
        return False
    r_enc, s_bytes = signature[:32], signature[32:]
    s = _decodeint(s_bytes)
    if s >= _L:
        return False
    a_affine = _decodepoint(public_key)
    r_affine = _decodepoint(r_enc)
    if a_affine is None or r_affine is None:
        return False
    h = _hint(r_enc + public_key + message) % _L
    left = _scalarmult_ext(_BASE_EXT, s)
    right = _ext_add(_to_ext(r_affine), _scalarmult_ext(_to_ext(a_affine), h))
    # Compare without inversion: X1/Z1 == X2/Z2 and Y1/Z1 == Y2/Z2.
    return (
        left[0] * right[3] % _Q == right[0] * left[3] % _Q
        and left[1] * right[3] % _Q == right[1] * left[3] % _Q
    )


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
