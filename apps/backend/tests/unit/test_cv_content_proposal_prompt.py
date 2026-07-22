"""``cv_content_proposal_prompt``: bounded, one-fact, versioned prompt requests."""

from __future__ import annotations

import re
from pathlib import Path
from uuid import uuid4

from app.schemas.cv_content_plan import CvSection
from app.schemas.cv_content_proposal import (
    CONTROLLED_REPHRASE_PROMPT_VERSION,
    FLATTEN_FOR_LOWER_ROLE_PROMPT_VERSION,
    TargetRoleContext,
)
from app.schemas.truth_fact import TransformationOperation
from app.services import cv_content_proposal_prompt
from app.services.cv_content_proposal_constraints import build_immutable_constraint_set
from app.services.cv_content_proposal_prompt import (
    build_controlled_proposal_request,
    compute_prompt_fingerprint,
    compute_target_role_context_fingerprint,
    resolve_prompt_template_version,
)


def _request(**overrides):
    fact_id = overrides.pop("source_fact_id", uuid4())
    constraints = build_immutable_constraint_set(
        fact_id=fact_id, fact_type="ACHIEVEMENT", source_text="Grew revenue by 30%."
    )
    values = dict(
        operation=TransformationOperation.CONTROLLED_REPHRASE,
        fact_type="ACHIEVEMENT",
        target_section=CvSection.EXPERIENCE,
        target_scope="EXPERIENCE",
        employment_scope_entity_id=None,
        source_fact_id=fact_id,
        source_text="Grew revenue by 30%.",
        immutable_constraints=constraints,
        target_role_context=TargetRoleContext(),
        tone_profile="professional",
        maximum_character_count=300,
        maximum_sentence_count=3,
    )
    values.update(overrides)
    return build_controlled_proposal_request(**values)


def test_resolves_prompt_template_version_per_operation() -> None:
    assert (
        resolve_prompt_template_version(TransformationOperation.CONTROLLED_REPHRASE)
        == CONTROLLED_REPHRASE_PROMPT_VERSION
    )
    assert (
        resolve_prompt_template_version(TransformationOperation.FLATTEN_FOR_LOWER_ROLE)
        == FLATTEN_FOR_LOWER_ROLE_PROMPT_VERSION
    )


def test_request_is_scoped_to_one_fact_only() -> None:
    request = _request()
    dumped = request.model_dump()
    # Exactly one fact_id-shaped identity field: source_fact_id.
    assert "fact_ids" not in dumped
    assert "other_facts" not in dumped
    assert request.immutable_constraints.fact_id == request.source_fact_id


def test_request_never_carries_forbidden_fields() -> None:
    request = _request()
    forbidden_field_names = {
        "job_description",
        "job_ad_text",
        "full_cv",
        "master_resume",
        "truth_library",
        "other_employment_entries",
        "legacy_record_key",
        "omitted_facts",
        "pending_facts",
        "api_key",
    }
    assert forbidden_field_names.isdisjoint(type(request).model_fields.keys())


def test_request_never_carries_source_reference_or_legacy_key() -> None:
    request = _request()
    assert "source_reference" not in type(request).model_fields
    assert "legacy_record_key" not in type(request).model_fields


def test_operation_is_present_in_prompt_request() -> None:
    request = _request(operation=TransformationOperation.FLATTEN_FOR_LOWER_ROLE)
    assert request.operation == TransformationOperation.FLATTEN_FOR_LOWER_ROLE


def test_immutable_constraints_are_present_in_request() -> None:
    request = _request()
    assert len(request.immutable_constraints.constraints) > 0


def test_target_role_context_is_bounded() -> None:
    context = TargetRoleContext(
        target_job_title="Analyst", target_seniority_level="Senior", target_role_family="Data"
    )
    assert set(type(context).model_fields.keys()) == {
        "target_job_title",
        "target_seniority_level",
        "target_role_family",
    }


def test_prompt_fingerprint_is_deterministic() -> None:
    fact_id = uuid4()
    r1 = _request(source_fact_id=fact_id)
    r2 = _request(source_fact_id=fact_id)
    assert compute_prompt_fingerprint(r1) == compute_prompt_fingerprint(r2)


def test_prompt_fingerprint_changes_with_prompt_version() -> None:
    fact_id = uuid4()
    r1 = _request(source_fact_id=fact_id, operation=TransformationOperation.CONTROLLED_REPHRASE)
    r2 = _request(source_fact_id=fact_id, operation=TransformationOperation.FLATTEN_FOR_LOWER_ROLE)
    assert r1.prompt_template_version != r2.prompt_template_version
    assert compute_prompt_fingerprint(r1) != compute_prompt_fingerprint(r2)


def test_prompt_fingerprint_changes_with_source_text() -> None:
    fact_id = uuid4()
    r1 = _request(source_fact_id=fact_id, source_text="Grew revenue by 30%.")
    r2 = _request(source_fact_id=fact_id, source_text="Grew revenue by 40%.")
    assert compute_prompt_fingerprint(r1) != compute_prompt_fingerprint(r2)


def test_target_role_context_fingerprint_changes_with_content() -> None:
    a = compute_target_role_context_fingerprint(TargetRoleContext(target_job_title="Analyst"))
    b = compute_target_role_context_fingerprint(TargetRoleContext(target_job_title="Manager"))
    assert a != b


# -- source boundary: no batch prompt, no other employment entries --


def test_prompt_module_never_references_forbidden_symbols() -> None:
    text = re.sub(
        r'""".*?"""', "", Path(cv_content_proposal_prompt.__file__).read_text(encoding="utf-8"), flags=re.DOTALL
    )
    forbidden = (
        "app.llm",
        "litellm",
        "app.database",
        "sqlalchemy",
        "app.models",
        "source_reference",
        "legacy_record_key",
        "master_resume",
    )
    lowered = text.lower()
    for needle in forbidden:
        assert needle.lower() not in lowered, f"prompt module references forbidden symbol: {needle}"
