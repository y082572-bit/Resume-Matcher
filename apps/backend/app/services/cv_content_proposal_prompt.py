"""Versioned, bounded prompt-request construction for one controlled proposal.

``build_controlled_proposal_request`` always builds a request scoped to
exactly one fact: no other fact_id, no other employment entry, no full
plan, no full P5a draft, no omitted/pending facts, no full job ad, and no
Master Resume ever enter the request. There is no batch prompt.
"""

from __future__ import annotations

import hashlib
from uuid import UUID

from app.schemas.cv_content_plan import CvSection
from app.schemas.cv_content_proposal import (
    CONTROLLED_REPHRASE_PROMPT_VERSION,
    FLATTEN_FOR_LOWER_ROLE_PROMPT_VERSION,
    PROPOSAL_RESPONSE_SCHEMA_VERSION,
    ControlledProposalRequest,
    ImmutableConstraintSet,
    TargetRoleContext,
)
from app.schemas.truth_fact import TransformationOperation
from app.services.truth_fingerprint import canonical_json_bytes


PROMPT_REQUEST_FINGERPRINT_VERSION = "cv-content-proposal-prompt-request-v1"
TARGET_ROLE_CONTEXT_FINGERPRINT_VERSION = "cv-content-proposal-target-role-context-v1"

#: Explicit, closed forbidden-transformation list embedded in every prompt.
#: A caller extending this tuple must bump ``prompt_template_version``.
FORBIDDEN_TRANSFORMATIONS: tuple[str, ...] = (
    "do not invent a new claim",
    "do not change any number",
    "do not change any date",
    "do not change any percentage",
    "do not change any currency amount",
    "do not change the company name",
    "do not change the job title",
    "do not add or remove a negation",
    "do not merge in another fact",
    "do not reference another employment entry",
)

#: The closed operation -> prompt-template-version map. Every eligible
#: operation must appear here; absence is a programming error.
_PROMPT_TEMPLATE_VERSION_BY_OPERATION: dict[TransformationOperation, str] = {
    TransformationOperation.CONTROLLED_REPHRASE: CONTROLLED_REPHRASE_PROMPT_VERSION,
    TransformationOperation.FLATTEN_FOR_LOWER_ROLE: FLATTEN_FOR_LOWER_ROLE_PROMPT_VERSION,
}


def resolve_prompt_template_version(operation: TransformationOperation) -> str:
    return _PROMPT_TEMPLATE_VERSION_BY_OPERATION[operation]


def build_controlled_proposal_request(
    *,
    operation: TransformationOperation,
    fact_type: str,
    target_section: CvSection,
    target_scope: str,
    employment_scope_entity_id: UUID | None,
    source_fact_id: UUID,
    source_text: str,
    immutable_constraints: ImmutableConstraintSet,
    target_role_context: TargetRoleContext,
    tone_profile: str,
    maximum_character_count: int,
    maximum_sentence_count: int,
) -> ControlledProposalRequest:
    return ControlledProposalRequest(
        prompt_template_version=resolve_prompt_template_version(operation),
        operation=operation,
        fact_type=fact_type,
        target_section=target_section,
        target_scope=target_scope,
        employment_scope_entity_id=employment_scope_entity_id,
        source_fact_id=source_fact_id,
        source_text=source_text,
        immutable_constraints=immutable_constraints,
        target_role_context=target_role_context,
        tone_profile=tone_profile,
        maximum_character_count=maximum_character_count,
        maximum_sentence_count=maximum_sentence_count,
        forbidden_transformations=FORBIDDEN_TRANSFORMATIONS,
        response_schema_version=PROPOSAL_RESPONSE_SCHEMA_VERSION,
    )


def compute_target_role_context_fingerprint(context: TargetRoleContext) -> str:
    content = {
        "fingerprint_version": TARGET_ROLE_CONTEXT_FINGERPRINT_VERSION,
        "content": {
            "target_job_title": context.target_job_title,
            "target_seniority_level": context.target_seniority_level,
            "target_role_family": context.target_role_family,
        },
    }
    return hashlib.sha256(canonical_json_bytes(content)).hexdigest()


def compute_prompt_fingerprint(request: ControlledProposalRequest) -> str:
    """The prompt's identity fingerprint.

    Two requests with identical content (including
    ``prompt_template_version``) always yield the same fingerprint -- this
    is exactly what P5b's retry policy requires: every retry attempt must
    reuse the same request fingerprint.
    """

    content = {
        "fingerprint_version": PROMPT_REQUEST_FINGERPRINT_VERSION,
        "content": {
            "prompt_template_version": request.prompt_template_version,
            "operation": request.operation.value,
            "fact_type": request.fact_type,
            "target_section": request.target_section.value,
            "target_scope": request.target_scope,
            "employment_scope_entity_id": (
                str(request.employment_scope_entity_id)
                if request.employment_scope_entity_id is not None
                else None
            ),
            "source_fact_id": str(request.source_fact_id),
            "source_text": request.source_text,
            "immutable_constraint_set_fingerprint": (
                request.immutable_constraints.immutable_constraint_set_fingerprint
            ),
            "target_role_context_fingerprint": compute_target_role_context_fingerprint(
                request.target_role_context
            ),
            "tone_profile": request.tone_profile,
            "maximum_character_count": request.maximum_character_count,
            "maximum_sentence_count": request.maximum_sentence_count,
            "forbidden_transformations": list(request.forbidden_transformations),
            "response_schema_version": request.response_schema_version,
        },
    }
    return hashlib.sha256(canonical_json_bytes(content)).hexdigest()
