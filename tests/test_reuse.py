"""End-to-end tests for historical resolution reuse.

POST /merges/{merge_id}/reuse-resolutions copies adjudications from an
already resolved source merge: conflicts are matched by JSON Pointer and
only reused when base/left/right and the missing semantics are identical
after canonicalization (an absent key is not an explicit null). Values
come from the source's final result; source-side deletions delete the
target key too. Matches apply atomically with a single revision bump;
zero matches is a no-op.
"""

import json as _json
import os
import tempfile

os.environ["DATABASE_PATH"] = os.path.join(tempfile.mkdtemp(), "test_reuse.db")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.main import app  # noqa: E402
from app.reuse import conflicts_equivalent  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def make_merge(client, base, left, right, schema=None):
    payload = {"base": base, "left": left, "right": right}
    if schema is not None:
        payload["result_schema"] = schema
    resp = client.post("/merges", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def resolve(client, merge_id, revision, resolutions):
    resp = client.post(
        f"/merges/{merge_id}/resolutions",
        json={"revision": revision, "resolutions": resolutions},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def reuse(client, merge_id, revision, source_id):
    return client.post(
        f"/merges/{merge_id}/reuse-resolutions",
        json={"revision": revision, "source_merge_id": source_id},
    )


@pytest.fixture
def resolved_source(client):
    """Three conflicts /a /b /c resolved as left=2, explicit 'B', right=3."""
    source = make_merge(
        client,
        base={"a": 1, "b": 1, "c": 1},
        left={"a": 2, "b": 2, "c": 2},
        right={"a": 3, "b": 3, "c": 3},
    )
    sid = source["merge_id"]
    done = resolve(
        client,
        sid,
        1,
        [
            {"pointer": "/a", "choice": "left"},
            {"pointer": "/b", "value": "B"},
            {"pointer": "/c", "choice": "right"},
        ],
    )
    assert done["status"] == "resolved"
    assert done["result"] == {"a": 2, "b": "B", "c": 3}
    return sid


# ---------------------------------------------------------------------------
# Basic reuse
# ---------------------------------------------------------------------------


def test_reuse_all_conflicts_from_resolved_source(client, resolved_source):
    target = make_merge(
        client,
        base={"a": 1, "b": 1, "c": 1},
        left={"a": 2, "b": 2, "c": 2},
        right={"a": 3, "b": 3, "c": 3},
    )
    mid = target["merge_id"]

    resp = reuse(client, mid, 1, resolved_source)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applied"] is True
    assert body["status"] == "resolved"
    assert body["revision"] == 2
    assert body["result"] == {"a": 2, "b": "B", "c": 3}
    assert body["conflicts"] == []
    assert body["applied_count"] == 3
    assert body["skipped_count"] == 0
    assert body["applied_resolutions"] == [
        {"pointer": "/a", "action": "set", "value": 2},
        {"pointer": "/b", "action": "set", "value": "B"},
        {"pointer": "/c", "action": "set", "value": 3},
    ]
    assert body["skipped"] == []


def test_reuse_partial_leaves_remaining_conflicts_pending(client, resolved_source):
    # Identical /a /b /c plus an extra conflict /d unknown to the source.
    target = make_merge(
        client,
        base={"a": 1, "b": 1, "c": 1, "d": 1},
        left={"a": 2, "b": 2, "c": 2, "d": 2},
        right={"a": 3, "b": 3, "c": 3, "d": 4},
    )
    mid = target["merge_id"]

    resp = reuse(client, mid, 1, resolved_source)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applied"] is True
    assert body["status"] == "pending"
    assert body["revision"] == 2
    assert body["result"] is None
    assert body["applied_count"] == 3
    assert [c["pointer"] for c in body["conflicts"]] == ["/d"]
    assert body["document"] == {"a": 2, "b": "B", "c": 3,
                                "d": {"__merge_conflict__": "/d"}}
    assert body["skipped"] == [
        {
            "pointer": "/d",
            "reason": "no_source_conflict",
            "detail": "the source merge has no recorded conflict at this pointer",
        }
    ]

    # The old revision is now stale; reusing again with it yields 409.
    resp = reuse(client, mid, 1, resolved_source)
    assert resp.status_code == 409
    assert resp.json()["current_revision"] == 2


def test_reuse_without_any_match_is_noop_without_revision_bump(client, resolved_source):
    # Target conflicts live at completely different pointers.
    target = make_merge(
        client,
        base={"x": 1, "y": 1},
        left={"x": 2, "y": 2},
        right={"x": 3, "y": 3},
    )
    mid = target["merge_id"]

    resp = reuse(client, mid, 1, resolved_source)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applied"] is False
    assert body["revision"] == 1
    assert body["status"] == "pending"
    assert body["applied_count"] == 0
    assert body["applied_resolutions"] == []
    assert body["skipped_count"] == 2
    assert {item["pointer"] for item in body["skipped"]} == {"/x", "/y"}
    assert {item["reason"] for item in body["skipped"]} == {"no_source_conflict"}

    # Nothing was written: conflicts and document are untouched.
    fetched = client.get(f"/merges/{mid}").json()
    assert fetched["revision"] == 1
    assert sorted(c["pointer"] for c in fetched["conflicts"]) == ["/x", "/y"]


def test_reuse_applied_values_come_from_source_final_result(client):
    # The source resolves /a with a custom explicit value, not either side.
    source = make_merge(
        client,
        base={"a": 1}, left={"a": 2}, right={"a": 3}
    )
    resolve(client, source["merge_id"], 1, [{"pointer": "/a", "value": 42}])
    target = make_merge(
        client,
        base={"a": 1}, left={"a": 2}, right={"a": 3}
    )

    resp = reuse(client, target["merge_id"], 1, source["merge_id"])
    assert resp.status_code == 200
    assert resp.json()["result"] == {"a": 42}


def test_reuse_explicit_null_value(client):
    source = make_merge(
        client,
        base={"a": 1}, left={"a": 2}, right={"a": 3}
    )
    resolve(client, source["merge_id"], 1, [{"pointer": "/a", "value": None}])
    target = make_merge(
        client,
        base={"a": 1}, left={"a": 2}, right={"a": 3}
    )

    resp = reuse(client, target["merge_id"], 1, source["merge_id"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "resolved"
    assert body["result"] == {"a": None}
    assert body["applied_resolutions"] == [
        {"pointer": "/a", "action": "set", "value": None}
    ]


# ---------------------------------------------------------------------------
# Deletion semantics
# ---------------------------------------------------------------------------


def test_reuse_source_deletion_deletes_target_key(client):
    # Left deletes /a and modifies /b; right modifies both: /a is a
    # delete-vs-modify conflict with the left side missing.
    docs = dict(
        base={"a": 1, "b": 1},
        left={"b": 2},
        right={"a": 3, "b": 3},
    )
    source = make_merge(client, **docs)
    assert next(c for c in source["conflicts"] if c["pointer"] == "/a")["missing"] == ["left"]
    resolve(
        client,
        source["merge_id"],
        1,
        [
            {"pointer": "/a", "choice": "left"},
            {"pointer": "/b", "choice": "left"},
        ],
    )

    target = make_merge(client, **docs)
    resp = reuse(client, target["merge_id"], 1, source["merge_id"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "resolved"
    assert body["result"] == {"b": 2}
    assert {"pointer": "/a", "action": "delete"} in body["applied_resolutions"]
    set_b = next(
        item for item in body["applied_resolutions"] if item["pointer"] == "/b"
    )
    assert set_b == {"pointer": "/b", "action": "set", "value": 2}
    assert body["skipped"] == []


def test_reuse_value_added_back_by_source_is_set_in_target(client):
    # The missing side won the conflict but the source instead resolved it
    # with an explicit value: /a exists in the result, so the target sets
    # it rather than deleting.
    docs = dict(
        base={"a": 1, "b": 1},
        left={"b": 2},
        right={"a": 3, "b": 3},
    )
    source = make_merge(client, **docs)
    resolve(
        client,
        source["merge_id"],
        1,
        [
            {"pointer": "/a", "value": "kept"},
            {"pointer": "/b", "choice": "right"},
        ],
    )

    target = make_merge(client, **docs)
    resp = reuse(client, target["merge_id"], 1, source["merge_id"])
    assert resp.status_code == 200
    assert resp.json()["result"] == {"a": "kept", "b": 3}


# ---------------------------------------------------------------------------
# Identity matching: base/left/right + missing semantics
# ---------------------------------------------------------------------------


def test_reuse_skips_conflicts_whose_candidates_differ(client, resolved_source):
    # /a matches (left 2/right 3); /b differs on the right (4 vs 3) and
    # /c differs on the base (10 vs 1); both must be skipped, not applied.
    target = make_merge(
        client,
        base={"a": 1, "b": 1, "c": 10},
        left={"a": 2, "b": 2, "c": 2},
        right={"a": 3, "b": 4, "c": 3},
    )
    mid = target["merge_id"]

    resp = reuse(client, mid, 1, resolved_source)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applied"] is True
    assert body["status"] == "pending"
    assert body["revision"] == 2
    assert [item["pointer"] for item in body["applied_resolutions"]] == ["/a"]
    skipped = {item["pointer"]: item for item in body["skipped"]}
    assert set(skipped) == {"/b", "/c"}
    for item in skipped.values():
        assert item["reason"] == "conflict_mismatch"
        assert item["detail"]
    assert skipped["/b"]["mismatched_fields"] == ["right"]
    assert skipped["/c"]["mismatched_fields"] == ["base"]
    assert sorted(c["pointer"] for c in body["conflicts"]) == ["/b", "/c"]


def test_reuse_distinguishes_missing_key_from_explicit_null(client):
    # Source: left deletes /k/x (the key is absent); target: left sets it
    # to an explicit null. Same pointer, same right candidate, but the
    # missing semantics differ — reuse is forbidden.
    common_base = {"k": {"x": 1}}
    common_right = {"k": {"x": 3}}
    source = make_merge(
        client,
        base=common_base,
        left={"k": {}},
        right=common_right,
    )
    source_conflict = next(
        c for c in source["conflicts"] if c["pointer"] == "/k/x"
    )
    assert source_conflict["missing"] == ["left"]
    assert source_conflict["left"] is None
    resolve(
        client,
        source["merge_id"],
        1,
        [
            {"pointer": "/k/x", "choice": "right"},
        ],
    )

    target = make_merge(
        client,
        base=common_base,
        left={"k": {"x": None}},
        right=common_right,
    )
    target_conflict = next(
        c for c in target["conflicts"] if c["pointer"] == "/k/x"
    )
    assert target_conflict["missing"] == []
    assert target_conflict["left"] is None

    resp = reuse(client, target["merge_id"], 1, source["merge_id"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applied"] is False
    assert body["revision"] == 1
    assert body["applied_resolutions"] == []
    assert len(body["skipped"]) == 1
    skip = body["skipped"][0]
    assert skip["pointer"] == "/k/x"
    assert skip["reason"] == "conflict_mismatch"
    # The serialized left candidate is null in both; only the missing
    # semantics reveal the difference.
    assert skip["mismatched_fields"] == ["missing"]

    # The target conflict stays completely untouched.
    fetched = client.get(f"/merges/{target['merge_id']}").json()
    assert fetched["revision"] == 1
    assert [c["pointer"] for c in fetched["conflicts"]] == ["/k/x"]


def test_conflicts_equivalent_normalization_unit():
    mk = lambda base, left, right, missing: {
        "base": base, "left": left, "right": right, "missing": missing
    }
    # Same content, different object key order: equivalent.
    a = mk({"x": 1, "y": 2}, [1, 2], {"z": [1, {"p": 1}]}, [])
    b = mk({"y": 2, "x": 1}, [1, 2], {"z": [1, {"p": 1}]}, [])
    assert conflicts_equivalent(a, b)[0] is True

    # Explicit null vs absent key (missing) are never equivalent even
    # though the serialized candidate is null in both cases.
    null_side = mk(1, None, 3, [])
    missing_side = mk(1, None, 3, ["left"])
    equal, fields = conflicts_equivalent(null_side, missing_side)
    assert equal is False and fields == ["missing"]

    # Strict JSON types: 1 vs true, 1 vs 1.0 do not match.
    assert conflicts_equivalent(mk(1, 1, 3, []), mk(1, True, 3, []))[0] is False
    assert conflicts_equivalent(mk(1, 1, 3, []), mk(1, 1.0, 3, []))[0] is False

    # List order is significant.
    assert conflicts_equivalent(mk(1, [1, 2], 3, []), mk(1, [2, 1], 3, []))[0] is False

    # missing list order is normalized; the serialized base is null when
    # the base key is absent on both sides.
    assert conflicts_equivalent(
        mk(None, 2, 3, ["base"]), mk(None, 2, 3, ["base"])
    )[0] is True


def test_reuse_matches_after_key_order_normalization(client):
    # The conflicting leaves are arrays (opaque to recursion); the objects
    # inside carry the same content in different key order on each side.
    source = make_merge(
        client,
        base={"list": [{"k": 1}]},
        left={"list": [{"k": 1, "a": 2}]},
        right={"list": [{"k": 1, "b": 3}]},
    )
    resolve(
        client,
        source["merge_id"],
        1,
        [{"pointer": "/list", "value": [{"final": True}]}],
    )
    target = make_merge(
        client,
        base={"list": [{"k": 1}]},
        left={"list": [{"a": 2, "k": 1}]},
        right={"list": [{"b": 3, "k": 1}]},
    )
    resp = reuse(client, target["merge_id"], 1, source["merge_id"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "resolved"
    assert body["result"] == {"list": [{"final": True}]}
    assert body["skipped"] == []


def test_reuse_does_not_match_after_strict_type_difference(client):
    # 1 vs 1.0 at /a: canonicalization keeps JSON types strict.
    source = make_merge(
        client,
        base={"a": 1}, left={"a": 2}, right={"a": 3}
    )
    resolve(client, source["merge_id"], 1, [{"pointer": "/a", "choice": "right"}])
    target = make_merge(
        client,
        base={"a": 1.0}, left={"a": 2}, right={"a": 3}
    )
    resp = reuse(client, target["merge_id"], 1, source["merge_id"])
    assert resp.status_code == 200
    body = resp.json()
    assert body["applied"] is False
    assert body["revision"] == 1
    assert body["skipped"][0]["reason"] == "conflict_mismatch"
    assert body["skipped"][0]["mismatched_fields"] == ["base"]


# ---------------------------------------------------------------------------
# Snapshot persistence
# ---------------------------------------------------------------------------


def test_source_conflict_snapshot_is_persisted_unchanged(client, resolved_source):
    row = db.query_one(
        "SELECT status, conflicts, source_conflicts FROM merges WHERE id = ?",
        (resolved_source,),
    )
    # The live conflict list is empty once resolved...
    assert row["status"] == "resolved"
    assert _json.loads(row["conflicts"]) == []
    # ...but the snapshot still records every original conflict candidate.
    snapshot = _json.loads(row["source_conflicts"])
    assert [c["pointer"] for c in snapshot] == ["/a", "/b", "/c"]
    assert snapshot[0] == {"pointer": "/a", "base": 1, "left": 2, "right": 3,
                          "missing": []}


def test_reuse_works_after_source_was_resolved_in_partial_batches(client):
    source = make_merge(
        client,
        base={"a": 1, "b": 1},
        left={"a": 2, "b": 2},
        right={"a": 3, "b": 3},
    )
    sid = source["merge_id"]
    resolve(client, sid, 1, [{"pointer": "/a", "choice": "left"}])
    resolve(client, sid, 2, [{"pointer": "/b", "choice": "right"}])

    # The snapshot predates both batches and still covers /a and /b.
    target = make_merge(
        client,
        base={"a": 1, "b": 1},
        left={"a": 2, "b": 2},
        right={"a": 3, "b": 3},
    )
    resp = reuse(client, target["merge_id"], 1, sid)
    assert resp.status_code == 200, resp.text
    assert resp.json()["result"] == {"a": 2, "b": 3}


# ---------------------------------------------------------------------------
# State guards and atomicity
# ---------------------------------------------------------------------------


def test_reuse_stale_revision_returns_409(client, resolved_source):
    target = make_merge(
        client,
        base={"a": 1}, left={"a": 2}, right={"a": 3}
    )
    resp = reuse(client, target["merge_id"], 999, resolved_source)
    assert resp.status_code == 409
    assert resp.json()["current_revision"] == 1


def test_reuse_against_resolved_target_returns_409(client, resolved_source):
    target = make_merge(
        client,
        base={"a": 1}, left={"a": 2}, right={"a": 3}
    )
    resolve(client, target["merge_id"], 1, [{"pointer": "/a", "choice": "left"}])
    resp = reuse(client, target["merge_id"], 2, resolved_source)
    assert resp.status_code == 409


def test_reuse_unknown_target_returns_404(client, resolved_source):
    resp = reuse(client, "does-not-exist", 1, resolved_source)
    assert resp.status_code == 404


def test_reuse_rejects_invalid_sources(client):
    target = make_merge(
        client,
        base={"a": 1}, left={"a": 2}, right={"a": 3}
    )

    # Unknown source merge -> 400 (nothing to reuse from).
    resp = reuse(client, target["merge_id"], 1, "no-such-source")
    assert resp.status_code == 400
    assert "does not exist" in resp.json()["detail"]

    # Pending source (still has open conflicts) -> 400.
    pending = make_merge(
        client,
        base={"a": 1}, left={"a": 2}, right={"a": 3}
    )
    resp = reuse(client, target["merge_id"], 1, pending["merge_id"])
    assert resp.status_code == 400
    assert "not resolved" in resp.json()["detail"]

    # No target state changed by the rejected requests.
    fetched = client.get(f"/merges/{target['merge_id']}").json()
    assert fetched["revision"] == 1
    assert fetched["status"] == "pending"


def test_reuse_rejects_source_without_snapshot(client):
    # A row predating conflict snapshots: status/result valid but
    # source_conflicts is NULL.
    source = make_merge(
        client,
        base={"a": 1}, left={"a": 2}, right={"a": 3}
    )
    resolve(client, source["merge_id"], 1, [{"pointer": "/a", "choice": "left"}])
    db.execute(
        "UPDATE merges SET source_conflicts = NULL WHERE id = ?",
        (source["merge_id"],),
    )
    target = make_merge(
        client,
        base={"a": 1}, left={"a": 2}, right={"a": 3}
    )
    resp = reuse(client, target["merge_id"], 1, source["merge_id"])
    assert resp.status_code == 400
    assert "conflict snapshot" in resp.json()["detail"]
    fetched = client.get(f"/merges/{target['merge_id']}").json()
    assert fetched["revision"] == 1


def test_reuse_result_schema_failure_writes_nothing(client):
    # Source resolves /a to 2; the target schema rejects anything below 5.
    source = make_merge(
        client,
        base={"a": 1}, left={"a": 2}, right={"a": 3}
    )
    resolve(client, source["merge_id"], 1, [{"pointer": "/a", "choice": "left"}])

    schema = {
        "type": "object",
        "properties": {"a": {"type": "integer", "minimum": 5}},
        "required": ["a"],
    }
    target = make_merge(
        client,
        base={"a": 1}, left={"a": 2}, right={"a": 3}, schema=schema
    )
    mid = target["merge_id"]

    resp = reuse(client, mid, 1, source["merge_id"])
    assert resp.status_code == 422, resp.text
    errors = resp.json()["errors"]
    assert any(e["instance_path"] == "/a" for e in errors)

    # The whole batch rolled back: revision, status and conflicts intact.
    fetched = client.get(f"/merges/{mid}").json()
    assert fetched["revision"] == 1
    assert fetched["status"] == "pending"
    assert [c["pointer"] for c in fetched["conflicts"]] == ["/a"]
    assert fetched["document"]["a"] == {"__merge_conflict__": "/a"}

    # Fixing the source value is irrelevant here; instead resolve directly
    # with a passing value to prove the gate order was the only blocker.
    done = resolve(client, mid, 1, [{"pointer": "/a", "value": 6}])
    assert done["status"] == "resolved"


def test_reuse_result_schema_passes_on_reused_final_result(client):
    schema = {
        "type": "object",
        "properties": {"a": {"type": "integer", "minimum": 1}},
        "required": ["a"],
    }
    source = make_merge(
        client,
        base={"a": 1}, left={"a": 2}, right={"a": 3}
    )
    resolve(client, source["merge_id"], 1, [{"pointer": "/a", "choice": "right"}])
    target = make_merge(
        client,
        base={"a": 1}, left={"a": 2}, right={"a": 3}, schema=schema
    )
    resp = reuse(client, target["merge_id"], 1, source["merge_id"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["result"] == {"a": 3}


def test_reuse_partial_match_does_not_trigger_schema_gate(client):
    # Two conflicts; only /a matches so the result is not final yet and the
    # schema (which /a alone would violate) must not be evaluated.
    source = make_merge(
        client,
        base={"a": 1}, left={"a": 2}, right={"a": 3}
    )
    resolve(client, source["merge_id"], 1, [{"pointer": "/a", "choice": "left"}])
    schema = {"type": "object", "required": ["a", "b"]}
    target = make_merge(
        client,
        base={"a": 1, "b": 1},
        left={"a": 2, "b": 2},
        right={"a": 3, "b": 3},
        schema=schema,
    )
    resp = reuse(client, target["merge_id"], 1, source["merge_id"])
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pending"
    assert body["revision"] == 2


def test_reuse_request_validation(client):
    # Missing source_merge_id / revision -> 422 from pydantic.
    resp = client.post(
        "/merges/whatever/reuse-resolutions",
        json={"revision": 1},
    )
    assert resp.status_code == 422
