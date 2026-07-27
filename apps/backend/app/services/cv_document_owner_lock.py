"""Explicit Provenance Stage P6-B2b-A: per-owner cross-process lock.

``PlatformOwnerLock`` is the only serialization primitive the Working Copy
Foundation uses to guard one owner's ``current.docx``/``binding.json``
pair against concurrent writers -- in this process and across processes.
It never depends on ``filelock``/``portalocker``; POSIX uses
``fcntl.flock`` on a dedicated lock file opened with ``O_NOFOLLOW``,
Windows uses ``CreateFileW`` + ``LockFileEx`` through ``ctypes`` (no
``msvcrt.open_osfhandle``, no ``os.fdopen`` on the raw ``HANDLE``).

``acquire`` returns a closed ``OwnerLockAcquireResult``. Only
``ACQUIRED`` ever carries an ``OwnerLockToken`` -- a capability object this
module creates and tracks in an in-memory registry keyed by a
monotonically increasing acquisition ID. ``validate_active_token`` never
trusts a token by its field values alone: it requires the *exact* object
this lock instance handed out for that acquisition ID to still be
registered (never released, never a forged/copied lookalike, never issued
by a different ``PlatformOwnerLock`` instance).

The Windows backend (``_windows_acquire``/``_windows_release``) never
touches ``ctypes.WinDLL`` directly -- all Win32 calls go through a small
``_Win32LockApi`` protocol. ``_CtypesWin32LockApi`` is the only
implementation used in production and is only ever constructed on
``win32``. Tests on non-Windows platforms inject a fake implementing the
same protocol, so the identity-verification and locking *logic* in
``_windows_acquire``/``_windows_release`` runs for real under test -- only
the underlying syscalls are faked.
"""

from __future__ import annotations

import errno
import itertools
import os
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.schemas.cv_document_working_copy import OwnerLockAcquireStatus


_POLL_INTERVAL_SECONDS = 0.02


class OwnerLockToken:
    """An opaque capability created only by ``PlatformOwnerLock.acquire``.
    Deliberately carries no ``__eq__``/``__hash__`` override -- identity
    (``is``) is the only notion of equality that matters here, so a forged
    or copied token with matching field values can never pass
    ``validate_active_token``."""

    __slots__ = ("_lock_instance_id", "_acquisition_id", "_owner_key_fingerprint")

    def __init__(self, *, lock_instance_id: int, acquisition_id: int, owner_key_fingerprint: str) -> None:
        self._lock_instance_id = lock_instance_id
        self._acquisition_id = acquisition_id
        self._owner_key_fingerprint = owner_key_fingerprint


@dataclass(frozen=True)
class OwnerLockAcquireResult:
    status: OwnerLockAcquireStatus
    token: OwnerLockToken | None = None
    diagnostics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.status == OwnerLockAcquireStatus.ACQUIRED:
            if self.token is None:
                raise ValueError("ACQUIRED requires a token")
        elif self.token is not None:
            raise ValueError(f"{self.status} must carry no token")


@dataclass
class _ActiveAcquisition:
    token: OwnerLockToken
    owner_key_fingerprint: str
    fd: int | None = None
    handle: int | None = None


_acquisition_id_counter = itertools.count(1)
_lock_instance_id_counter = itertools.count(1)


class PlatformOwnerLock:
    """A single-use, per-owner cross-process lock bound to one
    ``lock_path``. One instance is acquired exactly once and released
    exactly once -- a second ``acquire`` call on an already-held instance
    is an internal contract violation (``RuntimeError``: no nested
    acquisition), never silently allowed."""

    def __init__(self, lock_path: Path, *, owner_key_fingerprint: str) -> None:
        self._lock_path = Path(lock_path)
        self._owner_key_fingerprint = owner_key_fingerprint
        self._instance_id = next(_lock_instance_id_counter)
        self._active: dict[int, _ActiveAcquisition] = {}
        self._current_acquisition_id: int | None = None

    def acquire(self, *, timeout_seconds: float) -> OwnerLockAcquireResult:
        if self._current_acquisition_id is not None:
            raise RuntimeError("PlatformOwnerLock does not support nested acquisition")

        if sys.platform == "win32":
            status, handle, diagnostics = _windows_acquire(self._lock_path, timeout_seconds)
        else:
            status, fd, diagnostics = _posix_acquire(self._lock_path, timeout_seconds)
            handle = None

        if status != OwnerLockAcquireStatus.ACQUIRED:
            return OwnerLockAcquireResult(status=status, diagnostics=diagnostics)

        acquisition_id = next(_acquisition_id_counter)
        token = OwnerLockToken(
            lock_instance_id=self._instance_id,
            acquisition_id=acquisition_id,
            owner_key_fingerprint=self._owner_key_fingerprint,
        )
        record = _ActiveAcquisition(
            token=token,
            owner_key_fingerprint=self._owner_key_fingerprint,
            fd=fd if sys.platform != "win32" else None,
            handle=handle if sys.platform == "win32" else None,
        )
        self._active[acquisition_id] = record
        self._current_acquisition_id = acquisition_id
        return OwnerLockAcquireResult(status=OwnerLockAcquireStatus.ACQUIRED, token=token)

    def validate_active_token(self, token: object, *, owner_key_fingerprint: str) -> bool:
        """``True`` only if ``token`` is the *exact* object this instance
        registered for its current acquisition, for the expected owner."""

        if not isinstance(token, OwnerLockToken):
            return False
        if self._current_acquisition_id is None:
            return False
        record = self._active.get(self._current_acquisition_id)
        if record is None:
            return False
        if record.token is not token:
            return False
        if token._lock_instance_id != self._instance_id:
            return False
        if record.owner_key_fingerprint != owner_key_fingerprint:
            return False
        return True

    def release(self, token: object) -> None:
        """Idempotent-by-contract: releasing an invalid/already-released
        token is a no-op rather than a raised exception, since ``finally``
        blocks must always be able to call this safely."""

        if self._current_acquisition_id is None:
            return
        record = self._active.get(self._current_acquisition_id)
        if record is None or not isinstance(token, OwnerLockToken) or record.token is not token:
            return

        acquisition_id = self._current_acquisition_id
        try:
            if sys.platform == "win32":
                if record.handle is not None:
                    _windows_release(record.handle)
            else:
                if record.fd is not None:
                    _posix_release(record.fd)
        finally:
            self._active.pop(acquisition_id, None)
            self._current_acquisition_id = None


# -- POSIX ---------------------------------------------------------------------


def _path_identity_matches(lstat_result: os.stat_result, fstat_result: os.stat_result) -> bool:
    return lstat_result.st_dev == fstat_result.st_dev and lstat_result.st_ino == fstat_result.st_ino


def _posix_acquire(
    lock_path: Path, timeout_seconds: float
) -> tuple[OwnerLockAcquireStatus, int | None, tuple[str, ...]]:
    import fcntl

    if lock_path.is_symlink():
        return OwnerLockAcquireStatus.LOCK_PATH_IDENTITY_MISMATCH, None, ("lock path is a symlink",)

    try:
        pre_lstat = os.lstat(lock_path)
    except FileNotFoundError:
        pre_lstat = None
    except OSError:
        return OwnerLockAcquireStatus.LOCK_PATH_IDENTITY_MISMATCH, None, ("lstat of lock path failed",)
    else:
        if stat.S_ISLNK(pre_lstat.st_mode):
            return OwnerLockAcquireStatus.LOCK_PATH_IDENTITY_MISMATCH, None, ("lock path is a symlink",)

    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            return OwnerLockAcquireStatus.LOCK_PATH_IDENTITY_MISMATCH, None, ("lock path is a symlink (ELOOP)",)
        raise

    try:
        fstat_after_open = os.fstat(fd)
        if not stat.S_ISREG(fstat_after_open.st_mode):
            os.close(fd)
            return OwnerLockAcquireStatus.LOCK_PATH_IDENTITY_MISMATCH, None, ("lock path is not a regular file",)
        try:
            post_open_lstat = os.lstat(lock_path)
        except OSError:
            os.close(fd)
            return OwnerLockAcquireStatus.LOCK_PATH_IDENTITY_MISMATCH, None, ("lock path vanished after open",)
        if stat.S_ISLNK(post_open_lstat.st_mode) or not _path_identity_matches(post_open_lstat, fstat_after_open):
            os.close(fd)
            return OwnerLockAcquireStatus.LOCK_PATH_IDENTITY_MISMATCH, None, ("post-open path-vs-fd identity mismatch",)

        deadline = time.monotonic() + timeout_seconds
        acquired = False
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                if time.monotonic() >= deadline:
                    break
                time.sleep(_POLL_INTERVAL_SECONDS)

        if not acquired:
            os.close(fd)
            return OwnerLockAcquireStatus.OWNER_LOCK_TIMEOUT, None, ("timed out waiting for exclusive lock",)

        try:
            post_lock_lstat = os.lstat(lock_path)
            post_lock_fstat = os.fstat(fd)
        except OSError:
            _safe_unlock_close(fd)
            return OwnerLockAcquireStatus.LOCK_PATH_IDENTITY_MISMATCH, None, ("lock path vanished after locking",)

        if stat.S_ISLNK(post_lock_lstat.st_mode):
            _safe_unlock_close(fd)
            return OwnerLockAcquireStatus.LOCK_PATH_IDENTITY_MISMATCH, None, ("lock path became a symlink",)
        if not stat.S_ISREG(post_lock_fstat.st_mode):
            _safe_unlock_close(fd)
            return OwnerLockAcquireStatus.LOCK_PATH_IDENTITY_MISMATCH, None, ("locked fd is not a regular file",)
        if not _path_identity_matches(post_lock_lstat, post_lock_fstat):
            _safe_unlock_close(fd)
            return OwnerLockAcquireStatus.LOCK_PATH_IDENTITY_MISMATCH, None, ("post-lock path-vs-fd identity mismatch",)
        if post_lock_fstat.st_nlink > 1:
            _safe_unlock_close(fd)
            return OwnerLockAcquireStatus.LOCK_PATH_IDENTITY_MISMATCH, None, ("lock path has a hardlink anomaly",)

        return OwnerLockAcquireStatus.ACQUIRED, fd, ()
    except BaseException:
        os.close(fd)
        raise


def _safe_unlock_close(fd: int) -> None:
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        os.close(fd)


def _posix_release(fd: int) -> None:
    _safe_unlock_close(fd)


# -- Windows ---------------------------------------------------------------------

#: The sentinel this module uses in place of the real ``INVALID_HANDLE_VALUE``
#: -- every ``_Win32LockApi.create_file`` implementation (real or fake) must
#: return this exact value instead of a real handle on failure.
_INVALID_HANDLE = -1

_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_OPEN_ALWAYS = 4
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_NORMAL = 0x80
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400

#: The lock file's own handle -- and every path-probe handle opened against
#: the same path -- always excludes ``FILE_SHARE_DELETE``: a lock a caller
#: cannot even unlink out from under is a stronger guarantee than one they
#: can.
_LOCK_ACCESS = _GENERIC_READ | _GENERIC_WRITE
_LOCK_SHARE_MODE = _FILE_SHARE_READ | _FILE_SHARE_WRITE
_LOCK_FLAGS = _FILE_ATTRIBUTE_NORMAL | _FILE_FLAG_OPEN_REPARSE_POINT


@dataclass(frozen=True)
class _ByHandleFileInfo:
    file_attributes: int
    volume_serial_number: int
    file_index_high: int
    file_index_low: int


class _Win32LockApi(Protocol):
    """The exact Win32 surface ``_windows_acquire``/``_windows_release``
    need. ``_CtypesWin32LockApi`` is the only production implementation;
    tests on non-Windows platforms supply a fake implementing this same
    protocol so the acquire/verify/lock *logic* below runs unmodified."""

    def create_file(
        self, path: str, desired_access: int, share_mode: int, creation_disposition: int, flags_and_attributes: int
    ) -> int: ...

    def get_last_error(self) -> int: ...

    def close_handle(self, handle: int) -> None: ...

    def get_by_handle_file_information(self, handle: int) -> _ByHandleFileInfo: ...

    def get_file_id_info(self, handle: int) -> tuple[int, bytes] | None: ...

    def lock_file_ex(self, handle: int, *, exclusive: bool, fail_immediately: bool, offset: int, length: int) -> bool: ...

    def unlock_file_ex(self, handle: int, *, offset: int, length: int) -> bool: ...


def _resolve_identity(api: _Win32LockApi, handle: int, info: _ByHandleFileInfo) -> tuple:
    """Prefer ``FileIdInfo`` (64-bit volume serial + 128-bit file ID);
    fall back to the ``BY_HANDLE_FILE_INFORMATION`` volume-serial/file-index
    pair only when ``FileIdInfo`` is unavailable."""

    file_id = api.get_file_id_info(handle)
    if file_id is not None:
        return file_id
    return (info.volume_serial_number, info.file_index_high, info.file_index_low)


def _probe_identity(api: _Win32LockApi, path_str: str) -> tuple[str | None, tuple | None]:
    """Opens a short-lived path-probe handle -- same access/share mode as
    the main lock handle, so it can never self-conflict -- purely to
    compute an independent file identity for comparison. Always closes the
    probe handle exactly once. Returns ``(error_message, None)`` on
    failure, or ``(None, identity)`` on success."""

    probe_handle = api.create_file(path_str, _LOCK_ACCESS, _LOCK_SHARE_MODE, _OPEN_EXISTING, _LOCK_FLAGS)
    if probe_handle == _INVALID_HANDLE:
        err = api.get_last_error()
        return f"path-probe CreateFileW failed (WinError {err})", None
    try:
        probe_info = api.get_by_handle_file_information(probe_handle)
        if probe_info.file_attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            return "path-probe handle is a reparse point", None
        return None, _resolve_identity(api, probe_handle, probe_info)
    finally:
        api.close_handle(probe_handle)


def _windows_acquire(
    lock_path: Path, timeout_seconds: float, *, api: _Win32LockApi | None = None
) -> tuple[OwnerLockAcquireStatus, int | None, tuple[str, ...]]:
    """Windows acquire: ``CreateFileW`` -> handle-based reparse-point
    check -> handle identity -> independent path-probe handle identity
    (post-open) -> ``LockFileEx`` (exclusive, fail-immediately, offset 0,
    length 1) -> handle + path-probe identity re-verification (post-lock)
    -> only then an ``ACQUIRED`` result. Any identity mismatch at either
    checkpoint unlocks (if locked) and closes the handle before returning
    ``LOCK_PATH_IDENTITY_MISMATCH`` -- never a token.

    ``api`` defaults to the real ``ctypes``-backed implementation, which is
    only ever constructed on ``win32``. Passing a fake ``api`` lets this
    exact function run its full identity/locking logic under test on any
    platform.
    """

    if api is None:
        if sys.platform != "win32":  # pragma: no cover - guarded by caller
            raise RuntimeError("_windows_acquire requires an explicit api on a non-win32 platform")
        api = _CtypesWin32LockApi()

    path_str = str(lock_path)

    handle = api.create_file(path_str, _LOCK_ACCESS, _LOCK_SHARE_MODE, _OPEN_ALWAYS, _LOCK_FLAGS)
    if handle == _INVALID_HANDLE:
        err = api.get_last_error()
        return OwnerLockAcquireStatus.LOCK_PATH_IDENTITY_MISMATCH, None, (f"CreateFileW failed (WinError {err})",)

    try:
        info = api.get_by_handle_file_information(handle)
        if info.file_attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            api.close_handle(handle)
            return OwnerLockAcquireStatus.LOCK_PATH_IDENTITY_MISMATCH, None, ("lock path handle is a reparse point",)
        main_identity = _resolve_identity(api, handle, info)

        error, probe_identity = _probe_identity(api, path_str)
        if error is not None:
            api.close_handle(handle)
            return OwnerLockAcquireStatus.LOCK_PATH_IDENTITY_MISMATCH, None, (error,)
        if probe_identity != main_identity:
            api.close_handle(handle)
            return OwnerLockAcquireStatus.LOCK_PATH_IDENTITY_MISMATCH, None, ("post-open path-probe identity mismatch",)

        deadline = time.monotonic() + timeout_seconds
        acquired = False
        while True:
            ok = api.lock_file_ex(handle, exclusive=True, fail_immediately=True, offset=0, length=1)
            if ok:
                acquired = True
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(_POLL_INTERVAL_SECONDS)

        if not acquired:
            api.close_handle(handle)
            return OwnerLockAcquireStatus.OWNER_LOCK_TIMEOUT, None, ("timed out waiting for exclusive lock",)

        post_lock_info = api.get_by_handle_file_information(handle)
        if post_lock_info.file_attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            api.unlock_file_ex(handle, offset=0, length=1)
            api.close_handle(handle)
            return (
                OwnerLockAcquireStatus.LOCK_PATH_IDENTITY_MISMATCH,
                None,
                ("lock path handle became a reparse point after locking",),
            )
        post_lock_identity = _resolve_identity(api, handle, post_lock_info)

        error, post_lock_probe_identity = _probe_identity(api, path_str)
        if error is not None or post_lock_probe_identity != post_lock_identity or post_lock_identity != main_identity:
            api.unlock_file_ex(handle, offset=0, length=1)
            api.close_handle(handle)
            return OwnerLockAcquireStatus.LOCK_PATH_IDENTITY_MISMATCH, None, ("post-lock path identity mismatch",)

        return OwnerLockAcquireStatus.ACQUIRED, handle, ()
    except BaseException:
        api.close_handle(handle)
        raise


def _windows_release(handle: int, *, api: _Win32LockApi | None = None) -> None:
    """``UnlockFileEx`` (only ever called on a handle that previously
    succeeded ``LockFileEx``) followed by exactly one ``CloseHandle``."""

    if api is None:
        if sys.platform != "win32":  # pragma: no cover - guarded by caller
            raise RuntimeError("_windows_release requires an explicit api on a non-win32 platform")
        api = _CtypesWin32LockApi()

    try:
        api.unlock_file_ex(handle, offset=0, length=1)
    finally:
        api.close_handle(handle)


class _CtypesWin32LockApi:
    """The only production ``_Win32LockApi`` implementation -- thin ctypes
    bindings over ``kernel32``, constructed only on ``win32``."""

    def __init__(self) -> None:
        if sys.platform != "win32":  # pragma: no cover - guarded by callers
            raise RuntimeError("_CtypesWin32LockApi is only usable on win32")
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

    def get_by_handle_file_information(self, handle: int) -> _ByHandleFileInfo:
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
        return _ByHandleFileInfo(
            file_attributes=info.dwFileAttributes,
            volume_serial_number=info.dwVolumeSerialNumber,
            file_index_high=info.nFileIndexHigh,
            file_index_low=info.nFileIndexLow,
        )

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

    def lock_file_ex(self, handle: int, *, exclusive: bool, fail_immediately: bool, offset: int, length: int) -> bool:
        ctypes = self._ctypes
        wintypes = self._wintypes

        class OVERLAPPED(ctypes.Structure):
            _fields_ = [
                ("Internal", ctypes.c_void_p),
                ("InternalHigh", ctypes.c_void_p),
                ("Offset", wintypes.DWORD),
                ("OffsetHigh", wintypes.DWORD),
                ("hEvent", wintypes.HANDLE),
            ]

        LockFileEx = self._kernel32.LockFileEx
        LockFileEx.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(OVERLAPPED),
        ]
        LockFileEx.restype = wintypes.BOOL

        flags = 0
        if exclusive:
            flags |= 0x00000002  # LOCKFILE_EXCLUSIVE_LOCK
        if fail_immediately:
            flags |= 0x00000001  # LOCKFILE_FAIL_IMMEDIATELY

        overlapped = OVERLAPPED()
        overlapped.Offset = offset
        overlapped.OffsetHigh = 0
        return bool(LockFileEx(handle, flags, 0, length, 0, ctypes.byref(overlapped)))

    def unlock_file_ex(self, handle: int, *, offset: int, length: int) -> bool:
        ctypes = self._ctypes
        wintypes = self._wintypes

        class OVERLAPPED(ctypes.Structure):
            _fields_ = [
                ("Internal", ctypes.c_void_p),
                ("InternalHigh", ctypes.c_void_p),
                ("Offset", wintypes.DWORD),
                ("OffsetHigh", wintypes.DWORD),
                ("hEvent", wintypes.HANDLE),
            ]

        UnlockFileEx = self._kernel32.UnlockFileEx
        UnlockFileEx.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(OVERLAPPED),
        ]
        UnlockFileEx.restype = wintypes.BOOL

        overlapped = OVERLAPPED()
        overlapped.Offset = offset
        overlapped.OffsetHigh = 0
        return bool(UnlockFileEx(handle, 0, length, 0, ctypes.byref(overlapped)))
