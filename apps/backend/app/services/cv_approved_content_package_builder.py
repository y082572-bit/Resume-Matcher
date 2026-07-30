"""Explicit Provenance Stage P6-C1b-A: the Approved Content Package builder.

``build_approved_content_package`` never trusts a caller's document input at
face value -- it always re-runs ``validate_approved_content_document_input``
first, independently recomputes the owner key and every fingerprint that
feeds ``package_fingerprint``, and re-verifies each ``ProposalApprovalDecision``
against its own already-defined fingerprint helpers rather than trusting the
caller's ``decision_fingerprint``/``decision_context_fingerprint`` fields at
face value.

This module never implements the ``ApprovedContentDocumentInput`` resolver,
the POST Proposal API, router changes, the frontend, or any later P6-C
orchestration stage -- it only builds and fingerprints one immutable,
in-memory ``ApprovedContentPackage`` value object.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import re
from collections.abc import Sequence

from app.schemas.cv_approved_content_package import (
    APPROVAL_DECISIONS_FINGERPRINT_VERSION,
    APPROVED_CONTENT_PACKAGE_FINGERPRINT_VERSION,
    APPROVED_CONTENT_PACKAGE_POLICY_VERSION,
    APPROVED_CONTENT_PACKAGE_SCHEMA_VERSION,
    FACT_SELECTION_IDENTITY_FINGERPRINT_VERSION,
    PAYLOAD_LIMIT_BYTES,
    ApprovedContentPackage,
    ApprovedContentPackageBuildResult,
    ApprovedContentPackageBuildStatus,
    ApprovedContentPackageFingerprintInput,
    ApprovedContentPackagePayload,
)
from app.schemas.cv_content_approval import FinalDispositionCode, ProposalApprovalDecision
from app.schemas.cv_content_plan import CvContentPlan, CvExperienceSectionPlan, PlannedFactUse
from app.schemas.cv_document_artifact import (
    ApprovedContentDocumentInput,
    ArtifactOwnerKind,
    DocumentInputValidationStatus,
    JobArtifactOwnerKey,
)
from app.services.cv_content_approval_builder import (
    compute_decision_context_fingerprint,
    compute_proposal_approval_decision_fingerprint,
)
from app.services.cv_content_approval_replay import compute_approved_content_replay_input_fingerprint
from app.services.cv_document_input_validation import (
    compute_document_input_fingerprint,
    validate_approved_content_document_input,
)
from app.services.cv_document_owner_identity import build_owner_key
from app.services.truth_fingerprint import canonical_json_bytes
from app.schemas.truth_entity import SHA256_PATTERN


_SHA256_RE = re.compile(SHA256_PATTERN)

#: Terminal disposition codes a fully resolved ``CvContentFinalDisposition``
#: may carry. A section may be entirely, terminally omitted -- there is
#: deliberately no rule requiring every section to carry an approved output.
_ACCEPTED_TERMINAL_DISPOSITION_CODES = frozenset(
    {
        FinalDispositionCode.INCLUDED_DETERMINISTIC,
        FinalDispositionCode.INCLUDED_APPROVED_PROPOSAL,
        FinalDispositionCode.REJECTED_BY_USER,
        FinalDispositionCode.SEMANTIC_VALIDATION_FAILED,
        FinalDispositionCode.INPUT_INVALID,
        FinalDispositionCode.NON_ELIGIBLE_BLOCKED,
    }
)

#: Never-terminal codes, listed only for documentation/tests -- rejection is
#: always driven by "not in _ACCEPTED_TERMINAL_DISPOSITION_CODES", never by
#: this closed list alone (a future disposition code would otherwise be
#: silently accepted).
_REJECTED_NON_TERMINAL_DISPOSITION_CODES = frozenset(
    {
        FinalDispositionCode.PENDING_USER_DECISION,
        FinalDispositionCode.SEMANTIC_VALIDATION_INCONCLUSIVE,
        FinalDispositionCode.SEMANTIC_VALIDATION_PROVIDER_ERROR,
        FinalDispositionCode.REQUIRES_REPLAN,
    }
)


def _iter_planned_fact_uses(plan: CvContentPlan) -> list[PlannedFactUse]:
    uses: list[PlannedFactUse] = []
    for section in plan.sections:
        if isinstance(section, CvExperienceSectionPlan):
            for entry in section.experience_entries:
                uses.extend(entry.header_fact_uses)
                uses.extend(entry.responsibility_fact_uses)
                uses.extend(entry.achievement_fact_uses)
        else:
            uses.extend(section.planned_fact_uses)
    return uses


def compute_approval_decisions_fingerprint(
    decisions: Sequence[ProposalApprovalDecision],
    *,
    expected_semantic_validation_result_fingerprint: str | None,
) -> str | None:
    """Independently re-verify every ``ProposalApprovalDecision`` and return
    the deterministic fingerprint of the fully verified, ordered set.

    Returns ``None`` (never raises) if any decision fails re-verification --
    a duplicate ``proposal_fingerprint`` target, a
    ``decision_context_fingerprint``/``decision_fingerprint`` that does not
    match its own recomputed value, or a decision that does not reference
    the current semantic-validation-result lineage.
    """

    seen_proposal_fingerprints: set[str] = set()
    verified: list[ProposalApprovalDecision] = []
    for decision in decisions:
        if decision.proposal_fingerprint in seen_proposal_fingerprints:
            return None
        seen_proposal_fingerprints.add(decision.proposal_fingerprint)

        if decision.semantic_validation_result_fingerprint != expected_semantic_validation_result_fingerprint:
            return None

        expected_context_fingerprint = compute_decision_context_fingerprint(
            proposal_fingerprint=decision.proposal_fingerprint,
            proposal_text_fingerprint=decision.proposal_text_fingerprint,
            semantic_validation_item_fingerprint=decision.semantic_validation_item_fingerprint,
            semantic_validation_result_fingerprint=decision.semantic_validation_result_fingerprint,
        )
        if expected_context_fingerprint != decision.decision_context_fingerprint:
            return None

        expected_decision_fingerprint = compute_proposal_approval_decision_fingerprint(
            decision_context_fingerprint=expected_context_fingerprint,
            decision=decision.decision,
            decision_policy_version=decision.decision_policy_version,
        )
        if expected_decision_fingerprint != decision.decision_fingerprint:
            return None

        verified.append(decision)

    ordered = sorted(verified, key=lambda item: (item.proposal_fingerprint, item.decision_fingerprint))
    content = {
        "fingerprint_version": APPROVAL_DECISIONS_FINGERPRINT_VERSION,
        "content": {"decisions": [item.model_dump(mode="json") for item in ordered]},
    }
    return hashlib.sha256(canonical_json_bytes(content)).hexdigest()


def compute_fact_selection_identity_fingerprint(
    source_decision_fingerprints: Sequence[str],
) -> str | None:
    """Deterministic identity of the exact set of P3 decision fingerprints a
    plan's facts were selected from. Returns ``None`` (never raises) if any
    entry is not a well-formed SHA-256 hex digest or is duplicated."""

    seen: set[str] = set()
    for fingerprint in source_decision_fingerprints:
        if not _SHA256_RE.fullmatch(fingerprint):
            return None
        if fingerprint in seen:
            return None
        seen.add(fingerprint)

    ordered = sorted(source_decision_fingerprints)
    content = {
        "fingerprint_version": FACT_SELECTION_IDENTITY_FINGERPRINT_VERSION,
        "content": {"source_decision_fingerprints": ordered},
    }
    return hashlib.sha256(canonical_json_bytes(content)).hexdigest()


def build_fingerprint_input(
    *,
    owner_key_fingerprint: str,
    owner_kind: ArtifactOwnerKind,
    document_input_fingerprint: str,
    approved_content_result_fingerprint: str,
    content_plan_fingerprint: str,
    semantic_validation_result_fingerprint: str | None,
    approval_decisions_fingerprint: str,
    fact_selection_identity_fingerprint: str,
    role_strategy_context_fingerprint: str | None,
    strategy_selection_result_fingerprint: str | None,
    strategy_ranking_input_fingerprint: str | None,
    strategy_integration_policy_version: str | None,
    p5a_result_fingerprint: str | None,
    p5b_result_fingerprint: str | None,
    payload_sha256: str,
    document_input_schema_version: str,
    document_input_policy_version: str,
    package_schema_version: str,
    package_policy_version: str,
) -> ApprovedContentPackageFingerprintInput:
    """Build the exact, deterministically reconstructable fingerprint input
    -- shared by the builder (fresh construction) and the SQL repository
    (re-derivation from a stored row, never trusting it at face value)."""

    return ApprovedContentPackageFingerprintInput(
        owner_key_fingerprint=owner_key_fingerprint,
        owner_kind=owner_kind,
        document_input_fingerprint=document_input_fingerprint,
        approved_content_result_fingerprint=approved_content_result_fingerprint,
        content_plan_fingerprint=content_plan_fingerprint,
        semantic_validation_result_fingerprint=semantic_validation_result_fingerprint,
        approval_decisions_fingerprint=approval_decisions_fingerprint,
        fact_selection_identity_fingerprint=fact_selection_identity_fingerprint,
        role_strategy_context_fingerprint=role_strategy_context_fingerprint,
        strategy_selection_result_fingerprint=strategy_selection_result_fingerprint,
        strategy_ranking_input_fingerprint=strategy_ranking_input_fingerprint,
        strategy_integration_policy_version=strategy_integration_policy_version,
        p5a_result_fingerprint=p5a_result_fingerprint,
        p5b_result_fingerprint=p5b_result_fingerprint,
        payload_sha256=payload_sha256,
        document_input_schema_version=document_input_schema_version,
        document_input_policy_version=document_input_policy_version,
        package_fingerprint_schema_version=APPROVED_CONTENT_PACKAGE_FINGERPRINT_VERSION,
        package_schema_version=package_schema_version,
        package_policy_version=package_policy_version,
    )


def compute_package_fingerprint(fingerprint_input: ApprovedContentPackageFingerprintInput) -> str:
    return hashlib.sha256(canonical_json_bytes(fingerprint_input.model_dump(mode="json"))).hexdigest()


def build_approved_content_package(
    *,
    owner_key: JobArtifactOwnerKey,
    document_input: ApprovedContentDocumentInput,
    package_schema_version: str = APPROVED_CONTENT_PACKAGE_SCHEMA_VERSION,
    package_policy_version: str = APPROVED_CONTENT_PACKAGE_POLICY_VERSION,
) -> ApprovedContentPackageBuildResult:
    validation_result = validate_approved_content_document_input(document_input)
    if validation_result.status != DocumentInputValidationStatus.VALID:
        return ApprovedContentPackageBuildResult(
            status=ApprovedContentPackageBuildStatus.DOCUMENT_INPUT_INVALID,
            input_violations=validation_result.violations,
        )

    plan = document_input.source_content_plan
    result = document_input.approved_content_result

    recomputed_owner_key = build_owner_key(
        person_entity_id=plan.person_entity_id,
        owner_kind=document_input.owner_kind,
        owner_reference_id=plan.job_or_application_id,
    )
    if recomputed_owner_key != owner_key:
        return ApprovedContentPackageBuildResult(status=ApprovedContentPackageBuildStatus.OWNER_MISMATCH)

    planned_fact_uses = _iter_planned_fact_uses(plan)
    planned_fingerprints = [use.planned_fact_use_fingerprint for use in planned_fact_uses]
    disposition_fingerprints = [item.planned_fact_use_fingerprint for item in result.dispositions]
    if len(planned_fingerprints) != len(disposition_fingerprints) or set(planned_fingerprints) != set(
        disposition_fingerprints
    ):
        return ApprovedContentPackageBuildResult(
            status=ApprovedContentPackageBuildStatus.TERMINAL_DISPOSITION_SET_VIOLATION,
            diagnostics=("dispositions do not cover exactly every PlannedFactUse in the content plan",),
        )
    if any(
        item.disposition_code not in _ACCEPTED_TERMINAL_DISPOSITION_CODES for item in result.dispositions
    ):
        return ApprovedContentPackageBuildResult(
            status=ApprovedContentPackageBuildStatus.TERMINAL_DISPOSITION_SET_VIOLATION,
            diagnostics=("a disposition carries a non-terminal disposition_code",),
        )

    # Defensive deep copies: ``ApprovedContentPackagePayload`` is itself
    # frozen, but that alone does not stop a caller from later mutating a
    # non-frozen nested object (``ApprovedCvContentResult``/``CvContentPlan``
    # are not ``frozen=True``) it still holds a reference to -- so every
    # nested value is deep-copied into the payload, never embedded by
    # reference.
    payload = ApprovedContentPackagePayload(
        approved_content_result=result.model_copy(deep=True),
        source_content_plan=plan.model_copy(deep=True),
        current_p55_replay_input=document_input.current_p55_replay_input.model_copy(deep=True),
        semantic_validation_replay_input=document_input.semantic_validation_replay_input.model_copy(deep=True),
        approval_decisions=tuple(
            decision.model_copy(deep=True) for decision in document_input.approval_decisions
        ),
        owner_kind=document_input.owner_kind,
        document_input_schema_version=document_input.document_input_schema_version,
        document_input_policy_version=document_input.document_input_policy_version,
    )
    payload_bytes = canonical_json_bytes(payload.model_dump(mode="json"))
    if len(payload_bytes) > PAYLOAD_LIMIT_BYTES:
        return ApprovedContentPackageBuildResult(status=ApprovedContentPackageBuildStatus.PAYLOAD_TOO_LARGE)
    payload_sha256 = hashlib.sha256(payload_bytes).hexdigest()

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
        return ApprovedContentPackageBuildResult(
            status=ApprovedContentPackageBuildStatus.DOCUMENT_INPUT_INVALID,
            diagnostics=("document_input_fingerprint does not match its recomputed content",),
        )

    approval_decisions_fingerprint = compute_approval_decisions_fingerprint(
        document_input.approval_decisions,
        expected_semantic_validation_result_fingerprint=result.semantic_validation_result_fingerprint,
    )
    if approval_decisions_fingerprint is None:
        return ApprovedContentPackageBuildResult(
            status=ApprovedContentPackageBuildStatus.DOCUMENT_INPUT_INVALID,
            diagnostics=("an approval decision failed independent re-verification",),
        )

    fact_selection_identity_fingerprint = compute_fact_selection_identity_fingerprint(
        plan.source_decision_fingerprints
    )
    if fact_selection_identity_fingerprint is None:
        return ApprovedContentPackageBuildResult(
            status=ApprovedContentPackageBuildStatus.DOCUMENT_INPUT_INVALID,
            diagnostics=("source_content_plan.source_decision_fingerprints failed identity re-verification",),
        )

    fingerprint_input = build_fingerprint_input(
        owner_key_fingerprint=owner_key.owner_key_fingerprint,
        owner_kind=document_input.owner_kind,
        document_input_fingerprint=recomputed_document_input_fingerprint,
        approved_content_result_fingerprint=result.result_fingerprint,
        content_plan_fingerprint=plan.content_plan_fingerprint,
        semantic_validation_result_fingerprint=result.semantic_validation_result_fingerprint,
        approval_decisions_fingerprint=approval_decisions_fingerprint,
        fact_selection_identity_fingerprint=fact_selection_identity_fingerprint,
        role_strategy_context_fingerprint=plan.role_strategy_context_fingerprint,
        strategy_selection_result_fingerprint=plan.strategy_selection_result_fingerprint,
        strategy_ranking_input_fingerprint=plan.strategy_ranking_input_fingerprint,
        strategy_integration_policy_version=plan.strategy_integration_policy_version,
        p5a_result_fingerprint=result.p5a_result_fingerprint,
        p5b_result_fingerprint=result.p5b_result_fingerprint,
        payload_sha256=payload_sha256,
        document_input_schema_version=document_input.document_input_schema_version,
        document_input_policy_version=document_input.document_input_policy_version,
        package_schema_version=package_schema_version,
        package_policy_version=package_policy_version,
    )
    package_fingerprint = compute_package_fingerprint(fingerprint_input)

    package = ApprovedContentPackage(
        package_fingerprint=package_fingerprint,
        owner_key_fingerprint=owner_key.owner_key_fingerprint,
        owner_kind=document_input.owner_kind,
        document_input_fingerprint=recomputed_document_input_fingerprint,
        approval_decisions_fingerprint=approval_decisions_fingerprint,
        fact_selection_identity_fingerprint=fact_selection_identity_fingerprint,
        payload_sha256=payload_sha256,
        payload_byte_size=len(payload_bytes),
        payload=payload,
        package_schema_version=package_schema_version,
        package_policy_version=package_policy_version,
        created_at=_utcnow_iso(),
    )
    return ApprovedContentPackageBuildResult(status=ApprovedContentPackageBuildStatus.BUILT, package=package)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
