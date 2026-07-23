"""Pure replay and stale-result detection for Explicit Provenance Stage P5.5.

Two fully separate replay families, matching the two-step split (Variant
B): semantic validation (Step 1) and approved content assembly (Step 2).
No function here reads a clock, performs a lookup, or touches a database.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

from app.schemas.cv_content_approval import (
    APPROVAL_DECISION_SCHEMA_VERSION,
    APPROVED_CONTENT_SCHEMA_VERSION,
    SEMANTIC_VALIDATION_SCHEMA_VERSION,
    ApprovedContentFreshnessStatus,
    ApprovedCvContentReplayInput,
    ApprovedCvContentReplayResult,
    ApprovedCvContentResult,
    ProposalApprovalDecision,
    ProposalSemanticValidationReplayInput,
    ProposalSemanticValidationReplayResult,
    ProposalSemanticValidationResult,
    SemanticValidationFreshnessStatus,
)
from app.schemas.cv_content_generation import CvContentGenerationReplayInput
from app.schemas.cv_content_plan import CvContentPlan
from app.schemas.cv_content_proposal import CvContentProposalReplayInput
from app.services.cv_content_approval_policy import (
    APPROVAL_DECISION_POLICY_VERSION,
    APPROVED_CONTENT_POLICY_VERSION,
)
from app.services.cv_content_proposal_replay import compute_p5a_replay_input_fingerprint
from app.services.truth_fingerprint import canonical_json_bytes


P5B_REPLAY_INPUT_FINGERPRINT_VERSION = "cv-content-approval-p5b-replay-input-v1"
SEMANTIC_VALIDATION_REPLAY_INPUT_FINGERPRINT_VERSION = (
    "cv-content-approval-semantic-validation-replay-input-v1"
)
APPROVED_CONTENT_REPLAY_INPUT_FINGERPRINT_VERSION = (
    "cv-content-approval-approved-content-replay-input-v1"
)


def compute_p5b_replay_input_fingerprint(replay_input: CvContentProposalReplayInput) -> str:
    """Fold P5b's own replay input into a single fingerprint for embedding
    in P5.5's Step 1 replay input -- Step 1 never re-derives P5b's replay
    fields itself, only hashes the exact snapshot it was given."""

    content = {
        "fingerprint_version": P5B_REPLAY_INPUT_FINGERPRINT_VERSION,
        "content": replay_input.model_dump(mode="json"),
    }
    return hashlib.sha256(canonical_json_bytes(content)).hexdigest()


# -- Step 1: semantic validation replay --------------------------------------


def replay_input_from_semantic_validation_result(
    *,
    plan: CvContentPlan,
    p5a_replay_input: CvContentGenerationReplayInput,
    p5a_generation_schema_version: str,
    p5a_generation_policy_version: str,
    p5b_replay_input: CvContentProposalReplayInput,
    p5b_proposal_schema_version: str,
    p5b_proposal_policy_version: str,
    fact_payload_set_fingerprint: str,
    result: ProposalSemanticValidationResult,
) -> ProposalSemanticValidationReplayInput:
    """Extract the staleness-relevant fields of a previously computed
    ``ProposalSemanticValidationResult``.

    ``plan``/``p5a_replay_input``/``p5b_replay_input`` are the exact inputs
    the result was computed from. The semantic-validator-specific fields
    (``validation_context_fingerprint``, the validator model configuration,
    prompt/policy versions) are read directly from ``result`` and its
    items' provenance -- never from a fresh ``SemanticValidationContext``,
    since Step 2 (``build_approved_cv_content``) never receives one. Any
    tampering with those stored fields is already caught independently by
    the caller's own ``result.result_fingerprint`` recomputation.
    """

    proposal_fingerprints = sorted(
        {item.proposal_fingerprint for item in result.validation_items}
        | {item.proposal_fingerprint for item in result.blocked_items}
    )
    immutable_constraint_set_fingerprints = sorted(
        {item.provenance.immutable_constraint_set_fingerprint for item in result.validation_items}
    )
    if result.validation_items:
        sample_provenance = result.validation_items[0].provenance
        validator_model_configuration_fingerprint = sample_provenance.model_configuration_fingerprint
        validation_prompt_version = sample_provenance.prompt_version
        validation_policy_version = sample_provenance.policy_version
        validation_schema_version = sample_provenance.schema_version
    else:
        validator_model_configuration_fingerprint = "0" * 64
        validation_prompt_version = "none"
        validation_policy_version = "none"
        validation_schema_version = SEMANTIC_VALIDATION_SCHEMA_VERSION

    return ProposalSemanticValidationReplayInput(
        content_plan_fingerprint=plan.content_plan_fingerprint,
        content_plan_schema_version=plan.schema_version,
        content_policy_version=plan.content_policy_version,
        p5a_result_fingerprint=result.p5a_result_fingerprint or "",
        p5a_replay_input_fingerprint=compute_p5a_replay_input_fingerprint(p5a_replay_input),
        p5a_generation_schema_version=p5a_generation_schema_version,
        p5a_generation_policy_version=p5a_generation_policy_version,
        p5b_result_fingerprint=result.p5b_result_fingerprint or "",
        p5b_replay_input_fingerprint=compute_p5b_replay_input_fingerprint(p5b_replay_input),
        p5b_proposal_schema_version=p5b_proposal_schema_version,
        p5b_proposal_policy_version=p5b_proposal_policy_version,
        fact_payload_set_fingerprint=fact_payload_set_fingerprint,
        proposal_fingerprints=tuple(proposal_fingerprints),
        validation_context_fingerprint=result.validation_context_fingerprint or "0" * 64,
        immutable_constraint_set_fingerprints=tuple(immutable_constraint_set_fingerprints),
        validator_model_configuration_fingerprint=validator_model_configuration_fingerprint,
        validation_prompt_version=validation_prompt_version,
        validation_policy_version=validation_policy_version,
        validation_schema_version=validation_schema_version,
        validation_item_fingerprints=tuple(
            sorted(item.semantic_validation_item_fingerprint for item in result.validation_items)
        ),
        blocked_validation_item_fingerprints=tuple(
            sorted(
                item.blocked_semantic_validation_item_fingerprint for item in result.blocked_items
            )
        ),
    )


def compute_semantic_validation_replay_input_fingerprint(
    replay_input: ProposalSemanticValidationReplayInput,
) -> str:
    content = {
        "fingerprint_version": SEMANTIC_VALIDATION_REPLAY_INPUT_FINGERPRINT_VERSION,
        "content": replay_input.model_dump(mode="json"),
    }
    return hashlib.sha256(canonical_json_bytes(content)).hexdigest()


def stale_semantic_validation_fields(
    previous: ProposalSemanticValidationReplayInput,
    current: ProposalSemanticValidationReplayInput,
) -> tuple[str, ...]:
    differing = {
        name
        for name in type(previous).model_fields
        if getattr(previous, name) != getattr(current, name)
    }
    return tuple(sorted(differing))


def is_semantic_validation_stale(
    previous: ProposalSemanticValidationReplayInput,
    current: ProposalSemanticValidationReplayInput,
) -> bool:
    return previous != current


def evaluate_semantic_validation_freshness(
    previous: ProposalSemanticValidationReplayInput,
    current: ProposalSemanticValidationReplayInput | None,
) -> ProposalSemanticValidationReplayResult:
    """``current=None`` means no current replay input was supplied at all --
    freshness is then ``FRESHNESS_NOT_VERIFIED``, never guessed as
    ``FRESH``."""

    if current is None:
        return ProposalSemanticValidationReplayResult(
            freshness_status=SemanticValidationFreshnessStatus.FRESHNESS_NOT_VERIFIED,
            stale_fields=(),
        )
    if previous == current:
        return ProposalSemanticValidationReplayResult(
            freshness_status=SemanticValidationFreshnessStatus.FRESH, stale_fields=()
        )
    return ProposalSemanticValidationReplayResult(
        freshness_status=SemanticValidationFreshnessStatus.STALE,
        stale_fields=stale_semantic_validation_fields(previous, current),
    )


# -- Step 2: approved content assembly replay --------------------------------


def replay_input_from_approved_content_result(
    *,
    semantic_validation_replay_input: ProposalSemanticValidationReplayInput,
    approval_decisions: Sequence[ProposalApprovalDecision],
    result: ApprovedCvContentResult,
) -> ApprovedCvContentReplayInput:
    """Extract the staleness-relevant fields of a previously computed
    ``ApprovedCvContentResult``."""

    section_fingerprints = (
        tuple(section.section_fingerprint for section in result.draft.sections)
        if result.draft is not None
        else ()
    )
    return ApprovedCvContentReplayInput(
        semantic_validation_result_fingerprint=result.semantic_validation_result_fingerprint or "",
        semantic_validation_replay_input_fingerprint=(
            compute_semantic_validation_replay_input_fingerprint(semantic_validation_replay_input)
        ),
        approval_decision_schema_version=APPROVAL_DECISION_SCHEMA_VERSION,
        approval_decision_policy_version=APPROVAL_DECISION_POLICY_VERSION,
        approved_content_schema_version=APPROVED_CONTENT_SCHEMA_VERSION,
        approved_content_policy_version=APPROVED_CONTENT_POLICY_VERSION,
        approval_decision_fingerprints=tuple(
            sorted(decision.decision_fingerprint for decision in approval_decisions)
        ),
        disposition_fingerprints=tuple(
            sorted(item.disposition_fingerprint for item in result.dispositions)
        ),
        approved_element_fingerprints=tuple(
            sorted(item.element_fingerprint for item in result.dispositions if item.element_fingerprint)
        ),
        section_fingerprints=section_fingerprints,
        draft_fingerprint=result.draft.draft_fingerprint if result.draft is not None else None,
        approved_content_result_fingerprint=result.result_fingerprint,
    )


def compute_approved_content_replay_input_fingerprint(
    replay_input: ApprovedCvContentReplayInput,
) -> str:
    content = {
        "fingerprint_version": APPROVED_CONTENT_REPLAY_INPUT_FINGERPRINT_VERSION,
        "content": replay_input.model_dump(mode="json"),
    }
    return hashlib.sha256(canonical_json_bytes(content)).hexdigest()


def stale_approved_content_fields(
    previous: ApprovedCvContentReplayInput, current: ApprovedCvContentReplayInput
) -> tuple[str, ...]:
    differing = {
        name
        for name in type(previous).model_fields
        if getattr(previous, name) != getattr(current, name)
    }
    return tuple(sorted(differing))


def is_approved_content_stale(
    previous: ApprovedCvContentReplayInput, current: ApprovedCvContentReplayInput
) -> bool:
    return previous != current


def evaluate_approved_content_freshness(
    previous: ApprovedCvContentReplayInput,
    current: ApprovedCvContentReplayInput | None,
) -> ApprovedCvContentReplayResult:
    if current is None:
        return ApprovedCvContentReplayResult(
            freshness_status=ApprovedContentFreshnessStatus.FRESHNESS_NOT_VERIFIED, stale_fields=()
        )
    if previous == current:
        return ApprovedCvContentReplayResult(
            freshness_status=ApprovedContentFreshnessStatus.FRESH, stale_fields=()
        )
    return ApprovedCvContentReplayResult(
        freshness_status=ApprovedContentFreshnessStatus.STALE,
        stale_fields=stale_approved_content_fields(previous, current),
    )
