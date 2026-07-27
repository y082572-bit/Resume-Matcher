"""Integration tests for the P6-B2b-A read-only Working Copy
reconciliation pass (``cv_document_working_copy_reconciliation.py``):
every listed issue category is detected across a multi-owner tree,
standard mode never mutates anything, an edited working copy is never a
finding, and reconciliation is idempotent.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from app.schemas.cv_document_artifact import ArtifactOwnerKind, CvDocxProposalArtifact, JobArtifactOwnerKey
from app.schemas.cv_document_working_copy import (
    WorkingCopyBinding,
    WorkingCopyReconciliationIssueCode,
    serialize_working_copy_binding,
)
from app.services.cv_document_owner_identity import build_owner_key
from app.services.cv_document_repository_protocol import InMemoryCvDocumentArtifactRepository
from app.services.cv_document_working_copy_paths import WorkingCopyPaths
from app.services.cv_document_working_copy_reconciliation import run_working_copy_reconciliation
from app.services.cv_document_working_copy_store import WorkingCopyStore


def _owner_key(ref: str) -> JobArtifactOwnerKey:
    return build_owner_key(person_entity_id=uuid4(), owner_kind=ArtifactOwnerKind.JOB, owner_reference_id=ref)


def _artifact(owner_key: JobArtifactOwnerKey, *, docx_bytes: bytes, revision: int, artifact_fingerprint: str) -> CvDocxProposalArtifact:
    docx_hash = hashlib.sha256(docx_bytes).hexdigest()
    return CvDocxProposalArtifact(
        artifact_id=uuid4(),
        owner_key=owner_key,
        approved_cv_content_fingerprint="a" * 64,
        content_plan_fingerprint="b" * 64,
        document_schema_version="v1",
        rendering_policy_version="v1",
        renderer_adapter_id="adapter",
        template_fingerprint="c" * 64,
        generation_input_fingerprint="d" * 64,
        generated_docx_content_hash=docx_hash,
        current_file_hash=docx_hash,
        proposal_revision=revision,
        superseded=False,
        artifact_fingerprint=artifact_fingerprint,
    )


@pytest.fixture
def env(tmp_path: Path):
    repo = InMemoryCvDocumentArtifactRepository()
    paths = WorkingCopyPaths(tmp_path / "working_copy_root", tmp_path / "blob_store_root")
    store = WorkingCopyStore(paths, max_docx_size_bytes=10_000_000)
    return repo, paths, store


def _issue_codes(report) -> set[WorkingCopyReconciliationIssueCode]:
    return {issue.issue_code for issue in report.issues}


# 1: a clean, freshly created pair reports nothing -------------------------------------


def test_clean_pair_reports_no_issues(env) -> None:
    repo, paths, store = env
    owner_key = _owner_key("clean")
    docx_bytes = b"clean pair payload"
    artifact = _artifact(owner_key, docx_bytes=docx_bytes, revision=1, artifact_fingerprint="e" * 64)
    repo.replace_current_proposal(owner_key, artifact, docx_bytes, expected_previous_revision=None)
    store.create_or_refresh_working_copy(owner_key=owner_key, repository=repo)

    report = run_working_copy_reconciliation(paths)
    assert report.issues == ()
    assert report.scanned_owner_count == 1


# 2: an edited working copy is never a finding ------------------------------------------


def test_edited_working_copy_is_never_reported_as_an_issue(env) -> None:
    repo, paths, store = env
    owner_key = _owner_key("edited")
    docx_bytes = b"original payload"
    artifact = _artifact(owner_key, docx_bytes=docx_bytes, revision=1, artifact_fingerprint="e" * 64)
    repo.replace_current_proposal(owner_key, artifact, docx_bytes, expected_previous_revision=None)
    store.create_or_refresh_working_copy(owner_key=owner_key, repository=repo)

    paths.current_docx_path(owner_key.owner_key_fingerprint).write_bytes(b"the user edited this docx by hand")

    report = run_working_copy_reconciliation(paths, owner_keys_by_fingerprint={owner_key.owner_key_fingerprint: owner_key}, repository=repo)
    assert report.issues == ()


# 3: docx without sidecar / sidecar without docx -----------------------------------------


def test_docx_without_sidecar_is_reported(env) -> None:
    _repo, paths, _store = env
    owner_key = _owner_key("docx-only")
    fp = owner_key.owner_key_fingerprint
    paths.ensure_owner_dirs(fp)
    paths.current_docx_path(fp).write_bytes(b"lonely docx bytes")

    report = run_working_copy_reconciliation(paths)
    assert WorkingCopyReconciliationIssueCode.DOCX_WITHOUT_SIDECAR in _issue_codes(report)


def test_sidecar_without_docx_is_reported(env) -> None:
    _repo, paths, _store = env
    owner_key = _owner_key("sidecar-only")
    fp = owner_key.owner_key_fingerprint
    paths.ensure_owner_dirs(fp)
    binding = WorkingCopyBinding(
        owner_key_fingerprint=fp,
        bound_proposal_artifact_fingerprint="e" * 64,
        bound_proposal_revision=1,
        bound_generated_proposal_sha256="f" * 64,
        last_known_tool_written_hash="f" * 64,
    )
    paths.binding_path(fp).write_bytes(serialize_working_copy_binding(binding))

    report = run_working_copy_reconciliation(paths)
    assert WorkingCopyReconciliationIssueCode.SIDECAR_WITHOUT_DOCX in _issue_codes(report)


# 4: invalid sidecar JSON ------------------------------------------------------------------


def test_invalid_sidecar_json_is_reported(env) -> None:
    _repo, paths, _store = env
    owner_key = _owner_key("invalid-sidecar")
    fp = owner_key.owner_key_fingerprint
    paths.ensure_owner_dirs(fp)
    paths.current_docx_path(fp).write_bytes(b"some docx bytes")
    paths.binding_path(fp).write_bytes(b"{this is not valid json")

    report = run_working_copy_reconciliation(paths)
    assert WorkingCopyReconciliationIssueCode.INVALID_SIDECAR in _issue_codes(report)


# 5: owner mismatch --------------------------------------------------------------------------


def test_owner_mismatch_is_reported(env) -> None:
    _repo, paths, _store = env
    owner_key = _owner_key("owner-mismatch")
    fp = owner_key.owner_key_fingerprint
    other_fp = "9" * 64
    paths.ensure_owner_dirs(fp)
    paths.current_docx_path(fp).write_bytes(b"docx bytes")
    binding = WorkingCopyBinding(
        owner_key_fingerprint=other_fp,  # deliberately does not match the containing directory
        bound_proposal_artifact_fingerprint="e" * 64,
        bound_proposal_revision=1,
        bound_generated_proposal_sha256="f" * 64,
        last_known_tool_written_hash="f" * 64,
    )
    paths.binding_path(fp).write_bytes(serialize_working_copy_binding(binding))

    report = run_working_copy_reconciliation(paths)
    assert WorkingCopyReconciliationIssueCode.OWNER_MISMATCH in _issue_codes(report)


# 6: stale binding (older revision than the live current proposal) -------------------------


def test_stale_binding_is_reported_when_repository_is_supplied(env) -> None:
    repo, paths, store = env
    owner_key = _owner_key("stale")
    fp = owner_key.owner_key_fingerprint
    docx_bytes_v1 = b"v1 payload"
    artifact_v1 = _artifact(owner_key, docx_bytes=docx_bytes_v1, revision=1, artifact_fingerprint="e" * 64)
    repo.replace_current_proposal(owner_key, artifact_v1, docx_bytes_v1, expected_previous_revision=None)
    store.create_or_refresh_working_copy(owner_key=owner_key, repository=repo)

    docx_bytes_v2 = b"v2 payload"
    artifact_v2 = _artifact(owner_key, docx_bytes=docx_bytes_v2, revision=2, artifact_fingerprint="f" * 64)
    repo.replace_current_proposal(owner_key, artifact_v2, docx_bytes_v2, expected_previous_revision=1)
    # Deliberately do not refresh the working copy -- its sidecar still
    # names revision 1 while the repository has moved to revision 2.

    report = run_working_copy_reconciliation(paths, owner_keys_by_fingerprint={fp: owner_key}, repository=repo)
    assert WorkingCopyReconciliationIssueCode.STALE_BINDING in _issue_codes(report)


def test_stale_binding_is_not_reported_without_a_repository(env) -> None:
    repo, paths, store = env
    owner_key = _owner_key("stale-no-repo")
    docx_bytes_v1 = b"v1 payload"
    artifact_v1 = _artifact(owner_key, docx_bytes=docx_bytes_v1, revision=1, artifact_fingerprint="e" * 64)
    repo.replace_current_proposal(owner_key, artifact_v1, docx_bytes_v1, expected_previous_revision=None)
    store.create_or_refresh_working_copy(owner_key=owner_key, repository=repo)

    docx_bytes_v2 = b"v2 payload"
    artifact_v2 = _artifact(owner_key, docx_bytes=docx_bytes_v2, revision=2, artifact_fingerprint="f" * 64)
    repo.replace_current_proposal(owner_key, artifact_v2, docx_bytes_v2, expected_previous_revision=1)

    report = run_working_copy_reconciliation(paths)  # no repository supplied
    assert WorkingCopyReconciliationIssueCode.STALE_BINDING not in _issue_codes(report)


# 7: binding mismatch (bound to current revision but hash disagrees) -----------------------


def test_binding_mismatch_is_reported_when_bound_to_current_revision_with_wrong_hash(env) -> None:
    repo, paths, _store = env
    owner_key = _owner_key("mismatch")
    fp = owner_key.owner_key_fingerprint
    docx_bytes = b"current payload"
    artifact = _artifact(owner_key, docx_bytes=docx_bytes, revision=1, artifact_fingerprint="e" * 64)
    repo.replace_current_proposal(owner_key, artifact, docx_bytes, expected_previous_revision=None)

    paths.ensure_owner_dirs(fp)
    paths.current_docx_path(fp).write_bytes(docx_bytes)
    tampered_binding = WorkingCopyBinding(
        owner_key_fingerprint=fp,
        bound_proposal_artifact_fingerprint=artifact.artifact_fingerprint,
        bound_proposal_revision=1,
        bound_generated_proposal_sha256="0" * 64,  # disagrees with the real proposal hash
        last_known_tool_written_hash=hashlib.sha256(docx_bytes).hexdigest(),
    )
    paths.binding_path(fp).write_bytes(serialize_working_copy_binding(tampered_binding))

    report = run_working_copy_reconciliation(paths, owner_keys_by_fingerprint={fp: owner_key}, repository=repo)
    assert WorkingCopyReconciliationIssueCode.BINDING_MISMATCH in _issue_codes(report)


# 8: symlink / empty / unexpected file / lock anomaly ---------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink semantics")
def test_symlinked_docx_is_reported(env) -> None:
    _repo, paths, _store = env
    owner_key = _owner_key("symlinked")
    fp = owner_key.owner_key_fingerprint
    paths.ensure_owner_dirs(fp)
    real_file = paths.owner_dir(fp) / "real.docx"
    real_file.write_bytes(b"real bytes")
    paths.current_docx_path(fp).symlink_to(real_file)

    report = run_working_copy_reconciliation(paths)
    assert WorkingCopyReconciliationIssueCode.SYMLINK_OR_REPARSE_POINT in _issue_codes(report)


def test_empty_docx_is_reported(env) -> None:
    _repo, paths, _store = env
    owner_key = _owner_key("empty-docx")
    fp = owner_key.owner_key_fingerprint
    paths.ensure_owner_dirs(fp)
    paths.current_docx_path(fp).write_bytes(b"")
    binding = WorkingCopyBinding(
        owner_key_fingerprint=fp,
        bound_proposal_artifact_fingerprint="e" * 64,
        bound_proposal_revision=1,
        bound_generated_proposal_sha256="f" * 64,
        last_known_tool_written_hash="f" * 64,
    )
    paths.binding_path(fp).write_bytes(serialize_working_copy_binding(binding))

    report = run_working_copy_reconciliation(paths)
    assert WorkingCopyReconciliationIssueCode.EMPTY_FILE in _issue_codes(report)


def test_unexpected_file_in_owner_directory_is_reported(env) -> None:
    _repo, paths, _store = env
    owner_key = _owner_key("unexpected")
    fp = owner_key.owner_key_fingerprint
    paths.ensure_owner_dirs(fp)
    (paths.owner_dir(fp) / "stray_file.txt").write_bytes(b"should not be here")

    report = run_working_copy_reconciliation(paths)
    assert WorkingCopyReconciliationIssueCode.UNEXPECTED_FILE in _issue_codes(report)


def test_lock_path_anomaly_is_reported(env) -> None:
    _repo, paths, _store = env
    (paths.locks_dir / "not-a-valid-fingerprint.lock").write_bytes(b"")

    report = run_working_copy_reconciliation(paths)
    assert WorkingCopyReconciliationIssueCode.LOCK_PATH_ANOMALY in _issue_codes(report)


# 9: standard mode is always read-only, and idempotent ---------------------------------------


def test_reconciliation_never_mutates_and_is_idempotent(env) -> None:
    repo, paths, store = env
    owner_key = _owner_key("idempotent")
    docx_bytes = b"idempotent payload"
    artifact = _artifact(owner_key, docx_bytes=docx_bytes, revision=1, artifact_fingerprint="e" * 64)
    repo.replace_current_proposal(owner_key, artifact, docx_bytes, expected_previous_revision=None)
    store.create_or_refresh_working_copy(owner_key=owner_key, repository=repo)

    fp = owner_key.owner_key_fingerprint
    docx_before = paths.current_docx_path(fp).read_bytes()
    binding_before = paths.binding_path(fp).read_bytes()

    report1 = run_working_copy_reconciliation(paths, owner_keys_by_fingerprint={fp: owner_key}, repository=repo)
    report2 = run_working_copy_reconciliation(paths, owner_keys_by_fingerprint={fp: owner_key}, repository=repo)

    assert paths.current_docx_path(fp).read_bytes() == docx_before
    assert paths.binding_path(fp).read_bytes() == binding_before
    assert report1 == report2


# 10: multi-owner scan reports issues scoped to the right owner -----------------------------


def test_multi_owner_scan_scopes_issues_correctly(env) -> None:
    repo, paths, store = env

    clean_owner = _owner_key("multi-clean")
    docx_bytes = b"clean payload"
    artifact = _artifact(clean_owner, docx_bytes=docx_bytes, revision=1, artifact_fingerprint="e" * 64)
    repo.replace_current_proposal(clean_owner, artifact, docx_bytes, expected_previous_revision=None)
    store.create_or_refresh_working_copy(owner_key=clean_owner, repository=repo)

    broken_owner = _owner_key("multi-broken")
    broken_fp = broken_owner.owner_key_fingerprint
    paths.ensure_owner_dirs(broken_fp)
    paths.current_docx_path(broken_fp).write_bytes(b"docx with no sidecar")

    report = run_working_copy_reconciliation(paths)
    assert report.scanned_owner_count == 2
    broken_issues = [issue for issue in report.issues if issue.owner_key_fingerprint == broken_fp]
    clean_issues = [issue for issue in report.issues if issue.owner_key_fingerprint == clean_owner.owner_key_fingerprint]
    assert any(issue.issue_code == WorkingCopyReconciliationIssueCode.DOCX_WITHOUT_SIDECAR for issue in broken_issues)
    assert clean_issues == []
