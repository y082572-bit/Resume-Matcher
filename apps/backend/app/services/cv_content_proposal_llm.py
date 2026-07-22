"""The controlled-proposal LLM adapter contract.

``ControlledProposalLlmAdapter`` is a pure ``Protocol`` -- there is no
production implementation here. A production adapter (LiteLLM-backed or
otherwise) lives entirely outside P5b's core and is injected by the caller.
This module never imports ``app.llm``, ``litellm``, ``LLMConfig``,
``get_llm_config``, the database, or SQLAlchemy.

Retry is owned entirely by P5b's builder, never by the adapter: an adapter
implementation must not change the model, temperature, top_p, timeout, or
prompt between calls, and must not retry on its own -- it returns exactly
one ``LlmAdapterResponse`` per ``generate()`` call, using
``provider_error_code`` to signal a failed attempt instead of raising.
"""

from __future__ import annotations

from typing import Protocol

from app.schemas.cv_content_proposal import (
    ControlledProposalRequest,
    LlmAdapterResponse,
    LlmModelConfiguration,
)


MODEL_RETRY_POLICY_VERSION = "proposal-model-retry-policy-v1"

#: Maximum total attempts P5b's builder will ever make for one proposal,
#: regardless of a caller-supplied ``LlmModelConfiguration.maximum_attempts``
#: value -- one base attempt plus at most one retry.
MAXIMUM_TOTAL_ATTEMPTS = 2


class ControlledProposalLlmAdapter(Protocol):
    """The sole boundary between P5b's core and any real LLM provider."""

    async def generate(
        self,
        *,
        request: ControlledProposalRequest,
        model_configuration: LlmModelConfiguration,
    ) -> LlmAdapterResponse: ...
