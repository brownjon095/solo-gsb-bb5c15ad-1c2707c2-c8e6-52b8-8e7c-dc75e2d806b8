"""Deterministic RFC 6902 JSON Patch generation between two JSON documents.

Diff rules:
  * objects recurse key-by-key, emitting add/remove/replace at leaf paths
  * arrays are opaque: a changed array yields a single "replace" op carrying
    the full final array, never per-index ops
  * any other type change at one path is a single "replace"

Paths are RFC 6901 JSON Pointers ("~" -> "~0", "/" -> "~1") and the op list
is sorted by path lexicographically. Every touched path is disjoint from the
others, so applying the ops in that order to the base document yields the
result exactly.
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from .merge import escape, parse_pointer


def generate_patch(base: Any, result: Any) -> list[dict]:
    """Build the deterministic JSON Patch (RFC 6902) from base to result."""
    ops: list[dict] = []
    _diff(base, result, "", ops)
    ops.sort(key=lambda op: op["path"])
    return ops


def _diff(base: Any, result: Any, path: str, ops: list[dict]) -> None:
    if json_equal(base, result):
        return
    if isinstance(base, dict) and isinstance(result, dict):
        for key in sorted(base.keys() | result.keys()):
            child = path + "/" + escape(key)
            if key not in result:
                ops.append({"op": "remove", "path": child})
            elif key not in base:
                ops.append({"op": "add", "path": child, "value": result[key]})
            else:
                _diff(base[key], result[key], child, ops)
        return
    # Scalars, arrays (opaque -> whole-array replace) and type changes.
    # At the root (path == "") RFC 6901 "" names the whole document, so a
    # root-level "replace" is likewise valid.
    ops.append({"op": "replace", "path": path, "value": result})


def json_equal(a: Any, b: Any) -> bool:
    """Deep equality with JSON-exact types.

    Unlike Python's ``==`` this distinguishes ``True`` from ``1`` and ``1``
    from ``1.0`` (and the same distinction inside arrays), so such changes
    are never silently dropped from the patch.
    """
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(json_equal(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(
            json_equal(x, y) for x, y in zip(a, b)
        )
    return a == b


class PatchApplyError(Exception):
    """A patch operation could not be applied to the document."""


def apply_patch(document: Any, patch: list[dict]) -> Any:
    """Apply an RFC 6902 patch (add/remove/replace only) to a document.

    Strict: every operation must address an existing parent (and, for
    ``remove``/``replace``, an existing target); anything else raises
    PatchApplyError. The input document is not mutated — the patched copy
    is returned. Used to replay an evidence package's patch on its base.
    """
    doc = copy.deepcopy(document)
    for index, op in enumerate(patch):
        doc = _apply_op(doc, op, index)
    return doc


def _apply_op(doc: Any, op: Any, index: int) -> Any:
    if not isinstance(op, dict):
        raise PatchApplyError(f"op {index}: not an object")
    kind = op.get("op")
    path = op.get("path")
    if kind not in ("add", "remove", "replace") or not isinstance(path, str):
        raise PatchApplyError(f"op {index}: missing or unsupported op/path")
    if kind in ("add", "replace") and "value" not in op:
        raise PatchApplyError(f"op {index}: {kind!r} op without a value")
    try:
        tokens = parse_pointer(path)
    except ValueError:
        raise PatchApplyError(f"op {index}: invalid JSON pointer {path!r}")
    if not tokens:
        # "" names the whole document; the root itself cannot be removed.
        if kind == "remove":
            raise PatchApplyError(f"op {index}: cannot remove the document root")
        return op["value"]
    parent = doc
    for token in tokens[:-1]:
        parent = _descend(parent, token, index, path)
    last = tokens[-1]
    if isinstance(parent, dict):
        if kind == "add":
            parent[last] = op["value"]
        elif kind == "replace":
            if last not in parent:
                raise PatchApplyError(f"op {index}: no value at {path!r}")
            parent[last] = op["value"]
        else:
            if last not in parent:
                raise PatchApplyError(f"op {index}: no value at {path!r}")
            del parent[last]
        return doc
    if isinstance(parent, list):
        if kind == "add" and last == "-":
            parent.append(op["value"])
            return doc
        try:
            position = int(last)
        except ValueError:
            raise PatchApplyError(f"op {index}: invalid array index in {path!r}")
        if kind == "add":
            if not 0 <= position <= len(parent):
                raise PatchApplyError(f"op {index}: array index out of bounds")
            parent.insert(position, op["value"])
        elif kind == "replace":
            if not 0 <= position < len(parent):
                raise PatchApplyError(f"op {index}: array index out of bounds")
            parent[position] = op["value"]
        else:
            if not 0 <= position < len(parent):
                raise PatchApplyError(f"op {index}: array index out of bounds")
            del parent[position]
        return doc
    raise PatchApplyError(f"op {index}: cannot address {path!r}")


def _descend(node: Any, token: str, index: int, path: str) -> Any:
    if isinstance(node, dict):
        if token not in node:
            raise PatchApplyError(f"op {index}: no value at {path!r}")
        return node[token]
    if isinstance(node, list):
        try:
            position = int(token)
        except ValueError:
            raise PatchApplyError(f"op {index}: invalid array index in {path!r}")
        if not 0 <= position < len(node):
            raise PatchApplyError(f"op {index}: array index out of bounds")
        return node[position]
    raise PatchApplyError(f"op {index}: cannot address {path!r}")


def canonical_json(value: Any) -> bytes:
    """Canonical serialization used for content hashing.

    Keys are sorted and whitespace is stripped, so semantically identical
    documents hash identically regardless of key order.
    """
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def sha256_hex(value: Any) -> str:
    """SHA-256 hex digest of a JSON document's canonical serialization."""
    return hashlib.sha256(canonical_json(value)).hexdigest()
