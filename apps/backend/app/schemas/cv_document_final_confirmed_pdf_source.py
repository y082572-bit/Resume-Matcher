"""Explicit Provenance Stage P6-B3b-A: the closed contract for reading a
Final DOCX Snapshot's exact bytes as the source input to a PDF conversion.

``FinalDocxSnapshotPdfSourceReader`` (``cv_document_final_docx_snapshot_pdf_source_reader.py``)
is the only producer of ``FinalDocxSnapshotPdfSourceReadResult``. It never
returns a raw ``FinalDocxSnapshot``/``bytes`` pair directly and never raises
a repository/storage exception to its own caller -- every technical or
lineage failure is instead represented as one of the closed statuses below.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, model_validator

from app.schemas.cv_document_final_snapshot import FinalDocxSnapshot


class FinalDocxSnapshotPdfSourceReadStatus(StrEnum):
    VERIFIED = "VERIFIED"
    OWNER_KEY_FINGERPRINT_MISMATCH = "OWNER_KEY_FINGERPRINT_MISMATCH"
    FINAL_SNAPSHOT_NOT_FOUND = "FINAL_SNAPSHOT_NOT_FOUND"
    FINAL_SNAPSHOT_OWNER_MISMATCH = "FINAL_SNAPSHOT_OWNER_MISMATCH"
    FINAL_DOCX_BYTES_NOT_FOUND = "FINAL_DOCX_BYTES_NOT_FOUND"
    FINAL_DOCX_HASH_MISMATCH = "FINAL_DOCX_HASH_MISMATCH"
    STORAGE_UNAVAILABLE = "STORAGE_UNAVAILABLE"


class FinalDocxSnapshotPdfSourceReadResult(BaseModel):
    """The single return value of
    ``FinalDocxSnapshotPdfSourceReader.read_source``. Only ``VERIFIED`` ever
    carries a ``snapshot``/``final_docx_bytes`` pair -- every other status
    forbids both."""

    model_config = ConfigDict(extra="forbid")

    status: FinalDocxSnapshotPdfSourceReadStatus
    snapshot: FinalDocxSnapshot | None = None
    final_docx_bytes: bytes | None = None

    @model_validator(mode="after")
    def _status_consistency(self) -> "FinalDocxSnapshotPdfSourceReadResult":
        if self.status == FinalDocxSnapshotPdfSourceReadStatus.VERIFIED:
            if self.snapshot is None or self.final_docx_bytes is None:
                raise ValueError("VERIFIED requires snapshot and final_docx_bytes")
            if not self.final_docx_bytes:
                raise ValueError("VERIFIED requires non-empty final_docx_bytes")
        else:
            if self.snapshot is not None or self.final_docx_bytes is not None:
                raise ValueError(f"{self.status} must carry no snapshot/final_docx_bytes")
        return self
