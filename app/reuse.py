"""Reuse historical conflict resolutions from an already resolved merge.

A new (target) merge often re-runs conflicts that an older (source) merge
already adjudicated. This module matches every *open* conflict of the
target against the source merge's conflict snapshot, purely by content:

  * the JSON Pointer must be identical (conflicts are matched item by
    item at the same path);
  * ``base`` / ``left`` / ``right`` must be identical after
    canonicalization (sorted-key JSON, so object key order is irrelevant
    but JSON types stay strict: ``1`` differs from ``true``);
  * the ``missing`` semantics must be identical — a key that was absent
    on one side is *not* the same as an explicit JSON ``null``.

For every match the final value is read from the source's immutable
result: the value found at the pointer is set in the target, while a
pointer that no longer exists in the source result means the source
chose to honour a deletion and the target key is deleted as well.

Everything here is pure and in-memory; the caller decides whether the
outcome is persisted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .merge import get_at, parse_pointer
from .patch import canonical_json

# Per-item skip reason codes returned in the response's ``skipped`` list.
REASON_NO_SOURCE_CONFLICT = "no_source_conflict"
REASON_CONFLICT_MISMATCH = "conflict_mismatch"
REASON_SOURCE_ROOT_DELETED = "source_root_deleted"

ACTION_SET = "set"
ACTION_DELETE = "delete"


@dataclass(frozen=True)
class ReuseMatch:
    """One reusable resolution selected for a target conflict."""

    pointer: str
    tokens: tuple[str, ...]
    action: str  # ACTION_SET | ACTION_DELETE
    value: Any = None


def _skip(pointer: str, reason: str, detail: str, **extra: Any) -> dict:
    entry = {"pointer": pointer, "reason": reason, "detail": detail}
    entry.update(extra)
    return entry


def conflicts_equivalent(target: dict, source: dict) -> tuple[bool, list[str]]:
    """Whether two conflicts share identical resolution semantics.

    Compares the canonicalized ``base`` / ``left`` / ``right`` values and
    the ``missing`` side list. Returns (equal, mismatched_fields); the
    field list is empty when the conflicts are equivalent.
    """
    mismatched: list[str] = []
    for field in ("base", "left", "right"):
        # Canonical bytes compare JSON-exactly: key order is normalized,
        # while null/absent is handled separately via ``missing`` and
        # distinct JSON types (1 vs true) never compare equal.
        if canonical_json(target.get(field)) != canonical_json(source.get(field)):
            mismatched.append(field)
    if sorted(target.get("missing", [])) != sorted(source.get("missing", [])):
        mismatched.append("missing")
    return not mismatched, mismatched


def read_source_resolution(source_result: Any, pointer: str) -> ReuseMatch | None:
    """Read the source's final adjudication at ``pointer``.

    A value present in the result becomes an explicit ``set``; a path
    absent from the result means the source honoured a deletion and
    becomes ``delete``. The document root cannot be deleted, so a missing
    root returns ``None`` for the caller to skip.
    """
    tokens = tuple(parse_pointer(pointer))
    try:
        value = get_at(source_result, list(tokens))
    except (KeyError, IndexError, TypeError, ValueError):
        if not tokens:
            return None
        return ReuseMatch(pointer, tokens, ACTION_DELETE)
    return ReuseMatch(pointer, tokens, ACTION_SET, value)


def build_reuse_plan(
    target_conflicts: list[dict],
    source_conflicts: list[dict],
    source_result: Any,
) -> tuple[list[ReuseMatch], list[dict]]:
    """Match each target conflict and derive the reuse batch.

    Returns (matches, skipped) in the target's conflict order; every
    target conflict appears in exactly one of the two lists.
    """
    source_by_pointer = {c["pointer"]: c for c in source_conflicts}
    matches: list[ReuseMatch] = []
    skipped: list[dict] = []
    for target in target_conflicts:
        pointer = target["pointer"]
        source = source_by_pointer.get(pointer)
        if source is None:
            skipped.append(
                _skip(
                    pointer,
                    REASON_NO_SOURCE_CONFLICT,
                    "the source merge has no recorded conflict at this pointer",
                )
            )
            continue
        equal, mismatched = conflicts_equivalent(target, source)
        if not equal:
            skipped.append(
                _skip(
                    pointer,
                    REASON_CONFLICT_MISMATCH,
                    "base/left/right or missing semantics differ after normalization",
                    mismatched_fields=mismatched,
                )
            )
            continue
        match = read_source_resolution(source_result, pointer)
        if match is None:
            skipped.append(
                _skip(
                    pointer,
                    REASON_SOURCE_ROOT_DELETED,
                    "the source result has no document root and the root cannot be deleted",
                )
            )
            continue
        matches.append(match)
    return matches, skipped


def match_to_batch_item(match: ReuseMatch, target_conflict: dict) -> dict:
    """Convert a planned match into an ``_apply_batch`` resolution item.

    A deletion is expressed by choosing the side recorded as missing on
    the target conflict: identity matching guarantees the same side is
    missing on both conflicts, so the existing missing-side (honour the
    deletion) semantics apply unchanged.
    """
    if match.action == ACTION_DELETE:
        return {
            "pointer": match.pointer,
            "choice": target_conflict["missing"][0],
            "value": None,
            "value_provided": False,
        }
    return {
        "pointer": match.pointer,
        "choice": None,
        "value": match.value,
        "value_provided": True,
    }


def match_to_view(match: ReuseMatch) -> dict:
    """Serialize an applied match for the API response."""
    if match.action == ACTION_DELETE:
        return {"pointer": match.pointer, "action": ACTION_DELETE}
    return {"pointer": match.pointer, "action": ACTION_SET, "value": match.value}
