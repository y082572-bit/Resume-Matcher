"""The controlled candidacy-thesis LLM adapter contract.

``ControlledCandidacyThesisAdapter`` is a pure ``Protocol`` -- there is no
production implementation here. A production adapter (LiteLLM-backed or
otherwise) lives entirely outside PRE-P4 core and is injected by the
caller. This module never imports ``app.llm``, ``litellm``, the database,
Career Positioning, a P4 builder, a router, or DOCX/PDF code.

Retry is owned entirely by the candidacy-thesis builder, never by the
adapter: an adapter implementation must not change the model, temperature,
top_p, timeout, or prompt between calls, and must not retry on its own --
it returns exactly one ``CandidacyThesisAdapterResponse`` per
``synthesize_thesis()`` call, using ``provider_error_code`` to signal a
failed attempt instead of raising.
"""

from __future__ import annotations

from typing import Protocol

from app.schemas.candidacy_thesis import (
    CandidacyThesisAdapterResponse,
    CandidacyThesisModelConfiguration,
    ControlledCandidacyThesisRequest,
)


class ControlledCandidacyThesisAdapter(Protocol):
    """The sole boundary between PRE-P4 Step 2's core and any real LLM
    provider."""

    async def synthesize_thesis(
        self,
        *,
        request: ControlledCandidacyThesisRequest,
        model_configuration: CandidacyThesisModelConfiguration,
    ) -> CandidacyThesisAdapterResponse: ...
