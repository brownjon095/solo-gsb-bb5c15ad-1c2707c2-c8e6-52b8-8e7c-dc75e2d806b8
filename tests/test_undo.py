"""End-to-end tests for "undo last adjudication batch".

``POST /merges/{merge_id}/resolution-undo`` takes the current revision and,
on a pending merge that still has an effective (not yet compensated)
adjudication event, rebuilds the document and the open conflicts from the
immutable initial conflict snapshot by replaying the audit history with
the most recent effective event skipped. The revision bumps by one and a
chained ``resolution-undo`` event is appended in the same transaction,
naming the compensated event's sequence number and the restored pointers.
Stale revision / resolved merge / nothing left to undo -> 409, unknown
merge -> 404, and every failure changes nothing and leaves no event.
"""

import os
import tempfile

os.environ["DATABASE_PATH"] = os.path.join(tempfile.mkdtemp(), "test_undo.db")

import json  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import db, undo  # noqa: E402
from app.audit import GENESIS_PREV_HASH  # noqa: E402
from app.main import app  # noqa: E402


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


def three_conflict_merge(client):
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


def do_undo(client, merge_id, revision):
    return client.post(
        f"/merges/{merge_id}/resolution-undo", json={"revision": revision}
    )


def undo_ok(client, merge_id, revision):
    resp = do_undo(client, merge_id, revision)
    assert resp.status_code == 200, resp.text
    return resp.json()


def audit(client, merge_id):
    resp = client.get(f"/merges/{merge_id}/audit")
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Basic rebuild semantics
# ---------------------------------------------------------------------------


def test_undo_restores_initial_snapshot_after_first_batch(client):
    merge = three_conflict_merge(client)
    mid = merge["merge_id"]
    initial_document = merge["document"]
    initial_conflicts = merge["conflicts"]

    resolve(client, mid, 1, [
        {"pointer": "/a", "choice": "left"},
        {"pointer": "/b", "value": 20},
    ])
    body = undo_ok(client, mid, 2)

    # State is rebuilt, not reverse-edited: document and conflicts are
    # byte-for-byte the immutable initial snapshot.
    assert body["status"] == "pending"
    assert body["result"] is None
    assert body["revision"] == 3
    assert body["document"] == initial_document
    assert body["conflicts"] == initial_conflicts
    assert body["conflict_count"] == 3
    assert body["restored_pointers"] == ["/a", "/b"]
    assert body["restored_count"] == 2
    assert body["undone_seq"] == 1
    assert body["undone_method"] == "resolutions"

    # The plain GET view reports exactly the same rebuilt state.
    fetched = client.get(f"/merges/{mid}").json()
    assert fetched["document"] == initial_document
    assert fetched["conflicts"] == initial_conflicts
    assert fetched["revision"] == 3


def test_undo_of_later_batch_keeps_earlier_batches_effect(client):
    merge = three_conflict_merge(client)
    mid = merge["merge_id"]
    resolve(client, mid, 1, [{"pointer": "/a", "choice": "left"}])
    resolve(client, mid, 2, [{"pointer": "/b", "value": 20}])

    body = undo_ok(client, mid, 3)
    assert body["undone_seq"] == 2
    assert body["restored_pointers"] == ["/b"]
    # The first batch still applies; only /b reopens.
    assert body["document"]["a"] == 2
    assert body["document"]["b"] == {"__merge_conflict__": "/b"}
    assert body["document"]["c"] == {"__merge_conflict__": "/c"}
    assert [c["pointer"] for c in body["conflicts"]] == ["/b", "/c"]
    assert body["revision"] == 4

    # Undo again walks to the first batch; /a reopens too.
    body = undo_ok(client, mid, 4)
    assert body["undone_seq"] == 1
    assert body["restored_pointers"] == ["/a"]
    assert [c["pointer"] for c in body["conflicts"]] == ["/a", "/b", "/c"]
    assert body["revision"] == 5


def test_explicit_null_value_reverts_to_placeholder(client):
    merge = three_conflict_merge(client)
    mid = merge["merge_id"]
    resolve(client, mid, 1, [{"pointer": "/a", "value": None}])
    body = undo_ok(client, mid, 2)
    assert body["document"]["a"] == {"__merge_conflict__": "/a"}
    assert [c["pointer"] for c in body["conflicts"]][0] == "/a"


def test_root_pointer_conflict_resolves_to_immutable_result(client):
    # A whole-document scalar conflict lives at pointer "". Resolving it
    # leaves no conflict, so the merge is resolved and immutable: undo is
    # refused like for any other resolved merge.
    merge = make_merge(client, 1, 2, 3)
    mid = merge["merge_id"]
    assert [c["pointer"] for c in merge["conflicts"]] == [""]
    done = resolve(client, mid, 1, [{"pointer": "", "choice": "left"}])
    assert done["status"] == "resolved" and done["result"] == 2
    assert do_undo(client, mid, 2).status_code == 409


class _Row(dict):
    """Dict-backed stand-in for a sqlite3.Row (subscript access)."""


def test_rebuild_state_replaces_document_for_root_pointer():
    # Defensive replay path: a root-pointer adjudication replaces the
    # whole document rather than mutating it in place.
    row = _Row(
        initial_document=json.dumps({"__merge_conflict__": ""}),
        source_conflicts=json.dumps(
            [{"pointer": "", "base": 1, "left": 2, "right": 3, "missing": []}]
        ),
    )
    event = _Row(
        seq=1,
        method="resolutions",
        pointers=json.dumps([""]),
        detail=json.dumps({"items": [{"pointer": "", "value": 42}]}),
    )

    document, conflicts = undo.rebuild_state(row, [event])
    assert document == 42
    assert conflicts == []

    # With the sole event skipped, the placeholder snapshot comes back.
    document, conflicts = undo.rebuild_state(row, [event], skip_seq=1)
    assert document == {"__merge_conflict__": ""}
    assert [c["pointer"] for c in conflicts] == [""]


def test_undo_restores_conflict_candidates_and_missing_semantics(client):
    # A delete/modify conflict: left removed /gone, right changed it.
    merge = make_merge(
        client,
        base={"keep": 1, "gone": 10},
        left={"keep": 2},
        right={"keep": 3, "gone": 30},
    )
    mid = merge["merge_id"]
    resolve(client, mid, 1, [{"pointer": "/gone", "choice": "left"}])
    body = undo_ok(client, mid, 2)
    conflict = next(c for c in body["conflicts"] if c["pointer"] == "/gone")
    initial = next(c for c in merge["conflicts"] if c["pointer"] == "/gone")
    assert conflict == initial
    assert conflict["missing"] == ["left"]
    assert body["document"]["gone"] == {"__merge_conflict__": "/gone"}


def test_replay_rebuilds_current_state_without_skipping_anything(client):
    # Invariant used by undo: replaying the whole audit history from the
    # snapshot reproduces the live document and open conflicts exactly.
    merge = three_conflict_merge(client)
    mid = merge["merge_id"]
    resolve(client, mid, 1, [{"pointer": "/a", "choice": "right"}])
    resolve(client, mid, 2, [{"pointer": "/b", "value": 7}])

    row = db.query_one("SELECT * FROM merges WHERE id = ?", (mid,))
    events = db.query_all(
        "SELECT * FROM merge_audit_events WHERE merge_id = ? ORDER BY seq ASC",
        (mid,),
    )
    document, conflicts = undo.rebuild_state(row, events)
    assert document == json.loads(row["document"])
    assert conflicts == json.loads(row["conflicts"])
    assert [c["pointer"] for c in conflicts] == ["/c"]


# ---------------------------------------------------------------------------
# Error cases: nothing changes, no events
# ---------------------------------------------------------------------------


def test_undo_without_any_adjudication_is_409(client):
    merge = three_conflict_merge(client)
    mid = merge["merge_id"]
    resp = do_undo(client, mid, 1)
    assert resp.status_code == 409
    assert "no effective adjudication" in resp.json()["detail"]
    # State untouched, no event appended.
    fetched = client.get(f"/merges/{mid}").json()
    assert fetched["revision"] == 1
    assert fetched["conflict_count"] == 3
    assert audit(client, mid)["events"] == []


def test_undo_after_everything_compensated_is_409(client):
    merge = three_conflict_merge(client)
    mid = merge["merge_id"]
    resolve(client, mid, 1, [{"pointer": "/a", "choice": "left"}])
    undo_ok(client, mid, 2)
    resp = do_undo(client, mid, 3)
    assert resp.status_code == 409
    # The rejected second undo left no event: seq 3 does not exist.
    body = audit(client, mid)
    assert [e["seq"] for e in body["events"]] == [1, 2]
    assert [e["method"] for e in body["events"]] == [
        "resolutions",
        "resolution-undo",
    ]


def test_stale_revision_is_409_and_changes_nothing(client):
    merge = three_conflict_merge(client)
    mid = merge["merge_id"]
    resolve(client, mid, 1, [{"pointer": "/a", "choice": "left"}])
    resp = do_undo(client, mid, 1)
    assert resp.status_code == 409
    assert resp.json()["current_revision"] == 2

    fetched = client.get(f"/merges/{mid}").json()
    assert fetched["revision"] == 2
    assert [c["pointer"] for c in fetched["conflicts"]] == ["/b", "/c"]
    # Only the resolution event exists; the failed undo wrote nothing.
    assert [e["method"] for e in audit(client, mid)["events"]] == ["resolutions"]


def test_resolved_merge_stays_immutable(client):
    merge = three_conflict_merge(client)
    mid = merge["merge_id"]
    done = resolve(client, mid, 1, [
        {"pointer": "/a", "choice": "left"},
        {"pointer": "/b", "choice": "right"},
        {"pointer": "/c", "value": 9},
    ])
    assert done["status"] == "resolved"
    result = done["result"]

    resp = do_undo(client, mid, 2)
    assert resp.status_code == 409
    assert "immutable" in resp.json()["detail"]

    fetched = client.get(f"/merges/{mid}").json()
    assert fetched["status"] == "resolved"
    assert fetched["result"] == result
    assert fetched["revision"] == 2
    # No undo event on the final trail.
    assert [e["method"] for e in audit(client, mid)["events"]] == ["resolutions"]


def test_undo_unknown_merge_returns_404(client):
    assert do_undo(client, "no-such-merge", 1).status_code == 404


def test_immediately_resolved_merge_cannot_undo(client):
    merge = make_merge(client, {"a": 1}, {"a": 2}, {"a": 2})
    assert merge["status"] == "resolved"
    assert do_undo(client, merge["merge_id"], 1).status_code == 409
    assert audit(client, merge["merge_id"])["events"] == []


# ---------------------------------------------------------------------------
# Undo / re-resolve interleaving: compensation skipping
# ---------------------------------------------------------------------------


def test_new_resolution_after_undo_is_undoable_again(client):
    merge = three_conflict_merge(client)
    mid = merge["merge_id"]
    resolve(client, mid, 1, [{"pointer": "/a", "choice": "left"}])
    undo_ok(client, mid, 2)

    # Re-resolve the reopened conflict differently.
    done = resolve(client, mid, 3, [{"pointer": "/a", "choice": "right"}])
    assert done["document"]["a"] == 3

    body = undo_ok(client, mid, 4)
    # The newest *effective* adjudication is the re-resolution (seq 3); the
    # compensated seq 1 is skipped, as is the undo (seq 2).
    assert body["undone_seq"] == 3
    assert body["restored_pointers"] == ["/a"]
    assert body["document"]["a"] == {"__merge_conflict__": "/a"}
    assert body["revision"] == 5

    # Nothing effective remains: seq 1 and seq 3 are both compensated.
    assert do_undo(client, mid, 5).status_code == 409


def test_undo_only_then_redoes_preserve_other_batches(client):
    merge = three_conflict_merge(client)
    mid = merge["merge_id"]
    resolve(client, mid, 1, [{"pointer": "/a", "choice": "left"}])   # seq 1
    resolve(client, mid, 2, [{"pointer": "/b", "choice": "right"}])  # seq 2
    undo_ok(client, mid, 3)                                           # seq 3: compensates 2
    resolve(client, mid, 4, [{"pointer": "/b", "value": 77}])        # seq 4
    body = undo_ok(client, mid, 5)                                    # seq 5: compensates 4
    assert body["undone_seq"] == 4
    assert body["restored_pointers"] == ["/b"]
    assert body["document"]["a"] == 2  # seq 1 still active
    assert body["document"]["b"] == {"__merge_conflict__": "/b"}
    body = undo_ok(client, mid, 6)                                    # seq 6: compensates 1
    assert body["undone_seq"] == 1
    assert [c["pointer"] for c in body["conflicts"]] == ["/a", "/b", "/c"]
    assert do_undo(client, mid, 7).status_code == 409


# ---------------------------------------------------------------------------
# Undo across every adjudication method
# ---------------------------------------------------------------------------


def test_undo_rule_batch_restores_candidates(client):
    merge = three_conflict_merge(client)
    mid = merge["merge_id"]
    resp = client.post(
        f"/merges/{mid}/rule-resolutions",
        json={"revision": 1, "rules": [{"right": "/a"}, {"left": "/b"}]},
    )
    assert resp.status_code == 200
    body = undo_ok(client, mid, 2)
    assert body["undone_method"] == "rule-resolutions"
    assert body["restored_pointers"] == ["/a", "/b"]
    assert body["document"]["a"] == {"__merge_conflict__": "/a"}
    assert body["document"]["b"] == {"__merge_conflict__": "/b"}
    assert [c["pointer"] for c in body["conflicts"]] == ["/a", "/b", "/c"]
    # The reopened conflict can now take a different adjudication.
    done = resolve(client, mid, 3, [{"pointer": "/a", "value": 42}])
    assert done["document"]["a"] == 42


def test_undo_reuse_batch_including_deletions(client):
    source = make_merge(
        client,
        base={"keep": 1, "gone": 10},
        left={"keep": 2},
        right={"keep": 3, "gone": 30},
    )
    sid = source["merge_id"]
    resolve(client, sid, 1, [
        {"pointer": "/gone", "choice": "left"},   # deletion
        {"pointer": "/keep", "choice": "right"},
    ])

    target = make_merge(
        client,
        base={"keep": 1, "gone": 10, "other": 1},
        left={"keep": 2, "other": 2},
        right={"keep": 3, "gone": 30, "other": 3},
    )
    mid = target["merge_id"]
    resp = client.post(
        f"/merges/{mid}/reuse-resolutions",
        json={"revision": 1, "source_merge_id": sid},
    )
    assert resp.status_code == 200
    assert "gone" not in resp.json()["document"]

    body = undo_ok(client, mid, 2)
    assert body["undone_method"] == "reuse-resolutions"
    assert body["restored_pointers"] == ["/gone", "/keep"]
    assert body["document"]["gone"] == {"__merge_conflict__": "/gone"}
    assert body["document"]["keep"] == {"__merge_conflict__": "/keep"}
    assert body["document"]["other"] == {"__merge_conflict__": "/other"}
    assert [c["pointer"] for c in body["conflicts"]] == ["/gone", "/keep", "/other"]


def test_undo_draft_commit(client):
    merge = three_conflict_merge(client)
    mid = merge["merge_id"]
    did = client.post(f"/merges/{mid}/drafts").json()["draft_id"]
    client.post(
        f"/merges/{mid}/drafts/{did}/resolutions",
        json={"resolutions": [
            {"pointer": "/a", "choice": "left"},
            {"pointer": "/b", "value": 55},
        ]},
    )
    resp = client.post(f"/merges/{mid}/drafts/{did}/commit")
    assert resp.status_code == 200

    body = undo_ok(client, mid, 2)
    assert body["undone_method"] == "draft-commit"
    assert body["restored_pointers"] == ["/a", "/b"]
    assert body["document"]["a"] == {"__merge_conflict__": "/a"}
    assert body["document"]["b"] == {"__merge_conflict__": "/b"}
    # Committing and undoing did not delete or reopen the draft row; it is
    # simply already committed, so re-committing it yields 409.
    assert client.post(f"/merges/{mid}/drafts/{did}/commit").status_code == 409


def test_undo_mixed_methods_in_reverse_order(client):
    source = make_merge(client, {"a": 1}, {"a": 2}, {"a": 3})
    sid = source["merge_id"]
    resolve(client, sid, 1, [{"pointer": "/a", "choice": "left"}])

    merge = three_conflict_merge(client)
    mid = merge["merge_id"]
    client.post(
        f"/merges/{mid}/reuse-resolutions",
        json={"revision": 1, "source_merge_id": sid},
    )
    client.post(
        f"/merges/{mid}/rule-resolutions",
        json={"revision": 2, "rules": [{"left": "/b"}]},
    )
    did = client.post(f"/merges/{mid}/drafts").json()["draft_id"]
    client.post(
        f"/merges/{mid}/drafts/{did}/resolutions",
        json={"resolutions": [{"pointer": "/c", "choice": "right"}]},
    )
    assert client.post(f"/merges/{mid}/drafts/{did}/commit").status_code == 200
    # The merge is now resolved, so it must first be impossible to undo...
    assert do_undo(client, mid, 4).status_code == 409

    # Redo the flow but leave one conflict open, then unwind everything.
    merge = three_conflict_merge(client)
    mid = merge["merge_id"]
    client.post(
        f"/merges/{mid}/reuse-resolutions",
        json={"revision": 1, "source_merge_id": sid},
    )
    client.post(
        f"/merges/{mid}/rule-resolutions",
        json={"revision": 2, "rules": [{"left": "/b"}]},
    )
    body = undo_ok(client, mid, 3)
    assert body["undone_method"] == "rule-resolutions"
    assert body["document"]["a"] == 2
    assert body["document"]["b"] == {"__merge_conflict__": "/b"}
    body = undo_ok(client, mid, 4)
    assert body["undone_method"] == "reuse-resolutions"
    assert body["document"]["a"] == {"__merge_conflict__": "/a"}
    assert [c["pointer"] for c in body["conflicts"]] == ["/a", "/b", "/c"]
    assert do_undo(client, mid, 5).status_code == 409


# ---------------------------------------------------------------------------
# Audit chain: undo events, compensation marks, verifiability
# ---------------------------------------------------------------------------


def test_undo_appends_a_verifiable_chained_event(client):
    merge = three_conflict_merge(client)
    mid = merge["merge_id"]
    resolve(client, mid, 1, [{"pointer": "/a", "choice": "left"}])
    undo_ok(client, mid, 2)

    body = audit(client, mid)
    assert body["chain_valid"] is True
    assert body["first_invalid_seq"] is None
    assert [e["seq"] for e in body["events"]] == [1, 2]

    adjudication, undo_event = body["events"]
    assert undo_event["method"] == "resolution-undo"
    assert (undo_event["revision_before"], undo_event["revision_after"]) == (2, 3)
    assert undo_event["pointers"] == ["/a"]
    assert undo_event["detail"] == {
        "undone_seq": 1,
        "restored_pointers": ["/a"],
        "undone_method": "resolutions",
    }
    assert adjudication["prev_hash"] == GENESIS_PREV_HASH
    assert undo_event["prev_hash"] == adjudication["hash"]
    assert undo_event["valid"] is True


def test_chain_stays_valid_through_undo_and_reresolve_cycles(client):
    merge = three_conflict_merge(client)
    mid = merge["merge_id"]
    resolve(client, mid, 1, [{"pointer": "/a", "choice": "left"}])
    undo_ok(client, mid, 2)
    resolve(client, mid, 3, [{"pointer": "/a", "value": 8}])
    undo_ok(client, mid, 4)
    resolve(client, mid, 5, [{"pointer": "/b", "choice": "right"}])
    undo_ok(client, mid, 6)

    body = audit(client, mid)
    assert body["chain_valid"] is True
    # Sequences are monotonic append-only; revisions never go backwards.
    assert [e["seq"] for e in body["events"]] == [1, 2, 3, 4, 5, 6]
    assert [(e["revision_before"], e["revision_after"]) for e in body["events"]] == [
        (1, 2),
        (2, 3),
        (3, 4),
        (4, 5),
        (5, 6),
        (6, 7),
    ]
    assert [e["detail"].get("undone_seq") for e in body["events"][1::2]] == [1, 3, 5]


def test_undo_events_are_returned_by_audit_and_counted(client):
    merge = three_conflict_merge(client)
    mid = merge["merge_id"]
    resolve(client, mid, 1, [{"pointer": "/a", "choice": "left"}])
    resolve(client, mid, 2, [{"pointer": "/b", "choice": "right"}])
    undo_ok(client, mid, 3)

    body = audit(client, mid)
    assert body["event_count"] == 3
    assert [e["method"] for e in body["events"]] == [
        "resolutions",
        "resolutions",
        "resolution-undo",
    ]
    assert all(e["valid"] for e in body["events"])


def test_tampered_undo_event_breaks_the_chain(client):
    merge = three_conflict_merge(client)
    mid = merge["merge_id"]
    resolve(client, mid, 1, [{"pointer": "/a", "choice": "left"}])
    undo_ok(client, mid, 2)

    db.execute(
        "UPDATE merge_audit_events SET pointers = ? WHERE merge_id = ? AND seq = 2",
        ('["/tampered"]', mid),
    )
    body = audit(client, mid)
    assert body["chain_valid"] is False
    assert body["first_invalid_seq"] == 2
    assert [e["valid"] for e in body["events"]] == [True, False]


def test_undo_refuses_when_history_was_tampered(client):
    merge = three_conflict_merge(client)
    mid = merge["merge_id"]
    resolve(client, mid, 1, [{"pointer": "/a", "choice": "left"}])
    # Rewrite the stored adjudication to name a pointer absent from the
    # immutable initial snapshot: its hash no longer matches.
    db.execute(
        "UPDATE merge_audit_events SET pointers = ?, detail = ? "
        "WHERE merge_id = ? AND seq = 1",
        (
            json.dumps(["/ghost"]),
            json.dumps({"items": [{"pointer": "/ghost", "choice": "left"}]}),
            mid,
        ),
    )
    resp = do_undo(client, mid, 2)
    assert resp.status_code == 500
    assert "audit chain" in resp.json()["detail"]

    # Fail closed: forward state untouched and no undo event appended.
    fetched = client.get(f"/merges/{mid}").json()
    assert fetched["revision"] == 2
    assert fetched["document"]["a"] == 2
    assert [e["seq"] for e in audit(client, mid)["events"]] == [1]

    # The merge keeps working after history is restored: nothing got
    # half-written by the refused undo.
    fetched_conflicts = [c["pointer"] for c in fetched["conflicts"]]
    assert fetched_conflicts == ["/b", "/c"]


def test_undo_refuses_when_history_has_a_gap(client):
    merge = three_conflict_merge(client)
    mid = merge["merge_id"]
    resolve(client, mid, 1, [{"pointer": "/a", "choice": "left"}])
    undo_ok(client, mid, 2)
    resolve(client, mid, 3, [{"pointer": "/b", "choice": "right"}])
    # Deleting the middle undo event breaks sequence/prev-hash continuity.
    db.execute(
        "DELETE FROM merge_audit_events WHERE merge_id = ? AND seq = 2", (mid,)
    )
    assert do_undo(client, mid, 4).status_code == 500
    # The latest adjudication is still applied; nothing was rolled back or
    # appended by the refused request.
    body = audit(client, mid)
    assert [e["seq"] for e in body["events"]] == [1, 3]


# ---------------------------------------------------------------------------
# Drafts created against reopened conflicts
# ---------------------------------------------------------------------------


def test_draft_after_undo_snapshots_reopened_conflicts(client):
    merge = three_conflict_merge(client)
    mid = merge["merge_id"]
    resolve(client, mid, 1, [{"pointer": "/a", "choice": "left"}])
    undo_ok(client, mid, 2)

    did = client.post(f"/merges/{mid}/drafts").json()["draft_id"]
    # The new draft is pinned at revision 3 and may readjudicate /a.
    client.post(
        f"/merges/{mid}/drafts/{did}/resolutions",
        json={"resolutions": [{"pointer": "/a", "choice": "right"}]},
    )
    resp = client.post(f"/merges/{mid}/drafts/{did}/commit")
    assert resp.status_code == 200, resp.text
    assert resp.json()["document"]["a"] == 3


def test_draft_pinned_before_undo_fails_to_commit(client):
    merge = three_conflict_merge(client)
    mid = merge["merge_id"]
    did = client.post(f"/merges/{mid}/drafts").json()["draft_id"]
    client.post(
        f"/merges/{mid}/drafts/{did}/resolutions",
        json={"resolutions": [{"pointer": "/a", "choice": "left"}]},
    )
    # Another client moves the merge on and undoes that move; revision 1 is
    # stale afterwards, so the pinned draft commit must fail with 409.
    resolve(client, mid, 1, [{"pointer": "/b", "choice": "right"}])
    undo_ok(client, mid, 2)
    resp = client.post(f"/merges/{mid}/drafts/{did}/commit")
    assert resp.status_code == 409
    assert resp.json()["current_revision"] == 3
    # The failed commit appended no event.
    assert [e["method"] for e in audit(client, mid)["events"]] == [
        "resolutions",
        "resolution-undo",
    ]
