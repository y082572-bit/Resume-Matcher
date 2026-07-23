"""Explicit Provenance Stage P6-A: adapter Protocols for the document
artifact lifecycle.

Every Protocol here is a pure contract -- P6-A implements no real DOCX
renderer, no real template provider, no real structural validator, no real
PDF converter, and no real local-open adapter. Tests exercise these
contracts only through deterministic in-memory fakes. Real filesystem,
Word-automation, LibreOffice, and OS-open adapters are explicitly deferred
to P6-B.

No Protocol here ever accepts a filesystem path, a filename as identity, a
shell command string, or a target to write in-place -- every adapter is
bytes-in, bytes/result-out.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.cv_content_approval import ApprovedCvContentResult
from app.schemas.cv_content_plan import CvContentPlan
from app.schemas.cv_document_artifact import (
    CvDocxRenderingResult,
    CvPdfConversionAttemptResult,
    DocxStructuralValidationResult,
    LocalDocumentOpenRequest,
    LocalDocumentOpenResult,
)
from app.schemas.truth_entity import SHA256_PATTERN


class DocxTemplateHandle(BaseModel):
    """An immutable template handle -- never a domain identity based on a
    filesystem path. ``template_bytes`` is plain ``bytes`` (never
    ``bytearray``) precisely because immutability is what lets a caller
    trust that a renderer cannot mutate it in place."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    template_bytes: bytes
    template_fingerprint: str = Field(pattern=SHA256_PATTERN)
    template_adapter_id: str = Field(min_length=1, max_length=255)


@runtime_checkable
class DocxTemplateProvider(Protocol):
    """Returns an immutable DOCX template. P6-A implements no filesystem
    provider -- tests use a deterministic in-memory fake."""

    def get_template(self) -> DocxTemplateHandle: ...


@runtime_checkable
class CvDocxRenderingAdapter(Protocol):
    """Places already-approved content into a DOCX template.

    Never generates new content, never paraphrases, never calls an LLM --
    it may only place ``approved_content_result`` content into
    ``template_bytes``. Never receives a target path (Master Resume or
    otherwise) to write in-place; it always returns fresh bytes.
    """

    @property
    def renderer_adapter_id(self) -> str: ...

    def render(
        self,
        *,
        approved_content_result: ApprovedCvContentResult,
        content_plan: CvContentPlan,
        template_bytes: bytes,
        template_fingerprint: str,
        rendering_policy_version: str,
    ) -> CvDocxRenderingResult: ...


@runtime_checkable
class DocxStructuralValidator(Protocol):
    """Validates exact DOCX bytes structurally. P6-A implements no real
    python-docx/OOXML validator -- tests use a deterministic fake."""

    def validate(
        self,
        *,
        exact_bytes: bytes,
        expected_sha256: str,
        validation_policy_version: str,
    ) -> DocxStructuralValidationResult: ...


@runtime_checkable
class CvPdfConversionAdapter(Protocol):
    """Converts exact, immutable snapshot bytes to PDF.

    Never accepts a live proposal path, a filename as identity, or a shell
    command string -- only exact bytes and their fingerprint.
    """

    @property
    def adapter_id(self) -> str: ...

    def convert(
        self,
        *,
        snapshot_bytes: bytes,
        snapshot_fingerprint: str,
        conversion_policy_version: str,
    ) -> CvPdfConversionAttemptResult: ...


@runtime_checkable
class LocalDocumentOpenAdapter(Protocol):
    """Opens a document artifact locally.

    P6-A defines this Protocol and its result shape only -- it must never
    be implemented to call ``open``, ``subprocess``, AppleScript,
    ``os.startfile``, or ``xdg-open`` in this stage. A real implementation
    is deferred to P6-B. Neither a repository locator nor a suggested
    filename ever participates in artifact identity.
    """

    def open_document(self, *, request: LocalDocumentOpenRequest) -> LocalDocumentOpenResult: ...
