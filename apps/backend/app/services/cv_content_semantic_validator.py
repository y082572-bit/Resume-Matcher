"""The controlled-proposal semantic-validator adapter contract.

``ControlledProposalSemanticValidator`` is a pure ``Protocol`` -- there is
no production implementation here. A production adapter (LiteLLM-backed or
otherwise) lives entirely outside P5.5's core and is injected by the
caller. This module never imports ``app.llm``, ``litellm``, ``LLMConfig``,
``get_llm_config``, the database, SQLAlchemy, or Stage 10C.

Retry is owned entirely by Step 1's builder
(``cv_content_semantic_validation_builder``), never by the adapter: an
adapter implementation must not change the model, temperature, top_p,
timeout, or prompt between calls, and must not retry on its own -- it
returns exactly one ``SemanticValidatorAdapterResponse`` per ``validate()``
call, using ``provider_error_code`` to signal a failed attempt instead of
raising.
"""

from __future__ import annotations

from typing import Protocol

from app.schemas.cv_content_approval import (
    ControlledSemanticValidationRequest,
    SemanticValidatorAdapterResponse,
    SemanticValidatorModelConfiguration,
    SemanticValidatorProviderErrorCode,
)


SEMANTIC_VALIDATOR_RETRY_POLICY_VERSION = "semantic-validator-retry-policy-v1"

#: Maximum total attempts Step 1's builder will ever make for one proposal,
#: regardless of a caller-supplied
#: ``SemanticValidatorModelConfiguration.maximum_attempts`` value -- one
#: base attempt plus at most one retry.
MAXIMUM_TOTAL_ATTEMPTS = 2

#: Retry is allowed only for these two closed error codes -- never for a
#: content/shape/identity failure or a semantic FAIL/INCONCLUSIVE verdict.
RETRYABLE_PROVIDER_ERROR_CODES: frozenset[SemanticValidatorProviderErrorCode] = frozenset(
    {
        SemanticValidatorProviderErrorCode.TIMEOUT,
        SemanticValidatorProviderErrorCode.TRANSPORT_ERROR,
    }
)


class ControlledProposalSemanticValidator(Protocol):
    """The sole boundary between P5.5's core and any real semantic-check
    provider. Never knows about a user's approval decision."""

    async def validate(
        self,
        *,
        request: ControlledSemanticValidationRequest,
        model_configuration: SemanticValidatorModelConfiguration,
    ) -> SemanticValidatorAdapterResponse: ...
