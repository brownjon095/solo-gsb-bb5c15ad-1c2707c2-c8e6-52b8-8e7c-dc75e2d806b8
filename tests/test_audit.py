"""End-to-end tests for the adjudication audit trail.

Every successful resolutions / rule-resolutions / reuse-resolutions /
draft-commit request appends exactly one event to the merge order's audit
chain inside the same SQLite transaction as the merge update; failed
requests and zero-match no-ops leave no events. GET /merges/{id}/audit
returns the events in sequence plus the chain verification verdict,
naming the first anomalous sequence number when the chain is broken.
"""

import hashlib
import json as _json
import os
import tempfile

os.environ["DATABASE_PATH"] = os.path.join(tempfile.mkdtemp(), "test_audit.db")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.audit import GENESIS_PREV_HASH  # noqa: E402
from app.main import app  # noqa: E402
from app.patch import canonical_json  # noqa: E402


@pytest.fixture(scope="module")
def client():
    # Context-manager form runs the lifespan, which creates the tables.
    with TestClient(app) as c:
        yield c


def make_merge(client, base, left, right, schema=None):
    payload = {"base": base, "left": left, "right": right}
    if schema is not None:
        payload["result_schema"] = schema
    resp = client.post("/merges", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def three_conflict_merge(client):
    """Three independent conflicts /a, /b, /c."""
    return make_merge(
        client,
        base={"a": 1, "b": 1, "c": 1},
        left={"a": 2, "b": 2, "c": 2},
        right={"a": 3, "b": 3, "c": 3},
    )


def resolve(client, merge_id, revision, resolutions):
    resp = client.post(
        f"/merges/{merge_id}/resolutions",
        json={"revision": revision, "resolutions": resolutions},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def audit(client, merge_id):
    resp = client.get(f"/merges/{merge_id}/audit")
    assert resp.status_code == 200, resp.text
    return resp.json()


def recompute_hash(merge_id, event):
    """Independently recompute an event's chain hash from its fields."""
    payload = {
        "merge_id": merge_id,
        "seq": event["seq"],
        "method": event["method"],
        "revision_before": event["revision_before"],
        "revision_after": event["revision_after"],
        "pointers": event["pointers"],
        "detail": event["detail"],
        "prev_hash": event["prev_hash"],
        "created_at": event["created_at"],
    }
    return hashlib.sha256(canonical_json(payload)).hexdigest()


# ---------------------------------------------------------------------------
# Empty trail and unknown merge
# ---------------------------------------------------------------------------


def test_audit_of_fresh_merge_is_empty_and_valid(client):
    merge = three_conflict_merge(client)
    body = audit(client, merge["merge_id"])
    assert body["merge_id"] == merge["merge_id"]
    assert body["event_count"] == 0
    assert body["events"] == []
    assert body["chain_valid"] is True
    assert body["first_invalid_seq"] is None


def test_immediately_resolved_merge_has_no_events(client):
    # No conflicts -> resolved at creation; creation is not an adjudication.
    merge = make_merge(client, {"a": 1}, {"a": 2}, {"a": 2})
    assert merge["status"] == "resolved"
    assert audit(client, merge["merge_id"])["events"] == []


def test_audit_unknown_merge_returns_404(client):
    assert client.get("/merges/does-not-exist/audit").status_code == 404


# ---------------------------------------------------------------------------
# Explicit resolutions
# ---------------------------------------------------------------------------


def test_resolution_batches_append_chained_events(client):
    merge = three_conflict_merge(client)
    mid = merge["merge_id"]

    resolve(client, mid, 1, [{"pointer": "/a", "choice": "left"}])
    resolve(client, mid, 2, [{"pointer": "/b", "value": 20}])
    done = resolve(client, mid, 3, [{"pointer": "/c", "choice": "right"}])
    assert done["status"] == "resolved"

    body = audit(client, mid)
    assert body["chain_valid"] is True
    assert body["first_invalid_seq"] is None
    assert body["event_count"] == 3

    events = body["events"]
    assert [e["seq"] for e in events] == [1, 2, 3]
    assert [e["method"] for e in events] == ["resolutions"] * 3
    assert [(e["revision_before"], e["revision_after"]) for e in events] == [
        (1, 2),
        (2, 3),
        (3, 4),
    ]
    assert [e["pointers"] for e in events] == [["/a"], ["/b"], ["/c"]]
    assert all(e["valid"] is True for e in events)
    assert all(len(e["hash"]) == 64 for e in events)
    assert all(e["created_at"] for e in events)

    # The chain links: genesis prev-hash first, then each predecessor.
    assert events[0]["prev_hash"] == GENESIS_PREV_HASH
    for previous, current in zip(events, events[1:]):
        assert current["prev_hash"] == previous["hash"]

    # Every hash is the SHA-256 of the event's canonical payload.
    for event in events:
        assert event["hash"] == recompute_hash(mid, event)

    # Detail records the per-pointer adjudication.
    assert events[0]["detail"] == {"items": [{"pointer": "/a", "choice": "left"}]}
    assert events[1]["detail"] == {"items": [{"pointer": "/b", "value": 20}]}
    assert events[2]["detail"] == {"items": [{"pointer": "/c", "choice": "right"}]}


def test_one_event_per_batch_covering_every_effective_pointer(client):
    merge = three_conflict_merge(client)
    mid = merge["merge_id"]
    resolve(
        client,
        mid,
        1,
        [
            {"pointer": "/a", "choice": "left"},
            {"pointer": "/b", "value": None},
            {"pointer": "/c", "choice": "right"},
        ],
    )
    body = audit(client, mid)
    assert body["event_count"] == 1
    event = body["events"][0]
    # Pointers are recorded in batch (application) order.
    assert event["pointers"] == ["/a", "/b", "/c"]
    assert event["revision_before"] == 1 and event["revision_after"] == 2
    # An explicit null value is preserved in the detail.
    assert event["detail"]["items"][1] == {"pointer": "/b", "value": None}


def test_failed_resolution_requests_leave_no_events(client):
    schema = {
        "type": "object",
        "properties": {"a": {"type": "integer", "minimum": 10}},
    }
    merge = three_conflict_merge(client)
    mid = merge["merge_id"]
    gated = make_merge(
        client,
        base={"a": 1},
        left={"a": 2},
        right={"a": 3},
        schema=schema,
    )
    gid = gated["merge_id"]

    # Unknown pointer -> 400.
    resp = client.post(
        f"/merges/{mid}/resolutions",
        json={"revision": 1, "resolutions": [{"pointer": "/nope", "value": 1}]},
    )
    assert resp.status_code == 400
    # Stale revision -> 409.
    resp = client.post(
        f"/merges/{mid}/resolutions",
        json={"revision": 999, "resolutions": [{"pointer": "/a", "choice": "left"}]},
    )
    assert resp.status_code == 409
    # Final-batch schema violation -> 422.
    resp = client.post(
        f"/merges/{gid}/resolutions",
        json={"revision": 1, "resolutions": [{"pointer": "/a", "value": 5}]},
    )
    assert resp.status_code == 422
    # A dry-run preview never writes -> no event either.
    resp = client.post(
        f"/merges/{mid}/resolution-preview",
        json={"revision": 1, "resolutions": [{"pointer": "/a", "choice": "left"}]},
    )
    assert resp.status_code == 200

    assert audit(client, mid)["events"] == []
    assert audit(client, gid)["events"] == []

    # Resolving for real afterwards still produces a valid chain, proving
    # the failed requests rolled back completely (no dangling events).
    resolve(client, mid, 1, [{"pointer": "/a", "choice": "left"}])
    body = audit(client, mid)
    assert body["event_count"] == 1
    assert body["chain_valid"] is True
    assert body["events"][0]["prev_hash"] == GENESIS_PREV_HASH


# ---------------------------------------------------------------------------
# Rule resolutions
# ---------------------------------------------------------------------------


def test_rule_resolutions_append_one_event_with_winner_pointers(client):
    merge = three_conflict_merge(client)
    mid = merge["merge_id"]

    # Zero hits first: a successful no-op that must leave no event.
    resp = client.post(
        f"/merges/{mid}/rule-resolutions",
        json={"revision": 1, "rules": [{"left": "/somewhere/else/*"}]},
    )
    assert resp.status_code == 200
    assert resp.json()["applied"] is False
    assert audit(client, mid)["events"] == []

    resp = client.post(
        f"/merges/{mid}/rule-resolutions",
        json={"revision": 1, "rules": [{"right": "/a"}, {"left": "/b"}]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["applied"] is True

    body = audit(client, mid)
    assert body["event_count"] == 1
    event = body["events"][0]
    assert event["method"] == "rule-resolutions"
    assert (event["revision_before"], event["revision_after"]) == (1, 2)
    # Winners apply (and are recorded) in sorted pointer order.
    assert event["pointers"] == ["/a", "/b"]
    assert event["detail"]["items"] == [
        {"pointer": "/a", "side": "right", "rule_index": 0},
        {"pointer": "/b", "side": "left", "rule_index": 1},
    ]
    assert body["chain_valid"] is True
    assert event["hash"] == recompute_hash(mid, event)


def test_failed_rule_requests_leave_no_events(client):
    merge = three_conflict_merge(client)
    mid = merge["merge_id"]

    # Invalid pattern -> 400.
    resp = client.post(
        f"/merges/{mid}/rule-resolutions",
        json={"revision": 1, "rules": [{"left": "/a/**/b"}]},
    )
    assert resp.status_code == 400
    # Stale revision -> 409.
    resp = client.post(
        f"/merges/{mid}/rule-resolutions",
        json={"revision": 999, "rules": [{"left": "/a"}]},
    )
    assert resp.status_code == 409

    assert audit(client, mid)["events"] == []


# ---------------------------------------------------------------------------
# Historical reuse
# ---------------------------------------------------------------------------


def test_reuse_appends_event_only_when_something_matched(client):
    source = three_conflict_merge(client)
    sid = source["merge_id"]
    resolve(
        client,
        sid,
        1,
        [
            {"pointer": "/a", "choice": "left"},
            {"pointer": "/b", "value": "B"},
            {"pointer": "/c", "choice": "right"},
        ],
    )

    target = three_conflict_merge(client)
    mid = target["merge_id"]

    # A reuse run with zero hits is a no-op: no event, no revision bump.
    other = make_merge(client, {"x": 1}, {"x": 2}, {"x": 3})
    resp = client.post(
        f"/merges/{other['merge_id']}/reuse-resolutions",
        json={"revision": 1, "source_merge_id": sid},
    )
    assert resp.status_code == 200
    assert resp.json()["applied"] is False
    assert audit(client, other["merge_id"])["events"] == []

    resp = client.post(
        f"/merges/{mid}/reuse-resolutions",
        json={"revision": 1, "source_merge_id": sid},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["applied"] is True

    body = audit(client, mid)
    assert body["event_count"] == 1
    event = body["events"][0]
    assert event["method"] == "reuse-resolutions"
    assert (event["revision_before"], event["revision_after"]) == (1, 2)
    assert event["pointers"] == ["/a", "/b", "/c"]
    assert event["detail"]["source_merge_id"] == sid
    assert event["detail"]["items"] == [
        {"pointer": "/a", "action": "set", "value": 2},
        {"pointer": "/b", "action": "set", "value": "B"},
        {"pointer": "/c", "action": "set", "value": 3},
    ]
    assert body["chain_valid"] is True
    assert event["hash"] == recompute_hash(mid, event)

    # A rejected reuse (stale revision, invalid source) leaves no event.
    resp = client.post(
        f"/merges/{mid}/reuse-resolutions",
        json={"revision": 1, "source_merge_id": sid},
    )
    assert resp.status_code == 409
    # The target is fully resolved now; use the still-pending zero-hit
    # merge for the invalid-source rejection.
    resp = client.post(
        f"/merges/{other['merge_id']}/reuse-resolutions",
        json={"revision": 1, "source_merge_id": "no-such-source"},
    )
    assert resp.status_code == 400
    assert audit(client, mid)["event_count"] == 1
    assert audit(client, other["merge_id"])["events"] == []


# ---------------------------------------------------------------------------
# Draft commit
# ---------------------------------------------------------------------------


def test_draft_commit_appends_event_but_edits_do_not(client):
    merge = three_conflict_merge(client)
    mid = merge["merge_id"]

    resp = client.post(f"/merges/{mid}/drafts")
    assert resp.status_code == 201
    did = resp.json()["draft_id"]

    # Creating and editing the draft touches only the draft: no events.
    resp = client.post(
        f"/merges/{mid}/drafts/{did}/resolutions",
        json={
            "resolutions": [
                {"pointer": "/a", "choice": "left"},
                {"pointer": "/b", "value": 20},
            ]
        },
    )
    assert resp.status_code == 200
    assert client.get(f"/merges/{mid}/drafts/{did}/preview").status_code == 200
    assert audit(client, mid)["events"] == []

    resp = client.post(f"/merges/{mid}/drafts/{did}/commit")
    assert resp.status_code == 200, resp.text

    body = audit(client, mid)
    assert body["event_count"] == 1
    event = body["events"][0]
    assert event["method"] == "draft-commit"
    assert (event["revision_before"], event["revision_after"]) == (1, 2)
    assert event["pointers"] == ["/a", "/b"]
    assert event["detail"]["draft_id"] == did
    assert event["detail"]["items"] == [
        {"pointer": "/a", "choice": "left"},
        {"pointer": "/b", "value": 20},
    ]
    assert body["chain_valid"] is True
    assert event["hash"] == recompute_hash(mid, event)


def test_failed_draft_commit_leaves_no_event(client):
    merge = three_conflict_merge(client)
    mid = merge["merge_id"]
    did = client.post(f"/merges/{mid}/drafts").json()["draft_id"]
    client.post(
        f"/merges/{mid}/drafts/{did}/resolutions",
        json={"resolutions": [{"pointer": "/a", "choice": "left"}]},
    )

    # Another request moves the merge on, so the pinned commit fails: 409.
    resolve(client, mid, 1, [{"pointer": "/b", "choice": "right"}])
    resp = client.post(f"/merges/{mid}/drafts/{did}/commit")
    assert resp.status_code == 409

    # Only the successful resolutions batch is on the trail.
    body = audit(client, mid)
    assert body["event_count"] == 1
    assert body["events"][0]["method"] == "resolutions"
    assert body["chain_valid"] is True


# ---------------------------------------------------------------------------
# Mixed methods on one merge: a single per-merge chain
# ---------------------------------------------------------------------------


def test_mixed_methods_share_one_chain_in_request_order(client):
    source = make_merge(client, {"a": 1}, {"a": 2}, {"a": 3})
    sid = source["merge_id"]
    resolve(client, sid, 1, [{"pointer": "/a", "choice": "left"}])

    merge = make_merge(
        client,
        base={"a": 1, "b": 1, "c": 1},
        left={"a": 2, "b": 2, "c": 2},
        right={"a": 3, "b": 3, "c": 3},
    )
    mid = merge["merge_id"]

    # 1) reuse resolves /a; 2) a rule resolves /b; 3) a draft commit /c.
    resp = client.post(
        f"/merges/{mid}/reuse-resolutions",
        json={"revision": 1, "source_merge_id": sid},
    )
    assert resp.status_code == 200
    resp = client.post(
        f"/merges/{mid}/rule-resolutions",
        json={"revision": 2, "rules": [{"left": "/b"}]},
    )
    assert resp.status_code == 200
    did = client.post(f"/merges/{mid}/drafts").json()["draft_id"]
    client.post(
        f"/merges/{mid}/drafts/{did}/resolutions",
        json={"resolutions": [{"pointer": "/c", "choice": "right"}]},
    )
    resp = client.post(f"/merges/{mid}/drafts/{did}/commit")
    assert resp.status_code == 200
    assert resp.json()["status"] == "resolved"

    body = audit(client, mid)
    assert body["chain_valid"] is True
    assert [e["method"] for e in body["events"]] == [
        "reuse-resolutions",
        "rule-resolutions",
        "draft-commit",
    ]
    assert [e["seq"] for e in body["events"]] == [1, 2, 3]
    assert [(e["revision_before"], e["revision_after"]) for e in body["events"]] == [
        (1, 2),
        (2, 3),
        (3, 4),
    ]
    assert [e["pointers"] for e in body["events"]] == [["/a"], ["/b"], ["/c"]]
    for event in body["events"]:
        assert event["hash"] == recompute_hash(mid, event)


# ---------------------------------------------------------------------------
# Chain verification against tampering
# ---------------------------------------------------------------------------


def merge_with_events(client, batches=3):
    """A merge resolved through `batches` single-pointer batches."""
    merge = three_conflict_merge(client)
    mid = merge["merge_id"]
    pointers = ["/a", "/b", "/c"][:batches]
    for revision, pointer in enumerate(pointers, start=1):
        resolve(client, mid, revision, [{"pointer": pointer, "choice": "left"}])
    return mid


def test_chain_detects_tampered_event_content(client):
    mid = merge_with_events(client)
    # Rewrite the recorded pointers of the middle event in place.
    db.execute(
        "UPDATE merge_audit_events SET pointers = ? WHERE merge_id = ? AND seq = 2",
        (_json.dumps(["/tampered"]), mid),
    )
    body = audit(client, mid)
    assert body["chain_valid"] is False
    # The stored hash no longer matches the recomputed payload at seq 2;
    # the untouched successor still verifies against the stored chain.
    assert body["first_invalid_seq"] == 2
    assert [e["valid"] for e in body["events"]] == [True, False, True]


def test_chain_detects_tampered_hash_link(client):
    mid = merge_with_events(client)
    db.execute(
        "UPDATE merge_audit_events SET prev_hash = ? WHERE merge_id = ? AND seq = 2",
        ("f" * 64, mid),
    )
    body = audit(client, mid)
    assert body["chain_valid"] is False
    assert body["first_invalid_seq"] == 2
    assert [e["valid"] for e in body["events"]] == [True, False, True]


def test_chain_detects_deleted_event(client):
    mid = merge_with_events(client)
    db.execute(
        "DELETE FROM merge_audit_events WHERE merge_id = ? AND seq = 2", (mid,)
    )
    body = audit(client, mid)
    assert body["chain_valid"] is False
    # Event 3 is the first anomaly: the sequence jumps and its prev_hash
    # points at the deleted event's hash.
    assert body["first_invalid_seq"] == 3
    assert [e["seq"] for e in body["events"]] == [1, 3]
    assert [e["valid"] for e in body["events"]] == [True, False]


def test_chain_detects_tampered_first_event(client):
    mid = merge_with_events(client, batches=2)
    db.execute(
        "UPDATE merge_audit_events SET method = ? WHERE merge_id = ? AND seq = 1",
        ("reuse-resolutions", mid),
    )
    body = audit(client, mid)
    assert body["chain_valid"] is False
    assert body["first_invalid_seq"] == 1
    assert [e["valid"] for e in body["events"]] == [False, True]


# ---------------------------------------------------------------------------
# Response compatibility
# ---------------------------------------------------------------------------


def test_existing_responses_carry_no_audit_fields(client):
    merge = three_conflict_merge(client)
    mid = merge["merge_id"]

    view_keys = {
        "merge_id",
        "status",
        "revision",
        "document",
        "conflicts",
        "conflict_count",
        "result",
        "created_at",
        "updated_at",
    }
    resp = client.post(
        f"/merges/{mid}/resolutions",
        json={"revision": 1, "resolutions": [{"pointer": "/a", "choice": "left"}]},
    )
    assert resp.status_code == 200
    assert set(resp.json()) == view_keys

    resp = client.post(
        f"/merges/{mid}/rule-resolutions",
        json={"revision": 2, "rules": [{"left": "/b"}]},
    )
    assert resp.status_code == 200
    assert set(resp.json()) == view_keys | {
        "rule_matches",
        "unmatched_conflicts",
        "applied",
    }

    assert set(client.get(f"/merges/{mid}").json()) == view_keys
