"""End-to-end API tests against a temporary SQLite database."""

import os
import tempfile

os.environ["DATABASE_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    # Context-manager form runs the lifespan, which creates the tables.
    with TestClient(app) as c:
        yield c


BASE = {"replicas": 1, "image": "app:v1", "db": {"host": "a", "port": 5432}}
LEFT = {"replicas": 3, "image": "app:v1", "db": {"host": "b", "port": 5432}}
RIGHT = {"replicas": 1, "image": "app:v2", "db": {"host": "c", "port": 5433}}


def create_merge(client, key=None):
    payload = {"base": BASE, "left": LEFT, "right": RIGHT}
    if key:
        payload["idempotency_key"] = key
    resp = client.post("/merges", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_auto_merge_and_conflicts(client):
    merge = create_merge(client)
    assert merge["status"] == "pending"
    assert merge["revision"] == 1
    # Single-side changes adopted, conflict only on /db/host.
    assert merge["document"]["replicas"] == 3
    assert merge["document"]["image"] == "app:v2"
    assert merge["document"]["db"]["port"] == 5433
    pointers = [c["pointer"] for c in merge["conflicts"]]
    assert pointers == ["/db/host"]
    conflict = merge["conflicts"][0]
    assert conflict["left"] == "b" and conflict["right"] == "c"
    assert merge["document"]["db"]["host"] == {"__merge_conflict__": "/db/host"}


def test_batch_resolution_to_immutable_result(client):
    merge = create_merge(client)
    mid = merge["merge_id"]

    # Batch 1: wrong pointer -> 400, nothing applied.
    resp = client.post(
        f"/merges/{mid}/resolutions",
        json={"revision": 1, "resolutions": [{"pointer": "/nope", "value": 1}]},
    )
    assert resp.status_code == 400

    # Batch 2: resolve the conflict with an explicit value.
    resp = client.post(
        f"/merges/{mid}/resolutions",
        json={
            "revision": 1,
            "resolutions": [{"pointer": "/db/host", "value": "final-host"}],
        },
    )
    assert resp.status_code == 200, resp.text
    done = resp.json()
    assert done["status"] == "resolved"
    assert done["revision"] == 2
    assert done["conflicts"] == []
    assert done["result"]["db"]["host"] == "final-host"
    assert done["result"]["replicas"] == 3

    # Result is immutable: further resolutions are rejected with 409.
    resp = client.post(
        f"/merges/{mid}/resolutions",
        json={"revision": 2, "resolutions": [{"pointer": "/db/host", "value": "x"}]},
    )
    assert resp.status_code == 409

    # Status query returns the same immutable result.
    fetched = client.get(f"/merges/{mid}").json()
    assert fetched["result"] == done["result"]


def test_stale_revision_returns_409(client):
    merge = create_merge(client)
    mid = merge["merge_id"]
    resp = client.post(
        f"/merges/{mid}/resolutions",
        json={"revision": 999, "resolutions": [{"pointer": "/db/host", "choice": "left"}]},
    )
    assert resp.status_code == 409
    assert resp.json()["current_revision"] == 1

    # Choice-based resolution still works with the right revision.
    resp = client.post(
        f"/merges/{mid}/resolutions",
        json={"revision": 1, "resolutions": [{"pointer": "/db/host", "choice": "right"}]},
    )
    assert resp.status_code == 200
    assert resp.json()["result"]["db"]["host"] == "c"


def test_partial_batches_bump_revision(client):
    merge = create_merge(client)
    # Add a second conflict so batches can be split.
    resp = client.post(
        "/merges",
        json={
            "base": {"a": 1, "b": 1},
            "left": {"a": 2, "b": 2},
            "right": {"a": 3, "b": 3},
        },
    )
    mid = resp.json()["merge_id"]
    assert resp.json()["conflict_count"] == 2

    # First batch resolves /a only; revision bumps to 2, still pending.
    resp = client.post(
        f"/merges/{mid}/resolutions",
        json={"revision": 1, "resolutions": [{"pointer": "/a", "choice": "left"}]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pending"
    assert body["revision"] == 2
    assert [c["pointer"] for c in body["conflicts"]] == ["/b"]

    # Old revision is now stale.
    resp = client.post(
        f"/merges/{mid}/resolutions",
        json={"revision": 1, "resolutions": [{"pointer": "/b", "value": 9}]},
    )
    assert resp.status_code == 409

    # Second batch finishes the merge.
    resp = client.post(
        f"/merges/{mid}/resolutions",
        json={"revision": 2, "resolutions": [{"pointer": "/b", "value": 9}]},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "resolved"
    assert resp.json()["result"] == {"a": 2, "b": 9}


def test_resolution_preview_is_side_effect_free(client):
    merge = create_merge(client)
    mid = merge["merge_id"]

    resp = client.post(
        f"/merges/{mid}/resolution-preview",
        json={"revision": 1, "resolutions": [{"pointer": "/db/host", "choice": "left"}]},
    )
    assert resp.status_code == 200, resp.text
    preview = resp.json()
    assert preview["merge_id"] == mid
    assert preview["revision"] == 1
    assert preview["document"]["db"]["host"] == "b"
    assert preview["document"]["replicas"] == 3
    assert preview["remaining_conflicts"] == []
    assert preview["would_resolve"] is True

    # A fully resolving preview carries the deterministic patch and digest.
    from app.patch import sha256_hex

    assert preview["result_sha256"] == sha256_hex(preview["document"])
    from tests.patch_apply import apply_patch

    assert apply_patch(BASE, preview["patch"]) == preview["document"]

    # Nothing persisted: revision, status and conflicts are untouched.
    fetched = client.get(f"/merges/{mid}").json()
    assert fetched["revision"] == 1
    assert fetched["status"] == "pending"
    assert [c["pointer"] for c in fetched["conflicts"]] == ["/db/host"]

    # The same batch is still accepted for real against revision 1.
    resp = client.post(
        f"/merges/{mid}/resolutions",
        json={"revision": 1, "resolutions": [{"pointer": "/db/host", "choice": "left"}]},
    )
    assert resp.status_code == 200
    assert resp.json()["result"] == preview["document"]


def test_resolution_preview_partial_batch(client):
    resp = client.post(
        "/merges",
        json={
            "base": {"a": 1, "b": 1},
            "left": {"a": 2, "b": 2},
            "right": {"a": 3, "b": 3},
        },
    )
    mid = resp.json()["merge_id"]

    resp = client.post(
        f"/merges/{mid}/resolution-preview",
        json={"revision": 1, "resolutions": [{"pointer": "/a", "value": 7}]},
    )
    assert resp.status_code == 200
    preview = resp.json()
    assert preview["would_resolve"] is False
    assert preview["document"] == {"a": 7, "b": {"__merge_conflict__": "/b"}}
    assert [c["pointer"] for c in preview["remaining_conflicts"]] == ["/b"]
    # No patch/digest until the batch would resolve everything.
    assert "patch" not in preview
    assert "result_sha256" not in preview


def test_resolution_preview_rejects_invalid_batch(client):
    merge = create_merge(client)
    mid = merge["merge_id"]

    # Unknown pointer.
    resp = client.post(
        f"/merges/{mid}/resolution-preview",
        json={"revision": 1, "resolutions": [{"pointer": "/nope", "value": 1}]},
    )
    assert resp.status_code == 400

    # Duplicate pointer within one batch.
    resp = client.post(
        f"/merges/{mid}/resolution-preview",
        json={
            "revision": 1,
            "resolutions": [
                {"pointer": "/db/host", "choice": "left"},
                {"pointer": "/db/host", "choice": "right"},
            ],
        },
    )
    assert resp.status_code == 400

    # Both choice and value on the same item.
    resp = client.post(
        f"/merges/{mid}/resolution-preview",
        json={
            "revision": 1,
            "resolutions": [{"pointer": "/db/host", "choice": "left", "value": "x"}],
        },
    )
    assert resp.status_code == 400

    # An explicit null value still counts as giving 'value'.
    resp = client.post(
        f"/merges/{mid}/resolution-preview",
        json={
            "revision": 1,
            "resolutions": [{"pointer": "/db/host", "choice": "left", "value": None}],
        },
    )
    assert resp.status_code == 400

    # Failed previews leave the merge untouched as well.
    fetched = client.get(f"/merges/{mid}").json()
    assert fetched["revision"] == 1
    assert fetched["status"] == "pending"


def test_resolution_preview_revision_and_state_guards(client):
    merge = create_merge(client)
    mid = merge["merge_id"]

    # Stale revision -> 409 with the current revision echoed.
    resp = client.post(
        f"/merges/{mid}/resolution-preview",
        json={"revision": 999, "resolutions": [{"pointer": "/db/host", "choice": "left"}]},
    )
    assert resp.status_code == 409
    assert resp.json()["current_revision"] == 1

    # Unknown merge -> 404.
    resp = client.post(
        "/merges/does-not-exist/resolution-preview",
        json={"revision": 1, "resolutions": [{"pointer": "/db/host", "choice": "left"}]},
    )
    assert resp.status_code == 404

    # Resolve for real; a preview against the finished merge -> 409.
    resp = client.post(
        f"/merges/{mid}/resolutions",
        json={"revision": 1, "resolutions": [{"pointer": "/db/host", "choice": "right"}]},
    )
    assert resp.status_code == 200
    resp = client.post(
        f"/merges/{mid}/resolution-preview",
        json={"revision": 2, "resolutions": [{"pointer": "/db/host", "choice": "left"}]},
    )
    assert resp.status_code == 409


def test_idempotency_key_replays_merge(client):
    first = create_merge(client, key="order-123")
    resp = client.post(
        "/merges",
        json={"base": BASE, "left": LEFT, "right": RIGHT, "idempotency_key": "order-123"},
    )
    assert resp.status_code == 200  # replay, not 201
    assert resp.json()["merge_id"] == first["merge_id"]

    # Header form works too.
    resp = client.post(
        "/merges",
        json={"base": BASE, "left": LEFT, "right": RIGHT},
        headers={"Idempotency-Key": "order-123"},
    )
    assert resp.json()["merge_id"] == first["merge_id"]


def test_no_conflict_merge_is_immediately_resolved(client):
    resp = client.post(
        "/merges",
        json={"base": {"a": 1}, "left": {"a": 2}, "right": {"a": 2}},
    )
    body = resp.json()
    assert body["status"] == "resolved"
    assert body["result"] == {"a": 2}


def test_unknown_merge_404(client):
    assert client.get("/merges/does-not-exist").status_code == 404
    assert client.get("/merges/does-not-exist/patch").status_code == 404


def test_patch_pending_returns_409(client):
    merge = create_merge(client)
    assert merge["status"] == "pending"
    resp = client.get(f"/merges/{merge['merge_id']}/patch")
    assert resp.status_code == 409
    assert "detail" in resp.json()


def test_patch_for_resolved_merge(client):
    merge = create_merge(client)
    mid = merge["merge_id"]
    resp = client.post(
        f"/merges/{mid}/resolutions",
        json={
            "revision": 1,
            "resolutions": [{"pointer": "/db/host", "value": "final-host"}],
        },
    )
    assert resp.status_code == 200
    result = resp.json()["result"]

    resp = client.get(f"/merges/{mid}/patch")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["merge_id"] == mid
    assert body["status"] == "resolved"
    assert body["patch"] == [
        {"op": "replace", "path": "/db/host", "value": "final-host"},
        {"op": "replace", "path": "/db/port", "value": 5433},
        {"op": "replace", "path": "/image", "value": "app:v2"},
        {"op": "replace", "path": "/replicas", "value": 3},
    ]

    # Digests are stable SHA-256 of canonical base / result.
    from app.patch import sha256_hex

    assert body["base_sha256"] == sha256_hex(BASE)
    assert body["result_sha256"] == sha256_hex(result)
    assert len(body["base_sha256"]) == 64

    # Applying ops in the returned order to the base rebuilds the result.
    from tests.patch_apply import apply_patch

    assert apply_patch(BASE, body["patch"]) == result


def test_patch_for_immediately_resolved_merge(client):
    resp = client.post(
        "/merges",
        json={"base": {"a": 1, "b": 2}, "left": {"a": 9}, "right": {"a": 9}},
    )
    mid = resp.json()["merge_id"]
    body = client.get(f"/merges/{mid}/patch").json()
    assert body["patch"] == [
        {"op": "replace", "path": "/a", "value": 9},
        {"op": "remove", "path": "/b"},
    ]


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# POST /merges/{merge_id}/rule-resolutions
# ---------------------------------------------------------------------------


def create_multi_conflict_merge(client):
    """Three independent conflicts /a, /b, /c."""
    resp = client.post(
        "/merges",
        json={
            "base": {"a": 1, "b": 1, "c": 1},
            "left": {"a": 2, "b": 2, "c": 2},
            "right": {"a": 3, "b": 3, "c": 3},
        },
    )
    assert resp.status_code == 201
    return resp.json()


def test_rule_resolutions_match_report_and_single_revision_bump(client):
    merge = create_multi_conflict_merge(client)
    mid = merge["merge_id"]

    resp = client.post(
        f"/merges/{mid}/rule-resolutions",
        json={
            "revision": 1,
            "rules": [
                {"right": "/a"},
                {"left": "/b"},
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applied"] is True
    # Still one conflict left: pending, but the revision bumped exactly once.
    assert body["status"] == "pending"
    assert body["revision"] == 2
    assert body["rule_matches"] == [
        {"rule_index": 0, "side": "right", "pointers": ["/a"]},
        {"rule_index": 1, "side": "left", "pointers": ["/b"]},
    ]
    assert body["unmatched_conflicts"] == ["/c"]
    assert body["document"] == {
        "a": 3,
        "b": 2,
        "c": {"__merge_conflict__": "/c"},
    }

    # The old revision is stale now, even though conflicts remain.
    resp = client.post(
        f"/merges/{mid}/rule-resolutions",
        json={"revision": 1, "rules": [{"right": "/c"}]},
    )
    assert resp.status_code == 409
    assert resp.json()["current_revision"] == 2


def test_rule_resolutions_finish_merge_and_immutable_after(client):
    merge = create_multi_conflict_merge(client)
    mid = merge["merge_id"]

    # A single rule resolving all three conflicts bumps the revision once.
    resp = client.post(
        f"/merges/{mid}/rule-resolutions",
        json={"revision": 1, "rules": [{"right": "/**"}]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "resolved"
    assert body["revision"] == 2
    assert body["result"] == {"a": 3, "b": 3, "c": 3}
    assert body["unmatched_conflicts"] == []
    assert body["rule_matches"] == [
        {"rule_index": 0, "side": "right", "pointers": ["/a", "/b", "/c"]}
    ]

    resp = client.post(
        f"/merges/{mid}/rule-resolutions",
        json={"revision": 2, "rules": [{"left": "/a"}]},
    )
    assert resp.status_code == 409


def test_rule_specificity_literal_over_wildcard(client):
    resp = client.post(
        "/merges",
        json={
            "base": {"db": {"host": "a", "port": 1}},
            "left": {"db": {"host": "b", "port": 2}},
            "right": {"db": {"host": "c", "port": 3}},
        },
    )
    mid = resp.json()["merge_id"]

    # The broad left '**' claims /db/port, but the literal right rule is
    # more specific and wins /db/host.
    resp = client.post(
        f"/merges/{mid}/rule-resolutions",
        json={
            "revision": 1,
            "rules": [
                {"left": "/**"},
                {"right": "/db/host"},
            ],
        },
    )
    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result == {"db": {"host": "c", "port": 2}}


def test_rule_declaration_order_breaks_ties(client):
    resp = client.post(
        "/merges",
        json={
            "base": {"db": {"host": "a"}},
            "left": {"db": {"host": "b"}},
            "right": {"db": {"host": "c"}},
        },
    )
    mid = resp.json()["merge_id"]
    resp = client.post(
        f"/merges/{mid}/rule-resolutions",
        json={
            "revision": 1,
            "rules": [{"left": "/db/*"}, {"right": "/db/*"}],
        },
    )
    assert resp.status_code == 200
    assert resp.json()["result"] == {"db": {"host": "b"}}


def test_single_star_and_trailing_double_star_patterns(client):
    resp = client.post(
        "/merges",
        json={
            "base": {"a": {"x": 1}},
            "left": {"a": {"x": 2}},
            "right": {"a": {"x": 3}},
        },
    )
    mid = resp.json()["merge_id"]

    # Single '*' matches exactly one segment.
    resp = client.post(
        f"/merges/{mid}/rule-resolutions",
        json={"revision": 1, "rules": [{"right": "/a/*"}]},
    )
    assert resp.status_code == 200
    assert resp.json()["result"] == {"a": {"x": 3}}

    # '**' also matches zero segments (a rule at /a/** covers /a itself).
    resp = client.post(
        "/merges",
        json={"base": {"a": 1}, "left": {"a": 2}, "right": {"a": 3}},
    )
    mid = resp.json()["merge_id"]
    resp = client.post(
        f"/merges/{mid}/rule-resolutions",
        json={"revision": 1, "rules": [{"left": "/a/**"}]},
    )
    assert resp.status_code == 200
    assert resp.json()["result"] == {"a": 2}


def test_rule_choosing_missing_side_deletes(client):
    # Left deletes /a and modifies /b; right modifies both -> /a is a
    # delete-vs-modify conflict with the missing side on the left.
    resp = client.post(
        "/merges",
        json={
            "base": {"a": 1, "b": 1},
            "left": {"b": 2},
            "right": {"a": 3, "b": 3},
        },
    )
    mid = resp.json()["merge_id"]
    conflicts = resp.json()["conflicts"]
    delete_conflict = next(c for c in conflicts if c["pointer"] == "/a")
    assert delete_conflict["missing"] == ["left"]

    resp = client.post(
        f"/merges/{mid}/rule-resolutions",
        json={"revision": 1, "rules": [{"left": "/**"}]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "resolved"
    # /a deleted via the missing-side semantics, /b takes the left value.
    assert body["result"] == {"b": 2}

def test_rule_no_matches_is_noop_without_revision_bump(client):
    merge = create_multi_conflict_merge(client)
    mid = merge["merge_id"]
    resp = client.post(
        f"/merges/{mid}/rule-resolutions",
        json={"revision": 1, "rules": [{"left": "/somewhere/else/*"}]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["applied"] is False
    assert body["revision"] == 1
    assert body["status"] == "pending"
    assert body["rule_matches"] == []
    assert sorted(body["unmatched_conflicts"]) == ["/a", "/b", "/c"]


def test_rule_resolutions_reject_invalid_pattern_atomically(client):
    merge = create_multi_conflict_merge(client)
    mid = merge["merge_id"]

    # The first rule would match; the second rule is invalid, so the whole
    # request must be rejected before anything is applied.
    resp = client.post(
        f"/merges/{mid}/rule-resolutions",
        json={
            "revision": 1,
            "rules": [{"left": "/a"}, {"right": "no-leading-slash"}],
        },
    )
    assert resp.status_code == 400
    assert "detail" in resp.json()

    # '**' is only allowed at the end; wildcard must be a whole segment.
    for pattern in ("/a/**/b", "/a/x*", "/a/**x"):
        resp = client.post(
            f"/merges/{mid}/rule-resolutions",
            json={"revision": 1, "rules": [{"left": pattern}]},
        )
        assert resp.status_code == 400, pattern

    # A rule without any pattern is invalid too.
    resp = client.post(
        f"/merges/{mid}/rule-resolutions",
        json={"revision": 1, "rules": [{}]},
    )
    assert resp.status_code == 400

    # No partial modification: revision and all conflicts are untouched.
    fetched = client.get(f"/merges/{mid}").json()
    assert fetched["revision"] == 1
    assert fetched["status"] == "pending"
    assert sorted(c["pointer"] for c in fetched["conflicts"]) == ["/a", "/b", "/c"]


def test_rule_resolutions_state_guards(client):
    merge = create_multi_conflict_merge(client)
    mid = merge["merge_id"]

    # Stale revision -> 409 with the current revision echoed.
    resp = client.post(
        f"/merges/{mid}/rule-resolutions",
        json={"revision": 999, "rules": [{"left": "/a"}]},
    )
    assert resp.status_code == 409
    assert resp.json()["current_revision"] == 1

    # Unknown merge -> 404.
    resp = client.post(
        "/merges/does-not-exist/rule-resolutions",
        json={"revision": 1, "rules": [{"left": "/a"}]},
    )
    assert resp.status_code == 404


def test_rule_application_failure_leaves_no_partial_change(client):
    # A root-level conflict whose chosen side is missing cannot be applied
    # (the document root cannot be deleted). That state is not reachable
    # through normal input, so craft it directly in the store row.
    import json as _json

    from app import db

    resp = client.post(
        "/merges",
        json={"base": 1, "left": 2, "right": 3},
    )
    mid = resp.json()["merge_id"]
    assert [c["pointer"] for c in resp.json()["conflicts"]] == [""]

    broken_conflict = [
        {"pointer": "", "base": 1, "left": None, "right": 3, "missing": ["left"]}
    ]
    db.execute(
        "UPDATE merges SET conflicts = ?, document = ? WHERE id = ?",
        (
            _json.dumps(broken_conflict),
            _json.dumps({"__merge_conflict__": ""}),
            mid,
        ),
    )

    resp = client.post(
        f"/merges/{mid}/rule-resolutions",
        json={"revision": 1, "rules": [{"left": ""}]},
    )
    assert resp.status_code == 400
    assert resp.json()["pointer"] == ""

    # The failed application committed nothing.
    fetched = client.get(f"/merges/{mid}").json()
    assert fetched["revision"] == 1
    assert fetched["status"] == "pending"


# ---------------------------------------------------------------------------
# POST /merge-analysis: stateless pre-merge impact analysis
# --------------------------------------------------------------------------------------


def test_merge_analysis_classification_and_counts(client):
    resp = client.post(
        "/merge-analysis",
        json={
            "base": {"replicas": 1, "image": "v1", "db": {"host": "a", "port": 5432}},
            "left": {"replicas": 3, "image": "v1", "db": {"host": "b", "port": 5432}},
            "right": {"replicas": 1, "image": "v2", "db": {"host": "c", "port": 5433}},
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["would_conflict"] is True
    assert body["counts"] == {
        "left_only": 1,
        "right_only": 2,
        "same_change": 0,
        "conflict": 1,
        "total": 4,
    }
    pointers = [c["pointer"] for c in body["changes"]]
    assert pointers == sorted(pointers)
    by_pointer = {c["pointer"]: c for c in body["changes"]}
    assert by_pointer["/db/host"]["type"] == "conflict"
    assert by_pointer["/db/host"]["left"] == "b"
    assert by_pointer["/db/host"]["right"] == "c"
    assert by_pointer["/db/host"]["missing"] == []
    assert by_pointer["/replicas"]["type"] == "left_only"
    assert by_pointer["/image"]["type"] == "right_only"
    assert by_pointer["/db/port"]["type"] == "right_only"


def test_merge_analysis_minimal_root_for_deleted_subtree(client):
    resp = client.post(
        "/merge-analysis",
        json={
            "base": {"a": {"x": 1, "y": 2}},
            "left": {},
            "right": {"a": {"x": 1, "y": 2}},
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert [c["pointer"] for c in body["changes"]] == ["/a"]
    assert body["changes"][0]["type"] == "left_only"
    assert body["changes"][0]["missing"] == ["left"]
    assert body["would_conflict"] is False


def test_merge_analysis_missing_vs_explicit_null(client):
    # Left explicitly nulls the key; right deletes it -> conflict, and the
    # record keeps null as a value while only right appears in missing.
    resp = client.post(
        "/merge-analysis",
        json={"base": {"a": 1}, "left": {"a": None}, "right": {}},
    )
    body = resp.json()
    assert len(body["changes"]) == 1
    record = body["changes"][0]
    assert record["pointer"] == "/a"
    assert record["type"] == "conflict"
    assert record["left"] is None
    assert record["missing"] == ["right"]


def test_merge_analysis_is_repeatable_and_persists_nothing(client):
    payload = {
        "base": {"replicas": 1, "db": {"host": "a", "port": 5432}},
        "left": {"replicas": 3, "db": {"host": "b", "port": 5432}},
        "right": {"replicas": 1, "db": {"host": "c", "port": 5433}},
    }
    first = client.post("/merge-analysis", json=payload)
    second = client.post("/merge-analysis", json=payload)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()

    # The analysis response carries no merge_id and analysis calls persist
    # nothing. (Earlier tests in the module-scoped database may already have
    # created merge orders, but repeated analyses must not add any.)
    assert "merge_id" not in first.json()
    from app import db

    count_before = db.query_one("SELECT COUNT(*) AS n FROM merges")["n"]
    client.post("/merge-analysis", json=payload)
    client.post("/merge-analysis", json=payload)
    count_after = db.query_one("SELECT COUNT(*) AS n FROM merges")["n"]
    assert count_after == count_before


def test_merge_analysis_no_conflicts_would_conflict_false(client):
    resp = client.post(
        "/merge-analysis",
        json={
            "base": {"a": 1, "b": 2},
            "left": {"a": 1, "b": 20},
            "right": {"a": 10, "b": 2},
        },
    )
    body = resp.json()
    assert body["would_conflict"] is False
    assert body["counts"]["conflict"] == 0
    assert body["counts"]["total"] == 2


def test_merge_analysis_requires_base_left_right(client):
    resp = client.post("/merge-analysis", json={"base": {}, "left": {}})
    assert resp.status_code == 422
