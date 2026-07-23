"""Unit tests for the ``ControlledProposalSemanticValidator`` Protocol and
its retry constants.

Confirms P5.5's core boundary: no production adapter here, no forbidden
imports, and the closed retry-eligibility table.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

from app.schemas.cv_content_approval import SemanticValidatorProviderErrorCode
from app.services import cv_content_semantic_validator as validator_module
from app.services.cv_content_semantic_validator import (
    MAXIMUM_TOTAL_ATTEMPTS,
    RETRYABLE_PROVIDER_ERROR_CODES,
    SEMANTIC_VALIDATOR_RETRY_POLICY_VERSION,
    ControlledProposalSemanticValidator,
)


def test_protocol_declares_a_single_async_validate_method() -> None:
    assert inspect.isclass(ControlledProposalSemanticValidator)
    assert hasattr(ControlledProposalSemanticValidator, "validate")
    members = [name for name in vars(ControlledProposalSemanticValidator) if not name.startswith("_")]
    assert members == ["validate"]


def test_maximum_total_attempts_is_two() -> None:
    assert MAXIMUM_TOTAL_ATTEMPTS == 2


def test_retryable_provider_error_codes_is_closed_to_timeout_and_transport() -> None:
    assert RETRYABLE_PROVIDER_ERROR_CODES == {
        SemanticValidatorProviderErrorCode.TIMEOUT,
        SemanticValidatorProviderErrorCode.TRANSPORT_ERROR,
    }


def test_non_retryable_error_codes_are_excluded() -> None:
    non_retryable = set(SemanticValidatorProviderErrorCode) - RETRYABLE_PROVIDER_ERROR_CODES
    expected = {
        SemanticValidatorProviderErrorCode.PROVIDER_REJECTED,
        SemanticValidatorProviderErrorCode.INVALID_RESPONSE_JSON,
        SemanticValidatorProviderErrorCode.RESPONSE_SCHEMA_MISMATCH,
        SemanticValidatorProviderErrorCode.EMPTY_RESPONSE,
        SemanticValidatorProviderErrorCode.TOKEN_LIMIT,
        SemanticValidatorProviderErrorCode.CONTENT_FILTERED,
        SemanticValidatorProviderErrorCode.UNSUPPORTED_FINISH_REASON,
        SemanticValidatorProviderErrorCode.RETRY_EXHAUSTED,
    }
    assert non_retryable == expected


def test_retry_policy_version_is_a_non_empty_string() -> None:
    assert isinstance(SEMANTIC_VALIDATOR_RETRY_POLICY_VERSION, str)
    assert SEMANTIC_VALIDATOR_RETRY_POLICY_VERSION


def test_module_never_imports_llm_or_db_or_stage_10c() -> None:
    raw_source = Path(validator_module.__file__).read_text(encoding="utf-8")
    source = re.sub(r'""".*?"""', "", raw_source, flags=re.DOTALL)
    forbidden = (
        "import litellm",
        "from app.llm",
        "LLMConfig",
        "get_llm_config",
        "from app.database",
        "from app import database",
        "sqlalchemy",
        "app.models",
        "truth_legacy_migrator",
        "cv_transformation_generation",
        "cv_transformation_plan",
        "cv_transformation_approval",
    )
    for needle in forbidden:
        assert needle not in source, f"forbidden reference found: {needle}"


async def test_a_fake_validator_satisfies_the_protocol_structurally() -> None:
    from app.schemas.cv_content_approval import (
        ControlledSemanticValidationRequest,
        ControlledSemanticValidationResponse,
        ProposalSemanticVerdict,
        SemanticValidatorAdapterResponse,
        SemanticValidatorModelConfiguration,
    )
    from app.schemas.cv_content_plan import CvSection
    from app.schemas.cv_content_proposal import ImmutableConstraintSet, TargetRoleContext
    from app.schemas.truth_fact import TransformationOperation
    from uuid import uuid4

    class _Fake:
        async def validate(self, *, request, model_configuration):
            return SemanticValidatorAdapterResponse(
                provider=model_configuration.provider,
                model_identifier=model_configuration.model_identifier,
                structured_response=ControlledSemanticValidationResponse(
                    verdict=ProposalSemanticVerdict.PASS,
                    source_fact_id=request.source_fact_id,
                    proposal_fingerprint=request.proposal_fingerprint,
                    operation=request.operation,
                    source_claim_summary="s",
                    proposal_claim_summary="p",
                ),
                raw_response_text="raw",
            )

    fact_id = uuid4()
    request = ControlledSemanticValidationRequest(
        prompt_template_version="controlled-semantic-validation-prompt-v1",
        operation=TransformationOperation.CONTROLLED_REPHRASE,
        fact_type="ACHIEVEMENT",
        target_section=CvSection.ACHIEVEMENTS,
        target_scope="PROJECT",
        employment_scope_entity_id=None,
        source_fact_id=fact_id,
        source_text="source",
        proposal_text="proposal",
        proposal_fingerprint="a" * 64,
        immutable_constraints=ImmutableConstraintSet(
            fact_id=fact_id, constraints=(), immutable_constraint_set_fingerprint="b" * 64
        ),
        target_role_context=TargetRoleContext(),
        response_schema_version="controlled-semantic-validation-response-v1",
    )
    model_configuration = SemanticValidatorModelConfiguration(
        provider="p",
        model_identifier="m",
        temperature=0.0,
        top_p=1.0,
        max_output_tokens=100,
        timeout_seconds=5.0,
        maximum_attempts=1,
        response_schema_version="controlled-semantic-validation-response-v1",
        retry_policy_version=SEMANTIC_VALIDATOR_RETRY_POLICY_VERSION,
    )
    fake: ControlledProposalSemanticValidator = _Fake()
    response = await fake.validate(request=request, model_configuration=model_configuration)
    assert response.structured_response.verdict == ProposalSemanticVerdict.PASS
