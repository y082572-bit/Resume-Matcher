"""Explicit Provenance Stage P6-A: independent re-validation of the raw
``ApprovedContentDocumentInput`` envelope before any document artifact is
ever generated.

Nothing on the envelope is trusted at face value -- ``result_fingerprint``,
``content_plan_fingerprint``, ``ready_for_p6``, and the caller's own
``document_input_fingerprint`` are all independently recomputed here and
required to match. Freshness is never accepted from the caller: it is
always recomputed via ``evaluate_approved_content_freshness`` from the
previous replay input (derived from the stored ``ApprovedCvContentResult``
itself) against the caller's current observation.

P6-A accepts **only** P5.5 content whose ``source_content_plan.plan_mode``
is ``CvContentPlanMode.ROLE_STRATEGY_INTEGRATED`` -- a ``LEGACY`` plan is
always rejected fail-closed (``LEGACY_CONTENT_PLAN_NOT_ALLOWED``), with no
fallback path that admits one. This is checked explicitly here, never left
to ``CvContentPlan``'s own Pydantic model validator alone: a plan built via
``model_copy(update=...)`` or ``model_construct(...)`` can carry a
``ROLE_STRATEGY_INTEGRATED`` mode alongside a ``None`` strategy field
without ever re-running that validator, so every strategy field
(``role_strategy_context_fingerprint``, ``strategy_selection_result_fingerprint``,
``strategy_ranking_input_fingerprint``, ``strategy_integration_policy_version``)
is independently re-checked for non-``None`` here too
(``CONTENT_PLAN_STRATEGY_FIELDS_INCOMPLETE``).
"""

from __future__ import annotations

import hashlib

from app.schemas.cv_content_approval import (
    ApprovalAssemblyStatus,
    ApprovedContentFreshnessStatus,
    ApprovedCvContentResult,
    FinalDispositionCode,
)
from app.schemas.cv_content_plan import CvContentPlan, CvContentPlanMode
from app.schemas.cv_document_artifact import (
    ApprovedContentDocumentInput,
    ApprovedContentDocumentInputValidationResult,
    DocumentInputValidationStatus,
    DocumentInputViolation,
    DocumentInputViolationCode,
)
from app.services.cv_content_approval_builder import compute_approved_content_result_fingerprint
from app.services.cv_content_approval_replay import (
    compute_approved_content_replay_input_fingerprint,
    evaluate_approved_content_freshness,
    replay_input_from_approved_content_result,
)
from app.services.cv_content_plan_builder import compute_content_plan_fingerprint
from app.services.cv_document_owner_identity import build_owner_key
from app.services.truth_fingerprint import canonical_json_bytes


DOCUMENT_INPUT_FINGERPRINT_VERSION = "cv-document-input-v1"

_UNRESOLVED_DISPOSITION_CODES = frozenset(
    {
        FinalDispositionCode.PENDING_USER_DECISION,
        FinalDispositionCode.SEMANTIC_VALIDATION_INCONCLUSIVE,
        FinalDispositionCode.SEMANTIC_VALIDATION_PROVIDER_ERROR,
    }
)


def recompute_approved_content_result_fingerprint(result: ApprovedCvContentResult) -> str:
    """Independently recompute ``ApprovedCvContentResult.result_fingerprint``
    from the result's own stored top-level fields -- never trusts the
    stored fingerprint itself."""

    draft_fingerprint = result.draft.draft_fingerprint if result.draft is not None else None
    disposition_fingerprints = [item.disposition_fingerprint for item in result.dispositions]
    return compute_approved_content_result_fingerprint(
        status=result.status,
        ready_for_p6=result.ready_for_p6,
        content_plan_fingerprint=result.content_plan_fingerprint,
        p5a_result_fingerprint=result.p5a_result_fingerprint,
        p5b_result_fingerprint=result.p5b_result_fingerprint,
        semantic_validation_result_fingerprint=result.semantic_validation_result_fingerprint,
        draft_fingerprint=draft_fingerprint,
        disposition_fingerprints=disposition_fingerprints,
    )


def recompute_content_plan_fingerprint(plan: CvContentPlan) -> str:
    """Independently recompute ``CvContentPlan.content_plan_fingerprint``
    from the plan's own stored top-level and section/pending/omission/
    conflict fingerprints -- never trusts the stored fingerprint itself."""

    return compute_content_plan_fingerprint(
        schema_version=plan.schema_version,
        target_context_fingerprint=plan.target_context_fingerprint,
        job_or_application_id=plan.job_or_application_id,
        content_policy_version=plan.content_policy_version,
        selection_policy_version=plan.selection_policy_version,
        transformation_policy_version=plan.transformation_policy_version,
        budget_profile_fingerprint=plan.budget_profile_fingerprint,
        source_decision_fingerprints=plan.source_decision_fingerprints,
        section_plan_fingerprints=[section.section_plan_fingerprint for section in plan.sections],
        pending_approval_fingerprints=[
            item.pending_approval_fingerprint for item in plan.pending_approvals
        ],
        omission_fingerprints=[item.omission_fingerprint for item in plan.omitted_facts],
        conflict_fingerprints=[item.conflict_fingerprint for item in plan.conflicts],
        career_positioning_snapshot_fingerprint=plan.career_positioning_snapshot_fingerprint,
        plan_mode=plan.plan_mode,
        role_strategy_context_fingerprint=plan.role_strategy_context_fingerprint,
        strategy_selection_result_fingerprint=plan.strategy_selection_result_fingerprint,
        strategy_ranking_input_fingerprint=plan.strategy_ranking_input_fingerprint,
        strategy_integration_policy_version=plan.strategy_integration_policy_version,
    )


def compute_document_input_fingerprint(
    *,
    approved_content_result_fingerprint: str,
    content_plan_fingerprint: str,
    owner_kind: str,
    owner_key_fingerprint: str,
    current_p55_replay_input_fingerprint: str,
) -> str:
    content = {
        "fingerprint_version": DOCUMENT_INPUT_FINGERPRINT_VERSION,
        "content": {
            "approved_content_result_fingerprint": approved_content_result_fingerprint,
            "content_plan_fingerprint": content_plan_fingerprint,
            "owner_kind": owner_kind,
            "owner_key_fingerprint": owner_key_fingerprint,
            "current_p55_replay_input_fingerprint": current_p55_replay_input_fingerprint,
        },
    }
    return hashlib.sha256(canonical_json_bytes(content)).hexdigest()


def validate_approved_content_document_input(
    document_input: ApprovedContentDocumentInput,
) -> ApprovedContentDocumentInputValidationResult:
    """Independently re-validate a raw ``ApprovedContentDocumentInput``.

    Type-level rejection of a P5a draft, a P5b proposal, or an arbitrary
    dict already happens at ``ApprovedContentDocumentInput`` construction
    time (its ``approved_content_result`` field is strictly typed as
    ``ApprovedCvContentResult``) -- this function only re-validates the
    *content* of an already well-typed envelope.

    P6-A accepts only P5.5 content originating from a
    ``ROLE_STRATEGY_INTEGRATED`` ``CvContentPlan``: a ``LEGACY`` plan is
    always rejected (``LEGACY_CONTENT_PLAN_NOT_ALLOWED``), and every
    strategy field on a nominally-integrated plan is independently
    re-checked for non-``None`` (``CONTENT_PLAN_STRATEGY_FIELDS_INCOMPLETE``)
    rather than trusted from ``CvContentPlan``'s own model validator alone.
    """

    violations: list[DocumentInputViolation] = []
    result = document_input.approved_content_result
    plan = document_input.source_content_plan

    recomputed_result_fingerprint = recompute_approved_content_result_fingerprint(result)
    if recomputed_result_fingerprint != result.result_fingerprint:
        violations.append(
            DocumentInputViolation(
                violation_code=DocumentInputViolationCode.RESULT_FINGERPRINT_MISMATCH,
                detail="approved_content_result.result_fingerprint does not match its recomputed content",
            )
        )

    if result.status != ApprovalAssemblyStatus.READY or not result.ready_for_p6:
        violations.append(
            DocumentInputViolation(
                violation_code=DocumentInputViolationCode.RESULT_NOT_READY,
                detail="approved_content_result must be status=READY and ready_for_p6=True",
            )
        )

    if result.pending_approval_fingerprints:
        violations.append(
            DocumentInputViolation(
                violation_code=DocumentInputViolationCode.RESULT_HAS_PENDING_APPROVALS,
                detail="approved_content_result carries unresolved pending_approval_fingerprints",
            )
        )

    if any(item.disposition_code in _UNRESOLVED_DISPOSITION_CODES for item in result.dispositions):
        violations.append(
            DocumentInputViolation(
                violation_code=DocumentInputViolationCode.RESULT_HAS_UNRESOLVED_SEMANTIC_VALIDATION,
                detail="approved_content_result carries an unresolved semantic-validation disposition",
            )
        )

    if plan.plan_mode != CvContentPlanMode.ROLE_STRATEGY_INTEGRATED:
        violations.append(
            DocumentInputViolation(
                violation_code=DocumentInputViolationCode.LEGACY_CONTENT_PLAN_NOT_ALLOWED,
                detail=(
                    "source_content_plan.plan_mode must be ROLE_STRATEGY_INTEGRATED; "
                    f"got {plan.plan_mode.value}"
                ),
            )
        )
    else:
        missing_strategy_fields = [
            name
            for name, value in (
                ("role_strategy_context_fingerprint", plan.role_strategy_context_fingerprint),
                ("strategy_selection_result_fingerprint", plan.strategy_selection_result_fingerprint),
                ("strategy_ranking_input_fingerprint", plan.strategy_ranking_input_fingerprint),
                ("strategy_integration_policy_version", plan.strategy_integration_policy_version),
            )
            if value is None
        ]
        if missing_strategy_fields:
            violations.append(
                DocumentInputViolation(
                    violation_code=DocumentInputViolationCode.CONTENT_PLAN_STRATEGY_FIELDS_INCOMPLETE,
                    detail=(
                        "source_content_plan.plan_mode=ROLE_STRATEGY_INTEGRATED requires every "
                        "strategy field to be non-None; missing: " + ", ".join(missing_strategy_fields)
                    ),
                )
            )

    recomputed_plan_fingerprint = recompute_content_plan_fingerprint(plan)
    if recomputed_plan_fingerprint != plan.content_plan_fingerprint:
        violations.append(
            DocumentInputViolation(
                violation_code=DocumentInputViolationCode.CONTENT_PLAN_FINGERPRINT_MISMATCH,
                detail="source_content_plan.content_plan_fingerprint does not match its recomputed content",
            )
        )

    if result.content_plan_fingerprint != plan.content_plan_fingerprint:
        violations.append(
            DocumentInputViolation(
                violation_code=DocumentInputViolationCode.CONTENT_PLAN_FINGERPRINT_DOES_NOT_MATCH_RESULT,
                detail=(
                    "approved_content_result.content_plan_fingerprint does not match "
                    "source_content_plan.content_plan_fingerprint"
                ),
            )
        )

    previous_p55_replay_input = replay_input_from_approved_content_result(
        semantic_validation_replay_input=document_input.semantic_validation_replay_input,
        approval_decisions=document_input.approval_decisions,
        result=result,
    )
    freshness = evaluate_approved_content_freshness(
        previous_p55_replay_input, document_input.current_p55_replay_input
    )
    if freshness.freshness_status != ApprovedContentFreshnessStatus.FRESH:
        violations.append(
            DocumentInputViolation(
                violation_code=DocumentInputViolationCode.RESULT_NOT_FRESH,
                detail=f"approved_content_result freshness is {freshness.freshness_status.value}",
            )
        )

    owner_key = build_owner_key(
        person_entity_id=plan.person_entity_id,
        owner_kind=document_input.owner_kind,
        owner_reference_id=plan.job_or_application_id,
    )

    current_p55_replay_input_fingerprint = compute_approved_content_replay_input_fingerprint(
        document_input.current_p55_replay_input
    )
    recomputed_document_input_fingerprint = compute_document_input_fingerprint(
        approved_content_result_fingerprint=result.result_fingerprint,
        content_plan_fingerprint=plan.content_plan_fingerprint,
        owner_kind=document_input.owner_kind.value,
        owner_key_fingerprint=owner_key.owner_key_fingerprint,
        current_p55_replay_input_fingerprint=current_p55_replay_input_fingerprint,
    )
    if recomputed_document_input_fingerprint != document_input.document_input_fingerprint:
        violations.append(
            DocumentInputViolation(
                violation_code=DocumentInputViolationCode.DOCUMENT_INPUT_FINGERPRINT_MISMATCH,
                detail="document_input_fingerprint does not match its recomputed content",
            )
        )

    if violations:
        return ApprovedContentDocumentInputValidationResult(
            status=DocumentInputValidationStatus.INVALID,
            violations=tuple(violations),
        )

    return ApprovedContentDocumentInputValidationResult(
        status=DocumentInputValidationStatus.VALID,
        owner_key=owner_key,
        recomputed_document_input_fingerprint=recomputed_document_input_fingerprint,
    )
