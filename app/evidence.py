"""Portable evidence packages for resolved merge orders.

An evidence package captures everything needed to re-check a resolved
merge order's outcome anywhere, without access to the service's database:

* ``base`` — the original common-ancestor document;
* ``result`` — the immutable final document;
* ``patch`` — the deterministic RFC 6902 patch from ``base`` to ``result``
  (the same one ``GET /merges/{merge_id}/patch`` returns);
* ``audit`` — the merge order's complete adjudication audit events,
  carrying their ``prev_hash``/``hash`` chain links;
* ``generated_at`` — the package generation time (UTC ISO 8601).

The payload is signed with HMAC-SHA256 over its canonical JSON (sorted
keys, no whitespace — the same serialization used for document digests
and audit hashes). The signing key comes from the ``EVIDENCE_HMAC_KEY``
environment variable; the application refuses to start when the variable
is missing or shorter than ``MIN_KEY_BYTES`` bytes, so a running service
always has a key.

Verification (``verify_package``) is pure: it never touches the database.
It runs a fixed sequence of stages and reports the *first* failing one:

1. ``signature`` — the HMAC is recomputed over the payload's canonical
   JSON and compared to the packaged signature in constant time;
2. ``audit_chain`` — the packaged events are re-hashed and checked for
   sequence and prev-hash continuity, exactly like the stored chain;
3. ``patch_replay`` — the patch is actually applied to ``base`` and the
   replayed document must equal the packaged ``result`` (JSON-exact).
"""

from __future__ import annotations

import hashlib
import hmac
import os
from datetime import datetime, timezone
from typing import Any

from . import audit
from .patch import (
    PatchApplyError,
    apply_patch,
    canonical_json,
    generate_patch,
    json_equal,
)

# Environment variable holding the HMAC key, and its minimum length.
KEY_ENV_VAR = "EVIDENCE_HMAC_KEY"
MIN_KEY_BYTES = 32

# Payload format marker; bump when the payload shape changes.
EVIDENCE_FORMAT = "merge-evidence/1"

# Verification stages, in the fixed order they are checked.
STAGE_SIGNATURE = "signature"
STAGE_AUDIT_CHAIN = "audit_chain"
STAGE_PATCH_REPLAY = "patch_replay"


class EvidenceKeyError(RuntimeError):
    """The evidence signing key is missing or too short."""


def get_signing_key() -> bytes:
    """Return the configured HMAC key, or raise EvidenceKeyError.

    Called once at application startup (a missing/short key fails the
    startup) and again whenever a package is signed or verified.
    """
    raw = os.environ.get(KEY_ENV_VAR)
    if not raw:
        raise EvidenceKeyError(
            f"{KEY_ENV_VAR} is not set: evidence packages cannot be signed. "
            f"Configure a secret of at least {MIN_KEY_BYTES} bytes."
        )
    key = raw.encode("utf-8")
    if len(key) < MIN_KEY_BYTES:
        raise EvidenceKeyError(
            f"{KEY_ENV_VAR} is too short: {len(key)} bytes, need at least "
            f"{MIN_KEY_BYTES}. Configure a longer secret."
        )
    return key


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sign_payload(payload: dict) -> str:
    """HMAC-SHA256 hex digest of the payload's canonical serialization."""
    return hmac.new(
        get_signing_key(), canonical_json(payload), hashlib.sha256
    ).hexdigest()


def build_package(
    merge_id: str, base: Any, result: Any, events: list[dict]
) -> dict:
    """Assemble and sign the evidence package for a resolved merge order.

    ``events`` must be the merge order's complete audit history in
    sequence order (see ``audit.list_event_dicts``); it is embedded
    verbatim so the chain can be re-verified from the package alone.
    """
    payload = {
        "format": EVIDENCE_FORMAT,
        "merge_id": merge_id,
        "generated_at": _now(),
        "base": base,
        "result": result,
        "patch": generate_patch(base, result),
        "audit": list(events),
    }
    return {"payload": payload, "signature": sign_payload(payload)}


def _failure(stage: str, reason: str) -> dict:
    return {"valid": False, "failed_stage": stage, "reason": reason}


def verify_package(package: dict) -> dict:
    """Verify an evidence package without any database access.

    ``package`` is the exported ``{"payload": ..., "signature": ...}``
    document (already schema-validated by the request model). The stages
    run in a fixed order — signature, audit chain, patch replay — and the
    response names the first failing stage together with its reason; a
    package that passes every stage yields ``{"valid": True}``.
    """
    payload = package["payload"]
    signature = package["signature"]

    # Stage 1: the signature must match the payload exactly. The
    # comparison is constant-time so it leaks nothing about the expected
    # MAC; any payload tampering or foreign key fails here first.
    expected = sign_payload(payload)
    if not hmac.compare_digest(expected, signature):
        return _failure(
            STAGE_SIGNATURE,
            "signature mismatch: the payload was tampered with or the "
            "package was signed with a different key",
        )

    # Stage 2: the embedded audit events must form one continuous chain
    # (sequence numbers, prev-hash linkage, recomputed event hashes).
    first_invalid_seq = audit.verify_event_dicts(
        payload["merge_id"], payload["audit"]
    )
    if first_invalid_seq is not None:
        return _failure(
            STAGE_AUDIT_CHAIN,
            "audit chain does not verify: first anomalous event at seq "
            f"{first_invalid_seq}",
        )

    # Stage 3: replay the patch on the packaged base; the replayed
    # document must be JSON-exactly the packaged immutable result.
    try:
        replayed = apply_patch(payload["base"], payload["patch"])
    except PatchApplyError as exc:
        return _failure(
            STAGE_PATCH_REPLAY,
            f"patch does not apply to base: {exc}",
        )
    if not json_equal(replayed, payload["result"]):
        return _failure(
            STAGE_PATCH_REPLAY,
            "replaying the patch on base does not yield the packaged result",
        )

    return {"valid": True, "failed_stage": None, "reason": None}
