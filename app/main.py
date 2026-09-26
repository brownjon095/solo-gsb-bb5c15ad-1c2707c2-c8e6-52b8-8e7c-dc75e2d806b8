"""FastAPI application: three-way configuration merge service."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from . import db, evidence, store
from .analysis import analyze_merge
from .models import (
    DraftUpdateRequest,
    EvidencePackage,
    MergeAnalysisRequest,
    MergeCreate,
    ReleasePlanPreviewRequest,
    ResolutionUndoRequest,
    ResolveRequest,
    ReuseResolutionsRequest,
    RuleResolveRequest,
)
from .release_plan import InvalidReleasePlan
from .validation import InvalidResultSchema, ResultValidationFailed


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fail fast: evidence packages are signed with a key from the
    # environment, so a missing or too-short key aborts the startup
    # instead of surfacing later as failing evidence requests.
    evidence.get_signing_key()
    # Dependencies are installed at image build; tables are created here,
    # so a single `docker compose up` brings up a ready-to-use service.
    db.init_schema()
    yield


app = FastAPI(
    title="Config Three-Way Merge API",
    version="1.0.0",
    lifespan=lifespan,
)


@app.exception_handler(store.MergeNotFound)
async def not_found_handler(request: Request, exc: store.MergeNotFound):
    return JSONResponse(status_code=404, content={"detail": "merge not found"})


@app.exception_handler(store.StaleRevision)
async def stale_revision_handler(request: Request, exc: store.StaleRevision):
    return JSONResponse(
        status_code=409,
        content={
            "detail": "stale revision: the merge was updated by another request",
            "current_revision": exc.current_revision,
        },
    )


@app.exception_handler(store.AlreadyResolved)
async def already_resolved_handler(request: Request, exc: store.AlreadyResolved):
    return JSONResponse(
        status_code=409,
        content={"detail": "merge already resolved: result is immutable"},
    )


@app.exception_handler(store.MergeNotResolved)
async def not_resolved_handler(request: Request, exc: store.MergeNotResolved):
    return JSONResponse(
        status_code=409,
        content={"detail": "merge is pending: no result patch is available yet"},
    )


@app.exception_handler(store.UnknownConflictPointer)
async def unknown_pointer_handler(request: Request, exc: store.UnknownConflictPointer):
    return JSONResponse(
        status_code=400,
        content={"detail": f"no open conflict at pointer {exc.pointer!r}"},
    )


@app.exception_handler(store.InvalidResolution)
async def invalid_resolution_handler(request: Request, exc: store.InvalidResolution):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(store.InvalidPattern)
async def invalid_pattern_handler(request: Request, exc: store.InvalidPattern):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(store.DraftNotFound)
async def draft_not_found_handler(request: Request, exc: store.DraftNotFound):
    return JSONResponse(status_code=404, content={"detail": "draft not found"})


@app.exception_handler(store.DraftNotOpen)
async def draft_not_open_handler(request: Request, exc: store.DraftNotOpen):
    return JSONResponse(
        status_code=409,
        content={
            "detail": "draft is no longer open: it was already committed or invalidated"
        },
    )


@app.exception_handler(store.DraftConflictSetChanged)
async def draft_conflicts_changed_handler(
    request: Request, exc: store.DraftConflictSetChanged
):
    return JSONResponse(
        status_code=409,
        content={
            "detail": "draft is stale: the merge's open conflicts no longer "
            "cover the draft's pointers"
        },
    )


@app.exception_handler(store.InvalidRuleSet)
async def invalid_ruleset_handler(request: Request, exc: store.InvalidRuleSet):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(store.RuleApplicationFailed)
async def rule_apply_failed_handler(request: Request, exc: store.RuleApplicationFailed):
    return JSONResponse(
        status_code=400,
        content={
            "detail": str(exc),
            "pointer": exc.pointer,
            "reason": exc.reason,
        },
    )


@app.exception_handler(store.SourceMergeInvalid)
async def source_merge_invalid_handler(request: Request, exc: store.SourceMergeInvalid):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(store.NothingToUndo)
async def nothing_to_undo_handler(request: Request, exc: store.NothingToUndo):
    return JSONResponse(
        status_code=409,
        content={
            "detail": "no effective adjudication to undo: the merge has no "
            "resolution batch that is not already compensated"
        },
    )


@app.exception_handler(store.AuditChainBroken)
async def audit_chain_broken_handler(request: Request, exc: store.AuditChainBroken):
    # Undo rebuilds state by replaying the audit history; a chain that does
    # not verify makes that history untrustworthy, so the request fails
    # closed: 500, full rollback, no state change and no undo event.
    return JSONResponse(
        status_code=500,
        content={
            "detail": "the merge's audit chain does not verify; undo refused. "
            "Inspect GET /merges/{merge_id}/audit for the first invalid event"
        },
    )


@app.exception_handler(InvalidReleasePlan)
async def invalid_release_plan_handler(request: Request, exc: InvalidReleasePlan):
    # The plan itself is malformed (duplicate node, self-dependency,
    # dangling edge or cycle) or references an unknown merge: the whole
    # request fails with 400 and no state is read for batching.
    content = {"detail": str(exc), "reason": exc.reason}
    content.update(exc.extra)
    return JSONResponse(status_code=400, content=content)


@app.exception_handler(InvalidResultSchema)
async def invalid_result_schema_handler(request: Request, exc: InvalidResultSchema):
    # The result_schema supplied to POST /merges is not itself a valid
    # Draft 2020-12 JSON Schema: 422, and no merge record was created.
    return JSONResponse(
        status_code=422,
        content={
            "detail": "invalid result_schema: not a valid JSON Schema (Draft 2020-12)",
            "errors": exc.errors,
        },
    )


@app.exception_handler(ResultValidationFailed)
async def result_validation_failed_handler(request: Request, exc: ResultValidationFailed):
    # The candidate final document violates the merge's result_schema:
    # 422, the whole batch was rolled back and the revision is unchanged.
    return JSONResponse(
        status_code=422,
        content={
            "detail": "final result does not satisfy result_schema",
            "errors": exc.errors,
        },
    )


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/merges", status_code=201)
def create_merge(
    body: MergeCreate,
    response: Response,
    idempotency_key: str | None = Header(default=None),
):
    """Submit base/left/right and start a merge order.

    Idempotent: a repeated idempotency key returns the existing merge
    order (HTTP 200) instead of creating a new one.
    """
    key = body.idempotency_key or idempotency_key
    view, created = store.create_merge(
        body.base, body.left, body.right, key, body.result_schema
    )
    if not created:
        response.status_code = 200
    return view


@app.post("/merge-analysis")
def merge_analysis(body: MergeAnalysisRequest):
    """Pre-merge impact analysis for a base/left/right triple.

    Accepts the same documents as ``POST /merges`` but never persists
    anything: it returns change records by JSON Pointer (sorted) classified
    into left_only / right_only / same_change / conflict, with per-category
    counts and ``would_conflict``. Missing keys and explicit nulls stay
    distinguishable; a deleted subtree or a type replacement is reported
    once at its minimal root path. Repeated identical requests return
    identical responses.
    """
    return analyze_merge(body.base, body.left, body.right)


@app.get("/merges/{merge_id}")
def get_merge(merge_id: str):
    """Status query: state, revision, open conflicts, final result."""
    return store.get_merge(merge_id)


@app.get("/merges/{merge_id}/patch")
def get_merge_patch(merge_id: str):
    """RFC 6902 patch from the original base_doc to the final result.

    Available once the merge is resolved: 409 while pending, 404 if unknown.
    The response also carries SHA-256 digests of base and result (canonical
    JSON: sorted keys, no whitespace).
    """
    return store.get_merge_patch(merge_id)


@app.get("/merges/{merge_id}/audit")
def get_merge_audit(merge_id: str):
    """Adjudication audit trail of the merge order, in sequence order.

    Every successful resolutions / rule-resolutions / reuse-resolutions /
    draft-commit request appended exactly one event inside the same
    transaction as the merge update; failed requests and zero-match no-ops
    left none. Each event records the method, the revision before/after
    and the JSON Pointers that took effect, and is chained to its
    predecessor via SHA-256 over canonical JSON. The response re-verifies
    the whole chain and, when it is broken, names the first anomalous
    sequence number. Unknown merge: 404.
    """
    return store.get_audit_trail(merge_id)


# ---------------------------------------------------------------------------
# Portable evidence packages
# ---------------------------------------------------------------------------


@app.get("/merges/{merge_id}/evidence")
def get_merge_evidence(merge_id: str):
    """Export a signed portable evidence package for a resolved merge.

    Only a resolved merge order can be exported: 409 while pending, 404
    if unknown. The payload carries the original base document, the
    immutable result, the deterministic RFC 6902 patch from base to
    result, the complete adjudication audit events and the generation
    time; it is signed with HMAC-SHA256 over its canonical JSON using the
    key from the EVIDENCE_HMAC_KEY environment variable. The package can
    be verified anywhere with POST /evidence/verify — no database needed.
    """
    return store.export_evidence(merge_id)


@app.post("/evidence/verify")
def verify_evidence(body: EvidencePackage):
    """Verify an exported evidence package without any database access.

    The stages run in a fixed order and the response names the first
    failing one together with its reason:

    1. ``signature`` — the HMAC-SHA256 is recomputed over the payload's
       canonical JSON and compared to the packaged signature in constant
       time;
    2. ``audit_chain`` — the packaged audit events are re-hashed and
       checked for sequence and prev-hash continuity;
    3. ``patch_replay`` — the patch is actually applied to ``base`` and
       must reproduce the packaged immutable ``result`` exactly.

    A package passing every stage yields ``{"valid": true}``; a
    structurally malformed body yields 422 before any stage runs.
    """
    return evidence.verify_package(body.model_dump())


def _resolution_items(body: ResolveRequest | DraftUpdateRequest) -> list[dict]:
    return [
        {
            "pointer": r.pointer,
            "choice": r.choice,
            "value": r.value,
            "value_provided": r.value_provided,
        }
        for r in body.resolutions
    ]


@app.post("/merges/{merge_id}/resolutions")
def resolve_merge(merge_id: str, body: ResolveRequest):
    """Submit a batch of conflict resolutions against the current revision.

    Returns 409 on a stale revision or once the merge is resolved.
    When the last conflict is resolved the result becomes immutable.
    """
    return store.apply_resolutions(merge_id, body.revision, _resolution_items(body))


@app.post("/merges/{merge_id}/resolution-preview")
def preview_resolution(merge_id: str, body: ResolveRequest):
    """Dry-run a resolution batch against the current revision.

    Same request shape as the committing endpoint, but the batch is
    validated and applied on an in-memory copy only: nothing is written,
    the revision is not bumped and the status never changes. Returns the
    would-be document and remaining conflicts; when the batch would
    resolve everything, the deterministic base-to-result patch and its
    SHA-256 are included.
    """
    return store.preview_resolutions(merge_id, body.revision, _resolution_items(body))


@app.post("/merges/{merge_id}/rule-resolutions")
def resolve_by_rules(merge_id: str, body: RuleResolveRequest):
    """Resolve conflicts in bulk via ordered RFC 6901-style path patterns.

    Each rule may give a ``left`` and/or ``right`` pattern (single-segment
    ``*`` and trailing ``**`` supported). Every open conflict is assigned
    its unique best match — more literal segments first, then fewer ``*``,
    then declaration order. All hits are validated first; only when every
    one is applicable do they commit in a single transaction with exactly
    one revision bump. A missing chosen side honours the deletion. Stale
    revision or a resolved merge yields 409; an invalid pattern or failed
    application yields 400 with no partial modification.
    """
    rule_items = [
        {"left": rule.left, "right": rule.right} for rule in body.rules
    ]
    return store.apply_rule_resolutions(merge_id, body.revision, rule_items)


@app.post("/merges/{merge_id}/reuse-resolutions")
def reuse_resolutions(merge_id: str, body: ReuseResolutionsRequest):
    """Reuse a resolved merge's adjudications on the current conflicts.

    The source merge must already be resolved. Each open conflict of the
    target is matched at the same JSON Pointer against the source's
    conflict snapshot; reuse requires base/left/right and the missing
    semantics to be identical after canonicalization (an absent key is
    not an explicit null). Matched values come from the source's final
    result, and a path the source deleted is deleted in the target too.
    All matches apply atomically with one revision bump; a stale
    revision, an illegal/unresolved source or a failed final
    result_schema gate writes nothing. With no matches nothing is
    written and the revision stays unchanged; ``applied`` / ``skipped``
    report every conflict and its per-item reason.
    """
    return store.reuse_resolutions(merge_id, body.revision, body.source_merge_id)


@app.post("/merges/{merge_id}/resolution-undo")
def undo_resolution(merge_id: str, body: ResolutionUndoRequest):
    """Undo the last effective adjudication batch of a pending merge.

    Only pending merges with at least one not-yet-compensated adjudication
    event accept an undo. The document and open conflicts are not reverse
    edited: they are rebuilt from the merge's immutable initial conflict
    snapshot by replaying the audit events, skipping the most recent
    effective one. That restores every pointer of the batch as an open
    conflict and bumps the revision by one. The undo appends a chained
    ``resolution-undo`` audit event naming the compensated event's sequence
    number and the restored pointers; later undos skip compensated events.
    A stale revision, a resolved (immutable) merge or nothing left to undo
    yields 409; an unknown merge yields 404. Nothing is changed and no
    event is left on any failure.
    """
    return store.undo_last_resolution(merge_id, body.revision)


# ---------------------------------------------------------------------------
# Resolution drafts
# ---------------------------------------------------------------------------


@app.post("/merges/{merge_id}/drafts", status_code=201)
def create_draft(merge_id: str):
    """Open a persistent resolution draft for a pending merge.

    The draft pins the merge revision at creation time and snapshots the
    working document and open conflicts, so edits and previews stay stable
    while other clients keep working on the merge. A resolved merge yields
    409, an unknown one 404.
    """
    return store.create_draft(merge_id)


@app.get("/merges/{merge_id}/drafts/{draft_id}")
def get_draft(merge_id: str, draft_id: str):
    """Read a draft: status, pinned base_revision and accumulated values."""
    return store.get_draft(merge_id, draft_id)


@app.post("/merges/{merge_id}/drafts/{draft_id}/resolutions")
def update_draft(merge_id: str, draft_id: str, body: DraftUpdateRequest):
    """Append or overwrite conflict values on the draft.

    Same choice/value semantics as the resolution endpoints; a pointer
    already present is overwritten, a new one is appended, and pointers
    may be filled in over several calls. Only the draft is written — the
    merge order (revision, conflicts, document) is never touched. A draft
    that was already committed yields 409.
    """
    return store.update_draft(merge_id, draft_id, _resolution_items(body))


@app.get("/merges/{merge_id}/drafts/{draft_id}/preview")
def preview_draft(merge_id: str, draft_id: str):
    """Dry-run the draft against its creation-time snapshot.

    Nothing is written. Returns the would-be document and the conflicts
    still open after the draft; when the draft would resolve everything,
    the deterministic base-to-result patch and its SHA-256 are included.
    """
    return store.preview_draft(merge_id, draft_id)


@app.post("/merges/{merge_id}/drafts/{draft_id}/commit")
def commit_draft(merge_id: str, draft_id: str):
    """Commit the draft: apply all of its values in a single transaction.

    The transaction re-reads and re-validates the merge revision (must
    still equal the draft's pinned base_revision), the open conflicts
    (must still cover every drafted pointer) and the result_schema gate
    (when the draft resolves the last conflict). On success every value
    is applied at once, the revision bumps exactly once and the draft is
    invalidated. Any changed condition yields 409 (a result_schema
    violation 422) and neither the merge nor the draft is partially
    written.
    """
    return store.commit_draft(merge_id, draft_id)


# ---------------------------------------------------------------------------
# Release orchestration preview
# ---------------------------------------------------------------------------


@app.post("/release-plans/preview")
def preview_release_plan(body: ReleasePlanPreviewRequest):
    """Preview stable release batches for a graph of existing merge orders.

    Each request node names an existing ``merge_id`` and the ``depends_on``
    prerequisites that must ship first. The endpoint only references
    existing merges and reads their current status/revision — it writes
    nothing and bumps no revision. Node merge_ids must be unique; every
    prerequisite must be a node in the same list, and self-dependencies and
    cycles are rejected. A duplicate/dangling/cyclic graph or an unknown
    merge fails the whole request with 400.

    Publishable nodes — merges that are ``resolved`` and whose
    prerequisites are all publishable — are peeled off dependency layer by
    dependency layer, one layer per release batch; nodes inside a batch are
    sorted by ``merge_id``. Every other node appears under ``blocked``: a
    still-pending merge is reported as ``pending`` (its own blocker), while
    a resolved merge whose prerequisites cannot ship is
    ``dependency_blocked`` and names the directly blocking dependency
    nodes. Each node carries its current ``revision`` so the caller can
    tell whether the plan has gone stale since it was composed.
    """
    return store.preview_release_plan(
        [node.model_dump() for node in body.nodes]
    )
