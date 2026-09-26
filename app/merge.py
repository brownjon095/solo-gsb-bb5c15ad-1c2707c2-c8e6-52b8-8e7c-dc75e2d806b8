"""Recursive three-way merge over JSON documents.

Merge rules (applied per JSON Pointer path):
  * only one side changed            -> adopt that side automatically
  * both sides changed identically   -> adopt the shared value
  * both sides changed differently   -> objects recurse key-by-key;
                                        anything else becomes a conflict
  * delete vs. unchanged             -> delete
  * delete vs. modify                -> conflict

Conflict locations keep a placeholder in the working document and both
candidates are returned to the client.
"""

from __future__ import annotations

from typing import Any

MISSING = object()  # sentinel: key absent on that side

PLACEHOLDER_KEY = "__merge_conflict__"


def placeholder(pointer: str) -> dict:
    """Marker left in the working document at a conflict location."""
    return {PLACEHOLDER_KEY: pointer}


def escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def unescape(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


def parse_pointer(pointer: str) -> list[str]:
    """Parse an RFC 6901 JSON Pointer into tokens. '' -> root."""
    if pointer == "":
        return []
    if not pointer.startswith("/"):
        raise ValueError(f"invalid JSON pointer: {pointer!r}")
    return [unescape(tok) for tok in pointer.split("/")[1:]]


def _eq(a: Any, b: Any) -> bool:
    if a is MISSING or b is MISSING:
        return a is MISSING and b is MISSING
    return a == b


def merge3(base: Any, left: Any, right: Any) -> tuple[Any, list[dict]]:
    """Merge left/right against base.

    Returns (document, conflicts): the working document with placeholders
    at conflict paths, and the conflict list with both candidates.
    """
    conflicts: list[dict] = []
    doc = _merge(base, left, right, "", conflicts)
    # The recursion visits object keys through a set union; sort on the way
    # out so conflict order (and thus snapshot/reuse order) is deterministic
    # regardless of Python's hash-seeded set iteration.
    conflicts.sort(key=lambda c: c["pointer"])
    return doc, conflicts


def _merge(base: Any, left: Any, right: Any, path: str, conflicts: list[dict]) -> Any:
    # Both sides agree: neither changed, or both made the same change.
    if _eq(left, right):
        return left
    # Only right changed.
    if _eq(left, base):
        return right
    # Only left changed.
    if _eq(right, base):
        return left
    # Both changed differently: recurse into objects, conflict otherwise.
    if isinstance(left, dict) and isinstance(right, dict):
        base_obj = base if isinstance(base, dict) else {}
        out: dict[str, Any] = {}
        for key in base_obj.keys() | left.keys() | right.keys():
            value = _merge(
                base_obj.get(key, MISSING),
                left.get(key, MISSING),
                right.get(key, MISSING),
                path + "/" + escape(key),
                conflicts,
            )
            if value is not MISSING:
                out[key] = value
        return out
    conflicts.append(
        {
            "pointer": path,
            "base": None if base is MISSING else base,
            "left": None if left is MISSING else left,
            "right": None if right is MISSING else right,
            "missing": [
                name
                for name, v in (("base", base), ("left", left), ("right", right))
                if v is MISSING
            ],
        }
    )
    return placeholder(path)


def get_at(doc: Any, tokens: list[str]) -> Any:
    node = doc
    for tok in tokens:
        if isinstance(node, dict):
            node = node[tok]
        elif isinstance(node, list):
            node = node[int(tok)]
        else:
            raise KeyError(tok)
    return node


def set_at(doc: Any, tokens: list[str], value: Any) -> Any:
    """Set value at pointer path, returning the (possibly new) document."""
    if not tokens:
        return value
    parent = get_at(doc, tokens[:-1])
    last = tokens[-1]
    if isinstance(parent, dict):
        parent[last] = value
    elif isinstance(parent, list):
        parent[int(last)] = value
    else:
        raise KeyError(last)
    return doc


def delete_at(doc: Any, tokens: list[str]) -> Any:
    """Remove the key at pointer path (used when a missing side is chosen)."""
    if not tokens:
        raise ValueError("cannot delete the document root")
    parent = get_at(doc, tokens[:-1])
    last = tokens[-1]
    if isinstance(parent, dict):
        parent.pop(last, None)
    elif isinstance(parent, list):
        del parent[int(last)]
    else:
        raise KeyError(last)
    return doc
