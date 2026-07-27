"""Explicit Provenance Stage P6-B2b-A: read-only Working Copy
reconciliation.

``run_working_copy_reconciliation`` is a pure observation pass over the
``owners/`` and ``locks/`` trees under a validated ``WorkingCopyPaths``.
It never moves, deletes, quarantines, or rewrites a ``current.docx``, a
``binding.json``, or a lock file -- calling it twice in a row on unchanged
storage always yields an identical report.

An edited working copy (``current.docx`` bytes that differ from
``binding.json``'s own ``last_known_tool_written_hash``) is **never** one
of the issue codes this module reports -- that is the exact, legitimate
signal ``create_or_refresh_working_copy`` treats as
``HALF_STATE_PRESERVED_POSSIBLE_USER_EDIT``. This module never emits a
global blob-orphan code (that remains exclusively
``cv_document_reconciliation.py``'s P6-B1 blob-store concern) -- every
issue here is scoped to one owner's working-copy pair or to the managed
tree's own structural shape.

``owner_keys_by_fingerprint``/``repository`` are optional: when supplied,
``STALE_BINDING`` and the "bound-to-current-revision-but-hash-disagrees"
half of ``BINDING_MISMATCH`` become checkable against the live Proposal
repository; when omitted, this module still reports every purely
structural issue (missing pairing, invalid sidecar JSON, owner mismatch,
symlinks, path escapes, empty/unreadable files, unexpected entries, lock
path anomalies) from the filesystem alone.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from app.schemas.cv_document_artifact import JobArtifactOwnerKey
from app.schemas.cv_document_working_copy import (
    WorkingCopyReconciliationIssue,
    WorkingCopyReconciliationIssueCode,
    WorkingCopyReconciliationReport,
    parse_working_copy_binding,
)
from app.services.cv_document_repository_protocol import CvDocumentArtifactRepository
from app.services.cv_document_working_copy_paths import WorkingCopyPaths


_OWNER_KEY_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
_EXPECTED_OWNER_ENTRIES = {"current.docx", "binding.json", "tmp"}


def _issue(
    code: WorkingCopyReconciliationIssueCode, detail: str, *, owner_key_fingerprint: str | None = None
) -> WorkingCopyReconciliationIssue:
    return WorkingCopyReconciliationIssue(issue_code=code, detail=detail, owner_key_fingerprint=owner_key_fingerprint)


def _scan_owner_dir(
    paths: WorkingCopyPaths,
    owner_fp: str,
    *,
    owner_keys_by_fingerprint: Mapping[str, JobArtifactOwnerKey] | None,
    repository: CvDocumentArtifactRepository | None,
) -> list[WorkingCopyReconciliationIssue]:
    issues: list[WorkingCopyReconciliationIssue] = []
    owner_dir = paths.owner_dir(owner_fp)

    for entry in owner_dir.iterdir():
        if entry.is_symlink():
            issues.append(
                _issue(
                    WorkingCopyReconciliationIssueCode.SYMLINK_OR_REPARSE_POINT,
                    f"{entry.name} is a symlink inside an owner directory",
                    owner_key_fingerprint=owner_fp,
                )
            )
            continue
        if entry.name not in _EXPECTED_OWNER_ENTRIES:
            issues.append(
                _issue(
                    WorkingCopyReconciliationIssueCode.UNEXPECTED_FILE,
                    f"unexpected entry {entry.name!r} inside an owner directory",
                    owner_key_fingerprint=owner_fp,
                )
            )

    docx_path = paths.current_docx_path(owner_fp)
    binding_path = paths.binding_path(owner_fp)

    docx_symlink = docx_path.is_symlink()
    binding_symlink = binding_path.is_symlink()
    if docx_symlink:
        issues.append(
            _issue(
                WorkingCopyReconciliationIssueCode.SYMLINK_OR_REPARSE_POINT,
                "current.docx is a symlink",
                owner_key_fingerprint=owner_fp,
            )
        )
    if binding_symlink:
        issues.append(
            _issue(
                WorkingCopyReconciliationIssueCode.SYMLINK_OR_REPARSE_POINT,
                "binding.json is a symlink",
                owner_key_fingerprint=owner_fp,
            )
        )

    docx_exists = docx_path.is_file() and not docx_symlink
    binding_exists = binding_path.is_file() and not binding_symlink

    docx_bytes: bytes | None = None
    if docx_exists:
        try:
            docx_bytes = docx_path.read_bytes()
        except OSError:
            issues.append(
                _issue(
                    WorkingCopyReconciliationIssueCode.UNREADABLE_FILE,
                    "current.docx could not be read",
                    owner_key_fingerprint=owner_fp,
                )
            )
        else:
            if len(docx_bytes) == 0:
                issues.append(
                    _issue(
                        WorkingCopyReconciliationIssueCode.EMPTY_FILE,
                        "current.docx is empty",
                        owner_key_fingerprint=owner_fp,
                    )
                )

    binding_raw: bytes | None = None
    if binding_exists:
        try:
            binding_raw = binding_path.read_bytes()
        except OSError:
            issues.append(
                _issue(
                    WorkingCopyReconciliationIssueCode.UNREADABLE_FILE,
                    "binding.json could not be read",
                    owner_key_fingerprint=owner_fp,
                )
            )
        else:
            if len(binding_raw) == 0:
                issues.append(
                    _issue(
                        WorkingCopyReconciliationIssueCode.EMPTY_FILE,
                        "binding.json is empty",
                        owner_key_fingerprint=owner_fp,
                    )
                )

    if docx_exists and not binding_exists:
        issues.append(
            _issue(
                WorkingCopyReconciliationIssueCode.DOCX_WITHOUT_SIDECAR,
                "current.docx exists with no binding.json sidecar",
                owner_key_fingerprint=owner_fp,
            )
        )
    if binding_exists and not docx_exists:
        issues.append(
            _issue(
                WorkingCopyReconciliationIssueCode.SIDECAR_WITHOUT_DOCX,
                "binding.json exists with no current.docx",
                owner_key_fingerprint=owner_fp,
            )
        )

    binding = None
    if binding_raw is not None and len(binding_raw) > 0:
        binding = parse_working_copy_binding(binding_raw)
        if binding is None:
            issues.append(
                _issue(
                    WorkingCopyReconciliationIssueCode.INVALID_SIDECAR,
                    "binding.json does not parse as a valid WorkingCopyBinding",
                    owner_key_fingerprint=owner_fp,
                )
            )
        elif binding.owner_key_fingerprint != owner_fp:
            issues.append(
                _issue(
                    WorkingCopyReconciliationIssueCode.OWNER_MISMATCH,
                    "binding.json owner_key_fingerprint does not match its containing directory",
                    owner_key_fingerprint=owner_fp,
                )
            )
            binding = None

    if binding is not None and repository is not None and owner_keys_by_fingerprint is not None:
        owner_key = owner_keys_by_fingerprint.get(owner_fp)
        if owner_key is not None:
            current_proposal = repository.get_current_proposal(owner_key)
            if current_proposal is not None:
                if binding.bound_proposal_revision == current_proposal.proposal_revision:
                    if (
                        binding.bound_proposal_artifact_fingerprint != current_proposal.artifact_fingerprint
                        or binding.bound_generated_proposal_sha256 != current_proposal.generated_docx_content_hash
                    ):
                        issues.append(
                            _issue(
                                WorkingCopyReconciliationIssueCode.BINDING_MISMATCH,
                                "binding.json claims the current proposal revision but disagrees with its recorded identity/hash",
                                owner_key_fingerprint=owner_fp,
                            )
                        )
                elif binding.bound_proposal_revision < current_proposal.proposal_revision:
                    issues.append(
                        _issue(
                            WorkingCopyReconciliationIssueCode.STALE_BINDING,
                            "binding.json is bound to an older proposal revision than the current one",
                            owner_key_fingerprint=owner_fp,
                        )
                    )

    lock_path = paths.lock_path(owner_fp)
    if lock_path.exists() or lock_path.is_symlink():
        if lock_path.is_symlink() or not lock_path.is_file():
            issues.append(
                _issue(
                    WorkingCopyReconciliationIssueCode.LOCK_PATH_ANOMALY,
                    "lock file is not a plain regular file",
                    owner_key_fingerprint=owner_fp,
                )
            )

    return issues


def run_working_copy_reconciliation(
    paths: WorkingCopyPaths,
    *,
    owner_keys_by_fingerprint: Mapping[str, JobArtifactOwnerKey] | None = None,
    repository: CvDocumentArtifactRepository | None = None,
) -> WorkingCopyReconciliationReport:
    """Produce a read-only inconsistency report over every owner directory
    currently present under ``paths.owners_dir``. Never mutates anything --
    see the module docstring for the exact read-only guarantee."""

    issues: list[WorkingCopyReconciliationIssue] = []
    scanned = 0

    for entry in sorted(paths.owners_dir.iterdir()):
        if entry.is_symlink():
            issues.append(
                _issue(
                    WorkingCopyReconciliationIssueCode.SYMLINK_OR_REPARSE_POINT,
                    f"{entry.name} under owners/ is a symlink",
                )
            )
            continue
        if not entry.is_dir() or not _OWNER_KEY_FINGERPRINT_RE.match(entry.name):
            issues.append(
                _issue(
                    WorkingCopyReconciliationIssueCode.UNEXPECTED_FILE,
                    f"{entry.name!r} under owners/ is not a valid owner_key_fingerprint directory",
                )
            )
            continue

        scanned += 1
        issues.extend(
            _scan_owner_dir(
                paths, entry.name, owner_keys_by_fingerprint=owner_keys_by_fingerprint, repository=repository
            )
        )

    if paths.locks_dir.exists():
        for entry in sorted(paths.locks_dir.iterdir()):
            if entry.is_symlink():
                issues.append(
                    _issue(
                        WorkingCopyReconciliationIssueCode.SYMLINK_OR_REPARSE_POINT,
                        f"{entry.name} under locks/ is a symlink",
                    )
                )
                continue
            if not entry.is_file() or not entry.name.endswith(".lock"):
                issues.append(
                    _issue(
                        WorkingCopyReconciliationIssueCode.LOCK_PATH_ANOMALY,
                        f"{entry.name!r} under locks/ is not a plain *.lock file",
                    )
                )
                continue
            stem = entry.name[: -len(".lock")]
            if not _OWNER_KEY_FINGERPRINT_RE.match(stem):
                issues.append(
                    _issue(
                        WorkingCopyReconciliationIssueCode.LOCK_PATH_ANOMALY,
                        f"{entry.name!r} does not name a valid owner_key_fingerprint",
                    )
                )

    return WorkingCopyReconciliationReport(issues=tuple(issues), scanned_owner_count=scanned)
