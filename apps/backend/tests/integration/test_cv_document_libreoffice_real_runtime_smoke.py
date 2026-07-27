"""Explicit Provenance Stage P6-B3a: the gated real-runtime smoke test.

Marked ``real_runtime`` -- excluded from the default backend suite (see
``[tool.pytest.ini_options] addopts`` in ``pyproject.toml``). Run
explicitly with::

    uv run pytest \\
      apps/backend/tests/integration/test_cv_document_libreoffice_real_runtime_smoke.py \\
      -m real_runtime -q

This test spawns a *real* ``soffice --headless --convert-to pdf``
subprocess against a real minimal DOCX built in a test tmp dir. On a
machine with no configured LibreOffice install, it skips with a clear
reason -- it must never report a real-runtime PASS without an actual
LibreOffice binary present.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import docx
import pytest

from app.schemas.cv_document_pdf_conversion import DocxToPdfConversionStatus
from app.services.cv_document_libreoffice_adapter import LibreOfficeDocxToPdfConverter
from app.services.cv_document_process_runner import PosixProcessRunner


pytestmark = pytest.mark.real_runtime


#: Fixed, configured absolute candidate paths -- never a PATH lookup as
#: authority. The first one that exists on this machine is used.
_CANDIDATE_SOFFICE_PATHS = (
    Path("/Applications/LibreOffice.app/Contents/MacOS/soffice"),
    Path("/usr/bin/soffice"),
    Path("/usr/lib/libreoffice/program/soffice"),
    Path("/opt/libreoffice/program/soffice"),
)


def _find_configured_soffice() -> Path | None:
    for candidate in _CANDIDATE_SOFFICE_PATHS:
        if candidate.is_file():
            return candidate
    return None


def _build_minimal_docx_bytes() -> bytes:
    document = docx.Document()
    document.add_paragraph("Explicit Provenance P6-B3a real-runtime smoke test.")
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def test_real_libreoffice_conversion_produces_a_pdf(tmp_path: Path) -> None:
    if sys.platform == "win32":
        pytest.skip("real-runtime LibreOffice conversion is out of scope on win32 in this stage")

    soffice_path = _find_configured_soffice()
    if soffice_path is None:
        pytest.skip("no configured LibreOffice/soffice install found on this machine")

    import hashlib

    docx_bytes = _build_minimal_docx_bytes()
    source_docx_sha256 = hashlib.sha256(docx_bytes).hexdigest()

    process_runner = PosixProcessRunner(
        max_stdout_bytes=1024 * 1024,
        max_stderr_bytes=1024 * 1024,
        read_chunk_size=8192,
        terminate_grace_seconds=2.0,
        final_wait_seconds=2.0,
        reader_join_timeout_seconds=5.0,
    )
    converter = LibreOfficeDocxToPdfConverter(
        soffice_path=soffice_path,
        scratch_root=tmp_path / "scratch",
        repo_root=tmp_path / "repo",
        working_copy_root=tmp_path / "working-copy",
        blob_root=tmp_path / "blobs",
        font_environment_id="real-runtime-smoke-fonts-v1",
        max_docx_size_bytes=10 * 1024 * 1024,
        max_pdf_size_bytes=20 * 1024 * 1024,
        timeout_seconds=60.0,
        process_runner=process_runner,
    )

    assert converter.runtime_identity is not None, "configured soffice was found but runtime identity init failed"

    workspace_root_before: Path | None = None
    original_setup = converter._setup_workspace

    def _capturing_setup():  # type: ignore[no-untyped-def]
        nonlocal workspace_root_before
        workspace = original_setup()
        if workspace is not None:
            workspace_root_before = workspace.root
        return workspace

    converter._setup_workspace = _capturing_setup  # type: ignore[method-assign]

    result = converter.convert_docx_to_pdf(
        docx_bytes=docx_bytes,
        source_snapshot_fingerprint="b" * 64,
        source_docx_sha256=source_docx_sha256,
        conversion_policy_version="cv-document-pdf-conversion-real-runtime-smoke-v1",
    )

    assert result.status == DocxToPdfConversionStatus.SUCCEEDED, (
        f"real LibreOffice conversion failed: status={result.status} "
        f"error_code={result.error_code} diagnostics={result.diagnostics}"
    )
    assert result.pdf_bytes is not None
    assert result.pdf_bytes.startswith(b"%PDF-")

    assert workspace_root_before is not None
    assert not workspace_root_before.exists(), "workspace cleanup was not confirmed after real conversion"
