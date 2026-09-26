"""End-to-end tests for portable evidence packages.

A resolved merge order can be exported as a signed evidence package:
base document, immutable result, deterministic RFC 6902 patch, complete
audit events and generation time, signed with HMAC-SHA256 over the
payload's canonical JSON using the key from EVIDENCE_HMAC_KEY. The
verification endpoint needs no database: it checks the signature
(constant-time), then the audit chain continuity, then actually replays
the patch on the base, reporting the first failing stage in that fixed
order. A missing or too-short key fails the application startup.
"""

import hashlib
import hmac
import json as _json
import os
import tempfile
from datetime import datetime

os.environ["DATABASE_PATH"] = os.path.join(tempfile.mkdtemp(), "test_evidence.db")
TEST_KEY = "test-evidence-key-0123456789abcdef0123456789abcdef"
os.environ["EVIDENCE_HMAC_KEY"] = TEST_KEY

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import db, evidence  # noqa: E402
from app.main import app  # noqa: E402
from app.patch import canonical_json  # noqa: E402


@pytest.fixture(scope="module")
def client():
    # Context-manager form runs the lifespan, which creates the tables.
    with TestClient(app) as c:
        yield c


def make_merge(client, base, left, right):
    resp = client.post("/merges", json={"base": base, "left": left, "right": right})
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


def export(client, merge_id):
    resp = client.get(f"/merges/{merge_id}/evidence")
    assert resp.status_code == 200, resp.text
    return resp.json()


def verify(client, package):
    return client.post("/evidence/verify", json=package)


def sign(payload, key=TEST_KEY):
    """Independently sign a payload the way the service does."""
    return hmac.new(
        key.encode("utf-8"), canonical_json(payload), hashlib.sha256
    ).hexdigest()


def resign(package, key=TEST_KEY):
    """Re-sign a (possibly modified) package, as a key holder would."""
    package["signature"] = sign(package["payload"], key)
    return package


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def test_export_unknown_merge_returns_404(client):
    assert client.get("/merges/does-not-exist/evidence").status_code == 404


def test_export_pending_merge_returns_409(client):
    merge = three_conflict_merge(client)
    assert merge["status"] == "pending"
    resp = client.get(f"/merges/{merge['merge_id']}/evidence")
    assert resp.status_code == 409


def test_export_resolved_merge_package_contents(client):
    merge = three_conflict_merge(client)
    mid = merge["merge_id"]
    done = resolve(
        client,
        mid,
        1,
        [
            {"pointer": "/a", "choice": "left"},
            {"pointer": "/b", "value": None},
            {"pointer": "/c", "choice": "right"},
        ],
    )
    assert done["status"] == "resolved"

    package = export(client, mid)
    assert set(package) == {"payload", "signature"}

    payload = package["payload"]
    assert set(payload) == {
        "format",
        "merge_id",
        "generated_at",
        "base",
        "result",
        "patch",
        "audit",
    }
    assert payload["format"] == "merge-evidence/1"
    assert payload["merge_id"] == mid
    # Generation time is a UTC ISO 8601 timestamp.
    generated_at = datetime.fromisoformat(payload["generated_at"])
    assert generated_at.tzinfo is not None

    # Base and immutable result are carried verbatim.
    assert payload["base"] == {"a": 1, "b": 1, "c": 1}
    assert payload["result"] == {"a": 2, "b": None, "c": 3}

    # The packaged patch is the same deterministic patch the merge exposes.
    patch_view = client.get(f"/merges/{mid}/patch").json()
    assert payload["patch"] == patch_view["patch"]

    # The complete audit history is embedded, chain links included, and
    # carries no server-side verification artifact.
    assert len(payload["audit"]) == 1
    event = payload["audit"][0]
    assert set(event) == {
        "seq",
        "method",
        "revision_before",
        "revision_after",
        "pointers",
        "detail",
        "prev_hash",
        "hash",
        "created_at",
    }
    assert event["seq"] == 1
    assert event["method"] == "resolutions"
    assert event["pointers"] == ["/a", "/b", "/c"]

    # The signature is the HMAC-SHA256 of the payload's canonical JSON.
    signature = package["signature"]
    assert len(signature) == 64
    assert signature == sign(payload)


def test_export_conflict_free_merge_has_empty_audit(client):
    merge = make_merge(client, {"a": 1}, {"a": 2}, {"a": 2})
    assert merge["status"] == "resolved"
    package = export(client, merge["merge_id"])
    payload = package["payload"]
    assert payload["audit"] == []
    assert payload["result"] == {"a": 2}
    assert payload["patch"] == [{"op": "replace", "path": "/a", "value": 2}]
    assert verify(client, package).json()["valid"] is True


def test_export_includes_complete_audit_history(client):
    merge = three_conflict_merge(client)
    mid = merge["merge_id"]

    resolve(client, mid, 1, [{"pointer": "/a", "choice": "left"}])
    # Undo restores /a, then a rule run resolves /a and /b.
    resp = client.post(f"/merges/{mid}/resolution-undo", json={"revision": 2})
    assert resp.status_code == 200, resp.text
    resp = client.post(
        f"/merges/{mid}/rule-resolutions",
        json={"revision": 3, "rules": [{"left": "/a"}, {"right": "/b"}]},
    )
    assert resp.status_code == 200, resp.text
    done = resolve(client, mid, 4, [{"pointer": "/c", "value": 30}])
    assert done["status"] == "resolved"

    package = export(client, mid)
    events = package["payload"]["audit"]
    assert [e["seq"] for e in events] == [1, 2, 3, 4]
    assert [e["method"] for e in events] == [
        "resolutions",
        "resolution-undo",
        "rule-resolutions",
        "resolutions",
    ]
    # Chain links are carried so the package is self-verifiable.
    assert events[0]["prev_hash"] == "0" * 64
    for previous, current in zip(events, events[1:]):
        assert current["prev_hash"] == previous["hash"]
    assert verify(client, package).json()["valid"] is True


# ---------------------------------------------------------------------------
# Verify: happy path and database independence
# ---------------------------------------------------------------------------


def test_verify_roundtrip_valid(client):
    merge = three_conflict_merge(client)
    mid = merge["merge_id"]
    resolve(
        client,
        mid,
        1,
        [
            {"pointer": "/a", "choice": "left"},
            {"pointer": "/b", "choice": "right"},
            {"pointer": "/c", "value": 30},
        ],
    )
    package = export(client, mid)
    resp = verify(client, package)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"valid": True, "failed_stage": None, "reason": None}


def test_verify_needs_no_database(client):
    merge = three_conflict_merge(client)
    mid = merge["merge_id"]
    resolve(
        client,
        mid,
        1,
        [
            {"pointer": "/a", "choice": "left"},
            {"pointer": "/b", "choice": "right"},
            {"pointer": "/c", "choice": "left"},
        ],
    )
    package = export(client, mid)

    # Wipe every trace of the merge: verification must still succeed,
    # proving it relies on the package alone.
    db.execute("DELETE FROM merge_audit_events WHERE merge_id = ?", (mid,))
    db.execute("DELETE FROM merges WHERE id = ?", (mid,))
    assert client.get(f"/merges/{mid}").status_code == 404

    resp = verify(client, package)
    assert resp.status_code == 200
    assert resp.json()["valid"] is True


# ---------------------------------------------------------------------------
# Verify: signature stage (tampered payload, forged signature)
# ---------------------------------------------------------------------------


def test_verify_tampered_result_fails_signature_stage(client):
    merge = three_conflict_merge(client)
    mid = merge["merge_id"]
    resolve(
        client,
        mid,
        1,
        [
            {"pointer": "/a", "choice": "left"},
            {"pointer": "/b", "choice": "right"},
            {"pointer": "/c", "choice": "left"},
        ],
    )
    package = export(client, mid)

    # Tamper with the payload but keep the original signature.
    package["payload"]["result"] = {"a": 2, "b": 3, "c": 999}
    body = verify(client, package).json()
    assert body["valid"] is False
    assert body["failed_stage"] == "signature"
    assert body["reason"]


def test_verify_tampered_base_fails_signature_stage(client):
    merge = three_conflict_merge(client)
    mid = merge["merge_id"]
    resolve(
        client,
        mid,
        1,
        [
            {"pointer": "/a", "choice": "left"},
            {"pointer": "/b", "choice": "right"},
            {"pointer": "/c", "choice": "left"},
        ],
    )
    package = export(client, mid)
    package["payload"]["base"] = {"a": 0, "b": 1, "c": 1}
    body = verify(client, package).json()
    assert body["valid"] is False
    assert body["failed_stage"] == "signature"


def test_verify_forged_signature_fails_signature_stage(client):
    merge = three_conflict_merge(client)
    mid = merge["merge_id"]
    resolve(
        client,
        mid,
        1,
        [
            {"pointer": "/a", "choice": "left"},
            {"pointer": "/b", "choice": "right"},
            {"pointer": "/c", "choice": "left"},
        ],
    )
    package = export(client, mid)

    for forged in ("0" * 64, "f" * 64, "not-a-hex-string"):
        package["signature"] = forged
        body = verify(client, package).json()
        assert body["valid"] is False
        assert body["failed_stage"] == "signature"

    # A signature made with a different key is equally a forgery.
    package["signature"] = sign(package["payload"], key="another-key-0123456789abcdef0123456789")
    body = verify(client, package).json()
    assert body["valid"] is False
    assert body["failed_stage"] == "signature"


def test_verify_signature_failure_masks_later_stages(client):
    merge = three_conflict_merge(client)
    mid = merge["merge_id"]
    resolve(
        client,
        mid,
        1,
        [
            {"pointer": "/a", "choice": "left"},
            {"pointer": "/b", "choice": "right"},
            {"pointer": "/c", "choice": "left"},
        ],
    )
    package = export(client, mid)

    # Break the chain and the patch as well: the signature stage still
    # reports first because the payload no longer matches the signature.
    package["payload"]["audit"][0]["pointers"] = ["/tampered"]
    package["payload"]["patch"] = []
    body = verify(client, package).json()
    assert body["valid"] is False
    assert body["failed_stage"] == "signature"


# ---------------------------------------------------------------------------
# Verify: audit chain stage
# ---------------------------------------------------------------------------


def resolved_package_with_events(client):
    """A valid exported package whose merge has three audit events."""
    merge = three_conflict_merge(client)
    mid = merge["merge_id"]
    resolve(client, mid, 1, [{"pointer": "/a", "choice": "left"}])
    resolve(client, mid, 2, [{"pointer": "/b", "choice": "right"}])
    done = resolve(client, mid, 3, [{"pointer": "/c", "value": 30}])
    assert done["status"] == "resolved"
    return export(client, mid)


def test_verify_detects_tampered_audit_event(client):
    package = resolved_package_with_events(client)
    # Rewrite a recorded event and re-sign with the real key, so only the
    # chain stage can catch it.
    package["payload"]["audit"][1]["pointers"] = ["/tampered"]
    resign(package)
    body = verify(client, package).json()
    assert body["valid"] is False
    assert body["failed_stage"] == "audit_chain"
    assert "seq 2" in body["reason"]


def test_verify_detects_tampered_chain_link(client):
    package = resolved_package_with_events(client)
    package["payload"]["audit"][1]["prev_hash"] = "f" * 64
    resign(package)
    body = verify(client, package).json()
    assert body["valid"] is False
    assert body["failed_stage"] == "audit_chain"
    assert "seq 2" in body["reason"]


def test_verify_detects_deleted_audit_event(client):
    package = resolved_package_with_events(client)
    del package["payload"]["audit"][1]
    resign(package)
    body = verify(client, package).json()
    assert body["valid"] is False
    assert body["failed_stage"] == "audit_chain"
    # Event 3 is the first anomaly: the sequence jumps and its prev_hash
    # points at the deleted event's hash.
    assert "seq 3" in body["reason"]


# ---------------------------------------------------------------------------
# Verify: patch replay stage
# ---------------------------------------------------------------------------


def test_verify_detects_wrong_patch_value(client):
    package = resolved_package_with_events(client)
    # A syntactically valid patch that replays to a different document.
    package["payload"]["patch"][0]["value"] = "bogus"
    resign(package)
    body = verify(client, package).json()
    assert body["valid"] is False
    assert body["failed_stage"] == "patch_replay"
    assert body["reason"]


def test_verify_detects_missing_patch_op(client):
    package = resolved_package_with_events(client)
    # Dropping an op means the replay no longer reaches the result.
    package["payload"]["patch"] = package["payload"]["patch"][1:]
    resign(package)
    body = verify(client, package).json()
    assert body["valid"] is False
    assert body["failed_stage"] == "patch_replay"


def test_verify_detects_unapplicable_patch(client):
    package = resolved_package_with_events(client)
    package["payload"]["patch"].append({"op": "remove", "path": "/no/such/key"})
    resign(package)
    body = verify(client, package).json()
    assert body["valid"] is False
    assert body["failed_stage"] == "patch_replay"


def test_verify_stage_order_chain_before_patch(client):
    package = resolved_package_with_events(client)
    # Both later stages are broken: the chain failure is reported first.
    package["payload"]["audit"][0]["pointers"] = ["/tampered"]
    package["payload"]["patch"] = []
    resign(package)
    body = verify(client, package).json()
    assert body["valid"] is False
    assert body["failed_stage"] == "audit_chain"


# ---------------------------------------------------------------------------
# Verify: malformed packages
# ---------------------------------------------------------------------------


def test_verify_malformed_body_returns_422(client):
    assert client.post("/evidence/verify", json={}).status_code == 422
    assert (
        client.post("/evidence/verify", json={"payload": {}}).status_code == 422
    )

    package = resolved_package_with_events(client)
    # A missing payload field, an extra payload field and an extra
    # top-level field are all structural errors, rejected before staging.
    broken = _json.loads(_json.dumps(package))
    del broken["payload"]["result"]
    assert verify(client, broken).status_code == 422

    broken = _json.loads(_json.dumps(package))
    broken["payload"]["unexpected"] = 1
    assert verify(client, broken).status_code == 422

    broken = _json.loads(_json.dumps(package))
    broken["unexpected"] = 1
    assert verify(client, broken).status_code == 422


# ---------------------------------------------------------------------------
# Signing key configuration
# ---------------------------------------------------------------------------


def test_missing_key_fails_startup(monkeypatch):
    monkeypatch.delenv("EVIDENCE_HMAC_KEY", raising=False)
    with pytest.raises(RuntimeError, match="EVIDENCE_HMAC_KEY"):
        evidence.get_signing_key()
    # The application startup (lifespan) fails the same way.
    with pytest.raises(RuntimeError, match="EVIDENCE_HMAC_KEY"):
        with TestClient(app):
            pass


def test_short_key_fails_startup(monkeypatch):
    monkeypatch.setenv("EVIDENCE_HMAC_KEY", "too-short")
    with pytest.raises(RuntimeError, match="too short"):
        evidence.get_signing_key()
    with pytest.raises(RuntimeError, match="too short"):
        with TestClient(app):
            pass


def test_exactly_min_length_key_is_accepted(monkeypatch):
    monkeypatch.setenv("EVIDENCE_HMAC_KEY", "k" * evidence.MIN_KEY_BYTES)
    assert evidence.get_signing_key() == b"k" * evidence.MIN_KEY_BYTES
