"""Explicit Provenance Stage P6-C1a: the closed public error contract for
the CV Document Workflow API.

Every mapping here is a static, fixed ``(http_status, code, message,
retryable)`` tuple -- never ``str(exception)``, a stack trace, SQL text, a
path, an executable, a workspace, a fingerprint, a SHA, ``person_entity_id``,
``master_resume_id``, an owner key, or any other domain diagnostic. An
*unexpected* ``Exception`` is only ever caught at the HTTP boundary (each
router endpoint's own outermost ``except Exception`` -- never
``BaseException``/``KeyboardInterrupt``/``SystemExit``); it is logged
server-side with its traceback and reported to the client as a single
generic 500.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi.responses import JSONResponse

from app.schemas.cv_document_workflow_api import ApiErrorResponse

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ErrorSpec:
    """One closed, static public error mapping."""

    http_status: int
    code: str
    message: str
    retryable: bool


#: Job missing and missing identity binding are always this exact, unified
#: public error -- the router never reveals which of the two occurred.
DOCUMENT_WORKFLOW_NOT_FOUND = ErrorSpec(
    http_status=404,
    code="DOCUMENT_WORKFLOW_NOT_FOUND",
    message="Document workflow was not found.",
    retryable=False,
)

DOCUMENT_WORKFLOW_UNAVAILABLE = ErrorSpec(
    http_status=503,
    code="DOCUMENT_WORKFLOW_UNAVAILABLE",
    message="Document workflow storage is temporarily unavailable. Please try again.",
    retryable=True,
)

DOCUMENT_WORKFLOW_CONFLICT = ErrorSpec(
    http_status=409,
    code="DOCUMENT_WORKFLOW_CONFLICT",
    message="The document workflow state has changed. Please refresh and try again.",
    retryable=False,
)

DOCUMENT_WORKFLOW_LOCKED = ErrorSpec(
    http_status=423,
    code="DOCUMENT_WORKFLOW_LOCKED",
    message="The document is temporarily locked. Please try again shortly.",
    retryable=True,
)

DOCUMENT_WORKFLOW_CONVERTER_UNAVAILABLE = ErrorSpec(
    http_status=503,
    code="DOCUMENT_WORKFLOW_CONVERTER_UNAVAILABLE",
    message="The PDF converter is temporarily unavailable. Please try again.",
    retryable=True,
)

INTERNAL_ERROR = ErrorSpec(
    http_status=500,
    code="INTERNAL_ERROR",
    message="An unexpected error occurred.",
    retryable=False,
)


def build_error_response(spec: ErrorSpec) -> JSONResponse:
    """Build the exact, closed public JSON body for ``spec``. ``details``
    is always omitted -- this router never populates it."""

    body = ApiErrorResponse(
        code=spec.code,
        message=spec.message,
        retryable=spec.retryable,
    )
    return JSONResponse(status_code=spec.http_status, content=body.model_dump(mode="json"))


def internal_error_response(exc: Exception) -> JSONResponse:
    """The sole handler for an unexpected ``Exception`` at the HTTP
    boundary: full traceback logged server-side, a single generic 500
    returned to the client."""

    logger.error("Unexpected error in CV document workflow endpoint", exc_info=exc)
    return build_error_response(INTERNAL_ERROR)
