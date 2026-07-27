"""Unit tests for the P6-B2b-A platform stable read
(``cv_document_working_copy_stable_read.py``): POSIX exact-bytes reads,
retry-on-replacement, retry exhaustion, symlink rejection, and missing-file
handling.

The second half of this file is the Windows *contract* suite: it calls
``windows_stable_read`` directly with a fake ``_Win32ReadApi`` so the real
share-mode/identity logic in that function executes on Darwin too -- not
just the platform-native POSIX backend. Nothing here is
``skipif(sys.platform != "win32")``; these tests run and pass on every
platform because they never touch a real Win32 call.
"""

from __future__ import annotations

import hashlib
import sys
import threading
from pathlib import Path

import pytest

from app.schemas.cv_document_working_copy import WorkingCopyStableReadStatus
from app.services.cv_document_working_copy_stable_read import posix_stable_read, windows_stable_read


_posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only stable-read backend under test")


# 1: exact bytes read -----------------------------------------------------------


@_posix_only
def test_stable_read_returns_exact_bytes_and_hash(tmp_path: Path) -> None:
    path = tmp_path / "current.docx"
    data = b"stable docx bytes"
    path.write_bytes(data)

    result = posix_stable_read(path, max_size_bytes=1_000_000)
    assert result.status == WorkingCopyStableReadStatus.STABLE_READ_OK
    assert result.data == data
    assert result.sha256 == hashlib.sha256(data).hexdigest()
    assert result.byte_size == len(data)


# 2: missing file -----------------------------------------------------------------


@_posix_only
def test_missing_file_is_reported_as_missing(tmp_path: Path) -> None:
    path = tmp_path / "does_not_exist.docx"
    result = posix_stable_read(path, max_size_bytes=1_000_000)
    assert result.status == WorkingCopyStableReadStatus.WORKING_COPY_MISSING
    assert result.data is None


# 3: symlink rejection --------------------------------------------------------------


@_posix_only
def test_symlinked_current_docx_is_rejected(tmp_path: Path) -> None:
    real_file = tmp_path / "real.docx"
    real_file.write_bytes(b"real bytes")
    symlink_path = tmp_path / "current.docx"
    symlink_path.symlink_to(real_file)

    result = posix_stable_read(symlink_path, max_size_bytes=1_000_000)
    assert result.status == WorkingCopyStableReadStatus.WORKING_COPY_UNREADABLE
    assert result.data is None


# 4: oversized file ------------------------------------------------------------------


@_posix_only
def test_oversized_file_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "current.docx"
    path.write_bytes(b"x" * 100)
    result = posix_stable_read(path, max_size_bytes=10)
    assert result.status == WorkingCopyStableReadStatus.WORKING_COPY_UNREADABLE


# 5: path replaced by a single concurrent writer still yields a stable read ---------


@_posix_only
def test_path_replaced_once_mid_read_still_settles_on_a_stable_result(tmp_path: Path) -> None:
    path = tmp_path / "current.docx"
    first_bytes = b"first version"
    final_bytes = b"final settled version"
    path.write_bytes(first_bytes)

    def replace_once() -> None:
        tmp_replacement = tmp_path / "current.docx.new"
        tmp_replacement.write_bytes(final_bytes)
        tmp_replacement.replace(path)

    writer = threading.Thread(target=replace_once, daemon=True)
    writer.start()
    result = posix_stable_read(path, max_size_bytes=1_000_000)
    writer.join(timeout=2)

    assert result.status == WorkingCopyStableReadStatus.STABLE_READ_OK
    # A single atomic os.replace() during the read always settles on
    # exactly one of the two byte-identical, self-consistent versions --
    # never a torn mix of the two.
    assert result.data in (first_bytes, final_bytes)
    assert result.sha256 == hashlib.sha256(result.data).hexdigest()


# 6: repeated modification exhausts retries -----------------------------------------


@_posix_only
def test_continuously_modified_file_exhausts_retries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Deterministically forces every one of the 5 attempts to observe a
    before/after identity mismatch (by making every ``os.fstat`` call
    report a different ``st_mtime`` than the real, unpatched ``os.lstat``
    sees) -- proving the retry loop truly exhausts and reports
    ``WORKING_COPY_CHANGED_DURING_READ`` rather than looping forever or
    silently trusting a torn read."""

    path = tmp_path / "current.docx"
    path.write_bytes(b"version-0")

    import os as os_module

    real_fstat = os_module.fstat
    counter = {"n": 0}

    def flaky_fstat(fd: int):
        counter["n"] += 1
        real_result = real_fstat(fd)
        fudged_mtime = real_result.st_mtime + counter["n"]
        return os_module.stat_result(
            (
                real_result.st_mode,
                real_result.st_ino,
                real_result.st_dev,
                real_result.st_nlink,
                real_result.st_uid,
                real_result.st_gid,
                real_result.st_size,
                real_result.st_atime,
                fudged_mtime,
                real_result.st_ctime,
            )
        )

    monkeypatch.setattr(os_module, "fstat", flaky_fstat)
    result = posix_stable_read(path, max_size_bytes=1_000_000)

    assert result.status == WorkingCopyStableReadStatus.WORKING_COPY_CHANGED_DURING_READ
    assert result.data is None
    # 5 attempts x 2 fstat calls (before-open, after-read) each.
    assert counter["n"] == 10


# ==================================================================================
# Windows contract tests -- executed on Darwin via a fake _Win32ReadApi
# ==================================================================================

_REPARSE = 0x400
_GENERIC_READ = 0x80000000
_FILE_SHARE_READ = 0x00000001
_OPEN_EXISTING = 3
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000


class _FakeWin32ReadApi:
    """A fully in-memory fake of the ``_Win32ReadApi`` protocol. Tracks
    every ``create_file``/``close_handle`` call (leak/double-close
    detection) plus how many times each identity source was consulted, and
    lets each test script exactly which calls fail and how."""

    def __init__(
        self,
        *,
        create_file_results: list[int | str] | None = None,
        file_attributes: int = 0,
        file_size: int | list[int | None] = 100,
        read_chunks: list[bytes] | None = None,
        read_ok: bool = True,
        file_id_supported: bool = True,
        identity_sequences: dict[int, list[tuple]] | None = None,
        last_error: int = 0,
    ) -> None:
        self._create_file_results = list(create_file_results or [])
        self._file_attributes = file_attributes
        self._file_size_calls = list(file_size) if isinstance(file_size, list) else None
        self._file_size_fixed = file_size if not isinstance(file_size, list) else None
        self._read_chunks = list(read_chunks) if read_chunks is not None else None
        self._read_ok = read_ok
        self._file_id_supported = file_id_supported
        self._identity_sequences: dict[int, list[tuple]] = {h: list(v) for h, v in (identity_sequences or {}).items()}
        self._last_error = last_error
        self._next_handle = 200
        self.open_handles: set[int] = set()
        self.closed_handles: list[int] = []
        self.create_file_calls: list[tuple] = []
        self.read_calls: list[tuple[int, int]] = []
        self.file_id_info_calls = 0
        self.by_handle_info_calls = 0

    def create_file(self, path, desired_access, share_mode, creation_disposition, flags_and_attributes) -> int:
        self.create_file_calls.append((path, desired_access, share_mode, creation_disposition, flags_and_attributes))
        outcome = self._create_file_results.pop(0) if self._create_file_results else "auto"
        if outcome == -1:
            self._last_error = self._last_error or 5
            return -1
        if outcome == "auto":
            handle = self._next_handle
            self._next_handle += 1
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

    def get_file_attributes(self, path: str) -> int:
        return self._file_attributes

    def get_file_size(self, handle: int) -> int | None:
        if self._file_size_calls is not None:
            return self._file_size_calls.pop(0) if self._file_size_calls else None
        return self._file_size_fixed

    def read_file_chunk(self, handle: int, max_bytes: int) -> bytes | None:
        self.read_calls.append((handle, max_bytes))
        if not self._read_ok:
            return None
        if self._read_chunks is not None:
            return self._read_chunks.pop(0)[:max_bytes] if self._read_chunks else b""
        return b""

    def _identity(self, handle: int) -> tuple:
        seq = self._identity_sequences.get(handle)
        if not seq:
            return (1, 0, 0)
        if len(seq) > 1:
            return seq.pop(0)
        return seq[0]

    def get_file_id_info(self, handle: int) -> tuple | None:
        self.file_id_info_calls += 1
        if not self._file_id_supported:
            return None
        return self._identity(handle)

    def get_by_handle_file_information(self, handle: int) -> tuple:
        self.by_handle_info_calls += 1
        return self._identity(handle)


def _read_api(**kwargs) -> _FakeWin32ReadApi:
    return _FakeWin32ReadApi(**kwargs)


# -- CreateFileW flags/access -----------------------------------------------------------


def test_windows_create_file_uses_generic_read_and_share_read_only(tmp_path: Path) -> None:
    data = b"hello world"
    api = _read_api(file_size=len(data), read_chunks=[data])
    result = windows_stable_read(tmp_path / "current.docx", max_size_bytes=1_000_000, api=api)
    assert result.status == WorkingCopyStableReadStatus.STABLE_READ_OK, result.diagnostics

    _, desired_access, share_mode, creation_disposition, flags = api.create_file_calls[0]
    assert desired_access == _GENERIC_READ
    assert share_mode == _FILE_SHARE_READ
    assert share_mode & 0x00000002 == 0  # FILE_SHARE_WRITE must never be set
    assert share_mode & 0x00000004 == 0  # FILE_SHARE_DELETE must never be set
    assert creation_disposition == _OPEN_EXISTING
    assert flags & _FILE_FLAG_OPEN_REPARSE_POINT


# -- sharing violation / missing-file mapping --------------------------------------------


def test_windows_sharing_violation_winerror_maps_to_working_copy_locked(tmp_path: Path) -> None:
    for winerror in (32, 33):
        api = _read_api(create_file_results=[-1], last_error=winerror)
        result = windows_stable_read(tmp_path / "current.docx", max_size_bytes=1_000_000, api=api)
        assert result.status == WorkingCopyStableReadStatus.WORKING_COPY_LOCKED
        assert api.open_handles == set()
        assert api.closed_handles == []  # CreateFileW itself failed -- nothing to close


def test_windows_missing_file_maps_to_working_copy_missing(tmp_path: Path) -> None:
    for winerror in (2, 3):
        api = _read_api(create_file_results=[-1], last_error=winerror)
        result = windows_stable_read(tmp_path / "current.docx", max_size_bytes=1_000_000, api=api)
        assert result.status == WorkingCopyStableReadStatus.WORKING_COPY_MISSING


# -- reparse point rejection -------------------------------------------------------------


def test_windows_reparse_point_is_rejected(tmp_path: Path) -> None:
    api = _read_api(file_attributes=_REPARSE)
    result = windows_stable_read(tmp_path / "current.docx", max_size_bytes=1_000_000, api=api)
    assert result.status == WorkingCopyStableReadStatus.WORKING_COPY_UNREADABLE
    assert "reparse point" in result.diagnostics[0]
    assert api.open_handles == set()
    assert api.closed_handles == [200]


# -- partial ReadFile composition / byte-count verification -----------------------------


def test_windows_partial_reads_are_composed_into_exact_bytes(tmp_path: Path) -> None:
    data = b"hello world, this is a longer docx payload"
    chunks = [data[0:3], data[3:9], data[9:20], data[20:]]
    api = _read_api(file_size=len(data), read_chunks=chunks)
    result = windows_stable_read(tmp_path / "current.docx", max_size_bytes=1_000_000, api=api)
    assert result.status == WorkingCopyStableReadStatus.STABLE_READ_OK
    assert result.data == data
    assert result.byte_size == len(data)
    assert len(api.read_calls) == len(chunks)  # every partial ReadFile call actually happened


def test_windows_short_read_byte_count_mismatch_is_rejected(tmp_path: Path) -> None:
    # Declared size is 10 but ReadFile hits EOF after only 5 bytes.
    api = _read_api(file_size=10, read_chunks=[b"12345"])
    result = windows_stable_read(tmp_path / "current.docx", max_size_bytes=1_000_000, api=api)
    assert result.status == WorkingCopyStableReadStatus.WORKING_COPY_UNREADABLE
    assert "expected byte count" in result.diagnostics[0]


def test_windows_size_changed_between_pre_and_post_read_is_rejected(tmp_path: Path) -> None:
    data = b"0123456789"
    api = _read_api(file_size=[len(data), 3], read_chunks=[data])
    result = windows_stable_read(tmp_path / "current.docx", max_size_bytes=1_000_000, api=api)
    assert result.status == WorkingCopyStableReadStatus.WORKING_COPY_CHANGED_DURING_READ
    assert "size changed" in result.diagnostics[0]


# -- FileIdInfo identity + BY_HANDLE_FILE_INFORMATION fallback ---------------------------


def test_windows_file_id_info_identity_is_compared(tmp_path: Path) -> None:
    data = b"stable bytes"
    api = _read_api(file_size=len(data), read_chunks=[data], file_id_supported=True)
    result = windows_stable_read(tmp_path / "current.docx", max_size_bytes=1_000_000, api=api)
    assert result.status == WorkingCopyStableReadStatus.STABLE_READ_OK
    assert api.file_id_info_calls >= 2  # before + after, at minimum
    assert api.by_handle_info_calls == 0  # never falls back when FileIdInfo works


def test_windows_identity_change_during_read_is_rejected_via_file_id_info(tmp_path: Path) -> None:
    data = b"stable bytes"
    api = _read_api(
        file_size=len(data),
        read_chunks=[data],
        file_id_supported=True,
        identity_sequences={200: [(1, b"a"), (2, b"b")]},
    )
    result = windows_stable_read(tmp_path / "current.docx", max_size_bytes=1_000_000, api=api)
    assert result.status == WorkingCopyStableReadStatus.WORKING_COPY_CHANGED_DURING_READ
    assert "identity changed" in result.diagnostics[0]
    assert api.open_handles == set()
    assert api.closed_handles == [200]  # no probe handle opened -- returned before reaching it


def test_windows_fallback_by_handle_file_information_is_used_when_file_id_unsupported(tmp_path: Path) -> None:
    data = b"stable bytes"
    api = _read_api(file_size=len(data), read_chunks=[data], file_id_supported=False)
    result = windows_stable_read(tmp_path / "current.docx", max_size_bytes=1_000_000, api=api)
    assert result.status == WorkingCopyStableReadStatus.STABLE_READ_OK
    assert api.file_id_info_calls >= 2
    assert api.by_handle_info_calls >= 3  # before, after, and the path-probe


def test_windows_fallback_identity_change_during_read_is_rejected(tmp_path: Path) -> None:
    data = b"stable bytes"
    api = _read_api(
        file_size=len(data),
        read_chunks=[data],
        file_id_supported=False,
        identity_sequences={200: [(1, 0, 0), (2, 0, 0)]},
    )
    result = windows_stable_read(tmp_path / "current.docx", max_size_bytes=1_000_000, api=api)
    assert result.status == WorkingCopyStableReadStatus.WORKING_COPY_CHANGED_DURING_READ


# -- path-probe identity mismatch (post-read) --------------------------------------------


def test_windows_path_probe_identity_mismatch_is_a_closed_failure(tmp_path: Path) -> None:
    data = b"stable bytes"
    # main handle (200) reports one stable identity for both before/after
    # calls; the path-probe handle (201) reports a different identity.
    api = _read_api(
        file_size=len(data),
        read_chunks=[data],
        identity_sequences={200: [(1, b"a")], 201: [(2, b"b")]},
    )
    result = windows_stable_read(tmp_path / "current.docx", max_size_bytes=1_000_000, api=api)
    assert result.status == WorkingCopyStableReadStatus.WORKING_COPY_CHANGED_DURING_READ
    assert "path-probe identity" in result.diagnostics[0]
    assert api.open_handles == set()
    assert sorted(api.closed_handles) == [200, 201]


def test_windows_path_probe_open_failure_after_read_is_a_closed_failure(tmp_path: Path) -> None:
    data = b"stable bytes"
    api = _read_api(file_size=len(data), read_chunks=[data], create_file_results=["auto", -1])
    result = windows_stable_read(tmp_path / "current.docx", max_size_bytes=1_000_000, api=api)
    assert result.status == WorkingCopyStableReadStatus.WORKING_COPY_CHANGED_DURING_READ
    assert "path-probe" in result.diagnostics[0]
    assert api.open_handles == set()
    assert api.closed_handles == [200]


# -- handle bookkeeping: exactly-once close, no leak, no double-close --------------------


def test_windows_success_path_closes_both_handles_exactly_once(tmp_path: Path) -> None:
    data = b"stable bytes"
    api = _read_api(file_size=len(data), read_chunks=[data])
    result = windows_stable_read(tmp_path / "current.docx", max_size_bytes=1_000_000, api=api)
    assert result.status == WorkingCopyStableReadStatus.STABLE_READ_OK
    assert api.open_handles == set()
    assert sorted(api.closed_handles) == [200, 201]
    assert len(api.closed_handles) == len(set(api.closed_handles))


def test_windows_no_handle_leak_across_every_error_path(tmp_path: Path) -> None:
    """Runs every failure branch back-to-back on fresh fakes and asserts
    zero leaked handles and no double-close in each case."""

    scenarios = [
        _read_api(create_file_results=[-1], last_error=32),
        _read_api(create_file_results=[-1], last_error=2),
        _read_api(file_attributes=_REPARSE),
        _read_api(file_size=10, read_chunks=[b"12345"]),
        _read_api(file_size=[10, 3], read_chunks=[b"0123456789"]),
        _read_api(file_size=12, read_chunks=[b"stable bytes"], identity_sequences={200: [(1, b"a"), (2, b"b")]}),
        _read_api(
            file_size=12,
            read_chunks=[b"stable bytes"],
            identity_sequences={200: [(1, b"a")], 201: [(2, b"b")]},
        ),
        _read_api(file_size=12, read_chunks=[b"stable bytes"], create_file_results=["auto", -1]),
    ]
    for api in scenarios:
        result = windows_stable_read(tmp_path / "current.docx", max_size_bytes=1_000_000, api=api)
        assert result.status != WorkingCopyStableReadStatus.STABLE_READ_OK
        assert api.open_handles == set(), f"handle leak: {api.open_handles}"
        assert len(api.closed_handles) == len(set(api.closed_handles)), "a handle was closed more than once"
