"""Explicit Provenance Stage P6-B2b-A: the Working Copy store.

``WorkingCopyStore`` is the single production entry point for the three
public Working Copy operations: ``create_or_refresh_working_copy``,
``reset_working_copy_to_current_proposal``, and ``inspect_working_copy``.
Each acquires the owner's ``PlatformOwnerLock`` exactly once, validates the
resulting capability token, and delegates to a matching ``_..._under_lock``
private method -- none of which ever re-acquires the lock.

Authority for a Proposal's content always remains
``CvDocumentArtifactRepository`` (the current proposal slot, the Proposal
artifact metadata, and the Proposal repository bytes); ``binding.json`` is
cache and binding metadata only. Every publish here re-verifies the
repository's own bytes hash against ``generated_docx_content_hash`` before
writing, and re-checks the current proposal's identity immediately before
the atomic ``os.replace`` -- a caller can never end up with a working copy
silently bound to a proposal the operation never actually observed.

An **edited working copy is never automatically overwritten**:
``create_or_refresh_working_copy``'s lifecycle matrix always preserves
``current.docx`` bytes that do not match either the current Proposal's own
hash or the sidecar's own record of what the tool last wrote.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from app.schemas.cv_document_artifact import CvDocxProposalArtifact, JobArtifactOwnerKey
from app.schemas.cv_document_working_copy import (
    OwnerLockAcquireStatus,
    WorkingCopyBinding,
    WorkingCopyBindingState,
    WorkingCopyCreateRefreshResult,
    WorkingCopyCreateRefreshStatus,
    WorkingCopyInspectionResult,
    WorkingCopyInspectionStatus,
    WorkingCopyObservedState,
    WorkingCopyResetConfirmation,
    WorkingCopyResetResult,
    WorkingCopyResetStatus,
    WorkingCopyStableReadResult,
    WorkingCopyStableReadStatus,
    compute_binding_state_fingerprint,
    parse_working_copy_binding,
    serialize_working_copy_binding,
)
from app.services.cv_document_owner_lock import PlatformOwnerLock
from app.services.cv_document_repository_protocol import CvDocumentArtifactRepository
from app.services.cv_document_working_copy_paths import WorkingCopyPaths
from app.services.cv_document_working_copy_stable_read import posix_stable_read, windows_stable_read


logger = logging.getLogger(__name__)

_DEFAULT_OWNER_LOCK_TIMEOUT_SECONDS = 10.0

#: The exception classes every public operation below is guaranteed to
#: close over instead of letting escape -- ``WorkingCopyPublishError`` (an
#: internal post-write verification failure), ``RuntimeError`` (an
#: internal owner-lock contract violation, e.g. nested acquisition),
#: ``OSError``/``PermissionError`` (``PermissionError`` is itself an
#: ``OSError`` subclass -- listed for clarity) for any unclassifiable
#: filesystem problem, and ``json.JSONDecodeError`` as a defense-in-depth
#: backstop even though sidecar parsing already never raises. Deliberately
#: never ``BaseException``/``KeyboardInterrupt``/``SystemExit``.
_LOCK_CAPABILITY_EXCEPTIONS = (RuntimeError,)
_STORAGE_EXCEPTIONS = (OSError, json.JSONDecodeError)


class WorkingCopyPublishError(Exception):
    """Raised only for an internal post-write verification failure -- a
    freshly re-read file not matching the bytes this process itself just
    wrote. Never raised for an ordinary caller-facing outcome, which is
    always a closed status instead."""


def _platform_stable_read(path: Path, *, max_size_bytes: int) -> WorkingCopyStableReadResult:
    if sys.platform == "win32":
        return windows_stable_read(path, max_size_bytes=max_size_bytes)
    return posix_stable_read(path, max_size_bytes=max_size_bytes)


def _read_sidecar_raw(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _classify_sidecar(
    raw: bytes | None, owner_key_fingerprint: str
) -> tuple[WorkingCopyBindingState, WorkingCopyBinding | None, str | None]:
    if raw is None:
        return WorkingCopyBindingState.MISSING, None, None
    raw_sha256 = hashlib.sha256(raw).hexdigest()
    parsed = parse_working_copy_binding(raw)
    if parsed is None or parsed.owner_key_fingerprint != owner_key_fingerprint:
        return WorkingCopyBindingState.INVALID, None, raw_sha256
    return WorkingCopyBindingState.VALID, parsed, raw_sha256


def _proposal_identity(artifact: CvDocxProposalArtifact | None) -> tuple[str, int, str] | None:
    if artifact is None:
        return None
    return (artifact.artifact_fingerprint, artifact.proposal_revision, artifact.generated_docx_content_hash)


def _new_binding_for(owner_key_fingerprint: str, proposal: CvDocxProposalArtifact) -> WorkingCopyBinding:
    return WorkingCopyBinding(
        owner_key_fingerprint=owner_key_fingerprint,
        bound_proposal_artifact_fingerprint=proposal.artifact_fingerprint,
        bound_proposal_revision=proposal.proposal_revision,
        bound_generated_proposal_sha256=proposal.generated_docx_content_hash,
        last_known_tool_written_hash=proposal.generated_docx_content_hash,
    )


def _lock_status_to_create_refresh(status: OwnerLockAcquireStatus) -> WorkingCopyCreateRefreshStatus:
    return WorkingCopyCreateRefreshStatus(status.value)


def _lock_status_to_reset(status: OwnerLockAcquireStatus) -> WorkingCopyResetStatus:
    return WorkingCopyResetStatus(status.value)


def _lock_status_to_inspection(status: OwnerLockAcquireStatus) -> WorkingCopyInspectionStatus:
    return WorkingCopyInspectionStatus(status.value)


@dataclass(frozen=True)
class _RawObservation:
    docx_status: WorkingCopyStableReadStatus
    docx_sha256: str | None
    sidecar_raw: bytes | None


@dataclass(frozen=True)
class _PublishPlan:
    rewrite_docx: bool
    new_binding: WorkingCopyBinding


@dataclass(frozen=True)
class _BranchOutcome:
    status: WorkingCopyCreateRefreshStatus
    publish: _PublishPlan | None


def _decide_create_refresh_branch(
    *,
    owner_key_fingerprint: str,
    docx_present: bool,
    docx_sha256: str | None,
    sidecar_state: WorkingCopyBindingState,
    sidecar_binding: WorkingCopyBinding | None,
    proposal_before: CvDocxProposalArtifact,
) -> _BranchOutcome:
    """The exact five-branch lifecycle matrix (A-E) from
    ``docs/explicit-provenance-stage-p6b2b-working-copy-foundation.md`` --
    see that document for the full rationale of each branch."""

    if not docx_present:
        if sidecar_state == WorkingCopyBindingState.MISSING:
            # A: neither current.docx nor binding.json exists.
            return _BranchOutcome(
                WorkingCopyCreateRefreshStatus.WORKING_COPY_CREATED,
                _PublishPlan(True, _new_binding_for(owner_key_fingerprint, proposal_before)),
            )
        # C: current.docx is missing but binding.json exists (VALID or INVALID).
        return _BranchOutcome(
            WorkingCopyCreateRefreshStatus.WORKING_COPY_RECREATED_FROM_CURRENT_PROPOSAL,
            _PublishPlan(True, _new_binding_for(owner_key_fingerprint, proposal_before)),
        )

    # current.docx exists.
    if sidecar_state == WorkingCopyBindingState.MISSING:
        # B
        if docx_sha256 == proposal_before.generated_docx_content_hash:
            return _BranchOutcome(
                WorkingCopyCreateRefreshStatus.RECOVERED_UNEDITED_HALF_STATE,
                _PublishPlan(False, _new_binding_for(owner_key_fingerprint, proposal_before)),
            )
        return _BranchOutcome(WorkingCopyCreateRefreshStatus.HALF_STATE_PRESERVED_POSSIBLE_USER_EDIT, None)

    if sidecar_state == WorkingCopyBindingState.VALID:
        # D
        assert sidecar_binding is not None
        if docx_sha256 == sidecar_binding.last_known_tool_written_hash:
            binding_is_current = (
                sidecar_binding.bound_proposal_artifact_fingerprint == proposal_before.artifact_fingerprint
                and sidecar_binding.bound_proposal_revision == proposal_before.proposal_revision
            )
            if binding_is_current:
                return _BranchOutcome(WorkingCopyCreateRefreshStatus.WORKING_COPY_UNCHANGED_NO_REFRESH_NEEDED, None)
            return _BranchOutcome(
                WorkingCopyCreateRefreshStatus.WORKING_COPY_REFRESHED,
                _PublishPlan(True, _new_binding_for(owner_key_fingerprint, proposal_before)),
            )
        return _BranchOutcome(WorkingCopyCreateRefreshStatus.HALF_STATE_PRESERVED_POSSIBLE_USER_EDIT, None)

    # E: sidecar_state == INVALID
    if docx_sha256 == proposal_before.generated_docx_content_hash:
        return _BranchOutcome(
            WorkingCopyCreateRefreshStatus.RECOVERED_UNEDITED_HALF_STATE,
            _PublishPlan(False, _new_binding_for(owner_key_fingerprint, proposal_before)),
        )
    return _BranchOutcome(WorkingCopyCreateRefreshStatus.HALF_STATE_PRESERVED_POSSIBLE_USER_EDIT, None)


class WorkingCopyStore:
    """The production Working Copy Foundation. Constructed with an already
    root-contract-validated ``WorkingCopyPaths`` -- this class never
    validates or derives roots itself."""

    def __init__(
        self,
        paths: WorkingCopyPaths,
        *,
        max_docx_size_bytes: int,
        owner_lock_timeout_seconds: float = _DEFAULT_OWNER_LOCK_TIMEOUT_SECONDS,
    ) -> None:
        self._paths = paths
        self._max_docx_size_bytes = max_docx_size_bytes
        self._owner_lock_timeout_seconds = owner_lock_timeout_seconds

    # -- public operations -----------------------------------------------------
    #
    # Each public operation below is a closed error channel: every internal
    # exception this class or its collaborators can raise -- an internal
    # post-write verification failure, an internal owner-lock contract
    # violation, or an unclassifiable filesystem problem -- is caught here
    # and mapped to a closed status instead of propagating. A caller of
    # this class never needs a ``try``/``except`` of its own.

    def create_or_refresh_working_copy(
        self, *, owner_key: JobArtifactOwnerKey, repository: CvDocumentArtifactRepository
    ) -> WorkingCopyCreateRefreshResult:
        try:
            return self._create_or_refresh_working_copy_unsafe(owner_key=owner_key, repository=repository)
        except WorkingCopyPublishError as exc:
            logger.error(f"create_or_refresh_working_copy post-write verification failed: {exc}")
            return WorkingCopyCreateRefreshResult(status=WorkingCopyCreateRefreshStatus.POST_WRITE_VERIFICATION_FAILED)
        except _LOCK_CAPABILITY_EXCEPTIONS as exc:
            logger.error(f"create_or_refresh_working_copy owner-lock capability failure: {exc}")
            return WorkingCopyCreateRefreshResult(status=WorkingCopyCreateRefreshStatus.LOCK_CAPABILITY_INVALID)
        except _STORAGE_EXCEPTIONS as exc:
            logger.error(f"create_or_refresh_working_copy storage failure: {exc}")
            return WorkingCopyCreateRefreshResult(status=WorkingCopyCreateRefreshStatus.STORAGE_UNAVAILABLE)

    def _create_or_refresh_working_copy_unsafe(
        self, *, owner_key: JobArtifactOwnerKey, repository: CvDocumentArtifactRepository
    ) -> WorkingCopyCreateRefreshResult:
        owner_fp = owner_key.owner_key_fingerprint
        lock = PlatformOwnerLock(self._paths.lock_path(owner_fp), owner_key_fingerprint=owner_fp)
        acquire_result = lock.acquire(timeout_seconds=self._owner_lock_timeout_seconds)
        if acquire_result.status != OwnerLockAcquireStatus.ACQUIRED:
            return WorkingCopyCreateRefreshResult(status=_lock_status_to_create_refresh(acquire_result.status))
        token = acquire_result.token
        try:
            if not lock.validate_active_token(token, owner_key_fingerprint=owner_fp):
                return WorkingCopyCreateRefreshResult(status=WorkingCopyCreateRefreshStatus.LOCK_CAPABILITY_INVALID)
            return self._create_or_refresh_under_lock(owner_key=owner_key, repository=repository)
        finally:
            lock.release(token)

    def reset_working_copy_to_current_proposal(
        self,
        *,
        owner_key: JobArtifactOwnerKey,
        repository: CvDocumentArtifactRepository,
        confirmation: WorkingCopyResetConfirmation,
    ) -> WorkingCopyResetResult:
        try:
            return self._reset_working_copy_to_current_proposal_unsafe(
                owner_key=owner_key, repository=repository, confirmation=confirmation
            )
        except WorkingCopyPublishError as exc:
            logger.error(f"reset_working_copy_to_current_proposal post-write verification failed: {exc}")
            return WorkingCopyResetResult(status=WorkingCopyResetStatus.POST_WRITE_VERIFICATION_FAILED)
        except _LOCK_CAPABILITY_EXCEPTIONS as exc:
            logger.error(f"reset_working_copy_to_current_proposal owner-lock capability failure: {exc}")
            return WorkingCopyResetResult(status=WorkingCopyResetStatus.LOCK_CAPABILITY_INVALID)
        except _STORAGE_EXCEPTIONS as exc:
            logger.error(f"reset_working_copy_to_current_proposal storage failure: {exc}")
            return WorkingCopyResetResult(status=WorkingCopyResetStatus.STORAGE_UNAVAILABLE)

    def _reset_working_copy_to_current_proposal_unsafe(
        self,
        *,
        owner_key: JobArtifactOwnerKey,
        repository: CvDocumentArtifactRepository,
        confirmation: WorkingCopyResetConfirmation,
    ) -> WorkingCopyResetResult:
        owner_fp = owner_key.owner_key_fingerprint
        lock = PlatformOwnerLock(self._paths.lock_path(owner_fp), owner_key_fingerprint=owner_fp)
        acquire_result = lock.acquire(timeout_seconds=self._owner_lock_timeout_seconds)
        if acquire_result.status != OwnerLockAcquireStatus.ACQUIRED:
            return WorkingCopyResetResult(status=_lock_status_to_reset(acquire_result.status))
        token = acquire_result.token
        try:
            if not lock.validate_active_token(token, owner_key_fingerprint=owner_fp):
                return WorkingCopyResetResult(status=WorkingCopyResetStatus.LOCK_CAPABILITY_INVALID)
            return self._reset_under_lock(owner_key=owner_key, repository=repository, confirmation=confirmation)
        finally:
            lock.release(token)

    def inspect_working_copy(self, *, owner_key: JobArtifactOwnerKey) -> WorkingCopyInspectionResult:
        try:
            return self._inspect_working_copy_unsafe(owner_key=owner_key)
        except _LOCK_CAPABILITY_EXCEPTIONS as exc:
            logger.error(f"inspect_working_copy owner-lock capability failure: {exc}")
            return WorkingCopyInspectionResult(status=WorkingCopyInspectionStatus.LOCK_CAPABILITY_INVALID)
        except _STORAGE_EXCEPTIONS as exc:
            logger.error(f"inspect_working_copy storage failure: {exc}")
            return WorkingCopyInspectionResult(status=WorkingCopyInspectionStatus.STORAGE_UNAVAILABLE)

    def _inspect_working_copy_unsafe(self, *, owner_key: JobArtifactOwnerKey) -> WorkingCopyInspectionResult:
        owner_fp = owner_key.owner_key_fingerprint
        lock = PlatformOwnerLock(self._paths.lock_path(owner_fp), owner_key_fingerprint=owner_fp)
        acquire_result = lock.acquire(timeout_seconds=self._owner_lock_timeout_seconds)
        if acquire_result.status != OwnerLockAcquireStatus.ACQUIRED:
            return WorkingCopyInspectionResult(status=_lock_status_to_inspection(acquire_result.status))
        token = acquire_result.token
        try:
            if not lock.validate_active_token(token, owner_key_fingerprint=owner_fp):
                return WorkingCopyInspectionResult(status=WorkingCopyInspectionStatus.LOCK_CAPABILITY_INVALID)
            return self._inspect_under_lock(owner_fp)
        finally:
            lock.release(token)

    # -- shared internals (never re-acquire the lock) --------------------------

    def _stable_read_under_lock(self, owner_fp: str) -> _RawObservation:
        docx_path = self._paths.current_docx_path(owner_fp)
        read_result = _platform_stable_read(docx_path, max_size_bytes=self._max_docx_size_bytes)
        sidecar_raw = _read_sidecar_raw(self._paths.binding_path(owner_fp))
        return _RawObservation(docx_status=read_result.status, docx_sha256=read_result.sha256, sidecar_raw=sidecar_raw)

    def _build_observed_state(self, owner_fp: str, observation: _RawObservation) -> WorkingCopyObservedState:
        assert observation.docx_sha256 is not None
        sidecar_state, sidecar_binding, sidecar_raw_sha256 = _classify_sidecar(observation.sidecar_raw, owner_fp)
        binding_schema_version = sidecar_binding.binding_schema_version if sidecar_binding else None
        bound_artifact_fingerprint = sidecar_binding.bound_proposal_artifact_fingerprint if sidecar_binding else None
        bound_revision = sidecar_binding.bound_proposal_revision if sidecar_binding else None
        fingerprint = compute_binding_state_fingerprint(
            owner_key_fingerprint=owner_fp,
            working_copy_sha256=observation.docx_sha256,
            binding_state=sidecar_state,
            sidecar_raw_sha256=sidecar_raw_sha256,
            binding_schema_version=binding_schema_version,
            bound_proposal_artifact_fingerprint=bound_artifact_fingerprint,
            bound_proposal_revision=bound_revision,
        )
        return WorkingCopyObservedState(
            owner_key_fingerprint=owner_fp,
            working_copy_sha256=observation.docx_sha256,
            binding_state=sidecar_state,
            binding_state_fingerprint=fingerprint,
            sidecar_raw_sha256=sidecar_raw_sha256,
            binding_schema_version=binding_schema_version,
            bound_proposal_artifact_fingerprint=bound_artifact_fingerprint,
            bound_proposal_revision=bound_revision,
        )

    def _write_temp_and_replace(self, tmp_dir: Path, final_path: Path, data: bytes, *, prefix: str) -> str:
        expected_sha256 = hashlib.sha256(data).hexdigest()
        fd, temp_name = tempfile.mkstemp(dir=tmp_dir, prefix=prefix, suffix=".tmp")
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            rehashed = hashlib.sha256(temp_path.read_bytes()).hexdigest()
            if rehashed != expected_sha256:
                raise WorkingCopyPublishError("temp file rehash mismatch immediately after write")
            os.replace(temp_path, final_path)
            return rehashed
        finally:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass

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

    def _atomic_publish(self, owner_fp: str, *, docx_bytes: bytes | None, binding: WorkingCopyBinding) -> str:
        """Steps 1-13 of ATOMIC PUBLICATION: temp write + fsync + rehash for
        the DOCX (when rewritten) and the sidecar, then two ``os.replace``
        calls, a best-effort directory fsync, and a post-write re-read
        verification of both final files."""

        self._paths.ensure_owner_dirs(owner_fp)
        tmp_dir = self._paths.owner_tmp_dir(owner_fp)
        docx_path = self._paths.current_docx_path(owner_fp)
        binding_path = self._paths.binding_path(owner_fp)

        if docx_bytes is not None:
            docx_sha256 = self._write_temp_and_replace(tmp_dir, docx_path, docx_bytes, prefix="docx-")
        else:
            docx_sha256 = binding.last_known_tool_written_hash

        sidecar_bytes = serialize_working_copy_binding(binding)
        self._write_temp_and_replace(tmp_dir, binding_path, sidecar_bytes, prefix="binding-")

        self._best_effort_fsync_dir(self._paths.owner_dir(owner_fp))

        if docx_bytes is not None:
            if hashlib.sha256(docx_path.read_bytes()).hexdigest() != docx_sha256:
                raise WorkingCopyPublishError("post-write current.docx verification failed")
        if binding_path.read_bytes() != sidecar_bytes:
            raise WorkingCopyPublishError("post-write binding.json verification failed")

        return docx_sha256

    def _publish_and_finalize(
        self,
        *,
        owner_key: JobArtifactOwnerKey,
        repository: CvDocumentArtifactRepository,
        proposal_before: CvDocxProposalArtifact,
        docx_bytes: bytes | None,
        new_binding: WorkingCopyBinding,
        success_status: WorkingCopyCreateRefreshStatus,
    ) -> WorkingCopyCreateRefreshResult:
        owner_fp = owner_key.owner_key_fingerprint

        proposal_after = repository.get_current_proposal(owner_key)
        if _proposal_identity(proposal_after) != _proposal_identity(proposal_before):
            return WorkingCopyCreateRefreshResult(
                status=WorkingCopyCreateRefreshStatus.CURRENT_PROPOSAL_CHANGED_DURING_OPERATION
            )

        published_sha256 = self._atomic_publish(owner_fp, docx_bytes=docx_bytes, binding=new_binding)
        return WorkingCopyCreateRefreshResult(
            status=success_status,
            published_working_copy_sha256=published_sha256,
            published_binding=new_binding,
        )

    # -- create_or_refresh_working_copy -----------------------------------------

    def _create_or_refresh_under_lock(
        self, *, owner_key: JobArtifactOwnerKey, repository: CvDocumentArtifactRepository
    ) -> WorkingCopyCreateRefreshResult:
        owner_fp = owner_key.owner_key_fingerprint
        self._paths.ensure_owner_dirs(owner_fp)

        proposal_before = repository.get_current_proposal(owner_key)
        if proposal_before is None:
            return WorkingCopyCreateRefreshResult(status=WorkingCopyCreateRefreshStatus.NO_CURRENT_PROPOSAL)

        proposal_bytes = repository.read_current_proposal_bytes(owner_key)
        if proposal_bytes is None or hashlib.sha256(proposal_bytes).hexdigest() != proposal_before.generated_docx_content_hash:
            return WorkingCopyCreateRefreshResult(status=WorkingCopyCreateRefreshStatus.REPOSITORY_BYTES_HASH_MISMATCH)

        observation = self._stable_read_under_lock(owner_fp)
        if observation.docx_status == WorkingCopyStableReadStatus.WORKING_COPY_CHANGED_DURING_READ:
            return WorkingCopyCreateRefreshResult(status=WorkingCopyCreateRefreshStatus.WORKING_COPY_CHANGED_DURING_READ)
        if observation.docx_status in (
            WorkingCopyStableReadStatus.WORKING_COPY_UNREADABLE,
            WorkingCopyStableReadStatus.WORKING_COPY_LOCKED,
        ):
            return WorkingCopyCreateRefreshResult(status=WorkingCopyCreateRefreshStatus.WORKING_COPY_UNREADABLE)

        docx_present = observation.docx_status == WorkingCopyStableReadStatus.STABLE_READ_OK
        sidecar_state, sidecar_binding, _sidecar_raw_sha256 = _classify_sidecar(observation.sidecar_raw, owner_fp)

        outcome = _decide_create_refresh_branch(
            owner_key_fingerprint=owner_fp,
            docx_present=docx_present,
            docx_sha256=observation.docx_sha256,
            sidecar_state=sidecar_state,
            sidecar_binding=sidecar_binding,
            proposal_before=proposal_before,
        )

        if outcome.publish is None:
            return WorkingCopyCreateRefreshResult(status=outcome.status)

        return self._publish_and_finalize(
            owner_key=owner_key,
            repository=repository,
            proposal_before=proposal_before,
            docx_bytes=proposal_bytes if outcome.publish.rewrite_docx else None,
            new_binding=outcome.publish.new_binding,
            success_status=outcome.status,
        )

    # -- reset_working_copy_to_current_proposal ----------------------------------

    def _reset_under_lock(
        self,
        *,
        owner_key: JobArtifactOwnerKey,
        repository: CvDocumentArtifactRepository,
        confirmation: WorkingCopyResetConfirmation,
    ) -> WorkingCopyResetResult:
        owner_fp = owner_key.owner_key_fingerprint
        self._paths.ensure_owner_dirs(owner_fp)

        observation = self._stable_read_under_lock(owner_fp)
        if observation.docx_status == WorkingCopyStableReadStatus.WORKING_COPY_CHANGED_DURING_READ:
            return WorkingCopyResetResult(status=WorkingCopyResetStatus.WORKING_COPY_CHANGED_DURING_READ)
        if observation.docx_status in (
            WorkingCopyStableReadStatus.WORKING_COPY_MISSING,
            WorkingCopyStableReadStatus.WORKING_COPY_UNREADABLE,
            WorkingCopyStableReadStatus.WORKING_COPY_LOCKED,
        ):
            return WorkingCopyResetResult(status=WorkingCopyResetStatus.WORKING_COPY_CHANGED_SINCE_CONFIRMATION)

        observed_state = self._build_observed_state(owner_fp, observation)
        if observed_state.working_copy_sha256 != confirmation.expected_working_copy_sha256:
            return WorkingCopyResetResult(status=WorkingCopyResetStatus.WORKING_COPY_CHANGED_SINCE_CONFIRMATION)
        if observed_state.binding_state_fingerprint != confirmation.expected_binding_state_fingerprint:
            return WorkingCopyResetResult(status=WorkingCopyResetStatus.WORKING_COPY_BINDING_CHANGED_SINCE_CONFIRMATION)

        proposal_before = repository.get_current_proposal(owner_key)
        if proposal_before is None:
            return WorkingCopyResetResult(status=WorkingCopyResetStatus.NO_CURRENT_PROPOSAL)
        if (
            proposal_before.artifact_fingerprint != confirmation.expected_target_proposal_artifact_fingerprint
            or proposal_before.proposal_revision != confirmation.expected_target_proposal_revision
        ):
            return WorkingCopyResetResult(status=WorkingCopyResetStatus.CURRENT_PROPOSAL_CHANGED_SINCE_CONFIRMATION)

        proposal_bytes = repository.read_current_proposal_bytes(owner_key)
        if proposal_bytes is None or hashlib.sha256(proposal_bytes).hexdigest() != proposal_before.generated_docx_content_hash:
            return WorkingCopyResetResult(status=WorkingCopyResetStatus.REPOSITORY_BYTES_HASH_MISMATCH)

        proposal_after = repository.get_current_proposal(owner_key)
        if _proposal_identity(proposal_after) != _proposal_identity(proposal_before):
            return WorkingCopyResetResult(status=WorkingCopyResetStatus.CURRENT_PROPOSAL_CHANGED_DURING_OPERATION)

        new_binding = _new_binding_for(owner_fp, proposal_before)
        published_sha256 = self._atomic_publish(owner_fp, docx_bytes=proposal_bytes, binding=new_binding)

        return WorkingCopyResetResult(
            status=WorkingCopyResetStatus.RESET_TO_CURRENT_PROPOSAL,
            published_working_copy_sha256=published_sha256,
            published_binding=new_binding,
        )

    # -- inspect_working_copy -----------------------------------------------------

    def _inspect_under_lock(self, owner_fp: str) -> WorkingCopyInspectionResult:
        self._paths.ensure_owner_dirs(owner_fp)
        observation = self._stable_read_under_lock(owner_fp)

        if observation.docx_status in (
            WorkingCopyStableReadStatus.WORKING_COPY_MISSING,
            WorkingCopyStableReadStatus.WORKING_COPY_LOCKED,
        ):
            return WorkingCopyInspectionResult(status=WorkingCopyInspectionStatus.WORKING_COPY_MISSING)
        if observation.docx_status == WorkingCopyStableReadStatus.WORKING_COPY_UNREADABLE:
            return WorkingCopyInspectionResult(status=WorkingCopyInspectionStatus.WORKING_COPY_UNREADABLE)
        if observation.docx_status == WorkingCopyStableReadStatus.WORKING_COPY_CHANGED_DURING_READ:
            return WorkingCopyInspectionResult(status=WorkingCopyInspectionStatus.WORKING_COPY_CHANGED_DURING_READ)

        observed_state = self._build_observed_state(owner_fp, observation)
        return WorkingCopyInspectionResult(status=WorkingCopyInspectionStatus.OBSERVED, observed_state=observed_state)
