"""Explicit Provenance Stage P6-B2b-A: platform stable read of
``current.docx``.

A stable read must observe bytes that were never concurrently mutated
while being read -- never trusted from a single ``read()`` call alone.
POSIX (``posix_stable_read``) re-``lstat``/``fstat``s the file before and
after reading and retries (bounded, 5 attempts) on any path replacement or
metadata change. Windows (``windows_stable_read``) instead opens the file
with ``FILE_SHARE_READ`` only (no ``FILE_SHARE_WRITE``, no
``FILE_SHARE_DELETE``) via ``CreateFileW``, so the OS itself refuses to let
a writer open the file concurrently -- a sharing violation maps to
``WORKING_COPY_LOCKED`` rather than being retried.

Neither function ever accepts a caller-supplied file handle/descriptor,
and neither ever trusts ``mtime`` alone as proof of stability -- POSIX
additionally compares ``st_dev``/``st_ino``/``st_size``/``st_ctime_ns``,
Windows additionally compares the file's ``FileId`` (or, as a fallback,
``BY_HANDLE_FILE_INFORMATION``) between two independently opened handles.

``windows_stable_read`` never touches ``ctypes.WinDLL`` directly -- every
Win32 call goes through a small ``_Win32ReadApi`` protocol.
``_CtypesWin32ReadApi`` is the only implementation used in production and
is only ever constructed on ``win32``. Tests on non-Windows platforms
inject a fake implementing the same protocol, so the share-mode/identity
verification *logic* in ``windows_stable_read`` runs for real under test
-- only the underlying syscalls are faked.
"""

from __future__ import annotations

import errno
import hashlib
import os
import stat
import sys
from pathlib import Path
from typing import Protocol

from app.schemas.cv_document_working_copy import (
    WorkingCopyStableReadResult,
    WorkingCopyStableReadStatus,
)


_MAX_STABLE_READ_ATTEMPTS = 5


def _ok(data: bytes) -> WorkingCopyStableReadResult:
    return WorkingCopyStableReadResult(
        status=WorkingCopyStableReadStatus.STABLE_READ_OK,
        data=data,
        sha256=hashlib.sha256(data).hexdigest(),
        byte_size=len(data),
    )


def _closed(status: WorkingCopyStableReadStatus, *diagnostics: str) -> WorkingCopyStableReadResult:
    return WorkingCopyStableReadResult(status=status, diagnostics=tuple(diagnostics))


# -- POSIX ---------------------------------------------------------------------


def _read_exact(fd: int, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        chunk = os.read(fd, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def posix_stable_read(path: Path, *, max_size_bytes: int) -> WorkingCopyStableReadResult:
    """POSIX stable read: up to 5 attempts, each re-verifying
    ``lstat``-before/``fstat``-after identity and byte count before
    trusting the read -- see module docstring for the full sequence."""

    for _attempt in range(_MAX_STABLE_READ_ATTEMPTS):
        try:
            lstat_before = os.lstat(path)
        except FileNotFoundError:
            return _closed(WorkingCopyStableReadStatus.WORKING_COPY_MISSING, "current.docx does not exist")
        except OSError as exc:
            return _closed(WorkingCopyStableReadStatus.WORKING_COPY_UNREADABLE, f"lstat failed: {exc}")

        if stat.S_ISLNK(lstat_before.st_mode):
            return _closed(WorkingCopyStableReadStatus.WORKING_COPY_UNREADABLE, "current.docx is a symlink")

        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return _closed(WorkingCopyStableReadStatus.WORKING_COPY_MISSING, "current.docx does not exist")
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                continue  # a symlink raced in between lstat and open -- retry
            return _closed(WorkingCopyStableReadStatus.WORKING_COPY_UNREADABLE, f"open failed: {exc}")

        try:
            fstat_before = os.fstat(fd)
            if fstat_before.st_dev != lstat_before.st_dev or fstat_before.st_ino != lstat_before.st_ino:
                continue  # path was replaced between lstat and open -- retry

            if fstat_before.st_size > max_size_bytes:
                return _closed(WorkingCopyStableReadStatus.WORKING_COPY_UNREADABLE, "current.docx exceeds max_size_bytes")

            data = _read_exact(fd, fstat_before.st_size)
            fstat_after = os.fstat(fd)
        finally:
            os.close(fd)

        if len(data) != fstat_before.st_size:
            continue  # short read -- file shrank mid-read; retry

        try:
            lstat_after = os.lstat(path)
        except OSError:
            continue

        if stat.S_ISLNK(lstat_after.st_mode):
            continue

        stable = (
            lstat_after.st_dev == fstat_after.st_dev
            and lstat_after.st_ino == fstat_after.st_ino
            and lstat_after.st_size == fstat_after.st_size
            and lstat_after.st_size == len(data)
            and lstat_after.st_mtime_ns == fstat_after.st_mtime_ns
            and lstat_after.st_ctime_ns == fstat_after.st_ctime_ns
            and fstat_after.st_dev == fstat_before.st_dev
            and fstat_after.st_ino == fstat_before.st_ino
        )
        if stable:
            return _ok(data)
        # metadata changed during the read -- retry

    return _closed(
        WorkingCopyStableReadStatus.WORKING_COPY_CHANGED_DURING_READ,
        f"current.docx changed during read after {_MAX_STABLE_READ_ATTEMPTS} attempts",
    )


# -- Windows ---------------------------------------------------------------------

_INVALID_HANDLE = -1

_GENERIC_READ = 0x80000000
_FILE_SHARE_READ = 0x00000001
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_NORMAL = 0x80
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_ERROR_FILE_NOT_FOUND = 2
_ERROR_PATH_NOT_FOUND = 3
_ERROR_SHARING_VIOLATION = 32
_ERROR_LOCK_VIOLATION = 33

#: The exact ``CreateFileW`` access/share mode a stable read always uses:
#: read-only, shared with other readers, but never with a writer and never
#: with a deleter.
_READ_ACCESS = _GENERIC_READ
_READ_SHARE_MODE = _FILE_SHARE_READ
_READ_FLAGS = _FILE_ATTRIBUTE_NORMAL | _FILE_FLAG_OPEN_REPARSE_POINT


class _Win32ReadApi(Protocol):
    """The exact Win32 surface ``windows_stable_read`` needs.
    ``_CtypesWin32ReadApi`` is the only production implementation; tests on
    non-Windows platforms supply a fake implementing this same protocol so
    the share-mode/identity logic below runs unmodified."""

    def create_file(
        self, path: str, desired_access: int, share_mode: int, creation_disposition: int, flags_and_attributes: int
    ) -> int: ...

    def get_last_error(self) -> int: ...

    def close_handle(self, handle: int) -> None: ...

    def get_file_attributes(self, path: str) -> int: ...

    def get_file_size(self, handle: int) -> int | None: ...

    def read_file_chunk(self, handle: int, max_bytes: int) -> bytes | None: ...

    def get_file_id_info(self, handle: int) -> tuple[int, bytes] | None: ...

    def get_by_handle_file_information(self, handle: int) -> tuple[int, int, int]: ...


def _read_exact_win32(api: _Win32ReadApi, handle: int, size: int) -> bytes | None:
    """Loops ``read_file_chunk`` until exactly ``size`` bytes are
    assembled, a short/EOF chunk is seen, or a chunk read fails -- the same
    "compose partial reads into exact bytes" contract ``_read_exact``
    enforces for the POSIX backend above."""

    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        chunk = api.read_file_chunk(handle, remaining)
        if chunk is None:
            return None
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def windows_stable_read(
    path: Path, *, max_size_bytes: int, api: _Win32ReadApi | None = None
) -> WorkingCopyStableReadResult:
    """Windows stable read: ``CreateFileW`` with ``FILE_SHARE_READ`` only
    (excludes ``FILE_SHARE_WRITE``/``FILE_SHARE_DELETE``), so an existing
    writable handle or a concurrent writer causes a sharing violation
    mapped to ``WORKING_COPY_LOCKED`` -- never silently retried as though
    it were a transient condition. Exactly one ``CreateFileW`` ->
    ``ReadFile`` -> ``CloseHandle`` sequence; never
    ``msvcrt.open_osfhandle``/``os.fdopen`` on the raw ``HANDLE``.

    ``api`` defaults to the real ``ctypes``-backed implementation, which is
    only ever constructed on ``win32``. Passing a fake ``api`` lets this
    exact function run its full share-mode/identity logic under test on
    any platform.
    """

    if api is None:
        if sys.platform != "win32":  # pragma: no cover - guarded by caller
            raise RuntimeError("windows_stable_read requires an explicit api on a non-win32 platform")
        api = _CtypesWin32ReadApi()

    path_str = str(path)

    handle = api.create_file(path_str, _READ_ACCESS, _READ_SHARE_MODE, _OPEN_EXISTING, _READ_FLAGS)
    if handle == _INVALID_HANDLE:
        err = api.get_last_error()
        if err in (_ERROR_FILE_NOT_FOUND, _ERROR_PATH_NOT_FOUND):
            return _closed(WorkingCopyStableReadStatus.WORKING_COPY_MISSING, "current.docx does not exist")
        if err in (_ERROR_SHARING_VIOLATION, _ERROR_LOCK_VIOLATION):
            return _closed(WorkingCopyStableReadStatus.WORKING_COPY_LOCKED, "current.docx is open elsewhere")
        return _closed(WorkingCopyStableReadStatus.WORKING_COPY_UNREADABLE, f"CreateFileW failed (WinError {err})")

    try:
        attrs = api.get_file_attributes(path_str)
        if attrs != 0xFFFFFFFF and (attrs & _FILE_ATTRIBUTE_REPARSE_POINT):
            return _closed(WorkingCopyStableReadStatus.WORKING_COPY_UNREADABLE, "current.docx is a reparse point")

        file_id_before = _get_file_identity(api, handle)

        size = api.get_file_size(handle)
        if size is None:
            return _closed(WorkingCopyStableReadStatus.WORKING_COPY_UNREADABLE, "GetFileSizeEx failed")
        if size > max_size_bytes:
            return _closed(WorkingCopyStableReadStatus.WORKING_COPY_UNREADABLE, "current.docx exceeds max_size_bytes")

        data = _read_exact_win32(api, handle, size)
        if data is None or len(data) != size:
            return _closed(
                WorkingCopyStableReadStatus.WORKING_COPY_UNREADABLE, "ReadFile did not return the expected byte count"
            )

        size_after = api.get_file_size(handle)
        if size_after is None:
            return _closed(WorkingCopyStableReadStatus.WORKING_COPY_UNREADABLE, "GetFileSizeEx (post-read) failed")
        if size_after != size:
            return _closed(WorkingCopyStableReadStatus.WORKING_COPY_CHANGED_DURING_READ, "current.docx size changed during read")

        file_id_after = _get_file_identity(api, handle)
        if file_id_before != file_id_after:
            return _closed(WorkingCopyStableReadStatus.WORKING_COPY_CHANGED_DURING_READ, "current.docx identity changed during read")

        probe_handle = api.create_file(path_str, _READ_ACCESS, _READ_SHARE_MODE, _OPEN_EXISTING, _READ_FLAGS)
        if probe_handle == _INVALID_HANDLE:
            return _closed(WorkingCopyStableReadStatus.WORKING_COPY_CHANGED_DURING_READ, "path-probe open failed after read")
        try:
            probe_identity = _get_file_identity(api, probe_handle)
        finally:
            api.close_handle(probe_handle)

        if probe_identity != file_id_after:
            return _closed(WorkingCopyStableReadStatus.WORKING_COPY_CHANGED_DURING_READ, "path-probe identity does not match read handle")

        return _ok(data)
    finally:
        api.close_handle(handle)


def _get_file_identity(api: _Win32ReadApi, handle: int) -> tuple:
    """Prefer ``FileIdInfo`` (64-bit volume serial + 128-bit file ID); fall
    back to the ``BY_HANDLE_FILE_INFORMATION`` volume-serial/file-index
    triple only when ``FileIdInfo`` is unavailable."""

    file_id = api.get_file_id_info(handle)
    if file_id is not None:
        return file_id
    return api.get_by_handle_file_information(handle)


class _CtypesWin32ReadApi:
    """The only production ``_Win32ReadApi`` implementation -- thin ctypes
    bindings over ``kernel32``, constructed only on ``win32``."""

    def __init__(self) -> None:
        if sys.platform != "win32":  # pragma: no cover - guarded by callers
            raise RuntimeError("_CtypesWin32ReadApi is only usable on win32")
        import ctypes

        self._ctypes = ctypes
        from ctypes import wintypes

        self._wintypes = wintypes
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    def create_file(
        self, path: str, desired_access: int, share_mode: int, creation_disposition: int, flags_and_attributes: int
    ) -> int:
        ctypes = self._ctypes
        wintypes = self._wintypes
        CreateFileW = self._kernel32.CreateFileW
        CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        CreateFileW.restype = wintypes.HANDLE
        handle = CreateFileW(path, desired_access, share_mode, None, creation_disposition, flags_and_attributes, None)
        invalid = wintypes.HANDLE(-1).value
        return _INVALID_HANDLE if handle == invalid else handle

    def get_last_error(self) -> int:
        return self._ctypes.get_last_error()

    def close_handle(self, handle: int) -> None:
        self._kernel32.CloseHandle(handle)

    def get_file_attributes(self, path: str) -> int:
        return self._kernel32.GetFileAttributesW(path)

    def get_file_size(self, handle: int) -> int | None:
        ctypes = self._ctypes
        wintypes = self._wintypes
        file_size = wintypes.LARGE_INTEGER()
        if not self._kernel32.GetFileSizeEx(handle, ctypes.byref(file_size)):
            return None
        return file_size.value

    def read_file_chunk(self, handle: int, max_bytes: int) -> bytes | None:
        ctypes = self._ctypes
        wintypes = self._wintypes
        buffer = ctypes.create_string_buffer(max_bytes)
        bytes_read = wintypes.DWORD(0)
        ok = self._kernel32.ReadFile(handle, buffer, max_bytes, ctypes.byref(bytes_read), None)
        if not ok:
            return None
        return buffer.raw[: bytes_read.value]

    def get_file_id_info(self, handle: int) -> tuple[int, bytes] | None:
        ctypes = self._ctypes

        class FILE_ID_INFO(ctypes.Structure):
            _fields_ = [("VolumeSerialNumber", ctypes.c_ulonglong), ("FileId", ctypes.c_byte * 16)]

        FileIdInfo = 18  # FILE_INFO_BY_HANDLE_CLASS.FileIdInfo
        GetFileInformationByHandleEx = getattr(self._kernel32, "GetFileInformationByHandleEx", None)
        if GetFileInformationByHandleEx is None:
            return None
        info = FILE_ID_INFO()
        ok = GetFileInformationByHandleEx(handle, FileIdInfo, ctypes.byref(info), ctypes.sizeof(info))
        if not ok:
            return None
        return (info.VolumeSerialNumber, bytes(info.FileId))

    def get_by_handle_file_information(self, handle: int) -> tuple[int, int, int]:
        ctypes = self._ctypes
        wintypes = self._wintypes

        class BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("dwFileAttributes", wintypes.DWORD),
                ("ftCreationTime", wintypes.FILETIME),
                ("ftLastAccessTime", wintypes.FILETIME),
                ("ftLastWriteTime", wintypes.FILETIME),
                ("dwVolumeSerialNumber", wintypes.DWORD),
                ("nFileSizeHigh", wintypes.DWORD),
                ("nFileSizeLow", wintypes.DWORD),
                ("nNumberOfLinks", wintypes.DWORD),
                ("nFileIndexHigh", wintypes.DWORD),
                ("nFileIndexLow", wintypes.DWORD),
            ]

        info = BY_HANDLE_FILE_INFORMATION()
        self._kernel32.GetFileInformationByHandle(handle, ctypes.byref(info))
        return (info.dwVolumeSerialNumber, info.nFileIndexHigh, info.nFileIndexLow)
