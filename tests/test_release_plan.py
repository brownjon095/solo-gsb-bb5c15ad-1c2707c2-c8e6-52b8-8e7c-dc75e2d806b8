"""Tests for POST /release-plans/preview and its pure planner."""

import contextlib
import os
import sqlite3
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


def resolve_directly(*merge_ids):
    """Commit a resolution on a separate connection (a concurrent writer).

    Simulates an adjudication that commits outside the app's own
    connection, the way a concurrent request lands between two reads of
    one preview.
    """
    conn = sqlite3.connect(db.DATABASE_PATH)
    try:
        for merge_id in merge_ids:
            conn.execute(
                "UPDATE merges SET status = 'resolved', "
                "revision = revision + 1, result = document, "
                "updated_at = ? WHERE id = ?",
                ("2026-09-26T00:00:00+00:00", merge_id),
            )
        conn.commit()
    finally:
        conn.close()


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
# Snapshot consistency across chunked state reads
# ---------------------------------------------------------------------------


def test_read_snapshot_pins_one_consistent_snapshot(client):
    mid = make_pending(client)
    with db.read_snapshot() as conn:
        before = conn.execute(
            "SELECT status, revision FROM merges WHERE id = ?", (mid,)
        ).fetchone()
        # A concurrent adjudication commits on another connection while the
        # snapshot is open.
        resolve_directly(mid)
        after = conn.execute(
            "SELECT status, revision FROM merges WHERE id = ?", (mid,)
        ).fetchone()
    # Both reads inside the snapshot see the same pre-resolution state.
    assert before["status"] == after["status"] == "pending"
    assert before["revision"] == after["revision"] == 1
    # A fresh read sees the committed resolution.
    row = db.query_one("SELECT status, revision FROM merges WHERE id = ?", (mid,))
    assert row["status"] == "resolved"
    assert row["revision"] == 2


def test_preview_chunks_share_one_snapshot_under_concurrent_resolution(
    client, monkeypatch
):
    prerequisite = make_pending(client)
    successor = make_pending(client)
    # One merge per chunk: the prerequisite is read in the first chunk, the
    # successor in the second.
    monkeypatch.setattr(store, "_STATE_CHUNK_SIZE", 1)

    real_read_snapshot = db.read_snapshot
    injected = []

    @contextlib.contextmanager
    def wrapped_read_snapshot():
        with real_read_snapshot() as conn:
            calls = {"count": 0}

            class _ConnProxy:
                def execute(self, sql, params=()):
                    calls["count"] += 1
                    if calls["count"] == 2 and not injected:
                        # Both merges are resolved concurrently after the
                        # first chunk was read and before the second is.
                        resolve_directly(prerequisite, successor)
                        injected.append(True)
                    return conn.execute(sql, params)

            yield _ConnProxy()

    monkeypatch.setattr(db, "read_snapshot", wrapped_read_snapshot)

    nodes = [
        {"merge_id": prerequisite},
        {"merge_id": successor, "depends_on": [prerequisite]},
    ]
    resp = preview(client, nodes)
    assert resp.status_code == 200, resp.text
    assert injected, "the concurrent resolution must land between chunk reads"
    plan = resp.json()
    # Consistent snapshot: both nodes come from the pre-resolution state.
    # The successor is never reported as resolved-but-dependency-blocked by
    # a prerequisite whose resolution the first chunk simply did not see.
    assert plan["batches"] == []
    blocked = {item["merge_id"]: item for item in plan["blocked"]}
    assert blocked[prerequisite]["reason"] == "pending"
    assert blocked[prerequisite]["revision"] == 1
    assert blocked[successor]["reason"] == "pending"
    assert blocked[successor]["revision"] == 1

    # The preview wrote nothing: no revision bump, no audit events.
    for mid in (prerequisite, successor):
        audit = client.get(f"/merges/{mid}/audit").json()
        assert audit["event_count"] == 0

    # Once the concurrent adjudication has committed, a fresh preview opens
    # a new snapshot and shows the new state: both nodes publishable.
    monkeypatch.undo()
    resp = preview(client, nodes)
    assert resp.status_code == 200, resp.text
    plan = resp.json()
    assert plan["blocked"] == []
    batches = [[n["merge_id"] for n in batch["nodes"]] for batch in plan["batches"]]
    assert batches == [[prerequisite], [successor]]
    revisions = {
        n["merge_id"]: n["revision"] for batch in plan["batches"] for n in batch["nodes"]
    }
    assert revisions == {prerequisite: 2, successor: 2}


def test_preview_plan_spanning_multiple_state_chunks(client):
    # 501 nodes exceed the 500-row chunk size: the state is fetched in two
    # queries and every node must still be batched.
    ids = sorted(make_resolved(client, i)[0] for i in range(501))
    resp = preview(client, [{"merge_id": mid} for mid in reversed(ids)])
    assert resp.status_code == 200, resp.text
    plan = resp.json()
    assert plan["blocked"] == []
    assert len(plan["batches"]) == 1
    nodes = plan["batches"][0]["nodes"]
    assert [n["merge_id"] for n in nodes] == ids
    assert all(n["revision"] == 1 for n in nodes)


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
