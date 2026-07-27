"""Unit tests for ``LibreOfficeDocxToPdfConverter``
(``app.services.cv_document_libreoffice_adapter``).

A fake ``ProcessRunner`` drives every scenario -- this suite never spawns a
real ``soffice`` process (that is exclusively the job of the gated
``real_runtime`` integration test). Real filesystem operations (workspace
creation, secure input write, secure PDF read, cleanup) run for real
against ``tmp_path``.
"""

from __future__ import annotations

import hashlib
import os
import stat
import sys
from pathlib import Path

import pytest

from app.schemas.cv_document_pdf_conversion import DocxToPdfConversionStatus
from app.schemas.cv_document_pdf_conversion_runtime import ConverterRuntimeIdentity
from app.services import cv_document_libreoffice_adapter as adapter_module
from app.services.cv_document_adapters import CvPdfConversionAdapter
from app.services.cv_document_libreoffice_adapter import (
    LibreOfficeConfigurationError,
    LibreOfficeDocxToPdfConverter,
    WorkspaceCleanupStatus,
)
from app.services.cv_document_pdf_converter_protocol import DocxToPdfConverter
from app.services.cv_document_process_runner import ProcessRunResult, ProcessRunStatus


pytestmark = pytest.mark.unit


_VALID_DOCX_BYTES = b"fake-docx-bytes-for-testing" * 4
_VALID_SNAPSHOT_FINGERPRINT = "a" * 64
_VALID_POLICY_VERSION = "cv-document-pdf-conversion-policy-v1"
_VALID_PDF_BYTES = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n%%EOF"


def _docx_sha256(data: bytes = _VALID_DOCX_BYTES) -> str:
    return hashlib.sha256(data).hexdigest()


class _FakeProcessRunner:
    """A fully scripted ``ProcessRunner`` fake: the version probe result is
    fixed at construction, and each ``convert`` call consumes the next
    queued result (or the last one, if only one was queued)."""

    def __init__(
        self,
        *,
        version_result: ProcessRunResult,
        convert_results: list[ProcessRunResult] | None = None,
    ) -> None:
        self.version_result = version_result
        self._convert_results = list(convert_results or [])
        self.calls: list[tuple[tuple[str, ...], dict[str, str]]] = []

    def run(
        self,
        *,
        argv: tuple[str, ...],
        cwd: Path,
        env: dict[str, str],
        timeout_seconds: float,
    ) -> ProcessRunResult:
        self.calls.append((argv, dict(env)))
        if "--version" in argv:
            return self.version_result
        if self._convert_results:
            return self._convert_results.pop(0) if len(self._convert_results) > 1 else self._convert_results[0]
        raise AssertionError("no convert result queued")


def _completed(exit_code: int = 0, stdout: bytes = b"", stderr: bytes = b"") -> ProcessRunResult:
    return ProcessRunResult(
        status=ProcessRunStatus.COMPLETED,
        exit_code=exit_code,
        bounded_stdout=stdout,
        bounded_stderr=stderr,
        termination_stage=None,
        max_stdout_bytes=1024 * 1024,
        max_stderr_bytes=1024 * 1024,
    )


def _make_fake_soffice(tmp_path: Path, *, content: bytes = b"#!/bin/sh\necho fake soffice\n") -> Path:
    soffice = tmp_path / "bin" / "soffice"
    soffice.parent.mkdir(parents=True, exist_ok=True)
    soffice.write_bytes(content)
    soffice.chmod(0o755)
    return soffice


def _build_converter(
    tmp_path: Path,
    *,
    process_runner: _FakeProcessRunner,
    soffice_path: Path | None = None,
) -> LibreOfficeDocxToPdfConverter:
    return LibreOfficeDocxToPdfConverter(
        soffice_path=soffice_path if soffice_path is not None else _make_fake_soffice(tmp_path),
        scratch_root=tmp_path / "scratch",
        repo_root=tmp_path / "repo",
        working_copy_root=tmp_path / "working-copy",
        blob_root=tmp_path / "blobs",
        font_environment_id="fonts-v1",
        max_docx_size_bytes=10 * 1024 * 1024,
        max_pdf_size_bytes=10 * 1024 * 1024,
        timeout_seconds=5.0,
        process_runner=process_runner,
    )


def _write_output_pdf_after_convert(runner: _FakeProcessRunner, adapter: LibreOfficeDocxToPdfConverter) -> None:
    """Not used directly -- workspace paths are internal. See the
    dedicated success test, which patches ``_setup_workspace`` instead."""


# -- Protocol shape -----------------------------------------------------------------


def test_docx_to_pdf_converter_protocol_is_runtime_checkable(tmp_path: Path) -> None:
    runner = _FakeProcessRunner(version_result=_completed(stdout=b"LibreOffice 7.6"))
    converter = _build_converter(tmp_path, process_runner=runner)
    assert isinstance(converter, DocxToPdfConverter)


def test_cv_pdf_conversion_adapter_protocol_is_unchanged() -> None:
    """The existing P6-A ``CvPdfConversionAdapter`` Protocol must remain
    untouched by this stage: it must still require ``adapter_id`` and
    ``convert(snapshot_bytes=..., snapshot_fingerprint=...,
    conversion_policy_version=...)`` -- a distinct shape from the new
    ``DocxToPdfConverter``."""

    import inspect

    convert_params = set(inspect.signature(CvPdfConversionAdapter.convert).parameters)
    assert "snapshot_bytes" in convert_params
    assert "snapshot_fingerprint" in convert_params
    assert "docx_bytes" not in convert_params


# -- Constructor / root contract -----------------------------------------------------


def test_constructor_rejects_scratch_root_equal_to_repo_root(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    runner = _FakeProcessRunner(version_result=_completed())
    with pytest.raises(LibreOfficeConfigurationError):
        LibreOfficeDocxToPdfConverter(
            soffice_path=_make_fake_soffice(tmp_path),
            scratch_root=shared,
            repo_root=shared,
            working_copy_root=tmp_path / "working-copy",
            blob_root=tmp_path / "blobs",
            font_environment_id="fonts-v1",
            max_docx_size_bytes=1024,
            max_pdf_size_bytes=1024,
            timeout_seconds=1.0,
            process_runner=runner,
        )


def test_constructor_rejects_scratch_root_inside_blob_root(tmp_path: Path) -> None:
    blob_root = tmp_path / "blobs"
    blob_root.mkdir()
    scratch_root = blob_root / "scratch"
    runner = _FakeProcessRunner(version_result=_completed())
    with pytest.raises(LibreOfficeConfigurationError):
        LibreOfficeDocxToPdfConverter(
            soffice_path=_make_fake_soffice(tmp_path),
            scratch_root=scratch_root,
            repo_root=tmp_path / "repo",
            working_copy_root=tmp_path / "working-copy",
            blob_root=blob_root,
            font_environment_id="fonts-v1",
            max_docx_size_bytes=1024,
            max_pdf_size_bytes=1024,
            timeout_seconds=1.0,
            process_runner=runner,
        )


def test_constructor_rejects_non_positive_limits(tmp_path: Path) -> None:
    runner = _FakeProcessRunner(version_result=_completed())
    with pytest.raises(LibreOfficeConfigurationError):
        LibreOfficeDocxToPdfConverter(
            soffice_path=_make_fake_soffice(tmp_path),
            scratch_root=tmp_path / "scratch",
            repo_root=tmp_path / "repo",
            working_copy_root=tmp_path / "working-copy",
            blob_root=tmp_path / "blobs",
            font_environment_id="fonts-v1",
            max_docx_size_bytes=0,
            max_pdf_size_bytes=1024,
            timeout_seconds=1.0,
            process_runner=runner,
        )


# -- Runtime identity / Windows fail-closed -------------------------------------------


def test_windows_platform_yields_converter_unavailable_without_runtime_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(adapter_module.sys, "platform", "win32")
    runner = _FakeProcessRunner(version_result=_completed())
    converter = _build_converter(tmp_path, process_runner=runner)

    assert converter.runtime_identity is None
    assert not runner.calls  # never even probes --version on win32

    result = converter.convert_docx_to_pdf(
        docx_bytes=_VALID_DOCX_BYTES,
        source_snapshot_fingerprint=_VALID_SNAPSHOT_FINGERPRINT,
        source_docx_sha256=_docx_sha256(),
        conversion_policy_version=_VALID_POLICY_VERSION,
    )
    assert result.status == DocxToPdfConversionStatus.CONVERTER_UNAVAILABLE
    assert result.pdf_bytes is None


def test_missing_executable_yields_unavailable_runtime_identity(tmp_path: Path) -> None:
    runner = _FakeProcessRunner(version_result=_completed())
    converter = _build_converter(tmp_path, process_runner=runner, soffice_path=tmp_path / "does-not-exist")
    assert converter.runtime_identity is None


def test_version_probe_failure_yields_unavailable_runtime_identity(tmp_path: Path) -> None:
    runner = _FakeProcessRunner(version_result=_completed(exit_code=1))
    converter = _build_converter(tmp_path, process_runner=runner)
    assert converter.runtime_identity is None


def test_runtime_identity_fingerprint_is_verified(tmp_path: Path) -> None:
    runner = _FakeProcessRunner(version_result=_completed(stdout=b"LibreOffice 7.6.2.1"))
    converter = _build_converter(tmp_path, process_runner=runner)
    identity = converter.runtime_identity
    assert identity is not None
    assert isinstance(identity, ConverterRuntimeIdentity)

    tampered = identity.model_dump()
    tampered["executable_sha256"] = "0" * 64
    with pytest.raises(Exception):
        ConverterRuntimeIdentity(**tampered)


def test_per_request_executable_identity_mismatch_blocks_conversion(tmp_path: Path) -> None:
    runner = _FakeProcessRunner(version_result=_completed(stdout=b"LibreOffice 7.6"))
    soffice_path = _make_fake_soffice(tmp_path)
    converter = _build_converter(tmp_path, process_runner=runner, soffice_path=soffice_path)
    assert converter.runtime_identity is not None

    # Mutate the executable's content after initialization -- the cached
    # sha256 no longer matches what a per-request revalidation observes.
    soffice_path.write_bytes(b"#!/bin/sh\necho a completely different binary\n")

    result = converter.convert_docx_to_pdf(
        docx_bytes=_VALID_DOCX_BYTES,
        source_snapshot_fingerprint=_VALID_SNAPSHOT_FINGERPRINT,
        source_docx_sha256=_docx_sha256(),
        conversion_policy_version=_VALID_POLICY_VERSION,
    )
    assert result.status == DocxToPdfConversionStatus.CONVERTER_UNAVAILABLE
    # No conversion process (beyond the initial --version probe) was ever run.
    assert all("--version" in call_argv for call_argv, _ in runner.calls)


# -- Request validation -----------------------------------------------------------


def test_docx_hash_mismatch_yields_invalid_request_and_creates_no_workspace(tmp_path: Path) -> None:
    runner = _FakeProcessRunner(version_result=_completed(stdout=b"LibreOffice 7.6"))
    converter = _build_converter(tmp_path, process_runner=runner)

    result = converter.convert_docx_to_pdf(
        docx_bytes=_VALID_DOCX_BYTES,
        source_snapshot_fingerprint=_VALID_SNAPSHOT_FINGERPRINT,
        source_docx_sha256="f" * 64,  # deliberately wrong
        conversion_policy_version=_VALID_POLICY_VERSION,
    )
    assert result.status == DocxToPdfConversionStatus.INVALID_REQUEST
    assert result.error_code == "INVALID_REQUEST"
    # No document content leaks into diagnostics.
    for entry in result.diagnostics:
        assert _VALID_DOCX_BYTES not in entry.encode("utf-8", errors="ignore")

    scratch_root = tmp_path / "scratch"
    if scratch_root.exists():
        assert list(scratch_root.iterdir()) == []


def test_empty_docx_bytes_is_invalid_request(tmp_path: Path) -> None:
    runner = _FakeProcessRunner(version_result=_completed(stdout=b"LibreOffice 7.6"))
    converter = _build_converter(tmp_path, process_runner=runner)

    result = converter.convert_docx_to_pdf(
        docx_bytes=b"",
        source_snapshot_fingerprint=_VALID_SNAPSHOT_FINGERPRINT,
        source_docx_sha256=hashlib.sha256(b"").hexdigest(),
        conversion_policy_version=_VALID_POLICY_VERSION,
    )
    assert result.status == DocxToPdfConversionStatus.INVALID_REQUEST


def test_oversized_docx_is_invalid_request(tmp_path: Path) -> None:
    runner = _FakeProcessRunner(version_result=_completed(stdout=b"LibreOffice 7.6"))
    converter = LibreOfficeDocxToPdfConverter(
        soffice_path=_make_fake_soffice(tmp_path),
        scratch_root=tmp_path / "scratch",
        repo_root=tmp_path / "repo",
        working_copy_root=tmp_path / "working-copy",
        blob_root=tmp_path / "blobs",
        font_environment_id="fonts-v1",
        max_docx_size_bytes=8,
        max_pdf_size_bytes=1024,
        timeout_seconds=5.0,
        process_runner=runner,
    )
    data = b"x" * 100
    result = converter.convert_docx_to_pdf(
        docx_bytes=data,
        source_snapshot_fingerprint=_VALID_SNAPSHOT_FINGERPRINT,
        source_docx_sha256=hashlib.sha256(data).hexdigest(),
        conversion_policy_version=_VALID_POLICY_VERSION,
    )
    assert result.status == DocxToPdfConversionStatus.INVALID_REQUEST


def test_malformed_snapshot_fingerprint_is_invalid_request(tmp_path: Path) -> None:
    runner = _FakeProcessRunner(version_result=_completed(stdout=b"LibreOffice 7.6"))
    converter = _build_converter(tmp_path, process_runner=runner)
    result = converter.convert_docx_to_pdf(
        docx_bytes=_VALID_DOCX_BYTES,
        source_snapshot_fingerprint="not-hex",
        source_docx_sha256=_docx_sha256(),
        conversion_policy_version=_VALID_POLICY_VERSION,
    )
    assert result.status == DocxToPdfConversionStatus.INVALID_REQUEST


# -- Process status mapping (via a fake process runner) -----------------------------


def _convert_with_process_result(
    tmp_path: Path, process_result: ProcessRunResult, *, also_write_valid_pdf: bool = False
) -> DocxToPdfConversionStatus:
    class _ScriptedRunner:
        def __init__(self) -> None:
            self.calls = 0

        def run(
            self, *, argv: tuple[str, ...], cwd: Path, env: dict[str, str], timeout_seconds: float
        ) -> ProcessRunResult:
            self.calls += 1
            if "--version" in argv:
                return _completed(stdout=b"LibreOffice 7.6")
            if also_write_valid_pdf:
                out_dir = cwd / "out"
                (out_dir / "input.pdf").write_bytes(_VALID_PDF_BYTES)
            return process_result

    runner = _ScriptedRunner()
    converter = _build_converter(tmp_path, process_runner=runner)  # type: ignore[arg-type]
    result = converter.convert_docx_to_pdf(
        docx_bytes=_VALID_DOCX_BYTES,
        source_snapshot_fingerprint=_VALID_SNAPSHOT_FINGERPRINT,
        source_docx_sha256=_docx_sha256(),
        conversion_policy_version=_VALID_POLICY_VERSION,
    )
    return result


def test_process_start_failed_maps_to_process_start_failed(tmp_path: Path) -> None:
    process_result = ProcessRunResult(
        status=ProcessRunStatus.START_FAILED,
        exit_code=None,
        bounded_stdout=b"",
        bounded_stderr=b"",
        termination_stage=None,
        max_stdout_bytes=1024 * 1024,
        max_stderr_bytes=1024 * 1024,
        diagnostics=("start failed",),
    )
    result = _convert_with_process_result(tmp_path, process_result)
    assert result.status == DocxToPdfConversionStatus.PROCESS_START_FAILED
    assert result.pdf_bytes is None


def test_output_limit_exceeded_maps_to_process_output_limit_exceeded(tmp_path: Path) -> None:
    process_result = ProcessRunResult(
        status=ProcessRunStatus.OUTPUT_LIMIT_EXCEEDED,
        exit_code=None,
        bounded_stdout=b"x" * 10,
        bounded_stderr=b"",
        termination_stage="sigkill",
        max_stdout_bytes=1024 * 1024,
        max_stderr_bytes=1024 * 1024,
        diagnostics=(),
    )
    result = _convert_with_process_result(tmp_path, process_result)
    assert result.status == DocxToPdfConversionStatus.PROCESS_OUTPUT_LIMIT_EXCEEDED


def test_timed_out_maps_to_conversion_timeout(tmp_path: Path) -> None:
    process_result = ProcessRunResult(
        status=ProcessRunStatus.TIMED_OUT,
        exit_code=None,
        bounded_stdout=b"",
        bounded_stderr=b"",
        termination_stage="sigkill",
        max_stdout_bytes=1024 * 1024,
        max_stderr_bytes=1024 * 1024,
        diagnostics=(),
    )
    result = _convert_with_process_result(tmp_path, process_result)
    assert result.status == DocxToPdfConversionStatus.CONVERSION_TIMEOUT


def test_termination_failed_maps_to_process_termination_failed(tmp_path: Path) -> None:
    process_result = ProcessRunResult(
        status=ProcessRunStatus.TERMINATION_FAILED,
        exit_code=None,
        bounded_stdout=b"",
        bounded_stderr=b"",
        termination_stage="sigkill_timeout",
        max_stdout_bytes=1024 * 1024,
        max_stderr_bytes=1024 * 1024,
        diagnostics=(),
    )
    result = _convert_with_process_result(tmp_path, process_result)
    assert result.status == DocxToPdfConversionStatus.PROCESS_TERMINATION_FAILED


def test_nonzero_exit_code_maps_to_conversion_failed_and_never_reads_pdf(tmp_path: Path) -> None:
    process_result = _completed(exit_code=1)
    result = _convert_with_process_result(tmp_path, process_result, also_write_valid_pdf=True)
    assert result.status == DocxToPdfConversionStatus.CONVERSION_FAILED
    assert result.pdf_bytes is None


def test_zero_exit_but_missing_pdf_maps_to_pdf_output_missing(tmp_path: Path) -> None:
    process_result = _completed(exit_code=0)
    result = _convert_with_process_result(tmp_path, process_result, also_write_valid_pdf=False)
    assert result.status == DocxToPdfConversionStatus.PDF_OUTPUT_MISSING


def test_successful_conversion_returns_exact_pdf_bytes_and_sha256(tmp_path: Path) -> None:
    process_result = _completed(exit_code=0)
    result = _convert_with_process_result(tmp_path, process_result, also_write_valid_pdf=True)
    assert result.status == DocxToPdfConversionStatus.SUCCEEDED
    assert result.pdf_bytes == _VALID_PDF_BYTES
    assert result.pdf_sha256 == hashlib.sha256(_VALID_PDF_BYTES).hexdigest()
    assert result.runtime_identity is not None
    assert result.error_code is None


# -- Workspace / argv / env isolation -------------------------------------------------


def test_fixed_argv_isolated_profile_and_reduced_env(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    class _CapturingRunner:
        def run(
            self, *, argv: tuple[str, ...], cwd: Path, env: dict[str, str], timeout_seconds: float
        ) -> ProcessRunResult:
            if "--version" in argv:
                return _completed(stdout=b"LibreOffice 7.6")
            captured["argv"] = argv
            captured["env"] = env
            captured["cwd"] = cwd
            out_dir = cwd / "out"
            (out_dir / "input.pdf").write_bytes(_VALID_PDF_BYTES)
            return _completed(exit_code=0)

    converter = _build_converter(tmp_path, process_runner=_CapturingRunner())  # type: ignore[arg-type]
    result = converter.convert_docx_to_pdf(
        docx_bytes=_VALID_DOCX_BYTES,
        source_snapshot_fingerprint=_VALID_SNAPSHOT_FINGERPRINT,
        source_docx_sha256=_docx_sha256(),
        conversion_policy_version=_VALID_POLICY_VERSION,
    )
    assert result.status == DocxToPdfConversionStatus.SUCCEEDED

    argv = captured["argv"]
    assert argv[1].startswith("-env:UserInstallation=file://")
    assert "--headless" in argv
    assert "--norestore" in argv
    assert "--convert-to" in argv
    assert "pdf" in argv
    assert "--outdir" in argv
    assert str(argv[-1]).endswith("input.docx")

    env = captured["env"]
    assert "PYTHONPATH" not in env
    assert set(env) == {"PATH", "HOME", "LANG", "LC_ALL"}


@pytest.mark.parametrize(
    "scratch_root_suffix, encoded_segment, raw_segment",
    [
        ("scratch with space", "scratch%20with%20space", "scratch with space"),
        ("scratch#hash", "scratch%23hash", "scratch#hash"),
        ("scratch%percent", "scratch%25percent", "scratch%percent"),
        ("scratch-café-unicode", "scratch-caf%C3%A9-unicode", "scratch-café-unicode"),
    ],
)
def test_profile_uri_is_percent_encoded_for_special_scratch_root_paths(
    tmp_path: Path, scratch_root_suffix: str, encoded_segment: str, raw_segment: str
) -> None:
    """The real, production ``convert_docx_to_pdf`` argv-building path must
    produce a properly percent-encoded ``file://`` URI for the LibreOffice
    profile directory -- never a raw ``"file://" + str(path)``
    concatenation -- even when ``scratch_root`` itself contains a space,
    ``#``, ``%``, or a non-ASCII Unicode character.

    ``encoded_segment``/``raw_segment`` are hardcoded literals -- never
    derived via ``Path.as_uri()``, ``urllib.parse.quote()``, or any other
    URI-encoding routine -- so this is an independent proof of the exact
    percent-encoding sequence, not a self-comparison against production's
    own encoding call."""

    captured: dict[str, object] = {}

    class _CapturingRunner:
        def run(
            self, *, argv: tuple[str, ...], cwd: Path, env: dict[str, str], timeout_seconds: float
        ) -> ProcessRunResult:
            if "--version" in argv:
                return _completed(stdout=b"LibreOffice 7.6")
            captured["argv"] = argv
            out_dir = cwd / "out"
            (out_dir / "input.pdf").write_bytes(_VALID_PDF_BYTES)
            return _completed(exit_code=0)

    scratch_root = tmp_path / scratch_root_suffix
    converter = LibreOfficeDocxToPdfConverter(
        soffice_path=_make_fake_soffice(tmp_path),
        scratch_root=scratch_root,
        repo_root=tmp_path / "repo",
        working_copy_root=tmp_path / "working-copy",
        blob_root=tmp_path / "blobs",
        font_environment_id="fonts-v1",
        max_docx_size_bytes=10 * 1024 * 1024,
        max_pdf_size_bytes=10 * 1024 * 1024,
        timeout_seconds=5.0,
        process_runner=_CapturingRunner(),  # type: ignore[arg-type]
    )

    result = converter.convert_docx_to_pdf(
        docx_bytes=_VALID_DOCX_BYTES,
        source_snapshot_fingerprint=_VALID_SNAPSHOT_FINGERPRINT,
        source_docx_sha256=_docx_sha256(),
        conversion_policy_version=_VALID_POLICY_VERSION,
    )
    assert result.status == DocxToPdfConversionStatus.SUCCEEDED

    # Pulled directly from the argv the real adapter passed to the process
    # runner -- never a locally reconstructed copy.
    argv = captured["argv"]
    profile_arguments = [arg for arg in argv if arg.startswith("-env:UserInstallation=")]
    assert len(profile_arguments) == 1
    profile_argument = profile_arguments[0]

    assert profile_argument.startswith("-env:UserInstallation=file://")
    assert profile_argument.endswith("/profile")
    assert encoded_segment in profile_argument
    assert raw_segment not in profile_argument

    if scratch_root_suffix == "scratch with space":
        assert " " not in profile_argument
    elif scratch_root_suffix == "scratch#hash":
        assert "#" not in profile_argument
    elif scratch_root_suffix == "scratch-café-unicode":
        assert "é" not in profile_argument
    elif scratch_root_suffix == "scratch%percent":
        assert "%25percent" in profile_argument
        assert "%percent" not in profile_argument


def test_exact_docx_bytes_written_to_isolated_input_path(tmp_path: Path) -> None:
    observed_docx_bytes: dict[str, bytes] = {}

    class _InspectingRunner:
        def run(
            self, *, argv: tuple[str, ...], cwd: Path, env: dict[str, str], timeout_seconds: float
        ) -> ProcessRunResult:
            if "--version" in argv:
                return _completed(stdout=b"LibreOffice 7.6")
            input_path = cwd / "in" / "input.docx"
            observed_docx_bytes["data"] = input_path.read_bytes()
            (cwd / "out" / "input.pdf").write_bytes(_VALID_PDF_BYTES)
            return _completed(exit_code=0)

    converter = _build_converter(tmp_path, process_runner=_InspectingRunner())  # type: ignore[arg-type]
    converter.convert_docx_to_pdf(
        docx_bytes=_VALID_DOCX_BYTES,
        source_snapshot_fingerprint=_VALID_SNAPSHOT_FINGERPRINT,
        source_docx_sha256=_docx_sha256(),
        conversion_policy_version=_VALID_POLICY_VERSION,
    )
    assert observed_docx_bytes["data"] == _VALID_DOCX_BYTES


def test_workspace_is_removed_after_successful_conversion(tmp_path: Path) -> None:
    workspace_roots: list[Path] = []

    class _RecordingRunner:
        def run(
            self, *, argv: tuple[str, ...], cwd: Path, env: dict[str, str], timeout_seconds: float
        ) -> ProcessRunResult:
            if "--version" in argv:
                return _completed(stdout=b"LibreOffice 7.6")
            workspace_roots.append(cwd)
            (cwd / "out" / "input.pdf").write_bytes(_VALID_PDF_BYTES)
            return _completed(exit_code=0)

    converter = _build_converter(tmp_path, process_runner=_RecordingRunner())  # type: ignore[arg-type]
    result = converter.convert_docx_to_pdf(
        docx_bytes=_VALID_DOCX_BYTES,
        source_snapshot_fingerprint=_VALID_SNAPSHOT_FINGERPRINT,
        source_docx_sha256=_docx_sha256(),
        conversion_policy_version=_VALID_POLICY_VERSION,
    )
    assert result.status == DocxToPdfConversionStatus.SUCCEEDED
    assert workspace_roots
    assert not workspace_roots[0].exists()


# -- Cleanup security contract --------------------------------------------------------


def test_cleanup_failure_after_success_blocks_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class _SucceedingRunner:
        def run(
            self, *, argv: tuple[str, ...], cwd: Path, env: dict[str, str], timeout_seconds: float
        ) -> ProcessRunResult:
            if "--version" in argv:
                return _completed(stdout=b"LibreOffice 7.6")
            (cwd / "out" / "input.pdf").write_bytes(_VALID_PDF_BYTES)
            return _completed(exit_code=0)

    converter = _build_converter(tmp_path, process_runner=_SucceedingRunner())  # type: ignore[arg-type]

    monkeypatch.setattr(
        converter,
        "_cleanup_workspace",
        lambda _workspace: adapter_module.WorkspaceCleanupResult(
            status=WorkspaceCleanupStatus.CLEANUP_FAILED,
            diagnostics=("forced cleanup failure for test",),
        ),
    )

    result = converter.convert_docx_to_pdf(
        docx_bytes=_VALID_DOCX_BYTES,
        source_snapshot_fingerprint=_VALID_SNAPSHOT_FINGERPRINT,
        source_docx_sha256=_docx_sha256(),
        conversion_policy_version=_VALID_POLICY_VERSION,
    )
    assert result.status == DocxToPdfConversionStatus.WORKSPACE_CLEANUP_FAILED
    assert result.pdf_bytes is None
    assert result.pdf_sha256 is None


def test_cleanup_failure_after_earlier_failure_preserves_primary_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _FailingRunner:
        def run(
            self, *, argv: tuple[str, ...], cwd: Path, env: dict[str, str], timeout_seconds: float
        ) -> ProcessRunResult:
            if "--version" in argv:
                return _completed(stdout=b"LibreOffice 7.6")
            return _completed(exit_code=1)

    converter = _build_converter(tmp_path, process_runner=_FailingRunner())  # type: ignore[arg-type]

    monkeypatch.setattr(
        converter,
        "_cleanup_workspace",
        lambda _workspace: adapter_module.WorkspaceCleanupResult(
            status=WorkspaceCleanupStatus.CLEANUP_FAILED,
            diagnostics=("forced cleanup failure for test",),
        ),
    )

    result = converter.convert_docx_to_pdf(
        docx_bytes=_VALID_DOCX_BYTES,
        source_snapshot_fingerprint=_VALID_SNAPSHOT_FINGERPRINT,
        source_docx_sha256=_docx_sha256(),
        conversion_policy_version=_VALID_POLICY_VERSION,
    )
    # The earlier CONVERSION_FAILED status is preserved, not overwritten.
    assert result.status == DocxToPdfConversionStatus.CONVERSION_FAILED
    assert any("forced cleanup failure" in entry for entry in result.diagnostics)


def test_workspace_setup_failure_never_creates_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _FakeProcessRunner(version_result=_completed(stdout=b"LibreOffice 7.6"))
    converter = _build_converter(tmp_path, process_runner=runner)

    monkeypatch.setattr(converter, "_setup_workspace", lambda: None)

    result = converter.convert_docx_to_pdf(
        docx_bytes=_VALID_DOCX_BYTES,
        source_snapshot_fingerprint=_VALID_SNAPSHOT_FINGERPRINT,
        source_docx_sha256=_docx_sha256(),
        conversion_policy_version=_VALID_POLICY_VERSION,
    )
    assert result.status == DocxToPdfConversionStatus.WORKSPACE_SETUP_FAILED
    assert len(runner.calls) == 1  # only the constructor's --version probe


# -- Diagnostics bounds / no raw exception escape -------------------------------------


def test_diagnostics_are_bounded_even_with_many_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class _FailingRunner:
        def run(
            self, *, argv: tuple[str, ...], cwd: Path, env: dict[str, str], timeout_seconds: float
        ) -> ProcessRunResult:
            if "--version" in argv:
                return _completed(stdout=b"LibreOffice 7.6")
            return ProcessRunResult(
                status=ProcessRunStatus.START_FAILED,
                exit_code=None,
                bounded_stdout=b"",
                bounded_stderr=b"",
                termination_stage=None,
                max_stdout_bytes=1024 * 1024,
                max_stderr_bytes=1024 * 1024,
                diagnostics=tuple(f"entry-{i}" for i in range(20)),
            )

    converter = _build_converter(tmp_path, process_runner=_FailingRunner())  # type: ignore[arg-type]
    monkeypatch.setattr(
        converter,
        "_cleanup_workspace",
        lambda _workspace: adapter_module.WorkspaceCleanupResult(
            status=WorkspaceCleanupStatus.CLEANUP_FAILED,
            diagnostics=tuple(f"cleanup-{i}" for i in range(20)),
        ),
    )

    result = converter.convert_docx_to_pdf(
        docx_bytes=_VALID_DOCX_BYTES,
        source_snapshot_fingerprint=_VALID_SNAPSHOT_FINGERPRINT,
        source_docx_sha256=_docx_sha256(),
        conversion_policy_version=_VALID_POLICY_VERSION,
    )
    assert len(result.diagnostics) <= 8


def test_unexpected_adapter_exception_never_escapes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class _ExplodingRunner:
        def run(
            self, *, argv: tuple[str, ...], cwd: Path, env: dict[str, str], timeout_seconds: float
        ) -> ProcessRunResult:
            if "--version" in argv:
                return _completed(stdout=b"LibreOffice 7.6")
            raise RuntimeError("simulated unexpected adapter failure")

    converter = _build_converter(tmp_path, process_runner=_ExplodingRunner())  # type: ignore[arg-type]
    result = converter.convert_docx_to_pdf(
        docx_bytes=_VALID_DOCX_BYTES,
        source_snapshot_fingerprint=_VALID_SNAPSHOT_FINGERPRINT,
        source_docx_sha256=_docx_sha256(),
        conversion_policy_version=_VALID_POLICY_VERSION,
    )
    assert result.status == DocxToPdfConversionStatus.CONVERTER_CONTRACT_VIOLATION
    assert result.error_code == "CONVERTER_CONTRACT_VIOLATION"
