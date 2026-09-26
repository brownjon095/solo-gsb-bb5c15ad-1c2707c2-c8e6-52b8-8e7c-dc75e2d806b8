"""Shared test setup.

The application refuses to start without an evidence-package signing key
(``EVIDENCE_HMAC_KEY``, min 32 bytes), so every test module gets a
default test key here. conftest is imported before any test module, and
``setdefault`` lets a module override it with its own key when a test
needs to recompute signatures independently.
"""

import os

os.environ.setdefault(
    "EVIDENCE_HMAC_KEY", "test-evidence-key-0123456789abcdef0123456789abcdef"
)
