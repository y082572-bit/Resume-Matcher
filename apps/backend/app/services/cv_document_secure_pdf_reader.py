"""Explicit Provenance Stage P6-B3a: secure executable identity
observation and secure PDF output read.

Both operations here share the same discipline: never trust a path alone,
never use ``Path.read_bytes``/``realpath``-as-authority, never follow a
symlink, and never accept an optional/skippable SHA-256. Every read opens
a handle with ``O_NOFOLLOW``, verifies the handle is a regular file,
confirms the pre-open ``lstat`` and post-open ``fstat`` describe the same
inode, reads bounded bytes, and re-verifies a final ``lstat`` against the
post-read ``fstat`` (``st_dev``/``st_ino``/``st_size``/``st_mtime_ns``)
before the bytes are ever trusted. Every handle is closed exactly once.

``observe_executable_identity`` computes a full, bounded, chunked SHA-256
of the configured converter executable -- this is the only place this
stage ever computes ``executable_sha256`` for a
``ConverterRuntimeIdentity``. ``read_pdf_output_securely`` performs the
same handle-based discipline against a converter's PDF output, plus a
bounded (5 attempt) retry loop on any observed instability and a minimal
technical validation (``%PDF-`` prefix, ``%%EOF`` trailer) -- never a real
PDF parser, never any inspection of CV content.
"""

from __future__ import annotations

import errno
import hashlib
import os
import stat
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


_MAX_READ_ATTEMPTS = 5
_READ_CHUNK_SIZE = 1024 * 1024

_PDF_MAGIC_PREFIX = b"%PDF-"
_PDF_EOF_MARKER = b"%%EOF"
_PDF_EOF_TAIL_WINDOW = 1024


def _read_exact_fd(fd: int, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        chunk = os.read(fd, min(remaining, _READ_CHUNK_SIZE))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


# -- Executable identity observation ---------------------------------------------


class ExecutableIdentityObservationStatus(StrEnum):
    """The closed set of outcomes of one ``observe_executable_identity``
    call."""

    OK = "OK"
    NOT_ABSOLUTE = "NOT_ABSOLUTE"
    MISSING = "MISSING"
    SYMLINK = "SYMLINK"
    NOT_REGULAR_FILE = "NOT_REGULAR_FILE"
    IDENTITY_CHANGED_DURING_READ = "IDENTITY_CHANGED_DURING_READ"
    UNREADABLE = "UNREADABLE"


@dataclass(frozen=True)
class ExecutableIdentityObservation:
    status: ExecutableIdentityObservationStatus
    canonical_path: str | None = None
    file_identity: str | None = None
    sha256: str | None = None
    size_bytes: int | None = None
    mtime_ns: int | None = None
    diagnostics: tuple[str, ...] = field(default_factory=tuple)


def observe_executable_identity(executable_path: Path) -> ExecutableIdentityObservation:
    """POSIX-only: configured absolute path -> ``lstat`` -> reject symlink
    -> reject non-regular (before ever opening, so a FIFO can never block
    this call) -> ``open(O_RDONLY | O_NOFOLLOW)`` -> ``fstat`` ->
    path/fd identity match -> bounded chunked SHA-256 to EOF -> ``fstat``
    after -> final ``lstat`` -> final path/fd identity match -> close
    exactly once."""

    if not executable_path.is_absolute():
        return ExecutableIdentityObservation(
            status=ExecutableIdentityObservationStatus.NOT_ABSOLUTE,
            diagnostics=("executable_path must be an absolute path",),
        )

    try:
        pre_lstat = os.lstat(executable_path)
    except FileNotFoundError:
        return ExecutableIdentityObservation(
            status=ExecutableIdentityObservationStatus.MISSING,
            diagnostics=("configured executable does not exist",),
        )
    except OSError as exc:
        return ExecutableIdentityObservation(
            status=ExecutableIdentityObservationStatus.UNREADABLE,
            diagnostics=(f"lstat failed: {exc}",),
        )

    if stat.S_ISLNK(pre_lstat.st_mode):
        return ExecutableIdentityObservation(
            status=ExecutableIdentityObservationStatus.SYMLINK,
            diagnostics=("executable_path is a symlink",),
        )
    if not stat.S_ISREG(pre_lstat.st_mode):
        return ExecutableIdentityObservation(
            status=ExecutableIdentityObservationStatus.NOT_REGULAR_FILE,
            diagnostics=("executable_path is not a regular file",),
        )

    try:
        fd = os.open(executable_path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return ExecutableIdentityObservation(
            status=ExecutableIdentityObservationStatus.MISSING,
            diagnostics=("configured executable does not exist",),
        )
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            return ExecutableIdentityObservation(
                status=ExecutableIdentityObservationStatus.SYMLINK,
                diagnostics=("executable_path is a symlink (ELOOP)",),
            )
        return ExecutableIdentityObservation(
            status=ExecutableIdentityObservationStatus.UNREADABLE,
            diagnostics=(f"open failed: {exc}",),
        )

    try:
        fstat_before = os.fstat(fd)
        if not stat.S_ISREG(fstat_before.st_mode):
            return ExecutableIdentityObservation(
                status=ExecutableIdentityObservationStatus.NOT_REGULAR_FILE,
                diagnostics=("executable_path handle is not a regular file",),
            )
        if fstat_before.st_dev != pre_lstat.st_dev or fstat_before.st_ino != pre_lstat.st_ino:
            return ExecutableIdentityObservation(
                status=ExecutableIdentityObservationStatus.IDENTITY_CHANGED_DURING_READ,
                diagnostics=("executable_path was replaced between lstat and open",),
            )

        hasher = hashlib.sha256()
        while True:
            try:
                chunk = os.read(fd, _READ_CHUNK_SIZE)
            except OSError as exc:
                return ExecutableIdentityObservation(
                    status=ExecutableIdentityObservationStatus.UNREADABLE,
                    diagnostics=(f"read failed: {exc}",),
                )
            if not chunk:
                break
            hasher.update(chunk)

        fstat_after = os.fstat(fd)

        try:
            final_lstat = os.lstat(executable_path)
        except OSError as exc:
            return ExecutableIdentityObservation(
                status=ExecutableIdentityObservationStatus.IDENTITY_CHANGED_DURING_READ,
                diagnostics=(f"final lstat failed: {exc}",),
            )

        if stat.S_ISLNK(final_lstat.st_mode):
            return ExecutableIdentityObservation(
                status=ExecutableIdentityObservationStatus.IDENTITY_CHANGED_DURING_READ,
                diagnostics=("executable_path became a symlink during read",),
            )

        identity_stable = (
            final_lstat.st_dev == fstat_after.st_dev
            and final_lstat.st_ino == fstat_after.st_ino
            and final_lstat.st_size == fstat_after.st_size
            and final_lstat.st_mtime_ns == fstat_after.st_mtime_ns
            and fstat_after.st_dev == fstat_before.st_dev
            and fstat_after.st_ino == fstat_before.st_ino
        )
        if not identity_stable:
            return ExecutableIdentityObservation(
                status=ExecutableIdentityObservationStatus.IDENTITY_CHANGED_DURING_READ,
                diagnostics=("executable identity changed during hashing",),
            )

        file_identity = f"{fstat_after.st_dev}:{fstat_after.st_ino}"
        return ExecutableIdentityObservation(
            status=ExecutableIdentityObservationStatus.OK,
            canonical_path=str(executable_path),
            file_identity=file_identity,
            sha256=hasher.hexdigest(),
            size_bytes=fstat_after.st_size,
            mtime_ns=fstat_after.st_mtime_ns,
        )
    finally:
        os.close(fd)


# -- Secure PDF output read --------------------------------------------------


class SecurePdfReadStatus(StrEnum):
    """The closed set of outcomes of one ``read_pdf_output_securely``
    call."""

    OK = "OK"
    MISSING = "MISSING"
    SYMLINK = "SYMLINK"
    NOT_REGULAR_FILE = "NOT_REGULAR_FILE"
    TOO_LARGE = "TOO_LARGE"
    UNREADABLE = "UNREADABLE"
    LOCKED = "LOCKED"
    CHANGED_DURING_READ = "CHANGED_DURING_READ"
    INVALID_PDF_STRUCTURE = "INVALID_PDF_STRUCTURE"


@dataclass(frozen=True)
class SecurePdfReadResult:
    status: SecurePdfReadStatus
    pdf_bytes: bytes | None = None
    pdf_sha256: str | None = None
    diagnostics: tuple[str, ...] = field(default_factory=tuple)


def read_pdf_output_securely(pdf_path: Path, *, max_pdf_size_bytes: int) -> SecurePdfReadResult:
    """Bounded (``_MAX_READ_ATTEMPTS``) handle-based secure PDF output
    read. Never uses a PDF parser and never inspects CV content -- only
    the ``%PDF-`` prefix and a trailing ``%%EOF`` marker are checked."""

    last_diagnostics: tuple[str, ...] = ("PDF output unstable after bounded retries",)

    for _attempt in range(_MAX_READ_ATTEMPTS):
        try:
            pre_lstat = os.lstat(pdf_path)
        except FileNotFoundError:
            return SecurePdfReadResult(
                status=SecurePdfReadStatus.MISSING, diagnostics=("PDF output does not exist",)
            )
        except OSError as exc:
            return SecurePdfReadResult(
                status=SecurePdfReadStatus.UNREADABLE, diagnostics=(f"lstat failed: {exc}",)
            )

        if stat.S_ISLNK(pre_lstat.st_mode):
            return SecurePdfReadResult(
                status=SecurePdfReadStatus.SYMLINK, diagnostics=("PDF output is a symlink",)
            )
        if not stat.S_ISREG(pre_lstat.st_mode):
            return SecurePdfReadResult(
                status=SecurePdfReadStatus.NOT_REGULAR_FILE,
                diagnostics=("PDF output is not a regular file",),
            )

        try:
            fd = os.open(pdf_path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return SecurePdfReadResult(
                status=SecurePdfReadStatus.MISSING, diagnostics=("PDF output does not exist",)
            )
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                last_diagnostics = ("PDF output became a symlink (ELOOP)",)
                continue
            if exc.errno in (errno.EACCES, errno.EPERM):
                return SecurePdfReadResult(
                    status=SecurePdfReadStatus.UNREADABLE, diagnostics=(f"open failed: {exc}",)
                )
            if exc.errno in (errno.EBUSY, errno.EAGAIN):
                return SecurePdfReadResult(
                    status=SecurePdfReadStatus.LOCKED, diagnostics=(f"open failed: {exc}",)
                )
            return SecurePdfReadResult(
                status=SecurePdfReadStatus.UNREADABLE, diagnostics=(f"open failed: {exc}",)
            )

        try:
            fstat_before = os.fstat(fd)
            if not stat.S_ISREG(fstat_before.st_mode):
                return SecurePdfReadResult(
                    status=SecurePdfReadStatus.NOT_REGULAR_FILE,
                    diagnostics=("PDF output handle is not a regular file",),
                )
            if fstat_before.st_dev != pre_lstat.st_dev or fstat_before.st_ino != pre_lstat.st_ino:
                last_diagnostics = ("PDF output was replaced between lstat and open",)
                continue

            size = fstat_before.st_size
            if size == 0:
                return SecurePdfReadResult(
                    status=SecurePdfReadStatus.MISSING, diagnostics=("PDF output is empty",)
                )
            if size > max_pdf_size_bytes:
                return SecurePdfReadResult(
                    status=SecurePdfReadStatus.TOO_LARGE,
                    diagnostics=("PDF output exceeds max_pdf_size_bytes",),
                )

            try:
                data = _read_exact_fd(fd, size)
            except OSError as exc:
                return SecurePdfReadResult(
                    status=SecurePdfReadStatus.UNREADABLE, diagnostics=(f"read failed: {exc}",)
                )
            if len(data) != size:
                last_diagnostics = ("short read while reading PDF output",)
                continue

            fstat_after = os.fstat(fd)
        finally:
            os.close(fd)

        try:
            final_lstat = os.lstat(pdf_path)
        except OSError:
            last_diagnostics = ("final lstat failed after read",)
            continue

        if stat.S_ISLNK(final_lstat.st_mode):
            last_diagnostics = ("PDF output became a symlink after read",)
            continue

        stable = (
            final_lstat.st_dev == fstat_after.st_dev
            and final_lstat.st_ino == fstat_after.st_ino
            and final_lstat.st_size == fstat_after.st_size
            and final_lstat.st_size == len(data)
            and final_lstat.st_mtime_ns == fstat_after.st_mtime_ns
            and fstat_after.st_dev == fstat_before.st_dev
            and fstat_after.st_ino == fstat_before.st_ino
        )
        if not stable:
            last_diagnostics = ("PDF output changed during read",)
            continue

        if not data.startswith(_PDF_MAGIC_PREFIX):
            return SecurePdfReadResult(
                status=SecurePdfReadStatus.INVALID_PDF_STRUCTURE,
                diagnostics=("PDF output missing %PDF- prefix",),
            )
        if _PDF_EOF_MARKER not in data[-_PDF_EOF_TAIL_WINDOW:]:
            return SecurePdfReadResult(
                status=SecurePdfReadStatus.INVALID_PDF_STRUCTURE,
                diagnostics=("PDF output missing %%EOF trailer",),
            )

        return SecurePdfReadResult(
            status=SecurePdfReadStatus.OK,
            pdf_bytes=data,
            pdf_sha256=hashlib.sha256(data).hexdigest(),
        )

    return SecurePdfReadResult(
        status=SecurePdfReadStatus.CHANGED_DURING_READ, diagnostics=last_diagnostics
    )
