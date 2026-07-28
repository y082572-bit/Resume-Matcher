"""Explicit Provenance Stage P6-C1a: the single shared binary response
helper for the CV Document Workflow API.

Never uses ``FileResponse`` and never accepts or derives a filesystem path
-- every response is built from exact, already-in-memory bytes via
``fastapi.Response(content=...)``. Every filename is one of exactly four
fixed, server-side constants; a caller-supplied ``job_id`` never appears in
a filename, making CR/LF header injection through a filename structurally
impossible.
"""

from __future__ import annotations

import hashlib
from enum import StrEnum

from fastapi import Response

_ETAG_PREFIX = b"cv-document-http-etag-v1\0"

_DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_PDF_MEDIA_TYPE = "application/pdf"


class BinaryDocumentKind(StrEnum):
    """The exactly four binary documents this router ever serves."""

    PROPOSAL_DOCX = "PROPOSAL_DOCX"
    WORKING_COPY_DOCX = "WORKING_COPY_DOCX"
    FINAL_DOCX = "FINAL_DOCX"
    FINAL_PDF = "FINAL_PDF"


_MEDIA_TYPE_BY_KIND: dict[BinaryDocumentKind, str] = {
    BinaryDocumentKind.PROPOSAL_DOCX: _DOCX_MEDIA_TYPE,
    BinaryDocumentKind.WORKING_COPY_DOCX: _DOCX_MEDIA_TYPE,
    BinaryDocumentKind.FINAL_DOCX: _DOCX_MEDIA_TYPE,
    BinaryDocumentKind.FINAL_PDF: _PDF_MEDIA_TYPE,
}

#: Fixed, server-side-only filenames -- never derived from ``job_id`` or any
#: other caller input.
_FILENAME_BY_KIND: dict[BinaryDocumentKind, str] = {
    BinaryDocumentKind.PROPOSAL_DOCX: "cv-proposal.docx",
    BinaryDocumentKind.WORKING_COPY_DOCX: "cv-working-copy.docx",
    BinaryDocumentKind.FINAL_DOCX: "cv-final.docx",
    BinaryDocumentKind.FINAL_PDF: "cv-final.pdf",
}


def compute_document_http_etag(content: bytes) -> str:
    """``sha256(b"cv-document-http-etag-v1\\0" + content)`` -- never an
    internal artifact/snapshot fingerprint or a blob SHA."""

    digest = hashlib.sha256(_ETAG_PREFIX + content).hexdigest()
    return f'"{digest}"'


def _if_none_match_hits(if_none_match: str | None, etag: str) -> bool:
    """Weak-compares each comma-separated ``If-None-Match`` validator
    against ``etag``, per GET's weak-comparison semantics -- ``W/"abc"``
    matches ``"abc"``. Never raises: any malformed token simply fails to
    match and falls through to a normal 200 with the full document."""

    if if_none_match is None:
        return False
    stripped = if_none_match.strip()
    if not stripped:
        return False
    for raw_token in stripped.split(","):
        token = raw_token.strip()
        if not token:
            continue
        if token == "*":
            return True
        if token.startswith("W/"):
            token = token[2:]
        if token == etag:
            return True
    return False


def build_binary_document_response(
    *,
    kind: BinaryDocumentKind,
    content: bytes,
    if_none_match: str | None = None,
    include_etag: bool,
) -> Response:
    """The one shared binary response builder for every DOCX/PDF download.

    ``include_etag=False`` is used only for the mutable working copy, which
    carries no ETag and never honors ``If-None-Match``.
    """

    filename = _FILENAME_BY_KIND[kind]
    media_type = _MEDIA_TYPE_BY_KIND[kind]

    if include_etag:
        etag = compute_document_http_etag(content)
        if _if_none_match_hits(if_none_match, etag):
            return Response(
                status_code=304,
                headers={
                    "ETag": etag,
                    "Cache-Control": "no-store",
                },
            )

    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    }
    if include_etag:
        headers["ETag"] = etag

    return Response(content=content, media_type=media_type, headers=headers)
