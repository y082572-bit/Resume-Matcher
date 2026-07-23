"""Controlled prompt-request construction for Job Posting Analysis.

The request built here carries only a ``JobPostingSnapshot``'s public
fields, its deterministic ``JobEvidenceSegment`` list, and a closed set of
output prohibitions. It never carries Marek's CV, facts, Truth Library,
Master Resume, prior applications, other postings, or a candidate profile.
"""

from __future__ import annotations

import hashlib

from app.schemas.job_posting_analysis import (
    ALL_JOB_ANALYSIS_PROHIBITIONS,
    ControlledJobAnalysisRequest,
    JobEvidenceSegment,
    JobPostingSnapshot,
)
from app.services.truth_fingerprint import canonical_json_bytes


PROMPT_FINGERPRINT_VERSION = "job-analysis-prompt-fingerprint-v1"


def build_controlled_job_analysis_request(
    *,
    snapshot: JobPostingSnapshot,
    segments: tuple[JobEvidenceSegment, ...],
    prompt_template_version: str,
    response_schema_version: str,
) -> ControlledJobAnalysisRequest:
    return ControlledJobAnalysisRequest(
        prompt_template_version=prompt_template_version,
        posting_fingerprint=snapshot.snapshot_fingerprint,
        source_language=snapshot.source_language,
        role_title=snapshot.role_title,
        company_name=snapshot.company_name,
        segments=segments,
        prohibitions=ALL_JOB_ANALYSIS_PROHIBITIONS,
        response_schema_version=response_schema_version,
    )


def compute_job_analysis_prompt_fingerprint(request: ControlledJobAnalysisRequest) -> str:
    projection = {
        "fingerprint_version": PROMPT_FINGERPRINT_VERSION,
        "content": request.model_dump(mode="json"),
    }
    return hashlib.sha256(canonical_json_bytes(projection)).hexdigest()
