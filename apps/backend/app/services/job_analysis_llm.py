"""The controlled job-posting-analysis LLM adapter contract.

``ControlledJobPostingAnalysisAdapter`` is a pure ``Protocol`` -- there is
no production implementation here. A production adapter (LiteLLM-backed or
otherwise) lives entirely outside PRE-P4 core and is injected by the
caller. This module never imports ``app.llm``, ``litellm``, the database,
SQLAlchemy, a router, Career Positioning, a P4 builder, or DOCX/PDF code.

Retry is owned entirely by the job-analysis builder, never by the adapter:
an adapter implementation must not change the model, temperature, top_p,
timeout, or prompt between calls, and must not retry on its own -- it
returns exactly one ``JobAnalysisAdapterResponse`` per ``analyze()`` call,
using ``provider_error_code`` to signal a failed attempt instead of
raising.
"""

from __future__ import annotations

from typing import Protocol

from app.schemas.job_posting_analysis import (
    ControlledJobAnalysisRequest,
    JobAnalysisAdapterResponse,
    JobAnalysisModelConfiguration,
)


class ControlledJobPostingAnalysisAdapter(Protocol):
    """The sole boundary between PRE-P4 Step 1's core and any real LLM
    provider."""

    async def analyze(
        self,
        *,
        request: ControlledJobAnalysisRequest,
        model_configuration: JobAnalysisModelConfiguration,
    ) -> JobAnalysisAdapterResponse: ...
