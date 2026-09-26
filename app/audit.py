"""Conflict adjudication audit trail with a per-merge SHA-256 hash chain.

Every successful state-changing adjudication — an explicit ``resolutions``
batch, a ``rule-resolutions`` run, a ``reuse-resolutions`` run, a draft
``commit`` or a ``resolution-undo`` — appends exactly one event to the
merge order's audit trail, inside the *same* SQLite transaction as the
merge update itself, so the event and the state change it describes commit
or roll back together. Failed requests (400/409/422) and no-op requests (a
rule run or reuse run that matched nothing, an undo with nothing to undo)
change no state and therefore leave no event.

Each event records the adjudication method, the revision before/after and
the JSON Pointers that actually took effect, plus method-specific detail
(the per-pointer decision, the reuse source, the committed draft). Events
form a hash chain per merge order: ``hash`` is the SHA-256 of the event's
canonical JSON (sorted keys, no whitespace — the same serialization used
for document digests), and the payload includes the previous event's
``hash``, so altering or deleting a recorded event breaks the chain at a
well-defined position. The first event of every merge order chains from
the genesis prev-hash of 64 zero characters.

``GET /merges/{merge_id}/audit`` replays the stored chain: every event is
re-hashed from its stored fields and checked for sequence and prev-hash
continuity; the response reports the chain verdict and, when the chain is
broken, the sequence number of the first anomalous event.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from . import db
from .patch import canonical_json

# Adjudication methods recorded on audit events.
METHOD_RESOLUTIONS = "resolutions"
METHOD_RULE_RESOLUTIONS = "rule-resolutions"
METHOD_REUSE_RESOLUTIONS = "reuse-resolutions"
METHOD_DRAFT_COMMIT = "draft-commit"
# An undo event compensates exactly one of the adjudication methods above;
# it never compensates another undo.
METHOD_RESOLUTION_UNDO = "resolution-undo"

METHODS = (
    METHOD_RESOLUTIONS,
    METHOD_RULE_RESOLUTIONS,
    METHOD_REUSE_RESOLUTIONS,
    METHOD_DRAFT_COMMIT,
    METHOD_RESOLUTION_UNDO,
)

# Adjudication methods whose batches can be replayed (and therefore undone).
ADJUDICATION_METHODS = (
    METHOD_RESOLUTIONS,
    METHOD_RULE_RESOLUTIONS,
    METHOD_REUSE_RESOLUTIONS,
    METHOD_DRAFT_COMMIT,
)

# Prev-hash carried by the first event of every merge order's chain.
GENESIS_PREV_HASH = "0" * 64


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _payload(
    merge_id: str,
    seq: int,
    method: str,
    revision_before: int,
    revision_after: int,
    pointers: list,
    detail: dict,
    prev_hash: str,
    created_at: str,
) -> dict:
    """The canonical-JSON payload an event's hash commits to."""
    return {
        "merge_id": merge_id,
        "seq": seq,
        "method": method,
        "revision_before": revision_before,
        "revision_after": revision_after,
        "pointers": pointers,
        "detail": detail,
        "prev_hash": prev_hash,
        "created_at": created_at,
    }


def compute_hash(payload: dict) -> str:
    """SHA-256 hex digest of an event payload's canonical serialization."""
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def record_event(
    conn,
    merge_id: str,
    method: str,
    revision_before: int,
    revision_after: int,
    pointers: list,
    detail: dict,
) -> None:
    """Append one event to the merge order's chain.

    Must be called inside the caller's ``db.transaction`` block: the
    INSERT commits or rolls back together with the merge update, so a
    failed request can never leave a dangling event. The sequence number
    and prev-hash continue the merge order's existing chain (or start it
    from the genesis prev-hash).
    """
    last = conn.execute(
        "SELECT seq, hash FROM merge_audit_events WHERE merge_id = ? "
        "ORDER BY seq DESC LIMIT 1",
        (merge_id,),
    ).fetchone()
    if last is None:
        seq, prev_hash = 1, GENESIS_PREV_HASH
    else:
        seq, prev_hash = last["seq"] + 1, last["hash"]
    created_at = _now()
    pointers = list(pointers)
    payload = _payload(
        merge_id,
        seq,
        method,
        revision_before,
        revision_after,
        pointers,
        detail,
        prev_hash,
        created_at,
    )
    conn.execute(
        """
        INSERT INTO merge_audit_events (
            merge_id, seq, method, revision_before, revision_after,
            pointers, detail, prev_hash, hash, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            merge_id,
            seq,
            method,
            revision_before,
            revision_after,
            json.dumps(pointers),
            json.dumps(detail),
            prev_hash,
            compute_hash(payload),
            created_at,
        ),
    )


def _raw_event(row) -> dict:
    """The event exactly as stored, without any verification artifact."""
    return {
        "seq": row["seq"],
        "method": row["method"],
        "revision_before": row["revision_before"],
        "revision_after": row["revision_after"],
        "pointers": json.loads(row["pointers"]),
        "detail": json.loads(row["detail"]),
        "prev_hash": row["prev_hash"],
        "hash": row["hash"],
        "created_at": row["created_at"],
    }


def _row_to_event(row, valid: bool) -> dict:
    event = _raw_event(row)
    event["valid"] = valid
    return event


def list_event_dicts(merge_id: str) -> list[dict]:
    """All of the merge order's events as plain dicts, in sequence order.

    Used to embed the complete adjudication history into an exported
    evidence package; the events carry their stored ``prev_hash``/``hash``
    so the chain can be re-verified later without database access.
    """
    rows = db.query_all(
        "SELECT * FROM merge_audit_events WHERE merge_id = ? ORDER BY seq ASC",
        (merge_id,),
    )
    return [_raw_event(row) for row in rows]


def verify_event_dicts(merge_id: str, events: list[dict]) -> int | None:
    """Verify a chain of event dicts; return the first anomalous seq or None.

    Pure: no database access, so an exported evidence package's audit
    history can be verified anywhere. Every event is re-hashed from its
    own fields and checked for sequence continuity and prev-hash linkage
    against its predecessor (the first event must chain from the genesis
    prev-hash), exactly like the stored-chain verification.
    """
    expected_seq = 1
    prev_hash = GENESIS_PREV_HASH
    for event in events:
        payload = _payload(
            merge_id,
            event["seq"],
            event["method"],
            event["revision_before"],
            event["revision_after"],
            event["pointers"],
            event["detail"],
            event["prev_hash"],
            event["created_at"],
        )
        if (
            event["seq"] != expected_seq
            or event["prev_hash"] != prev_hash
            or compute_hash(payload) != event["hash"]
        ):
            return event["seq"]
        # Walk the *carried* values, so one anomalous event does not mask
        # the integrity of its untouched successors.
        expected_seq = event["seq"] + 1
        prev_hash = event["hash"]
    return None


def _verify_rows(rows) -> tuple[int | None, dict[int, bool]]:
    """Re-hash stored rows; return (first_invalid_seq, {seq: valid}).

    Shared by the trail endpoint and by operations (resolution undo) that
    must only rely on a chain they can verify: every event is re-hashed
    from its own fields and checked for sequence continuity and prev-hash
    linkage against its predecessor (the first event must chain from the
    genesis prev-hash).
    """
    first_invalid_seq: int | None = None
    valid_by_seq: dict[int, bool] = {}
    expected_seq = 1
    prev_hash = GENESIS_PREV_HASH
    for row in rows:
        payload = _payload(
            row["merge_id"],
            row["seq"],
            row["method"],
            row["revision_before"],
            row["revision_after"],
            json.loads(row["pointers"]),
            json.loads(row["detail"]),
            row["prev_hash"],
            row["created_at"],
        )
        valid = (
            row["seq"] == expected_seq
            and row["prev_hash"] == prev_hash
            and compute_hash(payload) == row["hash"]
        )
        valid_by_seq[row["seq"]] = valid
        if not valid and first_invalid_seq is None:
            first_invalid_seq = row["seq"]
        # The chain walks the *stored* values, so one tampered event does
        # not mask the integrity of its untouched successors.
        expected_seq = row["seq"] + 1
        prev_hash = row["hash"]
    return first_invalid_seq, valid_by_seq


def verify_chain(conn, merge_id: str) -> bool:
    """Verify the merge order's stored chain using the caller's connection.

    Safe to call inside a ``db.transaction`` block (it runs on the given
    connection rather than opening another one). Resolution undo rebuilds
    state from the stored history, so it refuses to proceed when the chain
    cannot be verified.
    """
    rows = conn.execute(
        "SELECT * FROM merge_audit_events WHERE merge_id = ? ORDER BY seq ASC",
        (merge_id,),
    ).fetchall()
    first_invalid_seq, _ = _verify_rows(rows)
    return first_invalid_seq is None


def get_audit_trail(merge_id: str) -> dict:
    """Return the merge order's events in sequence plus the chain verdict.

    Every stored event is re-hashed from its own fields and checked for
    sequence continuity and prev-hash linkage against its predecessor (the
    first event must chain from the genesis prev-hash). ``chain_valid`` is
    true only when every event passes all checks; ``first_invalid_seq`` is
    the sequence number of the first anomalous event, or ``None`` when the
    whole chain verifies.
    """
    rows = db.query_all(
        "SELECT * FROM merge_audit_events WHERE merge_id = ? ORDER BY seq ASC",
        (merge_id,),
    )
    first_invalid_seq, valid_by_seq = _verify_rows(rows)
    events = [_row_to_event(row, valid_by_seq[row["seq"]]) for row in rows]
    return {
        "merge_id": merge_id,
        "event_count": len(events),
        "events": events,
        "chain_valid": first_invalid_seq is None,
        "first_invalid_seq": first_invalid_seq,
    }
