"""Unit tests for the P6-B2b-A Working Copy Root Contract
(``cv_document_working_copy_paths.py``): absolute-root enforcement, Git
worktree rejection, blob/working-copy root overlap rejection, symlink
rejection, and deterministic owner-scoped path derivation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.schemas.cv_document_working_copy import WorkingCopyRootViolationCode
from app.services.cv_document_working_copy_paths import WorkingCopyPaths, WorkingCopyPathError


_FP_A = "a" * 64
_FP_B = "b" * 64


# 1: absolute root is accepted and creates the expected layout -----------------


def test_absolute_root_is_accepted_and_creates_layout(tmp_path: Path) -> None:
    root = tmp_path / "working_copy_root"
    paths = WorkingCopyPaths(root, tmp_path / "blob_store_root")
    assert paths.root == root.resolve()
    assert paths.locks_dir.is_dir()
    assert paths.owners_dir.is_dir()


# 2: relative root is rejected ---------------------------------------------------


def test_relative_working_copy_root_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(WorkingCopyPathError) as excinfo:
        WorkingCopyPaths(Path("relative_working_copy"), tmp_path / "blob_store_root")
    assert excinfo.value.code == WorkingCopyRootViolationCode.RELATIVE_ROOT


def test_relative_blob_store_root_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(WorkingCopyPathError) as excinfo:
        WorkingCopyPaths(tmp_path / "working_copy_root", Path("relative_blob_store"))
    assert excinfo.value.code == WorkingCopyRootViolationCode.RELATIVE_ROOT


# 3: Git worktree rejection -------------------------------------------------------


def test_root_inside_a_git_worktree_is_rejected(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True)
    candidate_root = repo_root / "nested" / "working_copy_root"
    with pytest.raises(WorkingCopyPathError) as excinfo:
        WorkingCopyPaths(candidate_root, tmp_path / "blob_store_root")
    assert excinfo.value.code == WorkingCopyRootViolationCode.ROOT_INSIDE_GIT_WORKTREE


def test_root_outside_any_git_worktree_is_accepted(tmp_path: Path) -> None:
    # tmp_path itself is never inside a Git-managed tree.
    paths = WorkingCopyPaths(tmp_path / "working_copy_root", tmp_path / "blob_store_root")
    assert paths.root.is_dir()


# 4: blob-root / working-copy-root overlap rejection -------------------------------


def test_root_equal_to_blob_root_is_rejected(tmp_path: Path) -> None:
    shared = tmp_path / "shared_root"
    with pytest.raises(WorkingCopyPathError) as excinfo:
        WorkingCopyPaths(shared, shared)
    assert excinfo.value.code == WorkingCopyRootViolationCode.ROOT_EQUALS_BLOB_ROOT


def test_root_inside_blob_root_is_rejected(tmp_path: Path) -> None:
    blob_root = tmp_path / "blob_store_root"
    working_copy_root = blob_root / "nested" / "working_copy_root"
    with pytest.raises(WorkingCopyPathError) as excinfo:
        WorkingCopyPaths(working_copy_root, blob_root)
    assert excinfo.value.code == WorkingCopyRootViolationCode.ROOT_INSIDE_BLOB_ROOT


def test_blob_root_inside_working_copy_root_is_rejected(tmp_path: Path) -> None:
    working_copy_root = tmp_path / "working_copy_root"
    blob_root = working_copy_root / "nested" / "blob_store_root"
    with pytest.raises(WorkingCopyPathError) as excinfo:
        WorkingCopyPaths(working_copy_root, blob_root)
    assert excinfo.value.code == WorkingCopyRootViolationCode.BLOB_ROOT_INSIDE_WORKING_COPY_ROOT


# 5: symlink/reparse rejection -----------------------------------------------------


def test_symlink_working_copy_root_is_rejected(tmp_path: Path) -> None:
    real_dir = tmp_path / "real_dir"
    real_dir.mkdir()
    symlinked_root = tmp_path / "symlinked_root"
    symlinked_root.symlink_to(real_dir, target_is_directory=True)
    with pytest.raises(WorkingCopyPathError) as excinfo:
        WorkingCopyPaths(symlinked_root, tmp_path / "blob_store_root")
    assert excinfo.value.code == WorkingCopyRootViolationCode.SYMLINK_OR_REPARSE_POINT


def test_symlinked_owner_directory_is_rejected_on_dir_creation(tmp_path: Path) -> None:
    paths = WorkingCopyPaths(tmp_path / "working_copy_root", tmp_path / "blob_store_root")
    real_dir = tmp_path / "real_owner_dir"
    real_dir.mkdir()
    (paths.owners_dir / _FP_A).symlink_to(real_dir, target_is_directory=True)
    with pytest.raises(WorkingCopyPathError) as excinfo:
        paths.ensure_owner_dirs(_FP_A)
    # A symlink pointing outside working_copy_root is caught by the
    # resolve()-based containment check (PATH_ESCAPE) before the dedicated
    # symlink check ever runs -- either way it is always fail-closed rejected.
    assert excinfo.value.code in (
        WorkingCopyRootViolationCode.SYMLINK_OR_REPARSE_POINT,
        WorkingCopyRootViolationCode.PATH_ESCAPE,
    )


# 6: deterministic, owner-scoped path derivation -----------------------------------


def test_owner_paths_are_deterministic_and_well_formed(tmp_path: Path) -> None:
    paths = WorkingCopyPaths(tmp_path / "working_copy_root", tmp_path / "blob_store_root")

    assert paths.lock_path(_FP_A) == paths.lock_path(_FP_A)
    assert paths.lock_path(_FP_A) == paths.locks_dir / f"{_FP_A}.lock"
    assert paths.owner_dir(_FP_A) == paths.owners_dir / _FP_A
    assert paths.current_docx_path(_FP_A) == paths.owner_dir(_FP_A) / "current.docx"
    assert paths.binding_path(_FP_A) == paths.owner_dir(_FP_A) / "binding.json"
    assert paths.owner_tmp_dir(_FP_A) == paths.owner_dir(_FP_A) / "tmp"

    # Two different owners never collide.
    assert paths.owner_dir(_FP_A) != paths.owner_dir(_FP_B)


def test_ensure_owner_dirs_creates_owner_and_tmp_directories(tmp_path: Path) -> None:
    paths = WorkingCopyPaths(tmp_path / "working_copy_root", tmp_path / "blob_store_root")
    paths.ensure_owner_dirs(_FP_A)
    assert paths.owner_dir(_FP_A).is_dir()
    assert paths.owner_tmp_dir(_FP_A).is_dir()


@pytest.mark.parametrize(
    "bad_fingerprint",
    [
        "",
        "short",
        "A" * 64,  # uppercase
        "g" * 64,  # non-hex
        "../../etc/passwd",
        "a" * 63,
        "a" * 65,
    ],
)
def test_invalid_owner_key_fingerprint_is_rejected_everywhere(tmp_path: Path, bad_fingerprint: str) -> None:
    paths = WorkingCopyPaths(tmp_path / "working_copy_root", tmp_path / "blob_store_root")
    for method in (paths.lock_path, paths.owner_dir, paths.current_docx_path, paths.binding_path, paths.owner_tmp_dir):
        with pytest.raises(WorkingCopyPathError) as excinfo:
            method(bad_fingerprint)
        assert excinfo.value.code == WorkingCopyRootViolationCode.INVALID_OWNER_KEY_FINGERPRINT
