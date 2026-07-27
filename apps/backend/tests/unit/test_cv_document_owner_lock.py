"""Unit tests for the P6-B2b-A per-owner cross-process lock
(``cv_document_owner_lock.py``): timeout, crash release, path/lock-file
identity protection, and the token-capability registry (forged/copied/
released/foreign tokens always rejected; the exact live token always
accepted; no nested acquisition on one instance).

The second half of this file is the Windows *contract* suite: it calls
``_windows_acquire``/``_windows_release`` directly with a fake
``_Win32LockApi`` so the real handle-identity/locking logic in those
functions executes on Darwin too -- not just the platform-native POSIX
backend. Nothing here is ``skipif(sys.platform != "win32")``; these tests
run and pass on every platform because they never touch a real Win32 call.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from app.schemas.cv_document_working_copy import OwnerLockAcquireStatus
from app.services.cv_document_owner_lock import (
    OwnerLockToken,
    PlatformOwnerLock,
    _ByHandleFileInfo,
    _windows_acquire,
    _windows_release,
)


_FP_A = "a" * 64
_FP_B = "b" * 64

_posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only lock backend under test")


# 1: exact live token is accepted, then released -------------------------------


@_posix_only
def test_acquire_and_release_round_trip(tmp_path: Path) -> None:
    lock = PlatformOwnerLock(tmp_path / "owner.lock", owner_key_fingerprint=_FP_A)
    result = lock.acquire(timeout_seconds=1.0)
    assert result.status == OwnerLockAcquireStatus.ACQUIRED
    assert result.token is not None
    assert lock.validate_active_token(result.token, owner_key_fingerprint=_FP_A) is True
    lock.release(result.token)
    assert lock.validate_active_token(result.token, owner_key_fingerprint=_FP_A) is False


# 2: timeout ------------------------------------------------------------------------


@_posix_only
def test_second_acquirer_times_out_while_first_holds_lock(tmp_path: Path) -> None:
    lock_path = tmp_path / "owner.lock"
    holder = PlatformOwnerLock(lock_path, owner_key_fingerprint=_FP_A)
    held = holder.acquire(timeout_seconds=1.0)
    assert held.status == OwnerLockAcquireStatus.ACQUIRED

    waiter = PlatformOwnerLock(lock_path, owner_key_fingerprint=_FP_A)
    result = waiter.acquire(timeout_seconds=0.2)
    assert result.status == OwnerLockAcquireStatus.OWNER_LOCK_TIMEOUT
    assert result.token is None

    holder.release(held.token)


# 3: crash release (OS releases flock automatically when the fd closes) -------------


@_posix_only
def test_lock_is_released_when_holder_process_exits_without_releasing(tmp_path: Path) -> None:
    import subprocess

    backend_root = Path(__file__).resolve().parents[2]
    lock_path = tmp_path / "owner.lock"
    child_script = (
        "from app.services.cv_document_owner_lock import PlatformOwnerLock\n"
        f"lock = PlatformOwnerLock({str(lock_path)!r}, owner_key_fingerprint={_FP_A!r})\n"
        "result = lock.acquire(timeout_seconds=1.0)\n"
        "assert result.status.value == 'ACQUIRED'\n"
        "import sys, os\n"
        "sys.stdout.write('ACQUIRED\\n')\n"
        "sys.stdout.flush()\n"
        "os._exit(1)\n"  # simulate a crash: never releases the lock explicitly
    )

    env = dict(os.environ, PYTHONPATH=str(backend_root))
    proc = subprocess.run(
        [sys.executable, "-c", child_script],
        capture_output=True,
        text=True,
        timeout=10,
        cwd=backend_root,
        env=env,
    )
    assert "ACQUIRED" in proc.stdout, proc.stderr

    waiter = PlatformOwnerLock(lock_path, owner_key_fingerprint=_FP_A)
    result = waiter.acquire(timeout_seconds=2.0)
    assert result.status == OwnerLockAcquireStatus.ACQUIRED
    waiter.release(result.token)


# 4: lock path identity protection ---------------------------------------------------


@_posix_only
def test_symlinked_lock_path_is_rejected(tmp_path: Path) -> None:
    real_file = tmp_path / "real.lock"
    real_file.write_bytes(b"")
    symlinked_path = tmp_path / "owner.lock"
    symlinked_path.symlink_to(real_file)

    lock = PlatformOwnerLock(symlinked_path, owner_key_fingerprint=_FP_A)
    result = lock.acquire(timeout_seconds=1.0)
    assert result.status == OwnerLockAcquireStatus.LOCK_PATH_IDENTITY_MISMATCH
    assert result.token is None


@_posix_only
def test_hardlinked_lock_path_is_rejected(tmp_path: Path) -> None:
    lock_path = tmp_path / "owner.lock"
    lock_path.write_bytes(b"")
    hardlink_path = tmp_path / "owner.lock.hardlink"
    os.link(lock_path, hardlink_path)

    lock = PlatformOwnerLock(lock_path, owner_key_fingerprint=_FP_A)
    result = lock.acquire(timeout_seconds=1.0)
    assert result.status == OwnerLockAcquireStatus.LOCK_PATH_IDENTITY_MISMATCH
    assert result.token is None


# 5: forged / copied / released / foreign-owner token rejection ---------------------


@_posix_only
def test_forged_token_is_rejected(tmp_path: Path) -> None:
    lock = PlatformOwnerLock(tmp_path / "owner.lock", owner_key_fingerprint=_FP_A)
    result = lock.acquire(timeout_seconds=1.0)
    assert result.status == OwnerLockAcquireStatus.ACQUIRED

    forged = OwnerLockToken(lock_instance_id=999_999, acquisition_id=1, owner_key_fingerprint=_FP_A)
    assert lock.validate_active_token(forged, owner_key_fingerprint=_FP_A) is False
    lock.release(result.token)


@_posix_only
def test_copied_token_object_is_rejected(tmp_path: Path) -> None:
    import copy

    lock = PlatformOwnerLock(tmp_path / "owner.lock", owner_key_fingerprint=_FP_A)
    result = lock.acquire(timeout_seconds=1.0)
    assert result.status == OwnerLockAcquireStatus.ACQUIRED

    copied = copy.copy(result.token)
    assert copied is not result.token
    assert lock.validate_active_token(copied, owner_key_fingerprint=_FP_A) is False
    lock.release(result.token)


@_posix_only
def test_released_token_is_rejected(tmp_path: Path) -> None:
    lock = PlatformOwnerLock(tmp_path / "owner.lock", owner_key_fingerprint=_FP_A)
    result = lock.acquire(timeout_seconds=1.0)
    token = result.token
    assert lock.validate_active_token(token, owner_key_fingerprint=_FP_A) is True
    lock.release(token)
    assert lock.validate_active_token(token, owner_key_fingerprint=_FP_A) is False


@_posix_only
def test_token_from_another_owner_lock_instance_is_rejected(tmp_path: Path) -> None:
    lock_a = PlatformOwnerLock(tmp_path / "a.lock", owner_key_fingerprint=_FP_A)
    lock_b = PlatformOwnerLock(tmp_path / "b.lock", owner_key_fingerprint=_FP_B)

    result_a = lock_a.acquire(timeout_seconds=1.0)
    result_b = lock_b.acquire(timeout_seconds=1.0)
    assert result_a.status == OwnerLockAcquireStatus.ACQUIRED
    assert result_b.status == OwnerLockAcquireStatus.ACQUIRED

    assert lock_a.validate_active_token(result_b.token, owner_key_fingerprint=_FP_A) is False
    assert lock_b.validate_active_token(result_a.token, owner_key_fingerprint=_FP_B) is False
    assert lock_a.validate_active_token(result_a.token, owner_key_fingerprint=_FP_A) is True
    assert lock_b.validate_active_token(result_b.token, owner_key_fingerprint=_FP_B) is True

    lock_a.release(result_a.token)
    lock_b.release(result_b.token)


# 6: no nested acquisition -----------------------------------------------------------


@_posix_only
def test_nested_acquisition_on_the_same_instance_is_rejected(tmp_path: Path) -> None:
    lock = PlatformOwnerLock(tmp_path / "owner.lock", owner_key_fingerprint=_FP_A)
    result = lock.acquire(timeout_seconds=1.0)
    assert result.status == OwnerLockAcquireStatus.ACQUIRED
    with pytest.raises(RuntimeError):
        lock.acquire(timeout_seconds=1.0)
    lock.release(result.token)


# 7: a fresh instance on the same path can acquire again after release -------------


@_posix_only
def test_a_released_lock_path_can_be_reacquired_by_a_new_instance(tmp_path: Path) -> None:
    lock_path = tmp_path / "owner.lock"
    first = PlatformOwnerLock(lock_path, owner_key_fingerprint=_FP_A)
    first_result = first.acquire(timeout_seconds=1.0)
    assert first_result.status == OwnerLockAcquireStatus.ACQUIRED
    first.release(first_result.token)

    second = PlatformOwnerLock(lock_path, owner_key_fingerprint=_FP_A)
    second_result = second.acquire(timeout_seconds=1.0)
    assert second_result.status == OwnerLockAcquireStatus.ACQUIRED
    second.release(second_result.token)


# ==================================================================================
# Windows contract tests -- executed on Darwin via a fake _Win32LockApi
# ==================================================================================


_REPARSE = 0x400
_LOCK_EXCLUSIVE_FAIL_IMMEDIATELY = object()  # marker: real flags asserted from kwargs, not this constant


class _FakeWin32LockApi:
    """A fully in-memory fake of the ``_Win32LockApi`` protocol. Tracks
    every ``create_file``/``close_handle`` call so tests can assert no
    handle is ever leaked or double-closed, and lets each test script
    exactly which calls fail and how."""

    def __init__(
        self,
        *,
        create_file_results: list[int | Exception] | None = None,
        reparse_handles: frozenset[int] = frozenset(),
        identities: dict[int, tuple] | None = None,
        file_id_supported: bool = True,
        lock_results: list[bool] | None = None,
        last_error: int = 0,
    ) -> None:
        self._create_file_results = list(create_file_results or [])
        self._reparse_handles = reparse_handles
        self._identities = dict(identities or {})
        self._file_id_supported = file_id_supported
        self._lock_results = list(lock_results if lock_results is not None else [True])
        self._last_error = last_error
        self._next_handle = 100
        self.open_handles: set[int] = set()
        self.closed_handles: list[int] = []
        self.create_file_calls: list[tuple[str, int, int, int, int]] = []
        self.lock_calls: list[tuple[int, bool, bool, int, int]] = []
        self.unlock_calls: list[tuple[int, int, int]] = []

    def create_file(self, path, desired_access, share_mode, creation_disposition, flags_and_attributes) -> int:
        self.create_file_calls.append((path, desired_access, share_mode, creation_disposition, flags_and_attributes))
        if self._create_file_results:
            outcome = self._create_file_results.pop(0)
        else:
            outcome = "auto"
        if outcome == "auto":
            handle = self._next_handle
            self._next_handle += 1
        elif outcome == -1:
            self._last_error = 5  # ERROR_ACCESS_DENIED, arbitrary non-zero
            return -1
        else:
            handle = outcome
        self.open_handles.add(handle)
        return handle

    def get_last_error(self) -> int:
        return self._last_error

    def close_handle(self, handle: int) -> None:
        assert handle in self.open_handles, f"double-close or close of unknown handle {handle}"
        self.open_handles.discard(handle)
        self.closed_handles.append(handle)

    def get_by_handle_file_information(self, handle: int) -> _ByHandleFileInfo:
        attrs = _REPARSE if handle in self._reparse_handles else 0
        identity = self._identities.get(handle, (1, 0, 0))
        return _ByHandleFileInfo(
            file_attributes=attrs, volume_serial_number=identity[0], file_index_high=identity[1], file_index_low=identity[2] if len(identity) > 2 else 0
        )

    def get_file_id_info(self, handle: int) -> tuple[int, bytes] | None:
        if not self._file_id_supported:
            return None
        identity = self._identities.get(handle, (1, 0, 0))
        return identity

    def lock_file_ex(self, handle: int, *, exclusive: bool, fail_immediately: bool, offset: int, length: int) -> bool:
        self.lock_calls.append((handle, exclusive, fail_immediately, offset, length))
        if self._lock_results:
            result = self._lock_results.pop(0)
        else:
            result = False
        if not result:
            self._last_error = 33  # ERROR_LOCK_VIOLATION
        return result

    def unlock_file_ex(self, handle: int, *, offset: int, length: int) -> bool:
        self.unlock_calls.append((handle, offset, length))
        return True


_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_OPEN_ALWAYS = 4
_OPEN_EXISTING = 3


def _same_identity_api(**kwargs) -> _FakeWin32LockApi:
    """A fake where every opened handle shares one identity -- the
    ordinary case of a real single file opened twice."""

    return _FakeWin32LockApi(identities={}, **kwargs)


# -- CreateFileW flags/access ---------------------------------------------------------


def test_windows_create_file_uses_generic_read_write_and_no_share_delete(tmp_path: Path) -> None:
    api = _same_identity_api()
    status, handle, _ = _windows_acquire(tmp_path / "owner.lock", 1.0, api=api)
    assert status == OwnerLockAcquireStatus.ACQUIRED

    main_call = api.create_file_calls[0]
    _, desired_access, share_mode, creation_disposition, _flags = main_call
    assert desired_access == (_GENERIC_READ | _GENERIC_WRITE)
    assert creation_disposition == _OPEN_ALWAYS
    assert share_mode & 0x00000004 == 0  # FILE_SHARE_DELETE must never be set
    assert share_mode == (_FILE_SHARE_READ | _FILE_SHARE_WRITE)

    _windows_release(handle, api=api)


# -- LockFileEx flags -------------------------------------------------------------------


def test_windows_lock_file_ex_uses_exclusive_fail_immediately_offset0_length1(tmp_path: Path) -> None:
    api = _same_identity_api()
    status, handle, _ = _windows_acquire(tmp_path / "owner.lock", 1.0, api=api)
    assert status == OwnerLockAcquireStatus.ACQUIRED

    assert len(api.lock_calls) == 1
    _, exclusive, fail_immediately, offset, length = api.lock_calls[0]
    assert exclusive is True
    assert fail_immediately is True
    assert offset == 0
    assert length == 1

    _windows_release(handle, api=api)


# -- post-open identity match / mismatch -----------------------------------------------


def test_windows_post_open_handle_and_probe_identity_match_succeeds(tmp_path: Path) -> None:
    api = _FakeWin32LockApi(identities={100: (1, 2, 3), 101: (1, 2, 3), 102: (1, 2, 3), 103: (1, 2, 3)})
    status, handle, diagnostics = _windows_acquire(tmp_path / "owner.lock", 1.0, api=api)
    assert status == OwnerLockAcquireStatus.ACQUIRED, diagnostics
    _windows_release(handle, api=api)


def test_windows_post_open_identity_mismatch_is_rejected(tmp_path: Path) -> None:
    # handle 100 = main handle, 101 = post-open probe -- different identity.
    api = _FakeWin32LockApi(identities={100: (1, 2, 3), 101: (9, 9, 9)})
    status, handle, diagnostics = _windows_acquire(tmp_path / "owner.lock", 1.0, api=api)
    assert status == OwnerLockAcquireStatus.LOCK_PATH_IDENTITY_MISMATCH
    assert handle is None
    assert "identity mismatch" in diagnostics[0]
    # both handles opened must be closed, none leaked, none double-closed.
    assert api.open_handles == set()
    assert sorted(api.closed_handles) == [100, 101]


def test_windows_post_lock_identity_mismatch_is_rejected_and_unlocked(tmp_path: Path) -> None:
    # main=100 matches post-open probe=101, but post-lock probe=102 differs.
    api = _FakeWin32LockApi(identities={100: (1, 2, 3), 101: (1, 2, 3), 102: (9, 9, 9)})
    status, handle, diagnostics = _windows_acquire(tmp_path / "owner.lock", 1.0, api=api)
    assert status == OwnerLockAcquireStatus.LOCK_PATH_IDENTITY_MISMATCH
    assert handle is None
    assert "post-lock" in diagnostics[0]
    assert len(api.unlock_calls) == 1  # UnlockFileEx called exactly once
    assert api.open_handles == set()
    assert sorted(api.closed_handles) == [100, 101, 102]


# -- reparse point rejection ------------------------------------------------------------


def test_windows_handle_reparse_point_is_rejected(tmp_path: Path) -> None:
    api = _FakeWin32LockApi(reparse_handles=frozenset({100}))
    status, handle, diagnostics = _windows_acquire(tmp_path / "owner.lock", 1.0, api=api)
    assert status == OwnerLockAcquireStatus.LOCK_PATH_IDENTITY_MISMATCH
    assert handle is None
    assert "reparse point" in diagnostics[0]
    assert api.open_handles == set()
    assert api.closed_handles == [100]
    # no LockFileEx should ever have been attempted.
    assert api.lock_calls == []


def test_windows_path_probe_reparse_point_is_rejected(tmp_path: Path) -> None:
    # main handle 100 is fine, but the post-open probe handle 101 is a reparse point.
    api = _FakeWin32LockApi(reparse_handles=frozenset({101}))
    status, handle, diagnostics = _windows_acquire(tmp_path / "owner.lock", 1.0, api=api)
    assert status == OwnerLockAcquireStatus.LOCK_PATH_IDENTITY_MISMATCH
    assert handle is None
    assert "reparse point" in diagnostics[0]
    assert api.open_handles == set()
    assert sorted(api.closed_handles) == [100, 101]
    assert api.lock_calls == []


# -- CreateFileW failure / WinError mapping ----------------------------------------------


def test_windows_create_file_failure_is_reported_as_identity_mismatch(tmp_path: Path) -> None:
    api = _FakeWin32LockApi(create_file_results=[-1])
    status, handle, diagnostics = _windows_acquire(tmp_path / "owner.lock", 1.0, api=api)
    assert status == OwnerLockAcquireStatus.LOCK_PATH_IDENTITY_MISMATCH
    assert handle is None
    assert "CreateFileW failed" in diagnostics[0]
    assert api.open_handles == set()
    assert api.closed_handles == []  # nothing was ever opened


def test_windows_lock_failure_with_winerror_32_33_eventually_times_out(tmp_path: Path) -> None:
    # LockFileEx repeatedly fails as though another process holds it
    # (WinError 32 sharing violation / 33 lock violation) -- the retry
    # loop must exhaust and report the existing closed OWNER_LOCK_TIMEOUT
    # status, never raise the raw WinError.
    api = _same_identity_api(lock_results=[False] * 50)
    status, handle, diagnostics = _windows_acquire(tmp_path / "owner.lock", 0.05, api=api)
    assert status == OwnerLockAcquireStatus.OWNER_LOCK_TIMEOUT
    assert handle is None
    assert api.get_last_error() in (32, 33)
    assert api.open_handles == set()
    # the post-open path-probe handle (101) and the main handle (100),
    # each closed exactly once.
    assert sorted(api.closed_handles) == [100, 101]


# -- release: UnlockFileEx only after a successful lock, exactly one CloseHandle --------


def test_windows_release_unlocks_then_closes_exactly_once(tmp_path: Path) -> None:
    api = _same_identity_api()
    status, handle, _ = _windows_acquire(tmp_path / "owner.lock", 1.0, api=api)
    assert status == OwnerLockAcquireStatus.ACQUIRED
    assert api.unlock_calls == []  # not unlocked yet

    _windows_release(handle, api=api)
    assert len(api.unlock_calls) == 1
    assert api.unlock_calls[0] == (handle, 0, 1)
    assert api.closed_handles.count(handle) == 1
    assert api.open_handles == set()


def test_windows_no_handle_leak_across_every_error_path(tmp_path: Path) -> None:
    """Runs every failure branch back-to-back on fresh fakes and asserts
    zero leaked handles in each case -- the aggregate handle-leak
    assertion the audit specifically called out."""

    scenarios = [
        _FakeWin32LockApi(create_file_results=[-1]),
        _FakeWin32LockApi(reparse_handles=frozenset({100})),
        _FakeWin32LockApi(reparse_handles=frozenset({101})),
        _FakeWin32LockApi(identities={100: (1, 2, 3), 101: (9, 9, 9)}),
        _FakeWin32LockApi(identities={100: (1, 2, 3), 101: (1, 2, 3), 102: (9, 9, 9)}),
        _same_identity_api(lock_results=[False] * 10),
    ]
    for api in scenarios:
        status, handle, _ = _windows_acquire(tmp_path / "owner.lock", 0.03, api=api)
        assert status != OwnerLockAcquireStatus.ACQUIRED
        assert handle is None
        assert api.open_handles == set(), f"handle leak: {api.open_handles}"
        assert len(api.closed_handles) == len(set(api.closed_handles)), "a handle was closed more than once"
