"""JouleX — stdlib-only, paper-only.

The vendored CF-KYA snapshot lives in ``joulex/vendor/`` as top-level
modules (``import kya``, ``from delegation import ...``), exactly as in
the firewall repo. This package puts that directory on ``sys.path`` at
import time so the vendored modules import unchanged. See
``joulex/vendor/VENDOR.md`` for provenance and hashes.
"""

from __future__ import annotations

import os
import sys

_VENDOR_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor")
if _VENDOR_DIR not in sys.path:
    sys.path.insert(0, _VENDOR_DIR)

__version__ = "0.1.0"
