"""Unit tests for Explicit Provenance Stage P6-C1b-A:
``build_approved_content_package``.

Reuses the shared, genuine ``ROLE_STRATEGY_INTEGRATED`` P5.5 pipeline
fixture (``build_ready_integrated_context``/``build_document_input``) that
``tests.unit.test_cv_document_input_validation`` already builds and
documents as the shared cross-module factory -- never a faked/hand-built
``ApprovedCvContentResult``/``CvContentPlan``.
"""

from __future__ import annotations

from unittest.mock import patch
from uuid import uuid4

import pytest

from app.schemas.cv_approved_content_package import ApprovedContentPackageBuildStatus
from app.schemas.cv_document_artifact import ArtifactOwnerKind
from app.services.cv_approved_content_package_builder import build_approved_content_package
from app.services.cv_document_owner_identity import build_owner_key
from tests.unit.test_cv_document_input_validation import build_document_input, build_ready_integrated_context


def _owner_key_for(ctx: dict, document_input, **overrides):
    plan = ctx["plan"]
    values = dict(
        person_entity_id=plan.person_entity_id,
        owner_kind=document_input.owner_kind,
        owner_reference_id=plan.job_or_application_id,
    )
    values.update(overrides)
    return build_owner_key(**values)


@pytest.mark.asyncio
async def test_valid_build_produces_built_package() -> None:
    ctx = await build_ready_integrated_context()
    document_input = build_document_input(ctx)
    owner_key = _owner_key_for(ctx, document_input)

    result = build_approved_content_package(owner_key=owner_key, document_input=document_input)

    assert result.status == ApprovedContentPackageBuildStatus.BUILT
    assert result.package is not None
    assert result.package.owner_key_fingerprint == owner_key.owner_key_fingerprint
    assert result.package.owner_kind == document_input.owner_kind
    assert result.package.payload_byte_size > 0
    assert result.package.payload.approval_decisions == document_input.approval_decisions


@pytest.mark.asyncio
async def test_document_input_validation_failure_is_reported() -> None:
    ctx = await build_ready_integrated_context()
    document_input = build_document_input(ctx)
    tampered = document_input.model_copy(update={"document_input_fingerprint": "a" * 64})
    owner_key = _owner_key_for(ctx, document_input)

    result = build_approved_content_package(owner_key=owner_key, document_input=tampered)

    assert result.status == ApprovedContentPackageBuildStatus.DOCUMENT_INPUT_INVALID
    assert result.package is None
    assert result.input_violations


@pytest.mark.asyncio
async def test_owner_exact_match_is_required() -> None:
    ctx = await build_ready_integrated_context()
    document_input = build_document_input(ctx)
    correct_owner_key = _owner_key_for(ctx, document_input)
    wrong_owner_key = _owner_key_for(ctx, document_input, owner_reference_id="a-completely-different-job-id")

    assert wrong_owner_key != correct_owner_key
    result = build_approved_content_package(owner_key=wrong_owner_key, document_input=document_input)
    assert result.status == ApprovedContentPackageBuildStatus.OWNER_MISMATCH
    assert result.package is None


@pytest.mark.asyncio
async def test_owner_kind_mismatch_is_rejected() -> None:
    ctx = await build_ready_integrated_context()
    document_input = build_document_input(ctx, owner_kind=ArtifactOwnerKind.JOB)
    wrong_kind_owner_key = _owner_key_for(ctx, document_input, owner_kind=ArtifactOwnerKind.APPLICATION)

    result = build_approved_content_package(owner_key=wrong_kind_owner_key, document_input=document_input)
    assert result.status == ApprovedContentPackageBuildStatus.OWNER_MISMATCH


@pytest.mark.asyncio
async def test_person_entity_mismatch_is_rejected() -> None:
    ctx = await build_ready_integrated_context()
    document_input = build_document_input(ctx)
    wrong_person_owner_key = _owner_key_for(ctx, document_input, person_entity_id=uuid4())

    result = build_approved_content_package(owner_key=wrong_person_owner_key, document_input=document_input)
    assert result.status == ApprovedContentPackageBuildStatus.OWNER_MISMATCH


@pytest.mark.asyncio
async def test_job_or_application_id_mismatch_is_rejected() -> None:
    ctx = await build_ready_integrated_context()
    document_input = build_document_input(ctx)
    wrong_reference_owner_key = _owner_key_for(ctx, document_input, owner_reference_id="some-other-reference-id")

    result = build_approved_content_package(owner_key=wrong_reference_owner_key, document_input=document_input)
    assert result.status == ApprovedContentPackageBuildStatus.OWNER_MISMATCH


@pytest.mark.asyncio
async def test_package_fingerprint_is_deterministic() -> None:
    ctx = await build_ready_integrated_context()
    document_input = build_document_input(ctx)
    owner_key = _owner_key_for(ctx, document_input)

    first = build_approved_content_package(owner_key=owner_key, document_input=document_input)
    second = build_approved_content_package(owner_key=owner_key, document_input=document_input)

    assert first.status == ApprovedContentPackageBuildStatus.BUILT
    assert second.status == ApprovedContentPackageBuildStatus.BUILT
    assert first.package.package_fingerprint == second.package.package_fingerprint
    # ``created_at`` is a wall-clock timestamp and deliberately excluded from
    # package_fingerprint -- every other field must still match exactly.
    assert first.package.model_dump(exclude={"created_at"}) == second.package.model_dump(exclude={"created_at"})


@pytest.mark.asyncio
async def test_policy_version_change_yields_a_different_package() -> None:
    ctx = await build_ready_integrated_context()
    document_input = build_document_input(ctx)
    owner_key = _owner_key_for(ctx, document_input)

    default_build = build_approved_content_package(owner_key=owner_key, document_input=document_input)
    changed_policy_build = build_approved_content_package(
        owner_key=owner_key, document_input=document_input, package_policy_version="cv-approved-content-package-policy-v2"
    )

    assert default_build.status == ApprovedContentPackageBuildStatus.BUILT
    assert changed_policy_build.status == ApprovedContentPackageBuildStatus.BUILT
    assert default_build.package.package_fingerprint != changed_policy_build.package.package_fingerprint


@pytest.mark.asyncio
async def test_schema_version_change_yields_a_different_package() -> None:
    ctx = await build_ready_integrated_context()
    document_input = build_document_input(ctx)
    owner_key = _owner_key_for(ctx, document_input)

    default_build = build_approved_content_package(owner_key=owner_key, document_input=document_input)
    changed_schema_build = build_approved_content_package(
        owner_key=owner_key, document_input=document_input, package_schema_version="cv-approved-content-package-schema-v2"
    )

    assert default_build.status == ApprovedContentPackageBuildStatus.BUILT
    assert changed_schema_build.status == ApprovedContentPackageBuildStatus.BUILT
    assert default_build.package.package_fingerprint != changed_schema_build.package.package_fingerprint


@pytest.mark.asyncio
async def test_payload_too_large_is_rejected() -> None:
    ctx = await build_ready_integrated_context()
    document_input = build_document_input(ctx)
    owner_key = _owner_key_for(ctx, document_input)

    with patch("app.services.cv_approved_content_package_builder.PAYLOAD_LIMIT_BYTES", 16):
        result = build_approved_content_package(owner_key=owner_key, document_input=document_input)

    assert result.status == ApprovedContentPackageBuildStatus.PAYLOAD_TOO_LARGE
    assert result.package is None


@pytest.mark.asyncio
async def test_package_payload_is_a_defensive_copy_not_a_shared_reference() -> None:
    ctx = await build_ready_integrated_context()
    document_input = build_document_input(ctx)
    owner_key = _owner_key_for(ctx, document_input)

    result = build_approved_content_package(owner_key=owner_key, document_input=document_input)
    assert result.status == ApprovedContentPackageBuildStatus.BUILT
    package = result.package

    assert package.payload.approved_content_result is not document_input.approved_content_result
    assert package.payload.source_content_plan is not document_input.source_content_plan
    assert package.payload.current_p55_replay_input is not document_input.current_p55_replay_input

    # Mutating the caller's own (non-frozen) nested object must never be
    # observable through the already-built package.
    document_input.approved_content_result.result_fingerprint = "9" * 64
    assert package.payload.approved_content_result.result_fingerprint != "9" * 64
