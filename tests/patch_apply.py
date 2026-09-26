"""Small RFC 6902 applier shared by tests (add/remove/replace only)."""

from __future__ import annotations

import copy
from typing import Any

from app.merge import parse_pointer


def apply_patch(doc: Any, patch: list[dict]) -> Any:
    doc = copy.deepcopy(doc)
    for op in patch:
        tokens = parse_pointer(op["path"])
        if not tokens:
            if op["op"] == "remove":
                doc = None
            else:
                doc = op["value"]
            continue
        parent = doc
        for tok in tokens[:-1]:
            parent = parent[int(tok)] if isinstance(parent, list) else parent[tok]
        last = tokens[-1]
        if op["op"] in ("add", "replace"):
            if isinstance(parent, list):
                parent[int(last)] = op["value"]
            else:
                parent[last] = op["value"]
        elif op["op"] == "remove":
            if isinstance(parent, list):
                del parent[int(last)]
            else:
                parent.pop(last)
        else:
            raise ValueError(f"unsupported op: {op['op']}")
    return doc
