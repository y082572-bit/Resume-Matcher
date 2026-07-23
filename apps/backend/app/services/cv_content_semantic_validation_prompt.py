"""Versioned, bounded prompt-request construction for one semantic
validation call.

``build_controlled_semantic_validation_request`` always builds a request
scoped to exactly one proposal for exactly one fact: no other fact_id, no
other proposal, no full plan, no full P5a draft, no full job ad, and no
Master Resume ever enter the request. There is no batch prompt.
"""

from __future__ import annotations

import hashlib
from uuid import UUID

from app.schemas.cv_content_approval import (
    SEMANTIC_VALIDATION_PROMPT_VERSION,
    SEMANTIC_VALIDATION_RESPONSE_SCHEMA_VERSION,
    ControlledSemanticValidationRequest,
)
from app.schemas.cv_content_plan import CvSection
from app.schemas.cv_content_proposal import ImmutableConstraintSet, TargetRoleContext
from app.schemas.truth_fact import TransformationOperation
from app.services.truth_fingerprint import canonical_json_bytes


PROMPT_REQUEST_FINGERPRINT_VERSION = "cv-content-semantic-validation-prompt-request-v1"

#: Explicit, closed forbidden-semantic-change list embedded in every
#: semantic validation prompt. A caller extending this tuple must bump
#: ``SEMANTIC_VALIDATION_PROMPT_VERSION``.
FORBIDDEN_SEMANTIC_CHANGES: tuple[str, ...] = (
    "do not add a new claim",
    "do not remove a material claim",
    "do not increase responsibility scope",
    "do not materially decrease responsibility scope",
    "do not change management scope",
    "do not change decision authority",
    "do not change ownership",
    "do not change achievement attribution",
    "do not change employment attribution",
    "do not change the company",
    "do not change the role",
    "do not change a date",
    "do not change a number",
    "do not change a percentage",
    "do not change a currency amount",
    "do not add or remove a negation",
    "do not change certainty",
    "do not change causality",
    "do not change team size",
    "do not change portfolio scale",
    "do not change geographic scope",
    "do not change product scope",
    "do not change customer scope",
    "flattening for a lower role must never become a false claim",
)


def build_controlled_semantic_validation_request(
    *,
    operation: TransformationOperation,
    fact_type: str,
    target_section: CvSection,
    target_scope: str,
    employment_scope_entity_id: UUID | None,
    source_fact_id: UUID,
    source_text: str,
    proposal_text: str,
    proposal_fingerprint: str,
    immutable_constraints: ImmutableConstraintSet,
    target_role_context: TargetRoleContext,
) -> ControlledSemanticValidationRequest:
    return ControlledSemanticValidationRequest(
        prompt_template_version=SEMANTIC_VALIDATION_PROMPT_VERSION,
        operation=operation,
        fact_type=fact_type,
        target_section=target_section,
        target_scope=target_scope,
        employment_scope_entity_id=employment_scope_entity_id,
        source_fact_id=source_fact_id,
        source_text=source_text,
        proposal_text=proposal_text,
        proposal_fingerprint=proposal_fingerprint,
        immutable_constraints=immutable_constraints,
        target_role_context=target_role_context,
        forbidden_semantic_changes=FORBIDDEN_SEMANTIC_CHANGES,
        response_schema_version=SEMANTIC_VALIDATION_RESPONSE_SCHEMA_VERSION,
    )


def compute_semantic_validation_prompt_fingerprint(
    request: ControlledSemanticValidationRequest,
) -> str:
    """The prompt's identity fingerprint.

    Two requests with identical content (including
    ``prompt_template_version``) always yield the same fingerprint -- this
    is exactly what the P5.5 retry policy requires: every retry attempt
    must reuse the same request fingerprint.
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
            "proposal_text": request.proposal_text,
            "proposal_fingerprint": request.proposal_fingerprint,
            "immutable_constraint_set_fingerprint": (
                request.immutable_constraints.immutable_constraint_set_fingerprint
            ),
            "target_role_context": {
                "target_job_title": request.target_role_context.target_job_title,
                "target_seniority_level": request.target_role_context.target_seniority_level,
                "target_role_family": request.target_role_context.target_role_family,
            },
            "forbidden_semantic_changes": list(request.forbidden_semantic_changes),
            "response_schema_version": request.response_schema_version,
        },
    }
    return hashlib.sha256(canonical_json_bytes(content)).hexdigest()
