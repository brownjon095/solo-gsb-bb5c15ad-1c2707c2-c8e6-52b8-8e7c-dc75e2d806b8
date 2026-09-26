"""Replay-based undo of the last effective adjudication batch.

Undo never reverse-edits the current working document. Instead the merge
is rebuilt from its *immutable initial conflict snapshot* (the original
three-way merge output with every conflict placeholder) by replaying the
merge order's audit events:

  * adjudication events (``resolutions`` / ``rule-resolutions`` /
    ``reuse-resolutions`` / ``draft-commit``) carry every pointer and
    decision their batch applied, so each one is convertible back into a
    resolution batch against the initial conflicts;
  * ``resolution-undo`` events are not adjudications — they mark, in their
    detail, the sequence number of the event they compensated, and replay
    skips both the undo and its compensated event.

Thus replaying the remaining adjudication events in sequence order yields
exactly the document and the open conflict set the merge had before the
undone batch. Re-resolution after an undo simply appends new events; the
chain (and the compensation marks) stay verifiable.

Everything here is pure and in-memory apart from the rows handed in by
``store``; the caller owns the single SQLite transaction.
"""

from __future__ import annotations

import copy
import json
from typing import Any

from .audit import (
    ADJUDICATION_METHODS,
    METHOD_DRAFT_COMMIT,
    METHOD_RESOLUTION_UNDO,
    METHOD_RESOLUTIONS,
    METHOD_REUSE_RESOLUTIONS,
    METHOD_RULE_RESOLUTIONS,
)
from .merge import parse_pointer, set_at
from .reuse import ACTION_DELETE

# Undo events carry the compensated event's sequence number here.
DETAIL_UNDONE_SEQ = "undone_seq"
# Restored pointers of the compensated batch, recorded on the undo event.
DETAIL_RESTORED_POINTERS = "restored_pointers"


class NothingToUndo(Exception):
    """The merge has no effective adjudication event that can be undone."""


class AuditReplayError(Exception):
    """Stored audit events are inconsistent with the initial snapshot.

    Defensive: the hash chain verification (GET .../audit) is the normal
    way to surface tampering; replay failing likewise aborts the undo so
    no state is ever written from an inconsistent history.
    """


def compensated_seqs(events: list) -> set[int]:
    """Sequence numbers of adjudication events already compensated.

    Undo events never compensate another undo, so these are always
    adjudication sequence numbers.
    """
    return {
        json.loads(row["detail"])[DETAIL_UNDONE_SEQ]
        for row in events
        if row["method"] == METHOD_RESOLUTION_UNDO
    }


def find_undo_target(events: list):
    """The most recent adjudication event not already compensated.

    Returns the row, or ``None`` when every adjudication (if any) has
    already been undone. Events are taken in stored (seq ASC) order.
    """
    undone = compensated_seqs(events)
    target = None
    for row in events:
        if row["method"] in ADJUDICATION_METHODS and row["seq"] not in undone:
            target = row
    return target


def _event_to_items(event_row, initial_conflict_by_pointer: dict) -> list[dict]:
    """Convert one stored adjudication event back into batch items.

    Batch items reuse the ``{pointer, choice, value, value_provided}``
    shape consumed at apply time: a ``choice`` selects a candidate of the
    *initial* conflict (a missing side deletes), while an explicit
    ``value`` overrides it.
    """
    method = event_row["method"]
    detail = json.loads(event_row["detail"])
    items: list[dict] = []
    for entry in detail.get("items", []):
        pointer = entry["pointer"]
        conflict = initial_conflict_by_pointer.get(pointer)
        if conflict is None:
            raise AuditReplayError(
                f"event seq {event_row['seq']} adjudicates pointer {pointer!r} "
                "absent from the initial conflict snapshot"
            )
        if method == METHOD_RESOLUTIONS or method == METHOD_DRAFT_COMMIT:
            if "choice" in entry and entry["choice"] is not None:
                item = {"pointer": pointer, "choice": entry["choice"]}
            elif "value" in entry:
                item = {
                    "pointer": pointer,
                    "choice": None,
                    "value": entry["value"],
                    "value_provided": True,
                }
            else:
                raise AuditReplayError(
                    f"event seq {event_row['seq']} item for {pointer!r} "
                    "records neither choice nor value"
                )
        elif method == METHOD_RULE_RESOLUTIONS:
            side = entry.get("side")
            if side not in ("left", "right"):
                raise AuditReplayError(
                    f"event seq {event_row['seq']} item for {pointer!r} "
                    f"has invalid side {side!r}"
                )
            item = {"pointer": pointer, "choice": side}
        elif method == METHOD_REUSE_RESOLUTIONS:
            action = entry.get("action")
            if action == ACTION_DELETE:
                # The reuse deletion was applied by choosing the side the
                # initial conflict records as missing; equivalence matching
                # guarantees the target has that same missing side.
                if not conflict["missing"]:
                    raise AuditReplayError(
                        f"event seq {event_row['seq']} item for {pointer!r} "
                        "records a deletion but no side is missing"
                    )
                item = {"pointer": pointer, "choice": conflict["missing"][0]}
            elif action == "set":
                item = {
                    "pointer": pointer,
                    "choice": None,
                    "value": entry.get("value"),
                    "value_provided": True,
                }
            else:
                raise AuditReplayError(
                    f"event seq {event_row['seq']} item for {pointer!r} "
                    f"has invalid reuse action {action!r}"
                )
        else:  # pragma: no cover - callers only pass adjudication events
            raise AuditReplayError(
                f"event seq {event_row['seq']} has non-adjudication method {method!r}"
            )
        items.append(item)
    return items


def _apply_replay_item(
    document: Any, open_by_pointer: dict[str, dict], item: dict
) -> Any:
    """Apply one replay item, closing its initial conflict.

    Mirrors the forward batch application: an adjudication closes a
    conflict, and conflicts are disjoint leaves, so replayed events can
    never touch a pointer another active event left open. Returns the
    (possibly replaced) document — a root-pointer (``""``) adjudication
    replaces the whole document rather than mutating it in place.
    """
    pointer = item["pointer"]
    conflict = open_by_pointer.pop(pointer, None)
    if conflict is None:
        raise AuditReplayError(
            f"pointer {pointer!r} is adjudicated more than once by active events"
        )
    tokens = parse_pointer(pointer)
    choice = item.get("choice")
    if choice is not None:
        if choice not in ("left", "right"):
            raise AuditReplayError(f"invalid choice {choice!r} for {pointer!r}")
        if choice in conflict["missing"]:
            # Honour the deletion: remove the key from the parent object.
            if not tokens:
                raise AuditReplayError("the document root cannot be deleted")
            parent = document
            for token in tokens[:-1]:
                parent = parent[token] if isinstance(parent, dict) else parent[int(token)]
            last = tokens[-1]
            if isinstance(parent, dict):
                parent.pop(last, None)
            else:
                del parent[int(last)]
        else:
            document = set_at(document, tokens, conflict[choice])
    elif item.get("value_provided"):
        document = set_at(document, tokens, item["value"])
    else:
        raise AuditReplayError(
            f"replay item for {pointer!r} needs 'choice' or 'value'"
        )
    return document


def rebuild_state(merge_row, events: list, skip_seq: int | None = None):
    """Rebuild (document, open_conflicts) by replaying the audit history.

    Starts from the immutable initial snapshot: the original three-way
    merge document (with every conflict placeholder) and the initial
    conflict list, both preserved on the merge row (and re-derived from
    base/left/right for rows predating the stored snapshot). Undo events
    and the adjudication events they compensate are skipped; ``skip_seq``
    additionally skips one adjudication (the one currently being undone).
    """
    initial_document, initial_conflicts = initial_snapshot(merge_row)
    initial_by_pointer = {c["pointer"]: c for c in initial_conflicts}

    document = copy.deepcopy(initial_document)
    open_by_pointer = copy.deepcopy(initial_by_pointer)

    undone = compensated_seqs(events)
    for row in events:
        if row["method"] not in ADJUDICATION_METHODS:
            # Undo (and any future non-adjudication) events carry no batch.
            continue
        if row["seq"] in undone or row["seq"] == skip_seq:
            continue
        for item in _event_to_items(row, initial_by_pointer):
            document = _apply_replay_item(document, open_by_pointer, item)

    open_conflicts = [c for c in initial_conflicts if c["pointer"] in open_by_pointer]
    return document, open_conflicts


def initial_snapshot(merge_row) -> tuple[Any, list[dict]]:
    """The immutable initial (document, conflicts) the replay starts from.

    New rows persist both explicitly; rows created before the snapshot was
    stored re-derive the identical outputs deterministically from the
    immutable base/left/right documents.
    """
    if merge_row["initial_document"] is not None:
        document = json.loads(merge_row["initial_document"])
    else:
        # Local import keeps merge3 out of this module's import surface for
        # callers that only need compensation bookkeeping.
        from .merge import merge3

        document, _ = merge3(
            json.loads(merge_row["base_doc"]),
            json.loads(merge_row["left_doc"]),
            json.loads(merge_row["right_doc"]),
        )
    # The conflict snapshot column has existed since before undo; fall back
    # to the live list only for rows predating even it.
    if merge_row["source_conflicts"] is not None:
        conflicts = json.loads(merge_row["source_conflicts"])
    else:
        conflicts = json.loads(merge_row["conflicts"])
    return document, conflicts
