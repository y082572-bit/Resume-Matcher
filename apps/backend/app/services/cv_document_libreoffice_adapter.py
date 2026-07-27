"""Explicit Provenance Stage P6-B3a: the LibreOffice/soffice PDF converter
foundation.

``LibreOfficeDocxToPdfConverter`` implements only ``DocxToPdfConverter``
(``cv_document_pdf_converter_protocol.py``) -- never the existing P6-A
``CvPdfConversionAdapter`` in ``cv_document_adapters.py``, which this
stage never modifies. Production support is macOS and Linux only; on
Windows this adapter always fail-closed returns
``CONVERTER_UNAVAILABLE`` without ever attempting a subprocess.

One ``convert_docx_to_pdf`` call, in order:

1. request validation (bytes non-empty/bounded, both fingerprints exactly
   64 lowercase hex, ``sha256(docx_bytes) == source_docx_sha256``,
   ``conversion_policy_version`` non-empty/bounded) -- an invalid request
   never creates a workspace, never writes a DOCX, never starts a process;
2. per-request executable identity revalidation against the
   ``ConverterRuntimeIdentity`` cached at construction (never trusted from
   cache alone);
3. an isolated, randomly named workspace under ``scratch_root`` (mode
   ``0700``, verified as a direct child of ``scratch_root``, never a
   symlink), with fixed ``in``/``out``/``profile`` subdirectories and fixed
   ``input.docx``/``input.pdf`` filenames -- never a caller-supplied path;
4. a secure, ``O_EXCL``-created input write;
5. a fixed argv and a small, explicit environment allowlist -- a caller
   never influences either;
6. ``ProcessRunner.run`` under a bounded timeout;
7. exhaustive mapping of every ``ProcessRunStatus`` to a
   ``DocxToPdfConversionStatus`` -- a nonzero exit code is always
   ``CONVERSION_FAILED`` and the PDF output is never read in that case;
8. on a zero exit code, a secure, handle-based PDF output read plus
   minimal technical validation (``%PDF-`` prefix, ``%%EOF`` trailer);
9. workspace cleanup in a ``finally`` block, verified before a
   ``SUCCEEDED`` result is ever built -- a cleanup failure after a
   technically successful conversion becomes ``WORKSPACE_CLEANUP_FAILED``
   with no PDF bytes; a cleanup failure after an earlier failure preserves
   that earlier status and appends a bounded cleanup diagnostic.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import sys
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from app.schemas.cv_document_pdf_conversion import (
    DocxToPdfConversionResult,
    DocxToPdfConversionStatus,
)
from app.schemas.cv_document_pdf_conversion_runtime import (
    ConverterRuntimeIdentity,
    build_converter_runtime_identity,
)
from app.services.cv_document_process_runner import ProcessRunner, ProcessRunResult, ProcessRunStatus
from app.services.cv_document_secure_pdf_reader import (
    ExecutableIdentityObservationStatus,
    SecurePdfReadResult,
    SecurePdfReadStatus,
    observe_executable_identity,
    read_pdf_output_securely,
)


_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")

_MAX_DIAGNOSTICS_ENTRIES = 8
_MAX_DIAGNOSTIC_LENGTH = 1000
_MAX_POLICY_VERSION_LENGTH = 255

_IMPLEMENTATION_ID = "libreoffice-soffice-headless"

#: The exact, minimal environment allowlist passed to ``soffice`` -- never
#: the caller's/process's full environment, never ``PYTHONPATH``.
#: ``PATH`` lets ``soffice`` resolve its own internal helper binaries;
#: ``HOME`` is required for profile/config path resolution even though
#: ``-env:UserInstallation`` overrides the actual profile directory;
#: ``LANG``/``LC_ALL`` are fixed to a deterministic UTF-8 locale so
#: conversion output does not vary with the calling process's locale.
_REDUCED_ENV_LANG = "en_US.UTF-8"


def _bounded_diagnostics(diagnostics: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        entry.replace("\x00", "")[:_MAX_DIAGNOSTIC_LENGTH]
        for entry in diagnostics[:_MAX_DIAGNOSTICS_ENTRIES]
    )


def _build_reduced_env() -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "LANG": _REDUCED_ENV_LANG,
        "LC_ALL": _REDUCED_ENV_LANG,
    }


class LibreOfficeConfigurationError(Exception):
    """Raised only for constructor-time configuration violations -- never
    a wrapper around a raw ``OSError``."""


def _canonical_existing_dir(path: Path, *, label: str) -> Path:
    if not path.is_absolute():
        raise LibreOfficeConfigurationError(f"{label} must be an absolute path")
    if path.is_symlink():
        raise LibreOfficeConfigurationError(f"{label} must not be a symlink")
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise LibreOfficeConfigurationError(f"{label} must not be a symlink")
    if not path.is_dir():
        raise LibreOfficeConfigurationError(f"{label} must be a directory")
    return path.resolve(strict=True)


def _reject_ancestor_relationship(scratch_root: Path, other: Path, *, label: str) -> None:
    resolved_other = other.resolve(strict=False)
    if scratch_root == resolved_other:
        raise LibreOfficeConfigurationError(f"scratch_root must not equal {label}")
    if scratch_root.is_relative_to(resolved_other):
        raise LibreOfficeConfigurationError(f"scratch_root must not live inside {label}")
    if resolved_other.is_relative_to(scratch_root):
        raise LibreOfficeConfigurationError(f"{label} must not live inside scratch_root")


def _validate_request(
    *,
    docx_bytes: bytes,
    source_snapshot_fingerprint: str,
    source_docx_sha256: str,
    conversion_policy_version: str,
    max_docx_size_bytes: int,
) -> tuple[str, ...] | None:
    """``None`` if the request is valid; otherwise a bounded diagnostics
    tuple describing the violation -- never the document content itself."""

    if not isinstance(docx_bytes, bytes) or len(docx_bytes) == 0:
        return ("docx_bytes must be non-empty bytes",)
    if len(docx_bytes) > max_docx_size_bytes:
        return ("docx_bytes exceeds max_docx_size_bytes",)
    if not isinstance(source_snapshot_fingerprint, str) or not _SHA256_HEX_RE.match(source_snapshot_fingerprint):
        return ("source_snapshot_fingerprint must be exactly 64 lowercase hex characters",)
    if not isinstance(source_docx_sha256, str) or not _SHA256_HEX_RE.match(source_docx_sha256):
        return ("source_docx_sha256 must be exactly 64 lowercase hex characters",)
    if hashlib.sha256(docx_bytes).hexdigest() != source_docx_sha256:
        return ("source_docx_sha256 does not match sha256(docx_bytes)",)
    if (
        not isinstance(conversion_policy_version, str)
        or not conversion_policy_version
        or len(conversion_policy_version) > _MAX_POLICY_VERSION_LENGTH
        or "\x00" in conversion_policy_version
    ):
        return ("conversion_policy_version must be non-empty and bounded",)
    return None


def _write_input_docx_securely(path: Path, data: bytes) -> tuple[bool, tuple[str, ...]]:
    """``O_CREAT | O_EXCL | O_WRONLY | O_NOFOLLOW`` at mode ``0600``, a
    partial-write loop that handles ``os.write`` returning ``0``, an
    ``fsync``, and a regular-file ``fstat`` check. Closes the handle
    exactly once. Never lets a raw ``OSError`` escape."""

    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        return False, (f"failed to create input DOCX: {exc}",)

    try:
        fstat_result = os.fstat(fd)
        if not stat.S_ISREG(fstat_result.st_mode):
            return False, ("input DOCX handle is not a regular file",)

        view = memoryview(data)
        offset = 0
        total = len(data)
        while offset < total:
            try:
                written = os.write(fd, view[offset:])
            except OSError as exc:
                return False, (f"failed to write input DOCX: {exc}",)
            if written == 0:
                return False, ("os.write returned 0 while writing input DOCX",)
            offset += written

        os.fsync(fd)
        return True, ()
    except OSError as exc:
        return False, (f"failed to write input DOCX: {exc}",)
    finally:
        os.close(fd)


class WorkspaceCleanupStatus(StrEnum):
    CLEAN = "CLEAN"
    CLEANUP_FAILED = "CLEANUP_FAILED"


@dataclass(frozen=True)
class WorkspaceCleanupResult:
    status: WorkspaceCleanupStatus
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Workspace:
    root: Path
    in_dir: Path
    out_dir: Path
    profile_dir: Path
    input_docx_path: Path
    output_pdf_path: Path
    root_identity: tuple[int, int]


class LibreOfficeDocxToPdfConverter:
    """Converts exact DOCX bytes to PDF via a headless
    LibreOffice/soffice subprocess. Implements only ``DocxToPdfConverter``
    -- see the module docstring for the full security contract."""

    def __init__(
        self,
        *,
        soffice_path: Path,
        scratch_root: Path,
        repo_root: Path,
        working_copy_root: Path,
        blob_root: Path,
        font_environment_id: str,
        max_docx_size_bytes: int,
        max_pdf_size_bytes: int,
        timeout_seconds: float,
        process_runner: ProcessRunner,
    ) -> None:
        if max_docx_size_bytes <= 0:
            raise LibreOfficeConfigurationError("max_docx_size_bytes must be positive")
        if max_pdf_size_bytes <= 0:
            raise LibreOfficeConfigurationError("max_pdf_size_bytes must be positive")
        if timeout_seconds <= 0:
            raise LibreOfficeConfigurationError("timeout_seconds must be positive")
        if not font_environment_id:
            raise LibreOfficeConfigurationError("font_environment_id must be non-empty")
        if not soffice_path.is_absolute():
            raise LibreOfficeConfigurationError("soffice_path must be an absolute path")

        self._soffice_path = soffice_path
        self._max_docx_size_bytes = max_docx_size_bytes
        self._max_pdf_size_bytes = max_pdf_size_bytes
        self._timeout_seconds = timeout_seconds
        self._process_runner = process_runner
        self._font_environment_id = font_environment_id

        self._scratch_root = _canonical_existing_dir(scratch_root, label="scratch_root")
        _reject_ancestor_relationship(self._scratch_root, repo_root, label="repo_root")
        _reject_ancestor_relationship(self._scratch_root, working_copy_root, label="working_copy_root")
        _reject_ancestor_relationship(self._scratch_root, blob_root, label="blob_root")

        if sys.platform == "win32":
            self._runtime_identity: ConverterRuntimeIdentity | None = None
        else:
            self._runtime_identity = self._build_runtime_identity()

    @property
    def runtime_identity(self) -> ConverterRuntimeIdentity | None:
        return self._runtime_identity

    def _build_runtime_identity(self) -> ConverterRuntimeIdentity | None:
        observation = observe_executable_identity(self._soffice_path)
        if observation.status != ExecutableIdentityObservationStatus.OK:
            return None

        version_result = self._process_runner.run(
            argv=(str(self._soffice_path), "--version"),
            cwd=self._scratch_root,
            env=_build_reduced_env(),
            timeout_seconds=self._timeout_seconds,
        )
        if version_result.status != ProcessRunStatus.COMPLETED or version_result.exit_code != 0:
            return None

        implementation_version = (
            version_result.bounded_stdout.decode("utf-8", errors="replace").strip()[:255] or "unknown"
        )

        return build_converter_runtime_identity(
            implementation_id=_IMPLEMENTATION_ID,
            implementation_version=implementation_version,
            executable_canonical_path=observation.canonical_path,
            executable_file_identity=observation.file_identity,
            executable_sha256=observation.sha256,
            platform_identity=sys.platform,
            font_environment_id=self._font_environment_id,
        )

    def convert_docx_to_pdf(
        self,
        *,
        docx_bytes: bytes,
        source_snapshot_fingerprint: str,
        source_docx_sha256: str,
        conversion_policy_version: str,
    ) -> DocxToPdfConversionResult:
        safe_policy_version = (
            conversion_policy_version
            if isinstance(conversion_policy_version, str)
            and conversion_policy_version
            and len(conversion_policy_version) <= _MAX_POLICY_VERSION_LENGTH
            and "\x00" not in conversion_policy_version
            else "unknown"
        )

        violation = _validate_request(
            docx_bytes=docx_bytes,
            source_snapshot_fingerprint=source_snapshot_fingerprint,
            source_docx_sha256=source_docx_sha256,
            conversion_policy_version=conversion_policy_version,
            max_docx_size_bytes=self._max_docx_size_bytes,
        )
        if violation is not None:
            return DocxToPdfConversionResult(
                status=DocxToPdfConversionStatus.INVALID_REQUEST,
                conversion_policy_version=safe_policy_version,
                error_code="INVALID_REQUEST",
                diagnostics=_bounded_diagnostics(violation),
            )

        if sys.platform == "win32":
            return DocxToPdfConversionResult(
                status=DocxToPdfConversionStatus.CONVERTER_UNAVAILABLE,
                conversion_policy_version=safe_policy_version,
                error_code="CONVERTER_UNAVAILABLE",
                diagnostics=("PDF conversion is not supported on win32 in this stage",),
            )

        if self._runtime_identity is None:
            return DocxToPdfConversionResult(
                status=DocxToPdfConversionStatus.CONVERTER_UNAVAILABLE,
                conversion_policy_version=safe_policy_version,
                error_code="CONVERTER_UNAVAILABLE",
                diagnostics=("converter runtime identity is unavailable",),
            )

        revalidation = observe_executable_identity(self._soffice_path)
        if (
            revalidation.status != ExecutableIdentityObservationStatus.OK
            or revalidation.canonical_path != self._runtime_identity.executable_canonical_path
            or revalidation.file_identity != self._runtime_identity.executable_file_identity
            or revalidation.sha256 != self._runtime_identity.executable_sha256
        ):
            return DocxToPdfConversionResult(
                status=DocxToPdfConversionStatus.CONVERTER_UNAVAILABLE,
                conversion_policy_version=safe_policy_version,
                error_code="CONVERTER_UNAVAILABLE",
                diagnostics=("executable identity revalidation failed before conversion",),
            )

        workspace = self._setup_workspace()
        if workspace is None:
            return DocxToPdfConversionResult(
                status=DocxToPdfConversionStatus.WORKSPACE_SETUP_FAILED,
                conversion_policy_version=safe_policy_version,
                error_code="WORKSPACE_SETUP_FAILED",
                diagnostics=("failed to create isolated workspace",),
            )

        primary_status = DocxToPdfConversionStatus.CONVERTER_CONTRACT_VIOLATION
        primary_error_code: str | None = "CONVERTER_CONTRACT_VIOLATION"
        primary_diagnostics: tuple[str, ...] = ()
        pdf_bytes: bytes | None = None
        pdf_sha256: str | None = None

        try:
            write_ok, write_diagnostics = _write_input_docx_securely(workspace.input_docx_path, docx_bytes)
            if not write_ok:
                primary_status = DocxToPdfConversionStatus.WORKSPACE_SETUP_FAILED
                primary_error_code = "WORKSPACE_SETUP_FAILED"
                primary_diagnostics = write_diagnostics
            else:
                profile_uri = workspace.profile_dir.resolve(strict=True).as_uri()
                argv = (
                    str(self._soffice_path),
                    f"-env:UserInstallation={profile_uri}",
                    "--headless",
                    "--norestore",
                    "--convert-to",
                    "pdf",
                    "--outdir",
                    str(workspace.out_dir),
                    str(workspace.input_docx_path),
                )
                run_result = self._process_runner.run(
                    argv=argv,
                    cwd=workspace.root,
                    env=_build_reduced_env(),
                    timeout_seconds=self._timeout_seconds,
                )
                (
                    primary_status,
                    primary_error_code,
                    primary_diagnostics,
                    pdf_bytes,
                    pdf_sha256,
                ) = self._interpret_process_result(run_result, workspace)
        except Exception as exc:  # never let a raw exception escape the adapter
            primary_status = DocxToPdfConversionStatus.CONVERTER_CONTRACT_VIOLATION
            primary_error_code = "CONVERTER_CONTRACT_VIOLATION"
            primary_diagnostics = (f"unexpected adapter failure: {exc}",)
            pdf_bytes = None
            pdf_sha256 = None
        finally:
            cleanup_result = self._cleanup_workspace(workspace)

        primary_diagnostics = _bounded_diagnostics(primary_diagnostics)

        if cleanup_result.status != WorkspaceCleanupStatus.CLEAN:
            if primary_status == DocxToPdfConversionStatus.SUCCEEDED:
                return DocxToPdfConversionResult(
                    status=DocxToPdfConversionStatus.WORKSPACE_CLEANUP_FAILED,
                    conversion_policy_version=safe_policy_version,
                    error_code="WORKSPACE_CLEANUP_FAILED",
                    diagnostics=_bounded_diagnostics(cleanup_result.diagnostics),
                )
            combined = _bounded_diagnostics((*primary_diagnostics, *cleanup_result.diagnostics))
            return DocxToPdfConversionResult(
                status=primary_status,
                conversion_policy_version=safe_policy_version,
                error_code=primary_error_code,
                diagnostics=combined,
            )

        if primary_status == DocxToPdfConversionStatus.SUCCEEDED:
            return DocxToPdfConversionResult(
                status=DocxToPdfConversionStatus.SUCCEEDED,
                pdf_bytes=pdf_bytes,
                pdf_sha256=pdf_sha256,
                runtime_identity=self._runtime_identity,
                conversion_policy_version=safe_policy_version,
                diagnostics=primary_diagnostics,
            )

        return DocxToPdfConversionResult(
            status=primary_status,
            conversion_policy_version=safe_policy_version,
            error_code=primary_error_code,
            diagnostics=primary_diagnostics,
        )

    def _interpret_process_result(
        self, run_result: ProcessRunResult, workspace: _Workspace
    ) -> tuple[DocxToPdfConversionStatus, str | None, tuple[str, ...], bytes | None, str | None]:
        if run_result.status == ProcessRunStatus.START_FAILED:
            return (
                DocxToPdfConversionStatus.PROCESS_START_FAILED,
                "PROCESS_START_FAILED",
                run_result.diagnostics,
                None,
                None,
            )
        if run_result.status == ProcessRunStatus.OUTPUT_LIMIT_EXCEEDED:
            return (
                DocxToPdfConversionStatus.PROCESS_OUTPUT_LIMIT_EXCEEDED,
                "PROCESS_OUTPUT_LIMIT_EXCEEDED",
                run_result.diagnostics,
                None,
                None,
            )
        if run_result.status == ProcessRunStatus.TIMED_OUT:
            return (
                DocxToPdfConversionStatus.CONVERSION_TIMEOUT,
                "CONVERSION_TIMEOUT",
                run_result.diagnostics,
                None,
                None,
            )
        if run_result.status == ProcessRunStatus.TERMINATION_FAILED:
            return (
                DocxToPdfConversionStatus.PROCESS_TERMINATION_FAILED,
                "PROCESS_TERMINATION_FAILED",
                run_result.diagnostics,
                None,
                None,
            )

        # ProcessRunStatus.COMPLETED
        if run_result.exit_code != 0:
            return (
                DocxToPdfConversionStatus.CONVERSION_FAILED,
                "CONVERSION_FAILED",
                run_result.diagnostics,
                None,
                None,
            )

        read_result = read_pdf_output_securely(
            workspace.output_pdf_path, max_pdf_size_bytes=self._max_pdf_size_bytes
        )
        return _map_pdf_read_result(read_result)

    def _setup_workspace(self) -> _Workspace | None:
        try:
            root_str = tempfile.mkdtemp(dir=self._scratch_root)
        except OSError:
            return None

        root = Path(root_str)
        try:
            os.chmod(root, 0o700)
            if root.is_symlink():
                return None
            if root.parent != self._scratch_root:
                return None

            root_lstat = os.lstat(root)
            root_identity = (root_lstat.st_dev, root_lstat.st_ino)

            in_dir = root / "in"
            out_dir = root / "out"
            profile_dir = root / "profile"
            for sub_dir in (in_dir, out_dir, profile_dir):
                sub_dir.mkdir(mode=0o700)

            return _Workspace(
                root=root,
                in_dir=in_dir,
                out_dir=out_dir,
                profile_dir=profile_dir,
                input_docx_path=in_dir / "input.docx",
                output_pdf_path=out_dir / "input.pdf",
                root_identity=root_identity,
            )
        except OSError:
            self._best_effort_remove(root)
            return None

    @staticmethod
    def _best_effort_remove(root: Path) -> None:
        try:
            if not root.is_symlink() and shutil.rmtree.avoids_symlink_attacks:
                shutil.rmtree(root)
        except OSError:
            pass

    def _cleanup_workspace(self, workspace: _Workspace | None) -> WorkspaceCleanupResult:
        if workspace is None:
            return WorkspaceCleanupResult(status=WorkspaceCleanupStatus.CLEAN)

        try:
            if workspace.root.is_symlink():
                return WorkspaceCleanupResult(
                    status=WorkspaceCleanupStatus.CLEANUP_FAILED,
                    diagnostics=("workspace root is a symlink at cleanup time",),
                )
            current_lstat = os.lstat(workspace.root)
        except OSError as exc:
            return WorkspaceCleanupResult(
                status=WorkspaceCleanupStatus.CLEANUP_FAILED,
                diagnostics=(f"workspace root lstat failed at cleanup time: {exc}",),
            )

        if (current_lstat.st_dev, current_lstat.st_ino) != workspace.root_identity:
            return WorkspaceCleanupResult(
                status=WorkspaceCleanupStatus.CLEANUP_FAILED,
                diagnostics=("workspace root identity changed before cleanup",),
            )
        if workspace.root.parent != self._scratch_root:
            return WorkspaceCleanupResult(
                status=WorkspaceCleanupStatus.CLEANUP_FAILED,
                diagnostics=("workspace root is no longer a direct child of scratch_root",),
            )
        if not shutil.rmtree.avoids_symlink_attacks:
            return WorkspaceCleanupResult(
                status=WorkspaceCleanupStatus.CLEANUP_FAILED,
                diagnostics=("platform shutil.rmtree cannot avoid symlink attacks",),
            )

        try:
            shutil.rmtree(workspace.root)
        except OSError as exc:
            return WorkspaceCleanupResult(
                status=WorkspaceCleanupStatus.CLEANUP_FAILED,
                diagnostics=(f"rmtree failed: {exc}",),
            )

        if workspace.root.exists():
            return WorkspaceCleanupResult(
                status=WorkspaceCleanupStatus.CLEANUP_FAILED,
                diagnostics=("workspace root still present after rmtree",),
            )

        return WorkspaceCleanupResult(status=WorkspaceCleanupStatus.CLEAN)


def _map_pdf_read_result(
    read_result: SecurePdfReadResult,
) -> tuple[DocxToPdfConversionStatus, str | None, tuple[str, ...], bytes | None, str | None]:
    if read_result.status == SecurePdfReadStatus.OK:
        return (
            DocxToPdfConversionStatus.SUCCEEDED,
            None,
            read_result.diagnostics,
            read_result.pdf_bytes,
            read_result.pdf_sha256,
        )

    mapping = {
        SecurePdfReadStatus.MISSING: DocxToPdfConversionStatus.PDF_OUTPUT_MISSING,
        SecurePdfReadStatus.SYMLINK: DocxToPdfConversionStatus.PDF_OUTPUT_INVALID,
        SecurePdfReadStatus.NOT_REGULAR_FILE: DocxToPdfConversionStatus.PDF_OUTPUT_INVALID,
        SecurePdfReadStatus.TOO_LARGE: DocxToPdfConversionStatus.PDF_OUTPUT_TOO_LARGE,
        SecurePdfReadStatus.UNREADABLE: DocxToPdfConversionStatus.PDF_OUTPUT_UNREADABLE,
        SecurePdfReadStatus.LOCKED: DocxToPdfConversionStatus.PDF_OUTPUT_LOCKED,
        SecurePdfReadStatus.CHANGED_DURING_READ: DocxToPdfConversionStatus.PDF_OUTPUT_CHANGED_DURING_READ,
        SecurePdfReadStatus.INVALID_PDF_STRUCTURE: DocxToPdfConversionStatus.PDF_OUTPUT_INVALID,
    }
    status = mapping[read_result.status]
    return (status, status.value, read_result.diagnostics, None, None)
