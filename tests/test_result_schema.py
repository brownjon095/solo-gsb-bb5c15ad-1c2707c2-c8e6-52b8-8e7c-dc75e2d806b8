"""End-to-end tests for the optional final-result JSON Schema gate.

POST /merges accepts an optional ``result_schema`` (Draft 2020-12). The
schema itself is validated on creation (422, no record), conflict-free
merges must satisfy it, and only a batch resolving the *last* open
conflict is checked for both explicit and rule-based resolutions — with
all violations reported in a stable order and no write / revision bump.
"""

import os
import tempfile

os.environ["DATABASE_PATH"] = os.path.join(tempfile.mkdtemp(), "test_schema.db")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _multi_conflict(client, schema):
    return client.post(
        "/merges",
        json={
            "base": {"a": 1, "b": 1},
            "left": {"a": 2, "b": 2},
            "right": {"a": 3, "b": 3},
            "result_schema": schema,
        },
    )


# ---------------------------------------------------------------------------
# POST /merges: the schema itself is the gate
# ---------------------------------------------------------------------------


def test_invalid_result_schema_returns_422_and_creates_nothing(client):
    resp = client.post(
        "/merges",
        json={
            "base": {"a": 1},
            "left": {"a": 2},
            "right": {"a": 3},
            "result_schema": {"type": "not-a-type", "required": "a"},
        },
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert "detail" in body
    # Every meta-schema violation is reported, sorted by instance_path.
    paths = [e["instance_path"] for e in body["errors"]]
    assert paths == sorted(paths)
    assert "/required" in paths and "/type" in paths
    for error in body["errors"]:
        assert set(error) == {"instance_path", "schema_path", "message"}
    # Nothing was created: the same idempotency key is still free to use.
    replay = client.post(
        "/merges",
        json={
            "base": {"a": 1},
            "left": {"a": 2},
            "right": {"a": 3},
            "result_schema": {"type": "not-a-type"},
            "idempotency_key": "bad-schema-key",
        },
    )
    assert replay.status_code == 422
    again = client.post(
        "/merges",
        json={"base": {"a": 1}, "left": {"a": 2}, "right": {"a": 3},
               "idempotency_key": "bad-schema-key"},
    )
    assert again.status_code == 201


def test_non_object_non_boolean_schema_returns_422(client):
    for invalid in ("a string", 42, ["type", "object"]):
        resp = client.post(
            "/merges",
            json={"base": {}, "left": {}, "right": {}, "result_schema": invalid},
        )
        assert resp.status_code == 422, invalid


def test_boolean_schemas_are_valid(client):
    # {} and booleans are valid Draft 2020-12 schemas.
    resp = client.post(
        "/merges",
        json={"base": {"a": 1}, "left": {"a": 2}, "right": {"a": 2},
              "result_schema": True},
    )
    assert resp.status_code == 201
    resp = client.post(
        "/merges",
        json={"base": {"a": 1}, "left": {"a": 2}, "right": {"a": 2},
              "result_schema": {}},
    )
    assert resp.status_code == 201


def test_conflict_free_merge_must_satisfy_schema(client):
    resp = client.post(
        "/merges",
        json={
            "base": {"a": 1},
            "left": {"a": 2},
            "right": {"a": 2},
            "result_schema": {"type": "object", "required": ["missing"]},
        },
    )
    assert resp.status_code == 422, resp.text
    errors = resp.json()["errors"]
    assert errors[0]["instance_path"] == ""
    assert errors[0]["schema_path"] == "/required"
    assert "required" in errors[0]["message"]


def test_conflict_free_merge_passing_schema_is_resolved(client):
    resp = client.post(
        "/merges",
        json={
            "base": {"a": 1},
            "left": {"a": 2},
            "right": {"a": 2},
            "result_schema": {"type": "object", "required": ["a"]},
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "resolved"
    assert body["result"] == {"a": 2}


def test_conflicting_merge_is_not_validated_at_creation(client):
    # The document still carries a placeholder; the gate waits for the
    # batch that resolves the last conflict.
    resp = _multi_conflict(client, False)  # boolean false: forbids everything
    assert resp.status_code == 201
    assert resp.json()["status"] == "pending"


# ---------------------------------------------------------------------------
# POST /merges/{id}/resolutions
# ---------------------------------------------------------------------------


def test_partial_resolution_batch_is_not_validated(client):
    resp = _multi_conflict(
        client,
        {"type": "object",
         "properties": {"a": {"type": "string"}, "b": {"type": "string"}}},
    )
    mid = resp.json()["merge_id"]

    # /a becomes the integer 2, which violates the schema — but the
    # document still has a placeholder at /b, so this batch is unchecked.
    resp = client.post(
        f"/merges/{mid}/resolutions",
        json={"revision": 1, "resolutions": [{"pointer": "/a", "choice": "left"}]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["revision"] == 2
    assert body["status"] == "pending"


def test_final_resolution_batch_validated_and_rolled_back(client):
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
        "required": ["a", "b"],
    }
    resp = _multi_conflict(client, schema)
    mid = resp.json()["merge_id"]

    resp = client.post(
        f"/merges/{mid}/resolutions",
        json={
            "revision": 1,
            "resolutions": [
                {"pointer": "/a", "choice": "left"},
                {"pointer": "/b", "choice": "right"},
            ],
        },
    )
    assert resp.status_code == 422, resp.text
    errors = resp.json()["errors"]
    # All violations listed, sorted by (instance_path, schema_path).
    assert [(e["instance_path"], e["schema_path"]) for e in errors] == [
        ("/a", "/properties/a/type"),
        ("/b", "/properties/b/type"),
    ]

    # Entire batch rolled back: revision and conflict list are unchanged.
    fetched = client.get(f"/merges/{mid}").json()
    assert fetched["revision"] == 1
    assert fetched["status"] == "pending"
    assert sorted(c["pointer"] for c in fetched["conflicts"]) == ["/a", "/b"]

    # The same candidates can be resubmitted; a compliant final batch passes.
    resp = client.post(
        f"/merges/{mid}/resolutions",
        json={
            "revision": 1,
            "resolutions": [
                {"pointer": "/a", "value": "x"},
                {"pointer": "/b", "value": "y"},
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "resolved"
    assert body["revision"] == 2
    assert body["result"] == {"a": "x", "b": "y"}


def test_final_resolution_after_partial_batch_is_validated(client):
    # The gate still applies when the last conflict is resolved by a later
    # batch, and sees the fully assembled document.
    resp = _multi_conflict(
        client,
        {"type": "object",
         "properties": {"a": {"type": "integer"}, "b": {"type": "string"}}},
    )
    mid = resp.json()["merge_id"]

    first = client.post(
        f"/merges/{mid}/resolutions",
        json={"revision": 1, "resolutions": [{"pointer": "/a", "choice": "left"}]},
    )
    assert first.status_code == 200 and first.json()["revision"] == 2

    # Resolving /b with an integer fails the final gate...
    bad = client.post(
        f"/merges/{mid}/resolutions",
        json={"revision": 2, "resolutions": [{"pointer": "/b", "choice": "right"}]},
    )
    assert bad.status_code == 422
    assert bad.json()["errors"][0]["instance_path"] == "/b"
    assert client.get(f"/merges/{mid}").json()["revision"] == 2

    # ...and the compliant value finishes the merge.
    good = client.post(
        f"/merges/{mid}/resolutions",
        json={"revision": 2, "resolutions": [{"pointer": "/b", "value": "ok"}]},
    )
    assert good.status_code == 200
    assert good.json()["result"] == {"a": 2, "b": "ok"}


def test_stale_revision_checked_before_schema(client):
    resp = _multi_conflict(client, {"type": "string"})
    mid = resp.json()["merge_id"]
    resp = client.post(
        f"/merges/{mid}/resolutions",
        json={
            "revision": 999,
            "resolutions": [
                {"pointer": "/a", "value": "x"},
                {"pointer": "/b", "value": "y"},
            ],
        },
    )
    assert resp.status_code == 409
    assert resp.json()["current_revision"] == 1


# ---------------------------------------------------------------------------
# POST /merges/{id}/rule-resolutions
# ---------------------------------------------------------------------------


def test_rule_resolutions_partial_batch_not_validated(client):
    resp = _multi_conflict(
        client,
        {"type": "object",
         "properties": {"a": {"type": "string"}, "b": {"type": "string"}}},
    )
    mid = resp.json()["merge_id"]

    resp = client.post(
        f"/merges/{mid}/rule-resolutions",
        json={"revision": 1, "rules": [{"right": "/a"}]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["applied"] is True
    assert body["revision"] == 2
    assert body["status"] == "pending"


def test_rule_resolutions_final_batch_validated_and_rolled_back(client):
    resp = _multi_conflict(
        client,
        {"type": "object",
         "properties": {"a": {"type": "string"}, "b": {"type": "string"}}},
    )
    mid = resp.json()["merge_id"]

    resp = client.post(
        f"/merges/{mid}/rule-resolutions",
        json={"revision": 1, "rules": [{"right": "/**"}]},
    )
    assert resp.status_code == 422, resp.text
    errors = resp.json()["errors"]
    assert [(e["instance_path"], e["schema_path"]) for e in errors] == [
        ("/a", "/properties/a/type"),
        ("/b", "/properties/b/type"),
    ]

    fetched = client.get(f"/merges/{mid}").json()
    assert fetched["revision"] == 1
    assert fetched["status"] == "pending"
    assert sorted(c["pointer"] for c in fetched["conflicts"]) == ["/a", "/b"]

    # A compliant batch (explicit string values) resolves the merge.
    resp = client.post(
        f"/merges/{mid}/resolutions",
        json={
            "revision": 1,
            "resolutions": [
                {"pointer": "/a", "value": "x"},
                {"pointer": "/b", "value": "y"},
            ],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "resolved"
    assert body["result"] == {"a": "x", "b": "y"}


def test_rule_resolutions_final_batch_passing_schema(client):
    resp = _multi_conflict(
        client,
        {"type": "object",
         "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}}},
    )
    mid = resp.json()["merge_id"]
    resp = client.post(
        f"/merges/{mid}/rule-resolutions",
        json={"revision": 1, "rules": [{"right": "/a", "left": "/b"}]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "resolved"
    assert body["revision"] == 2
    assert body["result"] == {"a": 3, "b": 2}


def test_rule_no_match_skips_validation_and_revision_bump(client):
    resp = _multi_conflict(client, False)  # forbids any result
    mid = resp.json()["merge_id"]
    resp = client.post(
        f"/merges/{mid}/rule-resolutions",
        json={"revision": 1, "rules": [{"left": "/elsewhere/*"}]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["applied"] is False
    assert body["revision"] == 1


# ---------------------------------------------------------------------------
# Pure validation helper
# ---------------------------------------------------------------------------


def test_error_order_is_stable():
    from app.validation import validate_result, ResultValidationFailed

    schema = {
        "type": "object",
        "properties": {
            "z": {"type": "string"},
            "a": {"type": "string", "minLength": 5},
        },
    }
    with pytest.raises(ResultValidationFailed) as exc_info:
        validate_result(schema, {"z": 1, "a": "x"})
    paths = [(e["instance_path"], e["schema_path"]) for e in exc_info.value.errors]
    assert paths == sorted(paths)
    assert [p[0] for p in paths] == ["/a", "/z"]
