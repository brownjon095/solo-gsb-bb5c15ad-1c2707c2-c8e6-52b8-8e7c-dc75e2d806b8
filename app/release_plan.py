"""Read-only release-orchestration preview over existing merge orders.

The caller supplies a node list — each node names a merge order and the
merge orders that must be published before it — together with the *current*
state (status/revision) of those merges. This module validates the graph
and computes stable release batches. It is pure and in-memory: it never
touches the database, so a preview cannot mutate any merge order.
"""

from __future__ import annotations

from typing import Any

STATUS_PENDING = "pending"
STATUS_RESOLVED = "resolved"

REASON_PENDING = "pending"
REASON_DEPENDENCY_BLOCKED = "dependency_blocked"

# DFS visit colours for cycle detection.
_WHITE = 0
_GRAY = 1
_BLACK = 2


class InvalidReleasePlan(Exception):
    """The node list names unknown merges or does not form a valid DAG."""

    def __init__(self, reason: str, detail: str, **extra: Any):
        # Stable machine-readable failure code, e.g. "duplicate_merge_id",
        # "self_dependency", "dependency_not_in_plan", "dependency_cycle",
        # "unknown_merge"; ``extra`` carries structured fields for the response.
        self.reason = reason
        self.extra = extra
        super().__init__(detail)


def _find_cycle(deps: dict[str, set[str]]) -> list[str] | None:
    """Return one dependency cycle as an ordered node path, or None.

    Edges point from a node to its prerequisites. Roots and neighbour lists
    are walked in merge_id order, so the same graph always yields the same
    reported cycle; the returned path repeats its first node at the end
    (``["a", "b", "a"]``).
    """
    color = {node: _WHITE for node in deps}
    for root in sorted(deps):
        if color[root] != _WHITE:
            continue
        color[root] = _GRAY
        path = [root]
        # (node, iterator over its still-unvisited prerequisite edges).
        stack: list[tuple[str, Any]] = [(root, iter(sorted(deps[root])))]
        while stack:
            node, edges = stack[-1]
            descended = False
            for dep in edges:
                if color[dep] == _GRAY:
                    start = path.index(dep)
                    return path[start:] + [dep]
                if color[dep] == _WHITE:
                    color[dep] = _GRAY
                    path.append(dep)
                    stack.append((dep, iter(sorted(deps[dep]))))
                    descended = True
                    break
            if not descended:
                color[node] = _BLACK
                stack.pop()
                path.pop()
    return None


def _validate_graph(nodes: list[dict]) -> dict[str, set[str]]:
    """Validate the request node list as a dependency graph.

    Checks, in order: unique merge_ids, no self-dependency, every
    prerequisite names another node in the list, and no cycle. Raises
    InvalidReleasePlan (400 at the API) on the first failing category; the
    offending node/dependency within a category is chosen deterministically.
    """
    ids = [node["merge_id"] for node in nodes]

    seen: set[str] = set()
    duplicates: set[str] = set()
    for merge_id in ids:
        if merge_id in seen:
            duplicates.add(merge_id)
        seen.add(merge_id)
    if duplicates:
        merge_id = sorted(duplicates)[0]
        raise InvalidReleasePlan(
            "duplicate_merge_id",
            f"merge {merge_id!r} appears more than once in the node list",
            merge_id=merge_id,
        )

    deps = {
        node["merge_id"]: set(node.get("depends_on") or []) for node in nodes
    }

    for merge_id in sorted(deps):
        if merge_id in deps[merge_id]:
            raise InvalidReleasePlan(
                "self_dependency",
                f"merge {merge_id!r} depends on itself",
                merge_id=merge_id,
            )

    known = set(deps)
    for merge_id in sorted(deps):
        missing = sorted(deps[merge_id] - known)
        if missing:
            dependency = missing[0]
            raise InvalidReleasePlan(
                "dependency_not_in_plan",
                f"merge {merge_id!r} depends on {dependency!r}, which is not "
                "a node in the plan",
                merge_id=merge_id,
                dependency=dependency,
            )

    cycle = _find_cycle(deps)
    if cycle is not None:
        raise InvalidReleasePlan(
            "dependency_cycle",
            "the dependency graph contains a cycle: " + " -> ".join(cycle),
            cycle=cycle,
        )

    return deps


def build_preview(nodes: list[dict], state: dict[str, dict]) -> dict:
    """Compute release batches from validated nodes and current merge state.

    ``state`` maps merge_id to ``{"status": ..., "revision": ...}`` read
    from the database. A node is publishable when its merge is ``resolved``
    and every prerequisite is publishable; publishable nodes are peeled off
    in layers, each layer one release batch (nodes inside a batch sorted by
    merge_id). Every node left over is reported as blocked: a still-pending
    merge is blocked on itself (``reason: "pending"``); a resolved merge is
    ``dependency_blocked`` and names the direct prerequisites that cannot be
    published yet. Each returned node carries its current revision so the
    caller can detect a plan that has gone stale.
    """
    deps = _validate_graph(nodes)
    ids = set(deps)

    unknown = sorted(ids - set(state))
    if unknown:
        raise InvalidReleasePlan(
            "unknown_merge",
            "the plan references unknown merge(s): " + ", ".join(unknown),
            merge_ids=unknown,
        )

    emitted: set[str] = set()
    remaining = set(ids)
    batches: list[dict] = []
    batch_no = 0
    while remaining:
        ready = sorted(
            merge_id
            for merge_id in remaining
            if deps[merge_id] <= emitted
            and state[merge_id]["status"] == STATUS_RESOLVED
        )
        if not ready:
            # Only blocked nodes are left; the graph is acyclic, so no
            # further layer can ever peel off.
            break
        batch_no += 1
        batches.append(
            {
                "batch": batch_no,
                "nodes": [
                    {
                        "merge_id": merge_id,
                        "revision": state[merge_id]["revision"],
                    }
                    for merge_id in ready
                ],
            }
        )
        emitted.update(ready)
        remaining.difference_update(ready)

    blocked: list[dict] = []
    for merge_id in sorted(remaining):
        if state[merge_id]["status"] != STATUS_RESOLVED:
            # The node itself is still pending: it is its own blocker, even
            # if a prerequisite happens to be blocked as well.
            blocked.append(
                {
                    "merge_id": merge_id,
                    "revision": state[merge_id]["revision"],
                    "reason": REASON_PENDING,
                    "blocked_by": [],
                }
            )
        else:
            # The graph is valid and acyclic: every prerequisite not yet
            # emitted is itself blocked and directly blocks this node.
            blockers = sorted(deps[merge_id] & remaining)
            blocked.append(
                {
                    "merge_id": merge_id,
                    "revision": state[merge_id]["revision"],
                    "reason": REASON_DEPENDENCY_BLOCKED,
                    "blocked_by": blockers,
                }
            )

    return {"batches": batches, "blocked": blocked}
