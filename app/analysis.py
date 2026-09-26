"""Pre-merge impact analysis: what would creating a merge order change?

Given the same ``base`` / ``left`` / ``right`` triple as ``POST /merges``,
walk the three documents jointly by JSON Pointer and emit one change
record per *minimal* changed path, classified as:

  * ``left_only``   - only the left side moved away from base
  * ``right_only``  - only the right side moved away from base
  * ``same_change`` - both sides made the identical change
  * ``conflict``    - both sides changed differently and the engine would
                      leave a conflict placeholder at the path

Records keep the three candidates plus a ``missing`` list, so an absent
key is never confused with an explicit JSON ``null`` (they compare
differently just like everywhere else in the service).

Granularity follows the merge engine in :mod:`app.merge`:

  * object subtrees both sides changed differently are recursed
    key-by-key, so changes are reported at leaf paths;
  * a whole subtree added/deleted on one side, or a type replacement
    (object <-> scalar/array), is a *single* record on the subtree root -
    the walk never descends into the subtree, so its leaves are not
    counted again;
  * classification and therefore ``would_conflict`` are computed with the
    exact same decisions as :func:`app.merge.merge3`, hence the flag
    predicts whether ``POST /merges`` would create a pending order.

Everything here is pure and in-memory; nothing is ever persisted and the
same triple always yields the same, pointer-sorted response.
"""

from __future__ import annotations

from typing import Any

from .merge import MISSING, escape

LEFT_ONLY = "left_only"
RIGHT_ONLY = "right_only"
SAME_CHANGE = "same_change"
CONFLICT = "conflict"

CATEGORIES = (LEFT_ONLY, RIGHT_ONLY, SAME_CHANGE, CONFLICT)


def _same(a: Any, b: Any) -> bool:
    """MISSING-aware equality, deliberately mirroring ``app.merge._eq``.

    The loose ``==`` (so ``1`` and ``True`` are not distinguished) keeps
    every classification decision identical to the merge engine's; an
    absent side (``MISSING``) never equals a present value, including an
    explicit ``None``.
    """
    if a is MISSING or b is MISSING:
        return a is MISSING and b is MISSING
    return a == b


def _record(kind: str, path: str, base: Any, left: Any, right: Any) -> dict:
    """Build one change record; missing sides are null plus a name in ``missing``."""
    return {
        "pointer": path,
        "type": kind,
        "base": None if base is MISSING else base,
        "left": None if left is MISSING else left,
        "right": None if right is MISSING else right,
        "missing": [
            name
            for name, value in (("base", base), ("left", left), ("right", right))
            if value is MISSING
        ],
    }


def _walk_one_side(
    kind: str, base: Any, left: Any, right: Any, path: str, changes: list[dict]
) -> None:
    """Expand a change made on exactly one side into minimal records.

    At entry the other side still equals base. When the unchanged side and
    the changed side are both objects, descend key-by-key so individual
    edits are reported at leaf paths. Any other transition - a whole
    subtree added (base absent), a whole subtree deleted (changed side
    absent) or a type replacement - is one record on the current path,
    which stops the walk from double-counting the subtree below.
    """
    if _same(left, right):
        # Child key present but equal on both sides: no change here.
        return
    changed = left if kind == LEFT_ONLY else right
    if isinstance(base, dict) and isinstance(changed, dict):
        for key in sorted(base.keys() | changed.keys()):
            child = path + "/" + escape(key)
            _walk_one_side(
                kind,
                base.get(key, MISSING),
                left.get(key, MISSING),
                right.get(key, MISSING),
                child,
                changes,
            )
        return
    changes.append(_record(kind, path, base, left, right))


def _walk(base: Any, left: Any, right: Any, path: str, changes: list[dict]) -> None:
    # Both sides agree: no change, or both made the very same change.
    if _same(left, right):
        if _same(left, base):
            return
        # Identical change on both sides. An identical edit inside an
        # existing object is expanded to leaf paths; an added/deleted whole
        # subtree or a type replacement is a single minimal-root record.
        if isinstance(left, dict) and isinstance(base, dict):
            for key in sorted(base.keys() | left.keys()):
                child = path + "/" + escape(key)
                _walk(
                    base.get(key, MISSING),
                    left.get(key, MISSING),
                    right.get(key, MISSING),
                    child,
                    changes,
                )
            return
        changes.append(_record(SAME_CHANGE, path, base, left, right))
        return
    # Only one side changed: expand it into leaf/minimal records.
    if _same(left, base):
        _walk_one_side(RIGHT_ONLY, base, left, right, path, changes)
        return
    if _same(right, base):
        _walk_one_side(LEFT_ONLY, base, left, right, path, changes)
        return
    # Both sides changed differently: objects recurse (same key union and
    # MISSING handling as app.merge.merge3), anything else conflicts.
    if isinstance(left, dict) and isinstance(right, dict):
        base_obj = base if isinstance(base, dict) else {}
        for key in sorted(base_obj.keys() | left.keys() | right.keys()):
            child = path + "/" + escape(key)
            _walk(
                base_obj.get(key, MISSING),
                left.get(key, MISSING),
                right.get(key, MISSING),
                child,
                changes,
            )
        return
    changes.append(_record(CONFLICT, path, base, left, right))


def analyze_merge(base: Any, left: Any, right: Any) -> dict:
    """Return the sorted change records and per-category counts.

    Pure: the same inputs always produce the same output and no storage is
    touched. ``would_conflict`` is true exactly when the triple would open
    at least one conflict in ``POST /merges`` (i.e. create a pending order).
    """
    changes: list[dict] = []
    _walk(base, left, right, "", changes)
    # The recursion already visits object keys in sorted order, but sort on
    # the way out as well so output order is guaranteed regardless of the
    # walk shape (and identical to the engine's conflict ordering).
    changes.sort(key=lambda change: change["pointer"])

    counts = {category: 0 for category in CATEGORIES}
    for change in changes:
        counts[change["type"]] += 1
    return {
        "changes": changes,
        "counts": {
            "left_only": counts[LEFT_ONLY],
            "right_only": counts[RIGHT_ONLY],
            "same_change": counts[SAME_CHANGE],
            "conflict": counts[CONFLICT],
            "total": len(changes),
        },
        "would_conflict": counts[CONFLICT] > 0,
    }
