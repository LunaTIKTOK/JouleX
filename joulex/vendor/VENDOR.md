# Vendored CF-KYA snapshot — provenance

Source: `~/workspace/cognitive-firewall-build/` (CF-KYA v0.1, `KYA_VERSION = "cf-kya-0.1"`).
Copied: 2026-11-05. The files below are **unmodified copies**. Do not edit
them here; fixes go upstream in the firewall repo, and this snapshot is
 refreshed only by re-copying from a tagged version and re-recording the
hashes.

## Files and SHA-256

| File | SHA-256 |
|---|---|
| `delegation.py` | `821247e53c42e008652053679da7624fcf086eda0da5dded30ed281ecb99e212` |
| `ed25519.py` | `6b238d3e2f0c16b911b86a2a66471b0603e8e8e0415dc20d02b36c63de158f09` |
| `ed25519_pure.py` | `ba78c476afc96900d7cfd628ff34ce79eebff0ca1fc86785b81a7f87f2b9bfae` |
| `intent_classification.py` | `bcdf97f3732680caff188cd08ad90e09ff3d194077cb9f099425f65f9e827e1e` |
| `kya.py` | `42150ef755be1268798b75278230e55e9ea72e3f7096040fc5eb6a132e3819b5` |
| `principal.py` | `67b924fac15541c7e3c6edeabff01d1894d10deef46298920d5d7c8a3b37150e` |

Verify with: `sha256sum -c VENDOR_HASHES.txt` from this directory.

## Why vendored (not a sys.path import)

The exchange must be standalone-runnable anywhere with Python 3.12 and
zero third-party dependencies. A runtime import from the firewall build
directory would tie the service to one workstation path and to unrelated
edits in that repo. Vendoring keeps the dependency explicit, hash-pinned,
and auditable. See DESIGN.md §8.

## Backend note

`ed25519.py` uses the `cryptography` library when importable and the
pure-python `ed25519_pure.py` fallback otherwise. This environment has no
`cryptography` installed, so the pure-python backend is active
(`ed25519.BACKEND == "ed25519_pure"`). Both backends implement RFC 8032
and produce identical, interoperable signatures.
