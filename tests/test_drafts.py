"""End-to-end tests for persistent resolution drafts.

A draft pins the merge revision at creation time, accumulates conflict
resolutions across edits (append or overwrite, merge untouched), previews
against its creation-time snapshot, and commits atomically: revision, open
conflicts and result_schema are re-validated inside a single transaction,
then every value is applied at once and the draft is invalidated.
"""

import os
import tempfile

os.environ["DATABASE_PATH"] = os.path.join(tempfile.mkdtemp(), "test_drafts.db")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    # Context-manager form runs the lifespan, which creates the tables.
    with TestClient(app) as c:
        yield c


def create_merge(client, schema=None):
    """Three independent conflicts /a, /b, /c."""
    payload = {
        "base": {"a": 1, "b": 1, "c": 1},
        "left": {"a": 2, "b": 2, "c": 2},
        "right": {"a": 3, "b": 3, "c": 3},
    }
    if schema is not None:
        payload["result_schema"] = schema
    resp = client.post("/merges", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def create_draft(client, merge_id):
    resp = client.post(f"/merges/{merge_id}/drafts")
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# POST /merges/{merge_id}/drafts
# ---------------------------------------------------------------------------


def test_create_draft_pins_revision_and_leaves_merge_untouched(client):
    merge = create_merge(client)
    mid = merge["merge_id"]

    draft = create_draft(client, mid)
    assert draft["merge_id"] == mid
    assert draft["status"] == "open"
    assert draft["base_revision"] == 1
    assert draft["resolutions"] == []
    assert draft["created_at"] and draft["updated_at"]

    # Creating the draft does not modify the merge order.
    fetched = client.get(f"/merges/{mid}").json()
    assert fetched["revision"] == 1
    assert fetched["status"] == "pending"
    assert fetched["conflict_count"] == 3

    # The draft is readable back.
    fetched_draft = client.get(f"/merges/{mid}/drafts/{draft['draft_id']}").json()
    assert fetched_draft == draft


def test_create_draft_guards(client):
    # Unknown merge -> 404.
    assert client.post("/merges/does-not-exist/drafts").status_code == 404

    # An already resolved merge accepts no drafts -> 409.
    resp = client.post(
        "/merges",
        json={"base": {"a": 1}, "left": {"a": 2}, "right": {"a": 2}},
    )
    mid = resp.json()["merge_id"]
    assert resp.json()["status"] == "resolved"
    resp = client.post(f"/merges/{mid}/drafts")
    assert resp.status_code == 409
    assert "already resolved" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# POST /merges/{merge_id}/drafts/{draft_id}/resolutions
# ---------------------------------------------------------------------------


def test_update_draft_appends_and_overwrites_without_touching_merge(client):
    merge = create_merge(client)
    mid = merge["merge_id"]
    draft = create_draft(client, mid)
    did = draft["draft_id"]

    # First edit: two pointers appended.
    resp = client.post(
        f"/merges/{mid}/drafts/{did}/resolutions",
        json={
            "resolutions": [
                {"pointer": "/a", "choice": "left"},
                {"pointer": "/b", "value": 20},
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["resolutions"] == [
        {"pointer": "/a", "choice": "left"},
        {"pointer": "/b", "value": 20},
    ]

    # Second edit: /a overwritten in place, /c appended. An explicit null
    # counts as giving 'value'.
    resp = client.post(
        f"/merges/{mid}/drafts/{did}/resolutions",
        json={
            "resolutions": [
                {"pointer": "/a", "choice": "right"},
                {"pointer": "/c", "value": None},
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["resolutions"] == [
        {"pointer": "/a", "choice": "right"},
        {"pointer": "/b", "value": 20},
        {"pointer": "/c", "value": None},
    ]
    assert resp.json()["base_revision"] == 1

    # Draft edits never modify the merge order.
    fetched = client.get(f"/merges/{mid}").json()
    assert fetched["revision"] == 1
    assert fetched["status"] == "pending"
    assert fetched["conflict_count"] == 3


def test_update_draft_reuses_resolution_error_formats(client):
    merge = create_merge(client)
    mid = merge["merge_id"]
    did = create_draft(client, mid)["draft_id"]

    # Unknown conflict pointer -> 400, same shape as resolution batches.
    resp = client.post(
        f"/merges/{mid}/drafts/{did}/resolutions",
        json={"resolutions": [{"pointer": "/nope", "value": 1}]},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "no open conflict at pointer '/nope'"

    # Both choice and value on the same item -> 400.
    resp = client.post(
        f"/merges/{mid}/drafts/{did}/resolutions",
        json={"resolutions": [{"pointer": "/a", "choice": "left", "value": 1}]},
    )
    assert resp.status_code == 400
    assert "both 'choice' and 'value'" in resp.json()["detail"]

    # Neither choice nor value -> 400.
    resp = client.post(
        f"/merges/{mid}/drafts/{did}/resolutions",
        json={"resolutions": [{"pointer": "/a"}]},
    )
    assert resp.status_code == 400
    assert "needs 'choice' or 'value'" in resp.json()["detail"]

    # Failed edits store nothing.
    assert client.get(f"/merges/{mid}/drafts/{did}").json()["resolutions"] == []


# ---------------------------------------------------------------------------
# GET /merges/{merge_id}/drafts/{draft_id}/preview
# ---------------------------------------------------------------------------


def test_preview_draft_reports_remaining_conflicts(client):
    merge = create_merge(client)
    mid = merge["merge_id"]
    did = create_draft(client, mid)["draft_id"]

    # Empty draft: everything still open.
    resp = client.get(f"/merges/{mid}/drafts/{did}/preview")
    assert resp.status_code == 200, resp.text
    preview = resp.json()
    assert preview["draft_id"] == did
    assert preview["base_revision"] == 1
    assert preview["current_revision"] == 1
    assert preview["would_resolve"] is False
    assert sorted(c["pointer"] for c in preview["remaining_conflicts"]) == [
        "/a",
        "/b",
        "/c",
    ]
    assert "patch" not in preview

    # Fill in one pointer: the preview shows it applied and the rest open.
    client.post(
        f"/merges/{mid}/drafts/{did}/resolutions",
        json={"resolutions": [{"pointer": "/a", "choice": "left"}]},
    )
    preview = client.get(f"/merges/{mid}/drafts/{did}/preview").json()
    assert preview["document"]["a"] == 2
    assert preview["document"]["b"] == {"__merge_conflict__": "/b"}
    assert sorted(c["pointer"] for c in preview["remaining_conflicts"]) == [
        "/b",
        "/c",
    ]
    assert preview["would_resolve"] is False

    # Complete the draft: full document, deterministic patch and digest.
    client.post(
        f"/merges/{mid}/drafts/{did}/resolutions",
        json={
            "resolutions": [
                {"pointer": "/b", "choice": "right"},
                {"pointer": "/c", "value": 9},
            ]
        },
    )
    preview = client.get(f"/merges/{mid}/drafts/{did}/preview").json()
    assert preview["would_resolve"] is True
    assert preview["remaining_conflicts"] == []
    assert preview["document"] == {"a": 2, "b": 3, "c": 9}

    from app.patch import sha256_hex
    from tests.patch_apply import apply_patch

    assert preview["result_sha256"] == sha256_hex(preview["document"])
    assert apply_patch({"a": 1, "b": 1, "c": 1}, preview["patch"]) == preview[
        "document"
    ]

    # Previews are side-effect free on both the merge and the draft.
    fetched = client.get(f"/merges/{mid}").json()
    assert fetched["revision"] == 1 and fetched["status"] == "pending"
    assert client.get(f"/merges/{mid}/drafts/{did}").json()["status"] == "open"


# ---------------------------------------------------------------------------
# POST /merges/{merge_id}/drafts/{draft_id}/commit
# ---------------------------------------------------------------------------


def test_commit_draft_applies_everything_and_invalidates(client):
    merge = create_merge(client)
    mid = merge["merge_id"]
    did = create_draft(client, mid)["draft_id"]
    client.post(
        f"/merges/{mid}/drafts/{did}/resolutions",
        json={
            "resolutions": [
                {"pointer": "/a", "choice": "left"},
                {"pointer": "/b", "value": 20},
                {"pointer": "/c", "choice": "right"},
            ]
        },
    )

    resp = client.post(f"/merges/{mid}/drafts/{did}/commit")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["draft_id"] == did
    assert body["status"] == "resolved"
    assert body["revision"] == 2
    assert body["conflicts"] == []
    assert body["result"] == {"a": 2, "b": 20, "c": 3}

    # The draft is invalidated: no more edits, previews or commits.
    assert client.get(f"/merges/{mid}/drafts/{did}").json()["status"] == "committed"
    resp = client.post(
        f"/merges/{mid}/drafts/{did}/resolutions",
        json={"resolutions": [{"pointer": "/a", "value": 0}]},
    )
    assert resp.status_code == 409
    assert client.get(f"/merges/{mid}/drafts/{did}/preview").status_code == 409
    assert client.post(f"/merges/{mid}/drafts/{did}/commit").status_code == 409

    # The merge result is immutable as usual.
    resp = client.post(
        f"/merges/{mid}/resolutions",
        json={"revision": 2, "resolutions": [{"pointer": "/a", "value": 0}]},
    )
    assert resp.status_code == 409


def test_commit_partial_draft_keeps_merge_pending(client):
    merge = create_merge(client)
    mid = merge["merge_id"]
    did = create_draft(client, mid)["draft_id"]
    client.post(
        f"/merges/{mid}/drafts/{did}/resolutions",
        json={"resolutions": [{"pointer": "/a", "choice": "left"}]},
    )

    resp = client.post(f"/merges/{mid}/drafts/{did}/commit")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "pending"
    assert body["revision"] == 2
    assert body["document"] == {
        "a": 2,
        "b": {"__merge_conflict__": "/b"},
        "c": {"__merge_conflict__": "/c"},
    }
    assert sorted(c["pointer"] for c in body["conflicts"]) == ["/b", "/c"]
    assert client.get(f"/merges/{mid}/drafts/{did}").json()["status"] == "committed"

    # The remaining conflicts are still resolvable on the merge itself.
    resp = client.post(
        f"/merges/{mid}/resolutions",
        json={
            "revision": 2,
            "resolutions": [
                {"pointer": "/b", "choice": "right"},
                {"pointer": "/c", "value": 7},
            ],
        },
    )
    assert resp.status_code == 200
    assert resp.json()["result"] == {"a": 2, "b": 3, "c": 7}


def test_commit_draft_stale_revision_is_409_without_partial_write(client):
    merge = create_merge(client)
    mid = merge["merge_id"]
    did = create_draft(client, mid)["draft_id"]
    client.post(
        f"/merges/{mid}/drafts/{did}/resolutions",
        json={
            "resolutions": [
                {"pointer": "/a", "choice": "left"},
                {"pointer": "/b", "choice": "left"},
            ]
        },
    )

    # Another client moves the merge on: revision 1 -> 2.
    resp = client.post(
        f"/merges/{mid}/resolutions",
        json={"revision": 1, "resolutions": [{"pointer": "/a", "choice": "right"}]},
    )
    assert resp.status_code == 200

    # The pinned revision no longer matches: 409 with the current revision.
    resp = client.post(f"/merges/{mid}/drafts/{did}/commit")
    assert resp.status_code == 409
    assert resp.json()["current_revision"] == 2

    # Neither the merge nor the draft was partially written.
    fetched = client.get(f"/merges/{mid}").json()
    assert fetched["revision"] == 2
    assert fetched["status"] == "pending"
    assert fetched["document"]["a"] == 3
    assert sorted(c["pointer"] for c in fetched["conflicts"]) == ["/b", "/c"]
    draft = client.get(f"/merges/{mid}/drafts/{did}").json()
    assert draft["status"] == "open"
    assert draft["resolutions"] == [
        {"pointer": "/a", "choice": "left"},
        {"pointer": "/b", "choice": "left"},
    ]

    # The preview still reflects the pinned snapshot and flags the drift.
    preview = client.get(f"/merges/{mid}/drafts/{did}/preview").json()
    assert preview["base_revision"] == 1
    assert preview["current_revision"] == 2


def test_commit_draft_after_merge_resolved_elsewhere_is_409(client):
    merge = create_merge(client)
    mid = merge["merge_id"]
    did = create_draft(client, mid)["draft_id"]
    client.post(
        f"/merges/{mid}/drafts/{did}/resolutions",
        json={"resolutions": [{"pointer": "/a", "choice": "left"}]},
    )
    resp = client.post(
        f"/merges/{mid}/rule-resolutions",
        json={"revision": 1, "rules": [{"right": "/**"}]},
    )
    assert resp.json()["status"] == "resolved"

    resp = client.post(f"/merges/{mid}/drafts/{did}/commit")
    assert resp.status_code == 409
    assert "already resolved" in resp.json()["detail"]
    # The failed commit does not invalidate the draft.
    assert client.get(f"/merges/{mid}/drafts/{did}").json()["status"] == "open"


def test_commit_draft_result_schema_gate(client):
    schema = {
        "type": "object",
        "properties": {"a": {"type": "integer", "minimum": 10}},
    }
    merge = create_merge(client, schema=schema)
    mid = merge["merge_id"]
    did = create_draft(client, mid)["draft_id"]
    client.post(
        f"/merges/{mid}/drafts/{did}/resolutions",
        json={
            "resolutions": [
                {"pointer": "/a", "value": 5},  # violates minimum: 10
                {"pointer": "/b", "choice": "left"},
                {"pointer": "/c", "choice": "right"},
            ]
        },
    )

    # The commit resolves the last conflict, so the schema gate applies:
    # 422 with the familiar sorted error list, and nothing is written.
    resp = client.post(f"/merges/{mid}/drafts/{did}/commit")
    assert resp.status_code == 422
    body = resp.json()
    assert body["detail"] == "final result does not satisfy result_schema"
    assert body["errors"][0]["instance_path"] == "/a"

    fetched = client.get(f"/merges/{mid}").json()
    assert fetched["revision"] == 1 and fetched["status"] == "pending"
    assert client.get(f"/merges/{mid}/drafts/{did}").json()["status"] == "open"

    # Fix the offending value in the draft and commit again: all applied.
    client.post(
        f"/merges/{mid}/drafts/{did}/resolutions",
        json={"resolutions": [{"pointer": "/a", "value": 42}]},
    )
    resp = client.post(f"/merges/{mid}/drafts/{did}/commit")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "resolved"
    assert resp.json()["result"] == {"a": 42, "b": 2, "c": 3}


def test_commit_empty_draft_is_400(client):
    merge = create_merge(client)
    mid = merge["merge_id"]
    did = create_draft(client, mid)["draft_id"]
    resp = client.post(f"/merges/{mid}/drafts/{did}/commit")
    assert resp.status_code == 400
    assert "no resolutions" in resp.json()["detail"]
    # Nothing changed on either side.
    assert client.get(f"/merges/{mid}").json()["revision"] == 1
    assert client.get(f"/merges/{mid}/drafts/{did}").json()["status"] == "open"


def test_draft_not_found_and_cross_merge_isolation(client):
    merge = create_merge(client)
    mid = merge["merge_id"]
    other = create_merge(client)
    did = create_draft(client, mid)["draft_id"]

    # Unknown draft id -> 404 on every draft endpoint.
    assert client.get(f"/merges/{mid}/drafts/nope").status_code == 404
    assert client.get(f"/merges/{mid}/drafts/nope/preview").status_code == 404
    assert client.post(f"/merges/{mid}/drafts/nope/commit").status_code == 404
    resp = client.post(
        f"/merges/{mid}/drafts/nope/resolutions",
        json={"resolutions": [{"pointer": "/a", "choice": "left"}]},
    )
    assert resp.status_code == 404

    # A draft is not reachable through another merge's URL -> 404.
    assert client.get(f"/merges/{other['merge_id']}/drafts/{did}").status_code == 404
    assert (
        client.post(f"/merges/{other['merge_id']}/drafts/{did}/commit").status_code
        == 404
    )

    # Unknown merge -> 404 as well.
    assert client.get("/merges/does-not-exist/drafts/x").status_code == 404


def test_draft_commit_uses_creation_snapshot_for_delete_semantics(client):
    # Left deletes /a; right modifies it -> delete-vs-modify conflict.
    resp = client.post(
        "/merges",
        json={
            "base": {"a": 1, "b": 1},
            "left": {"b": 2},
            "right": {"a": 3, "b": 3},
        },
    )
    mid = resp.json()["merge_id"]
    did = create_draft(client, mid)["draft_id"]
    client.post(
        f"/merges/{mid}/drafts/{did}/resolutions",
        json={
            "resolutions": [
                {"pointer": "/a", "choice": "left"},  # missing side: delete
                {"pointer": "/b", "choice": "left"},
            ]
        },
    )
    resp = client.post(f"/merges/{mid}/drafts/{did}/commit")
    assert resp.status_code == 200, resp.text
    assert resp.json()["result"] == {"b": 2}
