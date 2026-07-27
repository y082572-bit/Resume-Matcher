"""Unit tests for executable identity observation and secure PDF output
read (``app.services.cv_document_secure_pdf_reader``)."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

import pytest

from app.services import cv_document_secure_pdf_reader as reader_module
from app.services.cv_document_secure_pdf_reader import (
    ExecutableIdentityObservationStatus,
    SecurePdfReadStatus,
    observe_executable_identity,
    read_pdf_output_securely,
)


pytestmark = pytest.mark.unit


_VALID_PDF = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n%%EOF"


# -- Executable identity observation ------------------------------------------------


def test_observe_executable_identity_rejects_relative_path() -> None:
    result = observe_executable_identity(Path("relative/soffice"))
    assert result.status == ExecutableIdentityObservationStatus.NOT_ABSOLUTE


def test_observe_executable_identity_rejects_missing_file(tmp_path: Path) -> None:
    result = observe_executable_identity(tmp_path / "does-not-exist")
    assert result.status == ExecutableIdentityObservationStatus.MISSING


def test_observe_executable_identity_rejects_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real-soffice"
    real.write_bytes(b"#!/bin/sh\necho fake\n")
    link = tmp_path / "soffice-link"
    link.symlink_to(real)

    result = observe_executable_identity(link)
    assert result.status == ExecutableIdentityObservationStatus.SYMLINK


def test_observe_executable_identity_rejects_non_regular_file(tmp_path: Path) -> None:
    fifo_path = tmp_path / "a-fifo"
    os.mkfifo(fifo_path)
    try:
        result = observe_executable_identity(fifo_path)
        assert result.status == ExecutableIdentityObservationStatus.NOT_REGULAR_FILE
    finally:
        fifo_path.unlink()


def test_observe_executable_identity_computes_exact_sha256(tmp_path: Path) -> None:
    executable = tmp_path / "soffice"
    payload = b"binary-content-for-hashing" * 100
    executable.write_bytes(payload)

    result = observe_executable_identity(executable)

    assert result.status == ExecutableIdentityObservationStatus.OK
    assert result.sha256 == hashlib.sha256(payload).hexdigest()
    assert result.canonical_path == str(executable)
    assert result.size_bytes == len(payload)


def test_observe_executable_identity_detects_final_path_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "soffice"
    executable.write_bytes(b"original-content")

    real_lstat = os.lstat
    call_count = {"n": 0}

    def _flaky_lstat(path: object, *args: object, **kwargs: object) -> os.stat_result:
        call_count["n"] += 1
        # The final lstat call (the second one against `executable`) reports
        # replaced content-bearing stats by pointing at a distinct temp file.
        if call_count["n"] >= 2 and str(path) == str(executable):
            replaced = tmp_path / "replaced"
            if not replaced.exists():
                replaced.write_bytes(b"different-content-longer")
            return real_lstat(replaced)
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(reader_module.os, "lstat", _flaky_lstat)

    result = observe_executable_identity(executable)
    assert result.status == ExecutableIdentityObservationStatus.IDENTITY_CHANGED_DURING_READ


def test_observe_executable_identity_closes_handle_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "soffice"
    executable.write_bytes(b"content")

    close_calls: list[int] = []
    real_close = os.close

    def _counting_close(fd: int) -> None:
        close_calls.append(fd)
        real_close(fd)

    monkeypatch.setattr(reader_module.os, "close", _counting_close)

    result = observe_executable_identity(executable)
    assert result.status == ExecutableIdentityObservationStatus.OK
    assert len(close_calls) == 1


# -- Secure PDF output read ---------------------------------------------------------


def test_read_pdf_output_securely_missing(tmp_path: Path) -> None:
    result = read_pdf_output_securely(tmp_path / "missing.pdf", max_pdf_size_bytes=1024)
    assert result.status == SecurePdfReadStatus.MISSING


def test_read_pdf_output_securely_rejects_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real.pdf"
    real.write_bytes(_VALID_PDF)
    link = tmp_path / "link.pdf"
    link.symlink_to(real)

    result = read_pdf_output_securely(link, max_pdf_size_bytes=1024)
    assert result.status == SecurePdfReadStatus.SYMLINK


def test_read_pdf_output_securely_rejects_fifo(tmp_path: Path) -> None:
    fifo_path = tmp_path / "output.pdf"
    os.mkfifo(fifo_path)
    try:
        result = read_pdf_output_securely(fifo_path, max_pdf_size_bytes=1024)
        assert result.status == SecurePdfReadStatus.NOT_REGULAR_FILE
    finally:
        fifo_path.unlink()


def test_read_pdf_output_securely_rejects_empty_file(tmp_path: Path) -> None:
    empty = tmp_path / "empty.pdf"
    empty.write_bytes(b"")

    result = read_pdf_output_securely(empty, max_pdf_size_bytes=1024)
    assert result.status == SecurePdfReadStatus.MISSING


def test_read_pdf_output_securely_rejects_too_large(tmp_path: Path) -> None:
    big = tmp_path / "big.pdf"
    big.write_bytes(_VALID_PDF + b"x" * 1024)

    result = read_pdf_output_securely(big, max_pdf_size_bytes=16)
    assert result.status == SecurePdfReadStatus.TOO_LARGE


def test_read_pdf_output_securely_rejects_unreadable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pdf_path = tmp_path / "unreadable.pdf"
    pdf_path.write_bytes(_VALID_PDF)

    real_open = os.open

    def _denied_open(path: object, flags: int, *args: object) -> int:
        if str(path) == str(pdf_path):
            raise PermissionError("simulated EACCES")
        return real_open(path, flags, *args)

    monkeypatch.setattr(reader_module.os, "open", _denied_open)

    result = read_pdf_output_securely(pdf_path, max_pdf_size_bytes=1024)
    assert result.status == SecurePdfReadStatus.UNREADABLE


def test_read_pdf_output_securely_reports_locked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import errno

    pdf_path = tmp_path / "locked.pdf"
    pdf_path.write_bytes(_VALID_PDF)

    real_open = os.open

    def _busy_open(path: object, flags: int, *args: object) -> int:
        if str(path) == str(pdf_path):
            raise OSError(errno.EBUSY, "simulated locked")
        return real_open(path, flags, *args)

    monkeypatch.setattr(reader_module.os, "open", _busy_open)

    result = read_pdf_output_securely(pdf_path, max_pdf_size_bytes=1024)
    assert result.status == SecurePdfReadStatus.LOCKED


def test_read_pdf_output_securely_detects_changed_before_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf_path = tmp_path / "output.pdf"
    pdf_path.write_bytes(_VALID_PDF)

    real_lstat = os.lstat
    call_count = {"n": 0}

    def _flaky_lstat(path: object, *args: object, **kwargs: object) -> os.stat_result:
        call_count["n"] += 1
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(reader_module.os, "lstat", _flaky_lstat)

    result = read_pdf_output_securely(pdf_path, max_pdf_size_bytes=1024)
    assert result.status == SecurePdfReadStatus.OK
    assert call_count["n"] >= 1


def test_read_pdf_output_securely_detects_changed_during_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf_path = tmp_path / "output.pdf"
    pdf_path.write_bytes(_VALID_PDF)

    real_fstat = os.fstat
    call_count = {"n": 0}

    def _flaky_fstat(fd: int) -> os.stat_result:
        call_count["n"] += 1
        result = real_fstat(fd)
        # Every attempt calls os.fstat exactly twice (before/after the
        # read) -- corrupt every "after" call so every attempt observes
        # instability and the bounded retry loop is fully exhausted.
        if call_count["n"] % 2 == 0:
            return os.stat_result(
                (
                    result.st_mode,
                    result.st_ino,
                    result.st_dev,
                    result.st_nlink,
                    result.st_uid,
                    result.st_gid,
                    result.st_size + 1,
                    0,
                    0,
                    0,
                )
            )
        return result

    monkeypatch.setattr(reader_module.os, "fstat", _flaky_fstat)

    result = read_pdf_output_securely(pdf_path, max_pdf_size_bytes=1024)
    assert result.status == SecurePdfReadStatus.CHANGED_DURING_READ


def test_read_pdf_output_securely_rejects_invalid_magic(tmp_path: Path) -> None:
    pdf_path = tmp_path / "invalid.pdf"
    pdf_path.write_bytes(b"NOT-A-PDF-HEADER\n%%EOF")

    result = read_pdf_output_securely(pdf_path, max_pdf_size_bytes=1024)
    assert result.status == SecurePdfReadStatus.INVALID_PDF_STRUCTURE


def test_read_pdf_output_securely_rejects_missing_eof_marker(tmp_path: Path) -> None:
    pdf_path = tmp_path / "no-eof.pdf"
    pdf_path.write_bytes(b"%PDF-1.7\nno trailer here at all")

    result = read_pdf_output_securely(pdf_path, max_pdf_size_bytes=1024)
    assert result.status == SecurePdfReadStatus.INVALID_PDF_STRUCTURE


def test_read_pdf_output_securely_exact_bytes_and_sha256(tmp_path: Path) -> None:
    pdf_path = tmp_path / "output.pdf"
    pdf_path.write_bytes(_VALID_PDF)

    result = read_pdf_output_securely(pdf_path, max_pdf_size_bytes=1024)

    assert result.status == SecurePdfReadStatus.OK
    assert result.pdf_bytes == _VALID_PDF
    assert result.pdf_sha256 == hashlib.sha256(_VALID_PDF).hexdigest()


def test_read_pdf_output_securely_bounded_retry_gives_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf_path = tmp_path / "unstable.pdf"
    pdf_path.write_bytes(_VALID_PDF)

    real_lstat = os.lstat
    attempts = {"n": 0}

    def _always_replaced_lstat(path: object, *args: object, **kwargs: object) -> os.stat_result:
        if str(path) == str(pdf_path):
            attempts["n"] += 1
        return real_lstat(path, *args, **kwargs)

    real_fstat = os.fstat

    def _mismatched_fstat(fd: int) -> os.stat_result:
        result = real_fstat(fd)
        # Always report a different inode/size than the path lstat sees,
        # forcing the "replaced between lstat and open" retry every time.
        return os.stat_result(
            (
                result.st_mode,
                result.st_ino + 999,
                result.st_dev,
                result.st_nlink,
                result.st_uid,
                result.st_gid,
                result.st_size,
                0,
                0,
                0,
            )
        )

    monkeypatch.setattr(reader_module.os, "lstat", _always_replaced_lstat)
    monkeypatch.setattr(reader_module.os, "fstat", _mismatched_fstat)

    result = read_pdf_output_securely(pdf_path, max_pdf_size_bytes=1024)
    assert result.status == SecurePdfReadStatus.CHANGED_DURING_READ
    assert attempts["n"] == 5  # exactly the bounded retry count, never more


def test_read_pdf_output_securely_closes_handle_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf_path = tmp_path / "output.pdf"
    pdf_path.write_bytes(_VALID_PDF)

    close_calls: list[int] = []
    real_close = os.close

    def _counting_close(fd: int) -> None:
        close_calls.append(fd)
        real_close(fd)

    monkeypatch.setattr(reader_module.os, "close", _counting_close)

    result = read_pdf_output_securely(pdf_path, max_pdf_size_bytes=1024)
    assert result.status == SecurePdfReadStatus.OK
    assert len(close_calls) == 1
