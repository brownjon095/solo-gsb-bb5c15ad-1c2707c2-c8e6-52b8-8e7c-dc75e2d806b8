"""Tests for POST /release-plans/preview and its pure planner."""

import contextlib
import os
import tempfile

os.environ["DATABASE_PATH"] = os.path.join(tempfile.mkdtemp(), "test_release.db")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import db, store  # noqa: E402
from app.main import app  # noqa: E402
from app.release_plan import InvalidReleasePlan, build_preview  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def make_resolved(client, value=1):
    """A conflict-free merge is born resolved at revision 1."""
    resp = client.post(
        "/merges",
        json={
            "base": {"v": 0},
            "left": {"v": value},
            "right": {"v": value},
        },
    )
    assert resp.status_code == 201, resp.text
    merge = resp.json()
    assert merge["status"] == "resolved"
    return merge["merge_id"], merge["revision"]


def make_pending(client, choice="left"):
    """A conflicting merge stays pending at revision 1."""
    resp = client.post(
        "/merges",
        json={
            "base": {"host": "a"},
            "left": {"host": "b"},
            "right": {"host": "c"},
        },
    )
    assert resp.status_code == 201, resp.text
    merge = resp.json()
    assert merge["status"] == "pending"
    return merge["merge_id"]


def resolve(client, merge_id, revision=1):
    resp = client.post(
        f"/merges/{merge_id}/resolutions",
        json={
            "revision": revision,
            "resolutions": [{"pointer": "/host", "choice": "left"}],
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def preview(client, nodes):
    return client.post("/release-plans/preview", json={"nodes": nodes})


# ---------------------------------------------------------------------------
# API: happy paths
# ---------------------------------------------------------------------------


def test_all_resolved_plan_batches_in_dependency_layers(client):
    a, a_rev = make_resolved(client, 1)
    b, b_rev = make_resolved(client, 2)
    c, c_rev = make_resolved(client, 3)

    resp = preview(
        client,
        [
            {"merge_id": c, "depends_on": [a, b]},
            {"merge_id": a, "depends_on": []},
            {"merge_id": b, "depends_on": [a]},
        ],
    )
    assert resp.status_code == 200, resp.text
    plan = resp.json()
    assert plan["blocked"] == []
    assert [[n["merge_id"] for n in batch["nodes"]] for batch in plan["batches"]] == [
        [a],
        [b],
        [c],
    ]
    assert [batch["batch"] for batch in plan["batches"]] == [1, 2, 3]
    # Every node carries its current revision.
    by_id = {
        n["merge_id"]: n["revision"]
        for batch in plan["batches"]
        for n in batch["nodes"]
    }
    assert by_id == {a: a_rev, b: b_rev, c: c_rev}


def test_nodes_within_a_batch_are_sorted_by_merge_id(client):
    ids = [make_resolved(client, i)[0] for i in range(3)]
    resp = preview(client, [{"merge_id": mid} for mid in reversed(ids)])
    assert resp.status_code == 200, resp.text
    assert [n["merge_id"] for n in resp.json()["batches"][0]["nodes"]] == sorted(ids)


def test_depends_on_defaults_to_empty(client):
    mid, _ = make_resolved(client)
    resp = preview(client, [{"merge_id": mid}])
    assert resp.status_code == 200, resp.text
    assert resp.json()["batches"][0]["nodes"][0]["merge_id"] == mid


def test_pending_node_and_its_successors_are_blocked(client):
    a, _ = make_resolved(client, 1)
    p = make_pending(client)
    d, _ = make_resolved(client, 2)
    e, _ = make_resolved(client, 3)

    resp = preview(
        client,
        [
            {"merge_id": a},
            {"merge_id": p, "depends_on": [a]},
            {"merge_id": d, "depends_on": [p]},
            # e has one released and one blocked prerequisite: the blocked
            # direct dependency is what blocks it.
            {"merge_id": e, "depends_on": [d, a]},
        ],
    )
    assert resp.status_code == 200, resp.text
    plan = resp.json()
    assert [[n["merge_id"] for n in plan["batches"][0]["nodes"]]] == [[a]]
    blocked = {item["merge_id"]: item for item in plan["blocked"]}
    assert sorted(blocked) == sorted([p, d, e])
    assert blocked[p]["reason"] == "pending"
    assert blocked[p]["blocked_by"] == []
    assert blocked[p]["revision"] == 1
    assert blocked[d]["reason"] == "dependency_blocked"
    assert blocked[d]["blocked_by"] == [p]
    assert blocked[e]["reason"] == "dependency_blocked"
    assert blocked[e]["blocked_by"] == [d]


def test_revision_change_is_visible_on_re_preview_without_plan_mutation(client):
    mid = make_pending(client)

    resp = preview(client, [{"merge_id": mid}])
    assert resp.status_code == 200
    assert resp.json()["blocked"][0]["revision"] == 1
    assert resp.json()["batches"] == []

    # The preview wrote nothing: the merge is still pending at revision 1,
    # and no audit event exists.
    status = client.get(f"/merges/{mid}").json()
    assert status["status"] == "pending" and status["revision"] == 1
    audit = client.get(f"/merges/{mid}/audit").json()
    assert audit["event_count"] == 0

    # Resolve elsewhere: the next preview promotes the node and reports the
    # new revision, letting the caller detect that its plan went stale.
    done = resolve(client, mid)
    assert done["revision"] == 2
    resp = preview(client, [{"merge_id": mid}])
    plan = resp.json()
    assert plan["blocked"] == []
    assert plan["batches"][0]["nodes"] == [{"merge_id": mid, "revision": 2}]


# ---------------------------------------------------------------------------
# API: invalid graphs and unknown merges fail the whole request
# ---------------------------------------------------------------------------


def test_empty_node_list_is_422(client):
    resp = preview(client, [])
    assert resp.status_code == 422


def test_duplicate_merge_id_fails_entire_request(client):
    mid, _ = make_resolved(client)
    resp = preview(client, [{"merge_id": mid}, {"merge_id": mid}])
    assert resp.status_code == 400
    body = resp.json()
    assert body["reason"] == "duplicate_merge_id"
    assert body["merge_id"] == mid


def test_self_dependency_is_400(client):
    mid, _ = make_resolved(client)
    resp = preview(client, [{"merge_id": mid, "depends_on": [mid]}])
    assert resp.status_code == 400
    assert resp.json()["reason"] == "self_dependency"


def test_dependency_outside_plan_is_400(client):
    a, _ = make_resolved(client, 1)
    b, _ = make_resolved(client, 2)
    resp = preview(client, [{"merge_id": a, "depends_on": [b]}])
    assert resp.status_code == 400
    body = resp.json()
    assert body["reason"] == "dependency_not_in_plan"
    assert body["merge_id"] == a and body["dependency"] == b


def test_cycle_is_400_and_names_the_cycle(client):
    a, _ = make_resolved(client, 1)
    b, _ = make_resolved(client, 2)
    c, _ = make_resolved(client, 3)
    resp = preview(
        client,
        [
            {"merge_id": a, "depends_on": [c]},
            {"merge_id": b, "depends_on": [a]},
            {"merge_id": c, "depends_on": [b]},
        ],
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["reason"] == "dependency_cycle"
    # The cycle path closes itself and only mentions the three nodes.
    cycle = body["cycle"]
    assert cycle[0] == cycle[-1]
    assert set(cycle) == {a, b, c}
    # No batches are returned: the whole request errors.
    assert "batches" not in body


def test_unknown_merge_is_400(client):
    mid, _ = make_resolved(client)
    resp = preview(client, [{"merge_id": mid}, {"merge_id": "does-not-exist"}])
    assert resp.status_code == 400
    body = resp.json()
    assert body["reason"] == "unknown_merge"
    assert body["merge_ids"] == ["does-not-exist"]


def test_graph_validation_runs_before_unknown_lookup(client):
    # A cyclic graph with a node that also does not exist reports the graph
    # error first; either way the request fails wholesale.
    mid = make_pending(client)
    resp = preview(
        client,
        [
            {"merge_id": mid, "depends_on": ["ghost"]},
            {"merge_id": "ghost", "depends_on": [mid]},
        ],
    )
    assert resp.status_code == 400
    assert resp.json()["reason"] in {
        "dependency_cycle",
        "unknown_merge",
        "dependency_not_in_plan",
    }


# ---------------------------------------------------------------------------
# Pure planner unit tests (no database)
# ---------------------------------------------------------------------------


def _st(ids, pending=()):
    return {
        mid: {
            "status": "pending" if mid in pending else "resolved",
            "revision": 1,
        }
        for mid in ids
    }


def test_planner_reports_stable_cycle_for_equivalent_graphs():
    graph = [
        {"merge_id": "x", "depends_on": ["z"]},
        {"merge_id": "y", "depends_on": ["x"]},
        {"merge_id": "z", "depends_on": ["y"]},
    ]
    state = _st("xyz")
    with pytest.raises(InvalidReleasePlan) as exc1:
        build_preview(graph, state)
    # Declaration order must not change which cycle is reported.
    shuffled = [graph[2], graph[0], graph[1]]
    with pytest.raises(InvalidReleasePlan) as exc2:
        build_preview(shuffled, state)
    assert exc1.value.extra["cycle"] == exc2.value.extra["cycle"]


def test_planner_self_loop_before_cycle_check():
    with pytest.raises(InvalidReleasePlan) as exc:
        build_preview(
            [{"merge_id": "a", "depends_on": ["a"]}], _st("a")
        )
    assert exc.value.reason == "self_dependency"


def test_planner_resolved_node_with_released_dependency_ships():
    plan = build_preview(
        [
            {"merge_id": "b", "depends_on": ["a"]},
            {"merge_id": "a"},
        ],
        _st("ab"),
    )
    assert [n["merge_id"] for n in plan["batches"][0]["nodes"]] == ["a"]
    assert [n["merge_id"] for n in plan["batches"][1]["nodes"]] == ["b"]


def test_planner_blocked_node_names_only_direct_blockers():
    plan = build_preview(
        [
            {"merge_id": "root"},
            {"merge_id": "p", "depends_on": ["root"]},
            {"merge_id": "d", "depends_on": ["p"]},
            {"merge_id": "leaf", "depends_on": ["d", "root"]},
        ],
        _st(["root", "p", "d", "leaf"], pending={"p"}),
    )
    blocked = {b["merge_id"]: b for b in plan["blocked"]}
    assert blocked["p"]["reason"] == "pending"
    assert blocked["d"]["blocked_by"] == ["p"]
    # root is released; only d (a direct dependency) blocks leaf.
    assert blocked["leaf"]["blocked_by"] == ["d"]


def test_planner_duplicate_dependency_entry_is_tolerated():
    plan = build_preview(
        [{"merge_id": "b", "depends_on": ["a", "a"]}, {"merge_id": "a"}],
        _st("ab"),
    )
    assert len(plan["batches"]) == 2


# ---------------------------------------------------------------------------
# Snapshot consistency: all chunk queries of one preview share one snapshot
# ---------------------------------------------------------------------------


def test_chunked_reads_share_one_snapshot_across_concurrent_adjudication(
    client, monkeypatch
):
    # More than one 500-id chunk: the prerequisite lives in chunk 1 and its
    # successor in chunk 2. Both are created pending; both are adjudicated
    # (by a concurrent writer) after chunk 1 is read and before chunk 2 is
    # read. Because every chunk reads one shared snapshot, the preview must
    # keep seeing the pre-adjudication state for both nodes; it must not mix
    # the old prerequisite (pending) with the new successor (resolved) and
    # falsely report the successor as dependency_blocked.
    p = make_pending(client)
    s = make_pending(client)
    fillers = [make_resolved(client, i)[0] for i in range(499)]

    nodes = [
        {"merge_id": p},
        *({"merge_id": mid} for mid in fillers),
        {"merge_id": s, "depends_on": [p]},
    ]
    assert len(nodes) == 501  # p + 499 fillers in chunk 1; s alone in chunk 2

    real_read_snapshot = db.read_snapshot
    fired = False

    def adjudicate_both():
        # A real, concurrent adjudication on the shared database: revisions
        # bump and an audit event is appended, exactly like another client.
        store.apply_resolutions(
            p, 1, [{"pointer": "/host", "choice": "left"}]
        )
        store.apply_resolutions(
            s, 1, [{"pointer": "/host", "choice": "left"}]
        )

    class _ConnProxy:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, query, params=()):
            nonlocal fired
            # The chunk-2 read begins only after chunk 1 has been consumed;
            # adjudicate there, while the snapshot transaction is open.
            if not fired and s in params:
                adjudicate_both()
                fired = True
            return self._conn.execute(query, params)

    @contextlib.contextmanager
    def hooked_read_snapshot():
        with real_read_snapshot() as conn:
            yield _ConnProxy(conn)

    monkeypatch.setattr(db, "read_snapshot", hooked_read_snapshot)

    resp = preview(client, nodes)
    assert resp.status_code == 200, resp.text
    assert fired, "test harness did not interleave the adjudications"
    plan = resp.json()

    # Snapshot view: neither adjudication is visible. The successors's own
    # merge is still pending, so it blocks on itself — never on p.
    blocked = {item["merge_id"]: item for item in plan["blocked"]}
    assert blocked[p] == {
        "merge_id": p,
        "revision": 1,
        "reason": "pending",
        "blocked_by": [],
    }
    assert blocked[s] == {
        "merge_id": s,
        "revision": 1,
        "reason": "pending",
        "blocked_by": [],
    }
    assert [n["merge_id"] for n in plan["batches"][0]["nodes"]] == sorted(
        fillers
    )

    # After the adjudications have committed, a fresh request opens a new
    # snapshot and reports the new state: p then s ship, at revision 2.
    resp = preview(client, nodes)
    assert resp.status_code == 200, resp.text
    plan = resp.json()
    assert plan["blocked"] == []
    assert [
        [n["merge_id"] for n in batch["nodes"]] for batch in plan["batches"]
    ] == [sorted(fillers + [p]), [s]]
    by_node = {
        n["merge_id"]: n["revision"]
        for batch in plan["batches"]
        for n in batch["nodes"]
    }
    assert by_node[p] == 2 and by_node[s] == 2

    # The preview path stays read-only: each merge carries exactly the one
    # event from the real adjudication above, none from either preview.
    for mid in (p, s):
        audit_view = client.get(f"/merges/{mid}/audit").json()
        assert audit_view["event_count"] == 1
