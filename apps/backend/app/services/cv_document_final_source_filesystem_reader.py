"""Explicit Provenance Stage P6-B2b-B: the real, filesystem-backed
``FinalDocxSourceReader``.

``FilesystemFinalDocxSourceReader`` implements the exact P6-A
``FinalDocxSourceReader`` Protocol (``cv_document_final_source_reader.py``)
over the P6-B2b-A Working Copy Foundation (``WorkingCopyPaths``,
``PlatformOwnerLock``, ``posix_stable_read``/``windows_stable_read``) and an
owner-bound ``ProposalArtifactLineageReader``. It never accepts a
caller-supplied filesystem path or raw bytes -- only a
``JobArtifactOwnerKey`` and the two ``expected_*`` hints
``FinalDocxSourceReader.read_finalization_source`` already defines.

Two clearly separated phases, in this exact order:

1. **Filesystem observation, under one ``PlatformOwnerLock`` acquisition**:
   re-derive ``owner_key_fingerprint`` and reject a mismatch before ever
   touching the lock; acquire the lock exactly once; stably read
   ``current.docx`` (exact bytes) and ``binding.json`` (sidecar) via the
   *existing* ``posix_stable_read``/``windows_stable_read`` primitives --
   never a second POSIX/WinAPI implementation, never ``Path.read_bytes()``
   for the sidecar; classify the sidecar (missing/invalid/owner-mismatch);
   release the lock in every path (``finally``). The result of this phase
   is a private, immutable ``_FilesystemFinalizationObservation`` -- never
   a ``FinalDocxSourceCapture``, and never visible outside this module.

2. **Owner-bound Proposal lineage re-verification, after the lock is
   released**: the sidecar's ``bound_proposal_artifact_fingerprint``/
   ``bound_proposal_revision``/``bound_generated_proposal_sha256`` are
   passed to ``ProposalArtifactLineageReader.read_verified_proposal_artifact_lineage``
   purely as *hints* -- they are never trusted as authority. Only a
   ``VERIFIED`` lineage result -- metadata from the authoritative SQL row,
   exact bytes re-hashed against that row's own
   ``generated_docx_content_hash`` -- is ever assembled into a
   ``FinalDocxSourceCapture``, and its ``docx_bytes`` always come from the
   phase-1 filesystem observation (the actual working copy the user may
   have edited), never from the Proposal's own lineage bytes (which exist
   solely to verify the sidecar's hash claim).

``WORKING_COPY_BINDING_INVALID`` means exactly one thing: ``binding.json``
was stably, successfully read and then failed to parse into a
``WorkingCopyBinding`` -- a *content* failure after a *successful* I/O.
It is never used to report an I/O failure. A stable-read ``LOCKED`` on
``binding.json`` maps to the same ``WORKING_COPY_LOCKED`` a locked
``current.docx`` would report; a stable-read ``UNREADABLE`` on
``binding.json`` maps to the same ``WORKING_COPY_UNREADABLE``. A
``WORKING_COPY_CHANGED_DURING_READ`` observed while reading either file
reuses the existing, generic status of that name -- one concept
("the working copy state changed mid-read"), regardless of which of the
two files it was observed on.

A ``VERIFIED`` result returned by the injected ``ProposalArtifactLineageReader``
is never trusted at face value either: before a ``FinalDocxSourceCapture``
is ever assembled, this reader defensively re-compares the returned
``ProposalArtifactLineage`` against the exact four values used to query
it (queried ``artifact_fingerprint``, the recomputed
``owner_key_fingerprint``, the queried ``proposal_revision``, the queried
``generated_docx_sha256``). Any divergence is a
``VERIFIED_LINEAGE_CONTRACT_VIOLATION`` -- a non-conforming
``ProposalArtifactLineageReader`` implementation, never a legitimate
Proposal-artifact outcome -- and blocks ``FinalDocxSourceCapture``
construction entirely.

``posix_stable_read``/``windows_stable_read`` themselves collapse an
oversize file into ``WORKING_COPY_UNREADABLE`` (see that module's own
docstring) -- this reader never modifies that module to carve out a
distinct oversize status. Instead it performs its own best-effort size
classification (an ``lstat`` immediately before, and again immediately
after, a failed stable read) so ``WORKING_COPY_TOO_LARGE``/
``WORKING_COPY_BINDING_TOO_LARGE`` can still be reported distinctly. This
classification is inherently racy (the file can change between the
classification ``lstat`` and the stable read it brackets) -- it is a
best-effort diagnostic refinement, never a security boundary; the stable
read itself remains the actual safety guarantee.
"""

from __future__ import annotations

import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from app.schemas.cv_document_artifact import JobArtifactOwnerKey
from app.schemas.cv_document_working_copy import (
    OwnerLockAcquireStatus,
    WorkingCopyStableReadResult,
    WorkingCopyStableReadStatus,
    parse_working_copy_binding,
)
from app.services.cv_document_final_source_reader import (
    FinalDocxSourceCapture,
    WorkingCopyReadResult,
    WorkingCopyReadStatus,
)
from app.services.cv_document_owner_identity import compute_owner_key_fingerprint
from app.services.cv_document_owner_lock import PlatformOwnerLock
from app.services.cv_document_proposal_artifact_lineage_reader import (
    ProposalArtifactLineageReader,
    ProposalArtifactLineageStatus,
)
from app.services.cv_document_working_copy_paths import WorkingCopyPaths
from app.services.cv_document_working_copy_stable_read import posix_stable_read, windows_stable_read


_DEFAULT_OWNER_LOCK_TIMEOUT_SECONDS = 10.0


# -- private, immutable filesystem observation (never exported, never a capture) ----


@dataclass(frozen=True)
class _FilesystemFinalizationObservation:
    """The exact result of phase 1 (filesystem observation under the owner
    lock). Private to this module -- never imported by the finalization
    builder or any SQL/lineage reader. Never a ``FinalDocxSourceCapture``:
    the sidecar hints it carries are not yet verified against anything."""

    owner_key_fingerprint: str
    docx_bytes: bytes
    sidecar_artifact_fingerprint_hint: str
    sidecar_proposal_revision_hint: int
    sidecar_generated_docx_sha256_hint: str


# -- totalized status mappings --------------------------------------------------------


_DOCX_STATUS_TO_READ_STATUS: dict[WorkingCopyStableReadStatus, WorkingCopyReadStatus] = {
    WorkingCopyStableReadStatus.WORKING_COPY_MISSING: WorkingCopyReadStatus.WORKING_COPY_MISSING,
    WorkingCopyStableReadStatus.WORKING_COPY_LOCKED: WorkingCopyReadStatus.WORKING_COPY_LOCKED,
    WorkingCopyStableReadStatus.WORKING_COPY_UNREADABLE: WorkingCopyReadStatus.WORKING_COPY_UNREADABLE,
    WorkingCopyStableReadStatus.WORKING_COPY_CHANGED_DURING_READ: (
        WorkingCopyReadStatus.WORKING_COPY_CHANGED_DURING_READ
    ),
}

#: ``binding.json``'s own stable-read I/O outcome -> ``WorkingCopyReadStatus``.
#: ``WORKING_COPY_MISSING`` is the only binding-specific member
#: (``WORKING_COPY_BINDING_MISSING``); ``LOCKED``/``UNREADABLE``/
#: ``CHANGED_DURING_READ`` reuse the same generic statuses ``current.docx``
#: would report for the identical I/O outcome. This dict never maps an I/O
#: failure onto ``WORKING_COPY_BINDING_INVALID`` -- that status is reserved
#: exclusively for a successful read that then fails to parse (see
#: ``parse_working_copy_binding`` below).
_BINDING_STATUS_TO_READ_STATUS: dict[WorkingCopyStableReadStatus, WorkingCopyReadStatus] = {
    WorkingCopyStableReadStatus.WORKING_COPY_MISSING: WorkingCopyReadStatus.WORKING_COPY_BINDING_MISSING,
    WorkingCopyStableReadStatus.WORKING_COPY_LOCKED: WorkingCopyReadStatus.WORKING_COPY_LOCKED,
    WorkingCopyStableReadStatus.WORKING_COPY_UNREADABLE: WorkingCopyReadStatus.WORKING_COPY_UNREADABLE,
    WorkingCopyStableReadStatus.WORKING_COPY_CHANGED_DURING_READ: (
        WorkingCopyReadStatus.WORKING_COPY_CHANGED_DURING_READ
    ),
}

#: Every non-``VERIFIED`` ``ProposalArtifactLineageStatus`` mapped 1:1 onto
#: its corresponding ``WorkingCopyReadStatus`` -- a complete dict, no
#: fallback. ``test_lineage_status_mapping_is_complete`` (unit tests) proves
#: ``set(ProposalArtifactLineageStatus) - {VERIFIED} == set(_LINEAGE_STATUS_TO_READ_STATUS)``.
_LINEAGE_STATUS_TO_READ_STATUS: dict[ProposalArtifactLineageStatus, WorkingCopyReadStatus] = {
    ProposalArtifactLineageStatus.INVALID_REQUEST: WorkingCopyReadStatus.WORKING_COPY_BINDING_INVALID,
    ProposalArtifactLineageStatus.NOT_FOUND: WorkingCopyReadStatus.SOURCE_PROPOSAL_NOT_FOUND,
    ProposalArtifactLineageStatus.OWNER_MISMATCH: WorkingCopyReadStatus.SOURCE_PROPOSAL_OWNER_MISMATCH,
    ProposalArtifactLineageStatus.REVISION_MISMATCH: WorkingCopyReadStatus.SOURCE_PROPOSAL_REVISION_MISMATCH,
    ProposalArtifactLineageStatus.GENERATED_HASH_MISMATCH: WorkingCopyReadStatus.SOURCE_PROPOSAL_HASH_MISMATCH,
    ProposalArtifactLineageStatus.BYTES_HASH_MISMATCH: WorkingCopyReadStatus.SOURCE_PROPOSAL_HASH_MISMATCH,
    ProposalArtifactLineageStatus.STORAGE_UNAVAILABLE: WorkingCopyReadStatus.STORAGE_UNAVAILABLE,
}


def _failure(status: WorkingCopyReadStatus, message: str) -> WorkingCopyReadResult:
    return WorkingCopyReadResult(status=status, error_code=status, error_message=message)


# -- best-effort size classification (never a second stable-read implementation) ----


def _lstat_size(path: Path) -> int | None:
    """``None`` if the path is absent, a symlink, or otherwise unreadable
    via ``lstat`` -- a size classification failure here always falls
    through to the ordinary stable-read outcome, never raises."""

    try:
        st = os.lstat(path)
    except OSError:
        return None
    if stat.S_ISLNK(st.st_mode):
        return None
    return st.st_size


def _platform_stable_read(path: Path, *, max_size_bytes: int) -> WorkingCopyStableReadResult:
    if sys.platform == "win32":
        return windows_stable_read(path, max_size_bytes=max_size_bytes)
    return posix_stable_read(path, max_size_bytes=max_size_bytes)


def _stable_read_with_size_classification(
    path: Path, *, max_size_bytes: int
) -> tuple[WorkingCopyStableReadResult, bool]:
    """Wraps the existing platform stable read with a best-effort size
    classification immediately before and (on an ``UNREADABLE`` outcome)
    immediately after -- so an oversize file can be reported distinctly
    without modifying ``cv_document_working_copy_stable_read.py``, which
    itself collapses oversize into ``WORKING_COPY_UNREADABLE``. Returns
    ``(result, is_too_large)``; ``is_too_large`` is only ever ``True``
    alongside an ``UNREADABLE`` ``result.status``."""

    pre_size = _lstat_size(path)
    if pre_size is not None and pre_size > max_size_bytes:
        return (
            WorkingCopyStableReadResult(
                status=WorkingCopyStableReadStatus.WORKING_COPY_UNREADABLE,
                diagnostics=("size classification (pre-read) exceeds max_size_bytes",),
            ),
            True,
        )

    result = _platform_stable_read(path, max_size_bytes=max_size_bytes)
    if result.status == WorkingCopyStableReadStatus.WORKING_COPY_UNREADABLE:
        post_size = _lstat_size(path)
        if post_size is not None and post_size > max_size_bytes:
            return result, True
    return result, False


class FilesystemFinalDocxSourceReader:
    """The real, filesystem-backed ``FinalDocxSourceReader``. Never accepts
    a caller-supplied filesystem path or bytes -- only a
    ``JobArtifactOwnerKey`` and the Protocol's own ``expected_*`` hints."""

    def __init__(
        self,
        paths: WorkingCopyPaths,
        lineage_reader: ProposalArtifactLineageReader,
        *,
        max_docx_size_bytes: int,
        max_binding_size_bytes: int,
        owner_lock_timeout_seconds: float = _DEFAULT_OWNER_LOCK_TIMEOUT_SECONDS,
    ) -> None:
        self._paths = paths
        self._lineage_reader = lineage_reader
        self._max_docx_size_bytes = max_docx_size_bytes
        self._max_binding_size_bytes = max_binding_size_bytes
        self._owner_lock_timeout_seconds = owner_lock_timeout_seconds

    def read_finalization_source(
        self,
        owner_key: JobArtifactOwnerKey,
        expected_proposal_artifact_fingerprint: str,
        expected_proposal_revision: int,
    ) -> WorkingCopyReadResult:
        # expected_proposal_artifact_fingerprint/expected_proposal_revision
        # are never used to drive this reader's own lineage lookup below --
        # that lookup is always keyed off this reader's own, freshly
        # re-read binding.json sidecar hints (never a caller-supplied
        # value). The finalization builder that calls this method already
        # independently cross-checks the returned capture against these
        # same two arguments (see cv_document_final_snapshot_builder.py's
        # own WORKING_COPY_BINDING_MISMATCH check) -- accepting them here is
        # solely to satisfy the shared FinalDocxSourceReader Protocol.
        del expected_proposal_artifact_fingerprint, expected_proposal_revision

        recomputed_owner_key_fingerprint = compute_owner_key_fingerprint(
            person_entity_id=owner_key.person_entity_id,
            owner_kind=owner_key.owner_kind,
            owner_reference_id=owner_key.owner_reference_id,
            owner_key_schema_version=owner_key.owner_key_schema_version,
        )
        if recomputed_owner_key_fingerprint != owner_key.owner_key_fingerprint:
            return _failure(
                WorkingCopyReadStatus.OWNER_KEY_FINGERPRINT_MISMATCH,
                "owner_key.owner_key_fingerprint does not match a recomputed fingerprint",
            )

        observation_or_failure = self._observe_under_lock(recomputed_owner_key_fingerprint)
        if isinstance(observation_or_failure, WorkingCopyReadResult):
            return observation_or_failure
        observation = observation_or_failure

        lineage_result = self._lineage_reader.read_verified_proposal_artifact_lineage(
            artifact_fingerprint=observation.sidecar_artifact_fingerprint_hint,
            expected_owner_key_fingerprint=observation.owner_key_fingerprint,
            expected_proposal_revision=observation.sidecar_proposal_revision_hint,
            expected_generated_docx_sha256=observation.sidecar_generated_docx_sha256_hint,
        )
        if lineage_result.status != ProposalArtifactLineageStatus.VERIFIED:
            mapped_status = _LINEAGE_STATUS_TO_READ_STATUS[lineage_result.status]
            return _failure(
                mapped_status,
                f"proposal artifact lineage verification did not succeed: {lineage_result.status.value}",
            )

        lineage = lineage_result.lineage
        assert lineage is not None  # guaranteed by ProposalArtifactLineageReadResult's own VERIFIED invariant

        # A VERIFIED result is never trusted at face value: it must cohere
        # exactly with the four values this reader itself queried the
        # lineage reader with, before a FinalDocxSourceCapture is ever
        # assembled from it. Any divergence here means the injected
        # ProposalArtifactLineageReader implementation violated its own
        # Protocol contract (VERIFIED must only ever reflect the row that
        # matches every one of these) -- never a legitimate Proposal-artifact
        # outcome, and never something this reader propagates as authority.
        if lineage.artifact_fingerprint != observation.sidecar_artifact_fingerprint_hint:
            return _failure(
                WorkingCopyReadStatus.STORAGE_UNAVAILABLE,
                "VERIFIED_LINEAGE_CONTRACT_VIOLATION: lineage.artifact_fingerprint does not match the "
                "artifact_fingerprint this reader queried the lineage reader with",
            )
        if lineage.owner_key_fingerprint != observation.owner_key_fingerprint:
            return _failure(
                WorkingCopyReadStatus.SOURCE_PROPOSAL_OWNER_MISMATCH,
                "VERIFIED_LINEAGE_CONTRACT_VIOLATION: lineage.owner_key_fingerprint does not match the "
                "recomputed owner identity",
            )
        if lineage.proposal_revision != observation.sidecar_proposal_revision_hint:
            return _failure(
                WorkingCopyReadStatus.SOURCE_PROPOSAL_REVISION_MISMATCH,
                "VERIFIED_LINEAGE_CONTRACT_VIOLATION: lineage.proposal_revision does not match the "
                "proposal_revision this reader queried the lineage reader with",
            )
        if lineage.generated_docx_content_hash != observation.sidecar_generated_docx_sha256_hint:
            return _failure(
                WorkingCopyReadStatus.SOURCE_PROPOSAL_HASH_MISMATCH,
                "VERIFIED_LINEAGE_CONTRACT_VIOLATION: lineage.generated_docx_content_hash does not match the "
                "generated_docx_sha256 this reader queried the lineage reader with",
            )

        capture = FinalDocxSourceCapture(
            owner_key_fingerprint=observation.owner_key_fingerprint,
            source_proposal_artifact_fingerprint=lineage.artifact_fingerprint,
            source_proposal_revision=lineage.proposal_revision,
            generated_proposal_sha256=lineage.generated_docx_content_hash,
            docx_bytes=observation.docx_bytes,
        )
        return WorkingCopyReadResult(status=WorkingCopyReadStatus.SUCCESS, capture=capture)

    # -- phase 1: filesystem observation, under exactly one owner-lock acquisition ----

    def _observe_under_lock(
        self, owner_key_fingerprint: str
    ) -> "_FilesystemFinalizationObservation | WorkingCopyReadResult":
        lock = PlatformOwnerLock(self._paths.lock_path(owner_key_fingerprint), owner_key_fingerprint=owner_key_fingerprint)
        acquire_result = lock.acquire(timeout_seconds=self._owner_lock_timeout_seconds)
        if acquire_result.status == OwnerLockAcquireStatus.OWNER_LOCK_TIMEOUT:
            return _failure(WorkingCopyReadStatus.WORKING_COPY_LOCKED, "timed out waiting for the owner lock")
        if acquire_result.status == OwnerLockAcquireStatus.LOCK_PATH_IDENTITY_MISMATCH:
            return _failure(
                WorkingCopyReadStatus.WORKING_COPY_LOCK_PATH_IDENTITY_MISMATCH,
                "owner lock path identity verification failed",
            )

        token = acquire_result.token
        try:
            if not lock.validate_active_token(token, owner_key_fingerprint=owner_key_fingerprint):
                return _failure(
                    WorkingCopyReadStatus.LOCK_CAPABILITY_INVALID, "owner lock token failed capability validation"
                )
            return self._observe(owner_key_fingerprint)
        finally:
            lock.release(token)

    def _observe(self, owner_key_fingerprint: str) -> "_FilesystemFinalizationObservation | WorkingCopyReadResult":
        docx_path = self._paths.current_docx_path(owner_key_fingerprint)
        binding_path = self._paths.binding_path(owner_key_fingerprint)

        docx_result, docx_too_large = _stable_read_with_size_classification(
            docx_path, max_size_bytes=self._max_docx_size_bytes
        )
        if docx_too_large:
            return _failure(WorkingCopyReadStatus.WORKING_COPY_TOO_LARGE, "current.docx exceeds max_docx_size_bytes")
        if docx_result.status != WorkingCopyStableReadStatus.STABLE_READ_OK:
            mapped_docx_status = _DOCX_STATUS_TO_READ_STATUS[docx_result.status]
            return _failure(
                mapped_docx_status, f"current.docx stable read did not succeed: {docx_result.status.value}"
            )
        assert docx_result.data is not None
        docx_bytes = bytes(docx_result.data)

        binding_result, binding_too_large = _stable_read_with_size_classification(
            binding_path, max_size_bytes=self._max_binding_size_bytes
        )
        if binding_too_large:
            return _failure(
                WorkingCopyReadStatus.WORKING_COPY_BINDING_TOO_LARGE, "binding.json exceeds max_binding_size_bytes"
            )
        if binding_result.status != WorkingCopyStableReadStatus.STABLE_READ_OK:
            mapped_binding_status = _BINDING_STATUS_TO_READ_STATUS[binding_result.status]
            return _failure(
                mapped_binding_status, f"binding.json stable read did not succeed: {binding_result.status.value}"
            )
        assert binding_result.data is not None

        parsed_binding = parse_working_copy_binding(binding_result.data)
        if parsed_binding is None:
            return _failure(WorkingCopyReadStatus.WORKING_COPY_BINDING_INVALID, "binding.json failed to parse")
        if parsed_binding.owner_key_fingerprint != owner_key_fingerprint:
            return _failure(
                WorkingCopyReadStatus.WORKING_COPY_BINDING_OWNER_MISMATCH,
                "binding.json owner_key_fingerprint does not match the recomputed owner identity",
            )

        return _FilesystemFinalizationObservation(
            owner_key_fingerprint=owner_key_fingerprint,
            docx_bytes=docx_bytes,
            sidecar_artifact_fingerprint_hint=parsed_binding.bound_proposal_artifact_fingerprint,
            sidecar_proposal_revision_hint=parsed_binding.bound_proposal_revision,
            sidecar_generated_docx_sha256_hint=parsed_binding.bound_generated_proposal_sha256,
        )
