"""Explicit Provenance Stage P6-B2b-A: the Working Copy Root Contract.

``WorkingCopyPaths`` is the only place this stage ever derives a
filesystem path for a working copy. Every path it hands out is built
exclusively from a regex-validated, lowercase, 64-character hex
``owner_key_fingerprint`` -- there is no method on this class that accepts
a caller-supplied path, and a public caller of the Working Copy Foundation
never sees or passes a ``current.docx``/``binding.json`` path directly.

Layout under ``working_copy_root``::

    <working_copy_root>/
      locks/
        <owner_key_fingerprint>.lock
      owners/
        <owner_key_fingerprint>/
          current.docx
          binding.json
          tmp/

The constructor fail-closed rejects, before ever touching the filesystem
beyond ``mkdir``:

- a relative ``working_copy_root`` or ``blob_store_root``,
- a ``working_copy_root`` inside an existing Git worktree,
- ``working_copy_root == blob_store_root``,
- ``working_copy_root`` inside ``blob_store_root`` (either direction).

Every derived path is also re-checked for symlink/reparse-point ancestors
and containment inside ``working_copy_root`` on every call -- never trusted
merely because the constructor validated the roots once.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.schemas.cv_document_working_copy import WorkingCopyRootViolationCode


_OWNER_KEY_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")

_LOCKS_DIRNAME = "locks"
_OWNERS_DIRNAME = "owners"
_CURRENT_DOCX_NAME = "current.docx"
_BINDING_NAME = "binding.json"
_TMP_DIRNAME = "tmp"
_LOCK_SUFFIX = ".lock"


class WorkingCopyPathError(Exception):
    """Raised only for Root Contract / path-derivation violations on this
    class's own public contract -- never a wrapper around a raw
    ``OSError``. Carries a closed violation code so a caller never has to
    parse a message string."""

    def __init__(self, code: WorkingCopyRootViolationCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


def _require_valid_owner_key_fingerprint(value: str) -> None:
    if not isinstance(value, str) or not _OWNER_KEY_FINGERPRINT_RE.match(value):
        raise WorkingCopyPathError(
            WorkingCopyRootViolationCode.INVALID_OWNER_KEY_FINGERPRINT,
            "owner_key_fingerprint must be exactly 64 lowercase hex characters",
        )


def _reject_symlink_ancestors(path: Path, code: WorkingCopyRootViolationCode) -> None:
    """Reject ``path`` itself if it is (or already is) a symlink.

    Deliberately never walks *above* ``path`` -- an ordinary OS-level
    symlink outside our own managed tree (e.g. macOS's ``/var`` ->
    ``/private/var``) is not a Root Contract violation; only a symlink at
    or under a path this stage itself manages (``working_copy_root``, an
    owner directory) is.
    """

    if path.is_symlink():
        raise WorkingCopyPathError(code, f"{path} is a symlink")


def _is_inside_git_worktree(root: Path) -> bool:
    """A ``.git`` entry (directory for a normal repo, file for a linked
    worktree) anywhere from ``root`` up to the filesystem root means
    ``root`` lives inside a Git-managed tree."""

    current = root
    while True:
        if (current / ".git").exists():
            return True
        parent = current.parent
        if parent == current:
            return False
        current = parent


def _check_containment(path: Path, boundary: Path) -> None:
    resolved_boundary = boundary.resolve(strict=False)
    resolved_path = path.resolve(strict=False)
    if not resolved_path.is_relative_to(resolved_boundary):
        raise WorkingCopyPathError(
            WorkingCopyRootViolationCode.PATH_ESCAPE,
            f"{path} escapes managed boundary {boundary}",
        )


def _validate_roots(working_copy_root: Path, blob_store_root: Path) -> Path:
    if not working_copy_root.is_absolute():
        raise WorkingCopyPathError(
            WorkingCopyRootViolationCode.RELATIVE_ROOT, "working_copy_root must be an absolute path"
        )
    if not blob_store_root.is_absolute():
        raise WorkingCopyPathError(
            WorkingCopyRootViolationCode.RELATIVE_ROOT, "blob_store_root must be an absolute path"
        )

    _reject_symlink_ancestors(working_copy_root, WorkingCopyRootViolationCode.SYMLINK_OR_REPARSE_POINT)

    resolved_working_copy_root = working_copy_root.resolve(strict=False)
    resolved_blob_store_root = blob_store_root.resolve(strict=False)

    if resolved_working_copy_root == resolved_blob_store_root:
        raise WorkingCopyPathError(
            WorkingCopyRootViolationCode.ROOT_EQUALS_BLOB_ROOT,
            "working_copy_root must not equal blob_store_root",
        )
    if resolved_working_copy_root.is_relative_to(resolved_blob_store_root):
        raise WorkingCopyPathError(
            WorkingCopyRootViolationCode.ROOT_INSIDE_BLOB_ROOT,
            "working_copy_root must not live inside blob_store_root",
        )
    if resolved_blob_store_root.is_relative_to(resolved_working_copy_root):
        raise WorkingCopyPathError(
            WorkingCopyRootViolationCode.BLOB_ROOT_INSIDE_WORKING_COPY_ROOT,
            "blob_store_root must not live inside working_copy_root",
        )
    if _is_inside_git_worktree(resolved_working_copy_root):
        raise WorkingCopyPathError(
            WorkingCopyRootViolationCode.ROOT_INSIDE_GIT_WORKTREE,
            "working_copy_root must not live inside a Git worktree",
        )

    return resolved_working_copy_root


class WorkingCopyPaths:
    """The validated root pair plus every derived, owner-scoped path. No
    method on this class ever accepts a caller-supplied filesystem path --
    only a regex-validated ``owner_key_fingerprint``."""

    def __init__(self, working_copy_root: Path, blob_store_root: Path) -> None:
        self._root = _validate_roots(working_copy_root, blob_store_root)
        self._locks_dir = self._root / _LOCKS_DIRNAME
        self._owners_dir = self._root / _OWNERS_DIRNAME
        self._root.mkdir(parents=True, exist_ok=True)
        self._locks_dir.mkdir(parents=True, exist_ok=True)
        self._owners_dir.mkdir(parents=True, exist_ok=True)
        _reject_symlink_ancestors(self._root, WorkingCopyRootViolationCode.SYMLINK_OR_REPARSE_POINT)

    @property
    def root(self) -> Path:
        return self._root

    @property
    def locks_dir(self) -> Path:
        return self._locks_dir

    @property
    def owners_dir(self) -> Path:
        return self._owners_dir

    def lock_path(self, owner_key_fingerprint: str) -> Path:
        _require_valid_owner_key_fingerprint(owner_key_fingerprint)
        path = self._locks_dir / f"{owner_key_fingerprint}{_LOCK_SUFFIX}"
        _check_containment(path, self._root)
        return path

    def owner_dir(self, owner_key_fingerprint: str) -> Path:
        _require_valid_owner_key_fingerprint(owner_key_fingerprint)
        path = self._owners_dir / owner_key_fingerprint
        _check_containment(path, self._root)
        return path

    def current_docx_path(self, owner_key_fingerprint: str) -> Path:
        path = self.owner_dir(owner_key_fingerprint) / _CURRENT_DOCX_NAME
        _check_containment(path, self._root)
        return path

    def binding_path(self, owner_key_fingerprint: str) -> Path:
        path = self.owner_dir(owner_key_fingerprint) / _BINDING_NAME
        _check_containment(path, self._root)
        return path

    def owner_tmp_dir(self, owner_key_fingerprint: str) -> Path:
        path = self.owner_dir(owner_key_fingerprint) / _TMP_DIRNAME
        _check_containment(path, self._root)
        return path

    def ensure_owner_dirs(self, owner_key_fingerprint: str) -> None:
        """Create ``owners/<fp>/`` and ``owners/<fp>/tmp/`` if absent --
        never creates ``current.docx``/``binding.json`` themselves."""

        owner_dir = self.owner_dir(owner_key_fingerprint)
        tmp_dir = self.owner_tmp_dir(owner_key_fingerprint)
        owner_dir.mkdir(parents=True, exist_ok=True)
        tmp_dir.mkdir(parents=True, exist_ok=True)
        _reject_symlink_ancestors(owner_dir, WorkingCopyRootViolationCode.SYMLINK_OR_REPARSE_POINT)
