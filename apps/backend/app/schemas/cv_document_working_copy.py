"""Explicit Provenance Stage P6-B2b-A: the Working Copy Foundation
contracts.

A *working copy* is a per-owner, locally editable ``current.docx`` living
outside Git under a caller-supplied ``working_copy_root``, paired with a
``binding.json`` sidecar that records which current Proposal DOCX it was
last published from. The sidecar is **cache and binding metadata only** --
authority always remains the current Proposal DOCX slot, the Proposal
artifact metadata, and the Proposal repository bytes exposed through
``CvDocumentArtifactRepository`` (``app/services/cv_document_repository_protocol.py``).
This module never introduces a second source of truth for a Proposal's
content.

Every model here is ``frozen=True``/``extra="forbid"``. Every status enum
lists both success and failure outcomes of one operation together (the
same pattern P6-A/P6-B1 use), so a caller always inspects exactly one
closed ``status`` field rather than juggling a raised exception and a
separate result.

P6-B2b-A defines no concrete ``FinalDocxSourceReader``, no finalization
wiring, no PDF conversion, no local file opener, no API, no router, no
frontend, no SQL, and no migration -- see
``docs/explicit-provenance-stage-p6b2b-working-copy-foundation.md``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.truth_entity import SHA256_PATTERN
from app.services.truth_fingerprint import canonical_json_bytes
import hashlib


WORKING_COPY_BINDING_SCHEMA_VERSION = "cv-document-working-copy-binding-schema-v1"
WORKING_COPY_OBSERVED_STATE_SCHEMA_VERSION = "cv-document-working-copy-observed-state-schema-v1"
WORKING_COPY_BINDING_STATE_FINGERPRINT_VERSION = "cv-document-working-copy-binding-state-fingerprint-v1"

#: Exactly 64 lowercase hex characters -- the only dynamic filesystem path
#: segment the Root Contract ever allows (see
#: ``cv_document_working_copy_paths.py``).
OWNER_KEY_FINGERPRINT_PATTERN = SHA256_PATTERN


# -- Root contract --------------------------------------------------------------


class WorkingCopyRootViolationCode(StrEnum):
    """Closed set of reasons ``WorkingCopyPaths`` fail-closed rejects a
    ``(working_copy_root, blob_store_root)`` pair. Never silently coerced,
    never guessed -- see ``cv_document_working_copy_paths.py``."""

    RELATIVE_ROOT = "RELATIVE_ROOT"
    ROOT_INSIDE_GIT_WORKTREE = "ROOT_INSIDE_GIT_WORKTREE"
    ROOT_EQUALS_BLOB_ROOT = "ROOT_EQUALS_BLOB_ROOT"
    ROOT_INSIDE_BLOB_ROOT = "ROOT_INSIDE_BLOB_ROOT"
    BLOB_ROOT_INSIDE_WORKING_COPY_ROOT = "BLOB_ROOT_INSIDE_WORKING_COPY_ROOT"
    SYMLINK_OR_REPARSE_POINT = "SYMLINK_OR_REPARSE_POINT"
    PATH_ESCAPE = "PATH_ESCAPE"
    INVALID_OWNER_KEY_FINGERPRINT = "INVALID_OWNER_KEY_FINGERPRINT"


# -- Working copy binding (sidecar) ----------------------------------------------


class WorkingCopyBinding(BaseModel):
    """The exact JSON structure persisted as one owner's ``binding.json``
    sidecar. Cache and binding metadata only -- never a second authority for
    Proposal content. Every field is independently re-checked against the
    live Proposal on every operation that reads this sidecar; nothing on
    this model is trusted at face value."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    owner_key_fingerprint: str = Field(pattern=SHA256_PATTERN)
    bound_proposal_artifact_fingerprint: str = Field(pattern=SHA256_PATTERN)
    bound_proposal_revision: int = Field(ge=1)
    bound_generated_proposal_sha256: str = Field(pattern=SHA256_PATTERN)
    last_known_tool_written_hash: str = Field(pattern=SHA256_PATTERN)
    binding_schema_version: Literal["cv-document-working-copy-binding-schema-v1"] = (
        WORKING_COPY_BINDING_SCHEMA_VERSION
    )


def serialize_working_copy_binding(binding: WorkingCopyBinding) -> bytes:
    """Deterministic bytes for a ``binding.json`` sidecar write -- the same
    ``WorkingCopyBinding`` value always serializes to the exact same bytes,
    so a rewrite of an unchanged binding never produces a spurious diff."""

    return canonical_json_bytes(binding.model_dump(mode="json"))


def parse_working_copy_binding(raw: bytes) -> WorkingCopyBinding | None:
    """Parse raw sidecar bytes into a ``WorkingCopyBinding``, or ``None`` on
    any structural/schema failure -- never raises to the caller."""

    try:
        import json

        data = json.loads(raw.decode("utf-8"))
        return WorkingCopyBinding.model_validate(data)
    except Exception:
        return None


# -- Working copy observed state --------------------------------------------------


class WorkingCopyBindingState(StrEnum):
    """The closed set of states a ``binding.json`` sidecar can be observed
    in. Never inferred from ``mtime`` -- always derived from an actual read
    and parse attempt."""

    VALID = "VALID"
    MISSING = "MISSING"
    INVALID = "INVALID"


class WorkingCopyObservedState(BaseModel):
    """A single, closed snapshot of one owner's working copy, taken under
    the owner lock and a stable read of ``current.docx``. Only ever built
    when ``current.docx`` was successfully, stably read -- a missing or
    unreadable working copy is reported by the wrapping operation result's
    own status instead, never by a partially-populated
    ``WorkingCopyObservedState``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    owner_key_fingerprint: str = Field(pattern=SHA256_PATTERN)
    working_copy_sha256: str = Field(pattern=SHA256_PATTERN)
    binding_state: WorkingCopyBindingState
    binding_state_fingerprint: str = Field(pattern=SHA256_PATTERN)
    sidecar_raw_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    binding_schema_version: str | None = None
    bound_proposal_artifact_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)
    bound_proposal_revision: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _state_consistency(self) -> "WorkingCopyObservedState":
        if self.binding_state == WorkingCopyBindingState.MISSING:
            if self.sidecar_raw_sha256 is not None:
                raise ValueError("MISSING binding_state must carry no sidecar_raw_sha256")
            if self.binding_schema_version is not None or self.bound_proposal_artifact_fingerprint is not None or self.bound_proposal_revision is not None:
                raise ValueError("MISSING binding_state must carry no parsed binding fields")
        elif self.binding_state == WorkingCopyBindingState.INVALID:
            if self.sidecar_raw_sha256 is None:
                raise ValueError("INVALID binding_state requires sidecar_raw_sha256")
        elif self.binding_state == WorkingCopyBindingState.VALID:
            if self.sidecar_raw_sha256 is None:
                raise ValueError("VALID binding_state requires sidecar_raw_sha256")
            if (
                self.binding_schema_version is None
                or self.bound_proposal_artifact_fingerprint is None
                or self.bound_proposal_revision is None
            ):
                raise ValueError("VALID binding_state requires the full parsed binding projection")
        return self


def compute_binding_state_fingerprint(
    *,
    owner_key_fingerprint: str,
    working_copy_sha256: str,
    binding_state: WorkingCopyBindingState,
    sidecar_raw_sha256: str | None,
    binding_schema_version: str | None,
    bound_proposal_artifact_fingerprint: str | None,
    bound_proposal_revision: int | None,
) -> str:
    content = {
        "fingerprint_version": WORKING_COPY_BINDING_STATE_FINGERPRINT_VERSION,
        "content": {
            "observed_state_schema_version": WORKING_COPY_OBSERVED_STATE_SCHEMA_VERSION,
            "owner_key_fingerprint": owner_key_fingerprint,
            "working_copy_sha256": working_copy_sha256,
            "binding_state": binding_state.value,
            "sidecar_raw_sha256": sidecar_raw_sha256,
            "binding_schema_version": binding_schema_version,
            "bound_proposal_artifact_fingerprint": bound_proposal_artifact_fingerprint,
            "bound_proposal_revision": bound_proposal_revision,
        },
    }
    return hashlib.sha256(canonical_json_bytes(content)).hexdigest()


# -- Stable read -------------------------------------------------------------------


class WorkingCopyStableReadStatus(StrEnum):
    """Closed outcomes of one stable read of ``current.docx``. Never a raw
    ``OSError``/``WinError`` -- see ``cv_document_working_copy_stable_read.py``
    for the POSIX/Windows mapping."""

    STABLE_READ_OK = "STABLE_READ_OK"
    WORKING_COPY_MISSING = "WORKING_COPY_MISSING"
    WORKING_COPY_LOCKED = "WORKING_COPY_LOCKED"
    WORKING_COPY_UNREADABLE = "WORKING_COPY_UNREADABLE"
    WORKING_COPY_CHANGED_DURING_READ = "WORKING_COPY_CHANGED_DURING_READ"


class WorkingCopyStableReadResult(BaseModel):
    """The single return value of one platform stable-read call. Only
    ``STABLE_READ_OK`` ever carries bytes/hash."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: WorkingCopyStableReadStatus
    data: bytes | None = None
    sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    byte_size: int | None = Field(default=None, ge=0)
    diagnostics: tuple[str, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _status_consistency(self) -> "WorkingCopyStableReadResult":
        if self.status == WorkingCopyStableReadStatus.STABLE_READ_OK:
            if self.data is None or self.sha256 is None or self.byte_size is None:
                raise ValueError("STABLE_READ_OK requires data/sha256/byte_size")
        else:
            if self.data is not None:
                raise ValueError(f"{self.status} must carry no data")
        return self


# -- Owner lock ----------------------------------------------------------------


class OwnerLockAcquireStatus(StrEnum):
    ACQUIRED = "ACQUIRED"
    OWNER_LOCK_TIMEOUT = "OWNER_LOCK_TIMEOUT"
    LOCK_PATH_IDENTITY_MISMATCH = "LOCK_PATH_IDENTITY_MISMATCH"


# -- create_or_refresh_working_copy -----------------------------------------------


class WorkingCopyCreateRefreshStatus(StrEnum):
    """The closed set of outcomes ``create_or_refresh_working_copy`` can
    return. Only the four ``*_CREATED``/``*_RECOVERED``/``*_REFRESHED``
    members and ``WORKING_COPY_UNCHANGED_NO_REFRESH_NEEDED`` ever publish or
    confirm a current pair; every other member leaves the working copy
    exactly as it was found."""

    WORKING_COPY_CREATED = "WORKING_COPY_CREATED"
    WORKING_COPY_REFRESHED = "WORKING_COPY_REFRESHED"
    WORKING_COPY_RECREATED_FROM_CURRENT_PROPOSAL = "WORKING_COPY_RECREATED_FROM_CURRENT_PROPOSAL"
    RECOVERED_UNEDITED_HALF_STATE = "RECOVERED_UNEDITED_HALF_STATE"
    WORKING_COPY_UNCHANGED_NO_REFRESH_NEEDED = "WORKING_COPY_UNCHANGED_NO_REFRESH_NEEDED"
    HALF_STATE_PRESERVED_POSSIBLE_USER_EDIT = "HALF_STATE_PRESERVED_POSSIBLE_USER_EDIT"
    NO_CURRENT_PROPOSAL = "NO_CURRENT_PROPOSAL"
    REPOSITORY_BYTES_HASH_MISMATCH = "REPOSITORY_BYTES_HASH_MISMATCH"
    CURRENT_PROPOSAL_CHANGED_DURING_OPERATION = "CURRENT_PROPOSAL_CHANGED_DURING_OPERATION"
    WORKING_COPY_CHANGED_DURING_READ = "WORKING_COPY_CHANGED_DURING_READ"
    WORKING_COPY_UNREADABLE = "WORKING_COPY_UNREADABLE"
    OWNER_LOCK_TIMEOUT = "OWNER_LOCK_TIMEOUT"
    LOCK_PATH_IDENTITY_MISMATCH = "LOCK_PATH_IDENTITY_MISMATCH"
    LOCK_CAPABILITY_INVALID = "LOCK_CAPABILITY_INVALID"
    POST_WRITE_VERIFICATION_FAILED = "POST_WRITE_VERIFICATION_FAILED"
    STORAGE_UNAVAILABLE = "STORAGE_UNAVAILABLE"


_CREATE_REFRESH_PUBLISHING_STATUSES = frozenset(
    {
        WorkingCopyCreateRefreshStatus.WORKING_COPY_CREATED,
        WorkingCopyCreateRefreshStatus.WORKING_COPY_REFRESHED,
        WorkingCopyCreateRefreshStatus.WORKING_COPY_RECREATED_FROM_CURRENT_PROPOSAL,
        WorkingCopyCreateRefreshStatus.RECOVERED_UNEDITED_HALF_STATE,
    }
)


class WorkingCopyCreateRefreshResult(BaseModel):
    """The single return value of ``create_or_refresh_working_copy``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: WorkingCopyCreateRefreshStatus
    published_working_copy_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    published_binding: WorkingCopyBinding | None = None
    diagnostics: tuple[str, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _status_consistency(self) -> "WorkingCopyCreateRefreshResult":
        if self.status in _CREATE_REFRESH_PUBLISHING_STATUSES:
            if self.published_working_copy_sha256 is None or self.published_binding is None:
                raise ValueError(f"{self.status} requires published_working_copy_sha256 and published_binding")
        else:
            if self.published_working_copy_sha256 is not None or self.published_binding is not None:
                raise ValueError(f"{self.status} must carry no published pair")
        return self


# -- reset_working_copy_to_current_proposal ---------------------------------------


class WorkingCopyResetConfirmation(BaseModel):
    """The exact confirmation a caller must present to
    ``reset_working_copy_to_current_proposal``. Never carries the caller's
    own DOCX bytes -- a reset always publishes exact repository proposal
    bytes only."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    expected_working_copy_sha256: str = Field(pattern=SHA256_PATTERN)
    expected_binding_state_fingerprint: str = Field(pattern=SHA256_PATTERN)
    expected_target_proposal_artifact_fingerprint: str = Field(pattern=SHA256_PATTERN)
    expected_target_proposal_revision: int = Field(ge=1)


class WorkingCopyResetStatus(StrEnum):
    RESET_TO_CURRENT_PROPOSAL = "RESET_TO_CURRENT_PROPOSAL"
    WORKING_COPY_CHANGED_SINCE_CONFIRMATION = "WORKING_COPY_CHANGED_SINCE_CONFIRMATION"
    WORKING_COPY_BINDING_CHANGED_SINCE_CONFIRMATION = "WORKING_COPY_BINDING_CHANGED_SINCE_CONFIRMATION"
    CURRENT_PROPOSAL_CHANGED_SINCE_CONFIRMATION = "CURRENT_PROPOSAL_CHANGED_SINCE_CONFIRMATION"
    CURRENT_PROPOSAL_CHANGED_DURING_OPERATION = "CURRENT_PROPOSAL_CHANGED_DURING_OPERATION"
    REPOSITORY_BYTES_HASH_MISMATCH = "REPOSITORY_BYTES_HASH_MISMATCH"
    NO_CURRENT_PROPOSAL = "NO_CURRENT_PROPOSAL"
    WORKING_COPY_CHANGED_DURING_READ = "WORKING_COPY_CHANGED_DURING_READ"
    OWNER_LOCK_TIMEOUT = "OWNER_LOCK_TIMEOUT"
    LOCK_PATH_IDENTITY_MISMATCH = "LOCK_PATH_IDENTITY_MISMATCH"
    LOCK_CAPABILITY_INVALID = "LOCK_CAPABILITY_INVALID"
    POST_WRITE_VERIFICATION_FAILED = "POST_WRITE_VERIFICATION_FAILED"
    STORAGE_UNAVAILABLE = "STORAGE_UNAVAILABLE"


class WorkingCopyResetResult(BaseModel):
    """The single return value of
    ``reset_working_copy_to_current_proposal``. Only
    ``RESET_TO_CURRENT_PROPOSAL`` ever publishes a new pair."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: WorkingCopyResetStatus
    published_working_copy_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    published_binding: WorkingCopyBinding | None = None
    diagnostics: tuple[str, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _status_consistency(self) -> "WorkingCopyResetResult":
        if self.status == WorkingCopyResetStatus.RESET_TO_CURRENT_PROPOSAL:
            if self.published_working_copy_sha256 is None or self.published_binding is None:
                raise ValueError("RESET_TO_CURRENT_PROPOSAL requires published_working_copy_sha256 and published_binding")
        else:
            if self.published_working_copy_sha256 is not None or self.published_binding is not None:
                raise ValueError(f"{self.status} must carry no published pair")
        return self


# -- inspect_working_copy -----------------------------------------------------------


class WorkingCopyInspectionStatus(StrEnum):
    OBSERVED = "OBSERVED"
    WORKING_COPY_MISSING = "WORKING_COPY_MISSING"
    WORKING_COPY_UNREADABLE = "WORKING_COPY_UNREADABLE"
    WORKING_COPY_CHANGED_DURING_READ = "WORKING_COPY_CHANGED_DURING_READ"
    OWNER_LOCK_TIMEOUT = "OWNER_LOCK_TIMEOUT"
    LOCK_PATH_IDENTITY_MISMATCH = "LOCK_PATH_IDENTITY_MISMATCH"
    LOCK_CAPABILITY_INVALID = "LOCK_CAPABILITY_INVALID"
    STORAGE_UNAVAILABLE = "STORAGE_UNAVAILABLE"


class WorkingCopyInspectionResult(BaseModel):
    """The single return value of ``inspect_working_copy`` -- read-only,
    never mutates the working copy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: WorkingCopyInspectionStatus
    observed_state: WorkingCopyObservedState | None = None
    diagnostics: tuple[str, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _status_consistency(self) -> "WorkingCopyInspectionResult":
        if self.status == WorkingCopyInspectionStatus.OBSERVED:
            if self.observed_state is None:
                raise ValueError("OBSERVED requires observed_state")
        else:
            if self.observed_state is not None:
                raise ValueError(f"{self.status} must carry no observed_state")
        return self


# -- Reconciliation -----------------------------------------------------------------


class WorkingCopyReconciliationIssueCode(StrEnum):
    """Closed set of P6-B2b-A working-copy inconsistencies a reconciliation
    pass can report. Always read-only to produce -- see
    ``cv_document_working_copy_reconciliation.py``. An edited working copy
    (``current.docx`` bytes differ from ``binding.json``'s
    ``last_known_tool_written_hash``) is never one of these -- it is the
    expected, legitimate signal ``create_or_refresh_working_copy`` treats as
    ``HALF_STATE_PRESERVED_POSSIBLE_USER_EDIT``, not a reconciliation
    finding."""

    DOCX_WITHOUT_SIDECAR = "DOCX_WITHOUT_SIDECAR"
    SIDECAR_WITHOUT_DOCX = "SIDECAR_WITHOUT_DOCX"
    INVALID_SIDECAR = "INVALID_SIDECAR"
    OWNER_MISMATCH = "OWNER_MISMATCH"
    BINDING_MISMATCH = "BINDING_MISMATCH"
    STALE_BINDING = "STALE_BINDING"
    SYMLINK_OR_REPARSE_POINT = "SYMLINK_OR_REPARSE_POINT"
    PATH_ESCAPE = "PATH_ESCAPE"
    EMPTY_FILE = "EMPTY_FILE"
    UNREADABLE_FILE = "UNREADABLE_FILE"
    UNEXPECTED_FILE = "UNEXPECTED_FILE"
    LOCK_PATH_ANOMALY = "LOCK_PATH_ANOMALY"


class WorkingCopyReconciliationIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    issue_code: WorkingCopyReconciliationIssueCode
    detail: str = Field(min_length=1, max_length=2000)
    owner_key_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)


class WorkingCopyReconciliationReport(BaseModel):
    """The single return value of ``run_working_copy_reconciliation``.
    Always read-only to produce -- never moves, deletes, or rewrites a
    working copy, a sidecar, or a lock file."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    issues: tuple[WorkingCopyReconciliationIssue, ...] = Field(default_factory=tuple)
    scanned_owner_count: int = Field(ge=0)
