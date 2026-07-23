"""Unit tests for ``cv_content_semantic_validation_prompt``.

Confirms the semantic-validation prompt request is bounded to one proposal
for one fact, carries no batch data, no other employment entry, no
Master Resume, no ``source_reference``/``legacy_record_key``, and no
corrected-proposal field -- and that its fingerprint is deterministic.
"""

from __future__ import annotations

from uuid import uuid4

from app.schemas.cv_content_plan import CvSection
from app.schemas.cv_content_proposal import (
    ImmutableConstraintSet,
    TargetRoleContext,
)
from app.schemas.truth_fact import TransformationOperation
from app.services.cv_content_proposal_constraints import build_immutable_constraint_set
from app.services.cv_content_semantic_validation_prompt import (
    FORBIDDEN_SEMANTIC_CHANGES,
    build_controlled_semantic_validation_request,
    compute_semantic_validation_prompt_fingerprint,
)


def _constraint_set(fact_id) -> ImmutableConstraintSet:
    return build_immutable_constraint_set(
        fact_id=fact_id,
        fact_type="ACHIEVEMENT",
        source_text="Grew revenue by 30% in one synthetic year.",
    )


def _build_request(**overrides):
    fact_id = overrides.pop("source_fact_id", uuid4())
    values = dict(
        operation=TransformationOperation.CONTROLLED_REPHRASE,
        fact_type="ACHIEVEMENT",
        target_section=CvSection.ACHIEVEMENTS,
        target_scope="PROJECT",
        employment_scope_entity_id=None,
        source_fact_id=fact_id,
        source_text="Grew revenue by 30% in one synthetic year.",
        proposal_text="Achieved a 30% revenue increase within one synthetic year.",
        proposal_fingerprint="a" * 64,
        immutable_constraints=_constraint_set(fact_id),
        target_role_context=TargetRoleContext(target_job_title="Synthetic Senior Analyst"),
    )
    values.update(overrides)
    return build_controlled_semantic_validation_request(**values)


def test_request_is_bounded_to_one_fact_one_proposal() -> None:
    request = _build_request()
    dumped = request.model_dump()
    assert dumped["source_fact_id"] is not None
    assert "other_fact_ids" not in dumped
    assert "proposals" not in dumped
    assert "plan" not in dumped
    assert "draft" not in dumped


def test_request_forbids_batch_and_out_of_scope_fields() -> None:
    request = _build_request()
    for field_name in type(request).model_fields:
        assert field_name not in (
            "master_resume",
            "truth_library",
            "source_reference",
            "legacy_record_key",
            "corrected_proposal",
            "job_ad_text",
        )


def test_request_carries_no_source_reference_or_legacy_record_key_value() -> None:
    request = _build_request()
    text = request.model_dump_json()
    assert "source_reference" not in text
    assert "legacy_record_key" not in text


def test_forbidden_semantic_changes_is_closed_and_versioned() -> None:
    request = _build_request()
    assert request.forbidden_semantic_changes == FORBIDDEN_SEMANTIC_CHANGES
    assert len(FORBIDDEN_SEMANTIC_CHANGES) > 0
    assert isinstance(FORBIDDEN_SEMANTIC_CHANGES, tuple)


def test_prompt_fingerprint_is_deterministic_for_identical_requests() -> None:
    fact_id = uuid4()
    request_a = _build_request(source_fact_id=fact_id)
    request_b = _build_request(source_fact_id=fact_id)
    assert compute_semantic_validation_prompt_fingerprint(
        request_a
    ) == compute_semantic_validation_prompt_fingerprint(request_b)


def test_prompt_fingerprint_changes_when_proposal_text_changes() -> None:
    fact_id = uuid4()
    request_a = _build_request(source_fact_id=fact_id)
    request_b = _build_request(source_fact_id=fact_id, proposal_text="A materially different proposal text.")
    assert compute_semantic_validation_prompt_fingerprint(
        request_a
    ) != compute_semantic_validation_prompt_fingerprint(request_b)


def test_prompt_fingerprint_changes_when_source_text_changes() -> None:
    fact_id = uuid4()
    request_a = _build_request(source_fact_id=fact_id)
    request_b = _build_request(source_fact_id=fact_id, source_text="A different source text entirely.")
    assert compute_semantic_validation_prompt_fingerprint(
        request_a
    ) != compute_semantic_validation_prompt_fingerprint(request_b)
