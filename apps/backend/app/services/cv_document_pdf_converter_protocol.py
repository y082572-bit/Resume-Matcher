"""Explicit Provenance Stage P6-B3a: the real-converter Protocol.

``DocxToPdfConverter`` is a pure contract for a real, addressable DOCX->PDF
converter implementation (e.g. LibreOffice/soffice). It never accepts a
filesystem path, a filename, an executable, an argv, an env, an output
path, or an expected PDF hash from a caller -- only exact bytes and their
fingerprints. It is deliberately distinct from the existing
``CvPdfConversionAdapter`` in ``cv_document_adapters.py`` (P6-A), which
this stage never modifies.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.schemas.cv_document_pdf_conversion import DocxToPdfConversionResult
from app.schemas.cv_document_pdf_conversion_runtime import ConverterRuntimeIdentity


@runtime_checkable
class DocxToPdfConverter(Protocol):
    """Converts exact DOCX bytes to PDF via a real, addressable converter
    runtime. ``runtime_identity`` is ``None`` only when no verified runtime
    could be established (e.g. an unsupported platform) -- callers must
    treat that as ``CONVERTER_UNAVAILABLE`` rather than attempting a
    conversion."""

    @property
    def runtime_identity(self) -> ConverterRuntimeIdentity | None: ...

    def convert_docx_to_pdf(
        self,
        *,
        docx_bytes: bytes,
        source_snapshot_fingerprint: str,
        source_docx_sha256: str,
        conversion_policy_version: str,
    ) -> DocxToPdfConversionResult: ...
