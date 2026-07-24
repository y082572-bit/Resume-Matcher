"""Explicit Provenance Stage P6-B1: content-addressed immutable blob store.

``CvDocumentBlobStore`` is the only place P6-B1 ever touches a document's
raw bytes on disk. Its public surface is bytes-in/bytes-out plus a
verified SHA-256 -- it never accepts a caller-supplied filesystem path, and
every path it ever opens is derived purely from a regex-validated,
lowercase, 64-character hex digest.

Layout under ``root``::

    root/blobs/<sha[0:2]>/<sha[2:4]>/<sha>.blob
    root/tmp/                              (write staging only)
    root/quarantine/                       (reserved for a future, explicit
                                             controlled reconciliation mode --
                                             never written to by this module)

Publishing a new blob is a strict *create-if-absent* operation
(``os.link``): the final blob path is never overwritten, whether or not an
existing file under that path is byte-identical to the new content. A
concurrent write of identical bytes always converges on exactly one final
blob; a write that lands on an existing, differently-hashed path (which can
only happen if something outside this module tampered with the managed
tree) fails closed (``EXISTING_BLOB_CORRUPTED``) rather than silently
overwriting it.

A malformed ``blob_sha256`` argument (wrong length, uppercase, a traversal
string) is a caller contract violation and always raises
``CvDocumentStorageError`` -- it is never silently coerced or path-escaped.
A legitimate runtime storage failure (missing file, hash mismatch, a
symlink discovered inside the managed tree) is never raised as a bare
``OSError``; it is always returned as a closed, typed result instead.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path

from app.schemas.cv_document_storage import (
    CvDocumentBlobReadResult,
    CvDocumentBlobWriteResult,
    CvDocumentStorageErrorCode,
    CvDocumentStorageOperationResult,
    DocumentBlobStorageStatus,
)


_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")

_DEFAULT_MEDIA_TYPE = "application/octet-stream"


class CvDocumentStorageError(Exception):
    """Raised only for precondition violations on the blob store's own
    public contract (a malformed ``blob_sha256`` argument) -- never a
    wrapper around a raw ``OSError``. Carries a closed, typed result so a
    caller never has to parse a message string."""

    def __init__(self, result: CvDocumentStorageOperationResult) -> None:
        super().__init__(result.error_code.value if result.error_code else "STORAGE_ERROR")
        self.result = result

    @classmethod
    def of(cls, error_code: CvDocumentStorageErrorCode, *diagnostics: str) -> "CvDocumentStorageError":
        return cls(
            CvDocumentStorageOperationResult(
                success=False, error_code=error_code, diagnostics=tuple(diagnostics)
            )
        )


def _is_valid_sha256_hex(value: str) -> bool:
    return isinstance(value, str) and bool(_SHA256_HEX_RE.match(value))


def _require_valid_sha256_hex(value: str) -> None:
    if not _is_valid_sha256_hex(value):
        raise CvDocumentStorageError.of(
            CvDocumentStorageErrorCode.INVALID_SHA256,
            "blob_sha256 must be exactly 64 lowercase hex characters",
        )


def _silent_unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


class CvDocumentBlobStore:
    """A synchronous, content-addressed, filesystem-backed blob store.

    Every path ever opened is derived exclusively from a validated SHA-256
    hex digest -- there is no ``write_to_path``/``read_from_path``/
    ``delete_path``/``open_arbitrary_file`` on this class, and none of its
    public methods accept a caller-supplied path.
    """

    def __init__(
        self,
        root: Path,
        *,
        max_blob_size_bytes: int,
        fsync_directory: bool = True,
    ) -> None:
        self._root = root.resolve(strict=False)
        self._max_blob_size_bytes = max_blob_size_bytes
        self._fsync_directory = fsync_directory
        self._blobs_dir = self._root / "blobs"
        self._tmp_dir = self._root / "tmp"
        self._quarantine_dir = self._root / "quarantine"
        for managed in (self._root, self._blobs_dir, self._tmp_dir, self._quarantine_dir):
            managed.mkdir(parents=True, exist_ok=True)

    # -- path derivation (never caller-supplied) --------------------------

    def _check_containment(self, path: Path, boundary: Path) -> None:
        """Reject anything outside ``boundary`` using resolved-path
        membership -- never a textual ``str.startswith`` check, which would
        incorrectly accept a sibling directory sharing a name prefix."""

        resolved_boundary = boundary.resolve(strict=False)
        resolved_path = path.resolve(strict=False)
        if not resolved_path.is_relative_to(resolved_boundary):
            raise CvDocumentStorageError.of(
                CvDocumentStorageErrorCode.PATH_CONTAINMENT_FAILURE,
                f"{path} escapes managed boundary {boundary}",
            )

    def _final_path_readonly(self, sha256_hex: str) -> Path:
        """Derive the final blob path for a read-only operation, checking
        every existing ancestor component for a symlink -- never creating
        a directory."""

        if self._blobs_dir.is_symlink():
            raise CvDocumentStorageError.of(
                CvDocumentStorageErrorCode.SYMLINK_ESCAPE, "managed blobs directory is a symlink"
            )
        level1 = self._blobs_dir / sha256_hex[0:2]
        level2 = level1 / sha256_hex[2:4]
        for level in (level1, level2):
            if level.is_symlink():
                raise CvDocumentStorageError.of(
                    CvDocumentStorageErrorCode.SYMLINK_ESCAPE, f"{level} is a symlink"
                )
        final_path = level2 / f"{sha256_hex}.blob"
        if final_path.is_symlink():
            raise CvDocumentStorageError.of(
                CvDocumentStorageErrorCode.SYMLINK_ESCAPE, "final blob path is itself a symlink"
            )
        self._check_containment(final_path, self._root)
        return final_path

    def _final_path_for_publish(self, sha256_hex: str) -> Path:
        """Derive (and create) the final blob path for a write, checking
        every ancestor component for a symlink before creating the next
        level -- a symlink discovered partway through always aborts before
        any directory beyond it is created."""

        if self._blobs_dir.is_symlink():
            raise CvDocumentStorageError.of(
                CvDocumentStorageErrorCode.SYMLINK_ESCAPE, "managed blobs directory is a symlink"
            )
        level1 = self._blobs_dir / sha256_hex[0:2]
        level2 = level1 / sha256_hex[2:4]
        for level in (level1, level2):
            if not level.exists():
                try:
                    level.mkdir()
                except FileExistsError:
                    pass  # a concurrent writer created it first -- re-check below
            if level.is_symlink():
                raise CvDocumentStorageError.of(
                    CvDocumentStorageErrorCode.SYMLINK_ESCAPE, f"{level} is a symlink"
                )
            if not level.is_dir():
                raise CvDocumentStorageError.of(
                    CvDocumentStorageErrorCode.PATH_CONTAINMENT_FAILURE,
                    f"{level} exists and is not a directory",
                )
        final_path = level2 / f"{sha256_hex}.blob"
        if final_path.is_symlink():
            raise CvDocumentStorageError.of(
                CvDocumentStorageErrorCode.SYMLINK_ESCAPE, "final blob path is itself a symlink"
            )
        self._check_containment(final_path, self._root)
        return final_path

    # -- write --------------------------------------------------------------

    def write_blob(
        self,
        data: bytes,
        *,
        expected_sha256: str | None = None,
        media_type: str = _DEFAULT_MEDIA_TYPE,
    ) -> CvDocumentBlobWriteResult:
        raw = bytes(data)

        if len(raw) > self._max_blob_size_bytes:
            return CvDocumentBlobWriteResult(
                success=False,
                error_code=CvDocumentStorageErrorCode.BLOB_TOO_LARGE,
                diagnostics=(f"{len(raw)} bytes exceeds max_blob_size_bytes={self._max_blob_size_bytes}",),
            )

        raw_sha256 = hashlib.sha256(raw).hexdigest()

        if expected_sha256 is not None:
            if not _is_valid_sha256_hex(expected_sha256):
                return CvDocumentBlobWriteResult(
                    success=False,
                    error_code=CvDocumentStorageErrorCode.INVALID_SHA256,
                    diagnostics=("expected_sha256 must be exactly 64 lowercase hex characters",),
                )
            if expected_sha256 != raw_sha256:
                return CvDocumentBlobWriteResult(
                    success=False,
                    error_code=CvDocumentStorageErrorCode.TEMP_HASH_MISMATCH,
                    diagnostics=("expected_sha256 does not match a fresh hash of the supplied bytes",),
                )

        try:
            final_path = self._final_path_for_publish(raw_sha256)
        except CvDocumentStorageError as exc:
            return CvDocumentBlobWriteResult(
                success=False, error_code=exc.result.error_code, diagnostics=exc.result.diagnostics
            )

        try:
            fd, temp_name = tempfile.mkstemp(dir=self._tmp_dir, prefix="blob-", suffix=".tmp")
        except OSError:
            return CvDocumentBlobWriteResult(
                success=False,
                error_code=CvDocumentStorageErrorCode.TEMP_WRITE_FAILED,
                diagnostics=("failed to create a temp file under the managed tmp directory",),
            )

        temp_path = Path(temp_name)
        try:
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError:
                return CvDocumentBlobWriteResult(
                    success=False,
                    error_code=CvDocumentStorageErrorCode.TEMP_WRITE_FAILED,
                    diagnostics=("failed while writing/flushing/fsyncing the temp file",),
                )

            temp_bytes = temp_path.read_bytes()
            temp_sha256 = hashlib.sha256(temp_bytes).hexdigest()
            if temp_sha256 != raw_sha256 or len(temp_bytes) != len(raw):
                return CvDocumentBlobWriteResult(
                    success=False,
                    error_code=CvDocumentStorageErrorCode.TEMP_HASH_MISMATCH,
                    diagnostics=("re-read temp file bytes do not match the freshly computed hash",),
                )

            return self._publish(temp_path, final_path, raw_sha256, len(raw), media_type)
        finally:
            _silent_unlink(temp_path)

    def _publish(
        self, temp_path: Path, final_path: Path, sha256_hex: str, byte_size: int, media_type: str
    ) -> CvDocumentBlobWriteResult:
        """Atomic create-if-absent publish via ``os.link`` -- the final
        blob path is never overwritten by this method, whether or not an
        existing file under it is byte-identical."""

        reused = False
        try:
            os.link(temp_path, final_path)
        except FileExistsError:
            existing_result = self._verify_existing_final(final_path, sha256_hex, byte_size)
            if existing_result is not None:
                return existing_result
            reused = True
        except OSError:
            return CvDocumentBlobWriteResult(
                success=False,
                error_code=CvDocumentStorageErrorCode.TEMP_WRITE_FAILED,
                diagnostics=("atomic create-if-absent publish (os.link) is not available on this filesystem",),
            )

        if self._fsync_directory:
            self._best_effort_fsync_dir(final_path.parent)

        final_sha256, final_size = self._rehash_final(final_path)
        if final_sha256 is None:
            return CvDocumentBlobWriteResult(
                success=False,
                error_code=CvDocumentStorageErrorCode.FINAL_BLOB_MISSING,
                diagnostics=("final blob disappeared immediately after publish",),
            )
        if final_sha256 != sha256_hex or final_size != byte_size:
            return CvDocumentBlobWriteResult(
                success=False,
                error_code=CvDocumentStorageErrorCode.FINAL_BLOB_HASH_MISMATCH,
                diagnostics=("final blob bytes do not match the expected hash immediately after publish",),
            )

        return CvDocumentBlobWriteResult(
            success=True,
            blob_sha256=final_sha256,
            byte_size=final_size,
            media_type=media_type,
            reused_existing=reused,
        )

    def _verify_existing_final(
        self, final_path: Path, sha256_hex: str, byte_size: int
    ) -> CvDocumentBlobWriteResult | None:
        """The final blob already exists (EEXIST). Never overwritten,
        identical or not -- return ``None`` (caller treats it as a reuse) if
        it is byte-identical, or a fail-closed corruption result (leaving
        the existing file completely untouched) otherwise."""

        if final_path.is_symlink():
            return CvDocumentBlobWriteResult(
                success=False,
                error_code=CvDocumentStorageErrorCode.SYMLINK_ESCAPE,
                diagnostics=("existing final blob path is a symlink",),
            )
        existing_sha256, existing_size = self._rehash_final(final_path)
        if existing_sha256 is None:
            return CvDocumentBlobWriteResult(
                success=False,
                error_code=CvDocumentStorageErrorCode.FINAL_BLOB_MISSING,
                diagnostics=("existing final blob could not be read after EEXIST",),
            )
        if existing_sha256 == sha256_hex and existing_size == byte_size:
            return None
        return CvDocumentBlobWriteResult(
            success=False,
            error_code=CvDocumentStorageErrorCode.EXISTING_BLOB_CORRUPTED,
            diagnostics=(
                "an existing final blob under this content address does not match its own hash -- "
                "left untouched; a controlled reconciliation mode is required to quarantine it",
            ),
        )

    def _rehash_final(self, final_path: Path) -> tuple[str | None, int | None]:
        try:
            if not final_path.is_file() or final_path.is_symlink():
                return None, None
            data = final_path.read_bytes()
        except OSError:
            return None, None
        return hashlib.sha256(data).hexdigest(), len(data)

    def _best_effort_fsync_dir(self, directory: Path) -> None:
        try:
            fd = os.open(directory, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    # -- read -----------------------------------------------------------------

    def read_blob(self, blob_sha256: str) -> CvDocumentBlobReadResult:
        _require_valid_sha256_hex(blob_sha256)

        try:
            final_path = self._final_path_readonly(blob_sha256)
        except CvDocumentStorageError as exc:
            return CvDocumentBlobReadResult(
                success=False,
                blob_sha256=blob_sha256,
                error_code=exc.result.error_code,
                diagnostics=exc.result.diagnostics,
            )

        if not final_path.is_file() or final_path.is_symlink():
            return CvDocumentBlobReadResult(
                success=False,
                blob_sha256=blob_sha256,
                error_code=CvDocumentStorageErrorCode.FINAL_BLOB_MISSING,
                diagnostics=("blob file does not exist",),
            )

        try:
            if final_path.stat().st_size > self._max_blob_size_bytes:
                return CvDocumentBlobReadResult(
                    success=False,
                    blob_sha256=blob_sha256,
                    error_code=CvDocumentStorageErrorCode.BLOB_TOO_LARGE,
                    diagnostics=("stored blob exceeds max_blob_size_bytes",),
                )
            data = final_path.read_bytes()
        except OSError:
            return CvDocumentBlobReadResult(
                success=False,
                blob_sha256=blob_sha256,
                error_code=CvDocumentStorageErrorCode.FINAL_BLOB_MISSING,
                diagnostics=("blob file could not be read",),
            )

        observed_sha256 = hashlib.sha256(data).hexdigest()
        if observed_sha256 != blob_sha256:
            return CvDocumentBlobReadResult(
                success=False,
                blob_sha256=blob_sha256,
                error_code=CvDocumentStorageErrorCode.FINAL_BLOB_HASH_MISMATCH,
                diagnostics=(
                    "re-hashed blob bytes do not match blob_sha256 -- never trusting storage_locator/mtime",
                ),
            )

        return CvDocumentBlobReadResult(
            success=True, blob_sha256=blob_sha256, data=data, byte_size=len(data)
        )

    def verify_blob(self, blob_sha256: str) -> DocumentBlobStorageStatus:
        """Re-hash the stored bytes fresh -- never trust ``mtime`` or a
        previously stored DB status as authority."""

        _require_valid_sha256_hex(blob_sha256)
        try:
            final_path = self._final_path_readonly(blob_sha256)
        except CvDocumentStorageError:
            return DocumentBlobStorageStatus.MISSING

        if not final_path.is_file() or final_path.is_symlink():
            return DocumentBlobStorageStatus.MISSING
        try:
            data = final_path.read_bytes()
        except OSError:
            return DocumentBlobStorageStatus.MISSING
        if hashlib.sha256(data).hexdigest() != blob_sha256:
            return DocumentBlobStorageStatus.HASH_MISMATCH
        return DocumentBlobStorageStatus.OK

    def blob_exists(self, blob_sha256: str) -> bool:
        _require_valid_sha256_hex(blob_sha256)
        try:
            final_path = self._final_path_readonly(blob_sha256)
        except CvDocumentStorageError:
            return False
        return final_path.is_file() and not final_path.is_symlink()

    def locator_for(self, blob_sha256: str) -> str:
        """A diagnostic locator string derived purely from ``blob_sha256``
        -- never itself content identity, and never accepted back as an
        input path by any method on this class."""

        _require_valid_sha256_hex(blob_sha256)
        final_path = self._final_path_readonly(blob_sha256)
        return str(final_path.relative_to(self._root))

    # -- diagnostics for reconciliation (read-only, internal use only) -------

    @property
    def root(self) -> Path:
        return self._root

    @property
    def blobs_dir(self) -> Path:
        return self._blobs_dir

    @property
    def tmp_dir(self) -> Path:
        return self._tmp_dir

    @property
    def quarantine_dir(self) -> Path:
        return self._quarantine_dir
