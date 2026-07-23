"""Controlled prompt-request construction for Candidacy Thesis.

The request built here carries only a fresh ``JobPostingAnalysisResult``'s
accepted role objective and hypotheses, plus a caller-supplied set of
approved fact payloads. It never carries pending facts, rejected facts,
the full Truth Library, the Master Resume, other applications, other job
postings, or any candidate-scoring instruction.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

from app.schemas.candidacy_thesis import (
    ALL_CANDIDACY_THESIS_PROHIBITIONS,
    ControlledCandidacyThesisRequest,
    PositioningLevel,
    RoleEvidenceCategory,
    ThesisPromptApprovedFact,
    ThesisPromptHypothesis,
)
from app.schemas.cv_content_generation import ApprovedFactPayloadSnapshot
from app.schemas.job_posting_analysis import EmployerProblemHypothesis, RoleObjective
from app.services.truth_fingerprint import canonical_json_bytes


PROMPT_FINGERPRINT_VERSION = "candidacy-thesis-prompt-fingerprint-v1"


def build_controlled_candidacy_thesis_request(
    *,
    posting_fingerprint: str,
    analysis_result_fingerprint: str,
    role_objective: RoleObjective,
    hypotheses: Sequence[EmployerProblemHypothesis],
    approved_facts: Sequence[ApprovedFactPayloadSnapshot],
    requested_positioning_level: PositioningLevel,
    priority_evidence_categories: Sequence[RoleEvidenceCategory],
    prompt_template_version: str,
    response_schema_version: str,
) -> ControlledCandidacyThesisRequest:
    return ControlledCandidacyThesisRequest(
        prompt_template_version=prompt_template_version,
        posting_fingerprint=posting_fingerprint,
        analysis_result_fingerprint=analysis_result_fingerprint,
        role_objective_fingerprint=role_objective.objective_fingerprint,
        role_objective_statement=role_objective.concise_statement,
        hypotheses=tuple(
            ThesisPromptHypothesis(
                hypothesis_fingerprint=h.hypothesis_fingerprint,
                problem_statement=h.problem_statement,
                affected_business_area=h.affected_business_area,
            )
            for h in hypotheses
        ),
        approved_facts=tuple(
            ThesisPromptApprovedFact(
                entity_id=f.entity_id,
                fact_id=f.fact_id,
                fact_type=f.fact_type,
                fact_revision=f.fact_revision,
                payload_fingerprint=f.payload_fingerprint,
                value_json=f.value_json,
            )
            for f in approved_facts
        ),
        requested_positioning_level=requested_positioning_level,
        priority_evidence_categories=tuple(priority_evidence_categories),
        prohibitions=ALL_CANDIDACY_THESIS_PROHIBITIONS,
        response_schema_version=response_schema_version,
    )


def compute_candidacy_thesis_prompt_fingerprint(
    request: ControlledCandidacyThesisRequest,
) -> str:
    projection = {
        "fingerprint_version": PROMPT_FINGERPRINT_VERSION,
        "content": request.model_dump(mode="json"),
    }
    return hashlib.sha256(canonical_json_bytes(projection)).hexdigest()
