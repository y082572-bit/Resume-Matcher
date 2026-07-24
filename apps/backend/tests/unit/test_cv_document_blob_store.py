"""Unit tests for the P6-B1 content-addressed blob store
(``cv_document_blob_store.py``): CAS identity, atomic no-replace publish,
containment/symlink protection, and fail-closed read/write behavior.
"""

from __future__ import annotations

import hashlib
import inspect
import os
import threading
from pathlib import Path

import pytest

from app.schemas.cv_document_storage import CvDocumentStorageErrorCode, DocumentBlobStorageStatus
from app.services.cv_document_blob_store import CvDocumentBlobStore, CvDocumentStorageError


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def store(tmp_path: Path) -> CvDocumentBlobStore:
    return CvDocumentBlobStore(tmp_path / "blob_store_root", max_blob_size_bytes=1_000_000)


# 1-2: valid write + hash correctness -----------------------------------------


def test_valid_bytes_create_a_blob(store: CvDocumentBlobStore) -> None:
    data = b"hello docx bytes"
    result = store.write_blob(data)
    assert result.success is True
    assert result.blob_sha256 == _sha256(data)
    assert store.blob_exists(result.blob_sha256)


def test_write_result_sha256_matches_a_fresh_hash_of_the_bytes(store: CvDocumentBlobStore) -> None:
    data = b"some content to hash"
    result = store.write_blob(data)
    assert result.blob_sha256 == hashlib.sha256(data).hexdigest()
    assert result.byte_size == len(data)


# 3: locator derived only from hash -------------------------------------------


def test_locator_is_derived_only_from_hash(store: CvDocumentBlobStore) -> None:
    data = b"locator derivation test"
    sha = _sha256(data)
    store.write_blob(data)
    locator = store.locator_for(sha)
    assert locator == f"blobs/{sha[0:2]}/{sha[2:4]}/{sha}.blob"
    # Calling it again (or before the blob physically exists) gives the same
    # deterministic string -- it is pure function of the hash.
    other_sha = _sha256(b"totally different bytes, never written")
    assert store.locator_for(other_sha) == f"blobs/{other_sha[0:2]}/{other_sha[2:4]}/{other_sha}.blob"


# 4: no caller-supplied path parameter on the public API ----------------------


def test_public_api_never_accepts_a_caller_supplied_path(store: CvDocumentBlobStore) -> None:
    for method in (store.write_blob, store.read_blob, store.verify_blob, store.blob_exists, store.locator_for):
        params = inspect.signature(method).parameters
        for name, param in params.items():
            assert "path" not in name.lower(), f"{method.__name__} exposes a path-like parameter: {name}"
            assert param.annotation is not Path, f"{method.__name__} accepts a Path argument: {name}"
    assert not hasattr(store, "write_to_path")
    assert not hasattr(store, "read_from_path")
    assert not hasattr(store, "delete_path")
    assert not hasattr(store, "open_arbitrary_file")


# 5-6: malformed hash rejection -------------------------------------------------


def test_traversal_string_as_hash_is_rejected(store: CvDocumentBlobStore) -> None:
    with pytest.raises(CvDocumentStorageError) as exc_info:
        store.read_blob("../../../../etc/passwd")
    assert exc_info.value.result.error_code == CvDocumentStorageErrorCode.INVALID_SHA256


def test_uppercase_hash_is_rejected(store: CvDocumentBlobStore) -> None:
    bad = "A" * 64
    with pytest.raises(CvDocumentStorageError) as exc_info:
        store.read_blob(bad)
    assert exc_info.value.result.error_code == CvDocumentStorageErrorCode.INVALID_SHA256


def test_wrong_length_hash_is_rejected(store: CvDocumentBlobStore) -> None:
    with pytest.raises(CvDocumentStorageError):
        store.blob_exists("abc123")


# 7: prefix-collision containment ----------------------------------------------


def test_prefix_collision_path_fails_containment(tmp_path: Path) -> None:
    store_local = CvDocumentBlobStore(tmp_path / "store", max_blob_size_bytes=1000)
    evil_sibling = tmp_path / "store_evil" / "escaped.blob"
    evil_sibling.parent.mkdir(parents=True, exist_ok=True)
    evil_sibling.write_bytes(b"escaped")
    # A naive ``str.startswith`` containment check would incorrectly accept
    # this path (it shares the "store" prefix); resolve()+is_relative_to must
    # reject it.
    with pytest.raises(CvDocumentStorageError) as exc_info:
        store_local._check_containment(evil_sibling, store_local.root)
    assert exc_info.value.result.error_code == CvDocumentStorageErrorCode.PATH_CONTAINMENT_FAILURE


# 8-9: symlink protection --------------------------------------------------------


def test_symlink_component_is_blocked(tmp_path: Path, store: CvDocumentBlobStore) -> None:
    data = b"symlink component test content"
    sha = _sha256(data)
    xx = sha[0:2]
    evil_target = tmp_path / "evil_target_dir"
    evil_target.mkdir()
    os.symlink(evil_target, store.blobs_dir / xx)

    result = store.write_blob(data)
    assert result.success is False
    assert result.error_code == CvDocumentStorageErrorCode.SYMLINK_ESCAPE

    read_result = store.read_blob(sha)
    assert read_result.success is False
    assert read_result.error_code == CvDocumentStorageErrorCode.SYMLINK_ESCAPE


def test_final_symlink_is_blocked(tmp_path: Path, store: CvDocumentBlobStore) -> None:
    data = b"final symlink test content"
    sha = _sha256(data)
    xx, yy = sha[0:2], sha[2:4]
    final_dir = store.blobs_dir / xx / yy
    final_dir.mkdir(parents=True)
    evil_target = tmp_path / "evil_target_file"
    evil_target.write_bytes(b"not the real blob")
    os.symlink(evil_target, final_dir / f"{sha}.blob")

    result = store.write_blob(data)
    assert result.success is False
    assert result.error_code == CvDocumentStorageErrorCode.SYMLINK_ESCAPE

    read_result = store.read_blob(sha)
    assert read_result.success is False
    assert read_result.error_code == CvDocumentStorageErrorCode.SYMLINK_ESCAPE


# 10: oversize blocked -----------------------------------------------------------


def test_oversized_blob_is_rejected(tmp_path: Path) -> None:
    tiny_store = CvDocumentBlobStore(tmp_path / "tiny_store", max_blob_size_bytes=4)
    result = tiny_store.write_blob(b"way too big for this store")
    assert result.success is False
    assert result.error_code == CvDocumentStorageErrorCode.BLOB_TOO_LARGE
    assert not any(tiny_store.blobs_dir.rglob("*.blob"))


# 11: temp hash is reverified ------------------------------------------------------


def test_expected_sha256_mismatch_is_rejected_before_any_write(store: CvDocumentBlobStore) -> None:
    data = b"content for expected-hash mismatch"
    wrong_hash = _sha256(b"different content entirely")
    result = store.write_blob(data, expected_sha256=wrong_hash)
    assert result.success is False
    assert result.error_code == CvDocumentStorageErrorCode.TEMP_HASH_MISMATCH
    assert not any(store.blobs_dir.rglob("*.blob"))


def test_temp_file_bytes_are_reverified_after_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulates on-disk corruption between the temp write and its
    mandatory re-read/rehash (section H, steps 14-16) -- final blob must
    never be published from unverified temp bytes."""

    local_store = CvDocumentBlobStore(tmp_path / "store", max_blob_size_bytes=10_000)
    original_read_bytes = Path.read_bytes

    def tampering_read_bytes(self: Path) -> bytes:
        data = original_read_bytes(self)
        if self.parent == local_store.tmp_dir:
            return data + b"\x00"
        return data

    monkeypatch.setattr(Path, "read_bytes", tampering_read_bytes)
    result = local_store.write_blob(b"hello world, this is a normal docx-like payload")
    assert result.success is False
    assert result.error_code == CvDocumentStorageErrorCode.TEMP_HASH_MISMATCH
    assert not any(local_store.blobs_dir.rglob("*.blob"))


# 12-14: existing blob reuse / corruption / no-overwrite --------------------------


def test_identical_existing_blob_is_reused(store: CvDocumentBlobStore) -> None:
    data = b"reuse me please"
    first = store.write_blob(data)
    assert first.success is True
    assert first.reused_existing is False

    second = store.write_blob(data)
    assert second.success is True
    assert second.reused_existing is True
    assert second.blob_sha256 == first.blob_sha256


def test_existing_corrupted_blob_blocks_and_is_never_overwritten(store: CvDocumentBlobStore) -> None:
    data = b"originally correct bytes"
    sha = _sha256(data)
    first = store.write_blob(data)
    assert first.success is True

    final_path = store.blobs_dir / sha[0:2] / sha[2:4] / f"{sha}.blob"
    original_bytes = final_path.read_bytes()
    # Corrupt the existing final blob out-of-band (simulating bitrot/tampering).
    final_path.write_bytes(b"corrupted bytes, different length!!")

    second = store.write_blob(data)
    assert second.success is False
    assert second.error_code == CvDocumentStorageErrorCode.EXISTING_BLOB_CORRUPTED

    # The corrupted file is left completely untouched by the failed write.
    assert final_path.read_bytes() == b"corrupted bytes, different length!!"
    assert final_path.read_bytes() != original_bytes


# 15: concurrent identical writers converge on one final blob --------------------


def test_concurrent_identical_writers_converge_on_one_final_blob(store: CvDocumentBlobStore) -> None:
    data = b"raced by two concurrent identical writers" * 100
    results: dict[str, object] = {}
    barrier = threading.Barrier(2)

    def worker(name: str) -> None:
        barrier.wait()
        results[name] = store.write_blob(data)

    threads = [threading.Thread(target=worker, args=(name,)) for name in ("A", "B")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    result_a, result_b = results["A"], results["B"]
    assert result_a.success is True and result_b.success is True  # type: ignore[union-attr]
    assert result_a.blob_sha256 == result_b.blob_sha256 == _sha256(data)  # type: ignore[union-attr]
    assert sorted([result_a.reused_existing, result_b.reused_existing]) == [False, True]  # type: ignore[union-attr]

    final_files = list(store.blobs_dir.rglob("*.blob"))
    assert len(final_files) == 1
    assert final_files[0].read_bytes() == data


# 16-17: failure-before-publish / temp cleanup -----------------------------------


def test_failure_before_publish_never_creates_a_final_blob(store: CvDocumentBlobStore) -> None:
    data = b"never published because expected hash is wrong"
    result = store.write_blob(data, expected_sha256="0" * 64)
    assert result.success is False
    assert not any(store.blobs_dir.rglob("*.blob"))


def test_controlled_failures_always_remove_the_temp_file(tmp_path: Path) -> None:
    tiny_store = CvDocumentBlobStore(tmp_path / "cleanup_store", max_blob_size_bytes=1_000_000)

    tiny_store.write_blob(b"ok bytes", expected_sha256="f" * 64)  # TEMP_HASH_MISMATCH path
    assert list(tiny_store.tmp_dir.iterdir()) == []

    data = b"stored once"
    tiny_store.write_blob(data)
    final_path = tiny_store._final_path_readonly(_sha256(data))
    final_path.write_bytes(b"corrupted now")
    tiny_store.write_blob(data)  # EXISTING_BLOB_CORRUPTED path
    assert list(tiny_store.tmp_dir.iterdir()) == []

    tiny_store.write_blob(data)  # reuse-identical path (after fixing back below)
    assert list(tiny_store.tmp_dir.iterdir()) == []


# 18-20: read behavior -------------------------------------------------------------


def test_read_blob_rehashes_bytes_rather_than_trusting_the_filename(store: CvDocumentBlobStore) -> None:
    data = b"bytes that will be tampered with post-write"
    result = store.write_blob(data)
    sha = result.blob_sha256
    assert sha is not None

    final_path = store.blobs_dir / sha[0:2] / sha[2:4] / f"{sha}.blob"
    final_path.write_bytes(b"tampered bytes, same filename")

    read_result = store.read_blob(sha)
    assert read_result.success is False
    assert read_result.error_code == CvDocumentStorageErrorCode.FINAL_BLOB_HASH_MISMATCH


def test_read_missing_blob_gives_a_closed_result(store: CvDocumentBlobStore) -> None:
    missing_sha = _sha256(b"never written")
    result = store.read_blob(missing_sha)
    assert result.success is False
    assert result.error_code == CvDocumentStorageErrorCode.FINAL_BLOB_MISSING
    assert result.data is None
    assert store.verify_blob(missing_sha) == DocumentBlobStorageStatus.MISSING
    assert store.blob_exists(missing_sha) is False


def test_read_hash_mismatch_gives_a_closed_result(store: CvDocumentBlobStore) -> None:
    data = b"content for hash-mismatch closed-result check"
    result = store.write_blob(data)
    sha = result.blob_sha256
    assert sha is not None
    final_path = store.blobs_dir / sha[0:2] / sha[2:4] / f"{sha}.blob"
    final_path.write_bytes(b"different bytes now")

    read_result = store.read_blob(sha)
    assert read_result.success is False
    assert read_result.error_code == CvDocumentStorageErrorCode.FINAL_BLOB_HASH_MISMATCH
    assert store.verify_blob(sha) == DocumentBlobStorageStatus.HASH_MISMATCH


def test_read_valid_blob_round_trips_exact_bytes(store: CvDocumentBlobStore) -> None:
    data = b"exact round trip bytes \x00\x01\x02"
    result = store.write_blob(data)
    read_result = store.read_blob(result.blob_sha256)
    assert read_result.success is True
    assert read_result.data == data
    assert store.verify_blob(result.blob_sha256) == DocumentBlobStorageStatus.OK
    assert store.blob_exists(result.blob_sha256) is True
