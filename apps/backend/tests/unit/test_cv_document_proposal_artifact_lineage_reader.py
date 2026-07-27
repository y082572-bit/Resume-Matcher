"""Unit tests for the P6-B2b-B owner-bound Proposal artifact lineage
contract: ``ProposalArtifactLineageStatus``, ``ProposalArtifactLineage``,
``ProposalArtifactLineageReadResult``, and the ``ProposalArtifactLineageReader``
Protocol itself.
"""

from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from app.services.cv_document_proposal_artifact_lineage_reader import (
    ProposalArtifactLineage,
    ProposalArtifactLineageReadResult,
    ProposalArtifactLineageReader,
    ProposalArtifactLineageStatus,
    validate_lineage_request,
)


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _lineage(**overrides) -> ProposalArtifactLineage:
    defaults = dict(
        artifact_fingerprint=_sha("artifact"),
        owner_key_fingerprint=_sha("owner"),
        proposal_revision=1,
        generated_docx_content_hash=_sha("generated"),
        docx_bytes=b"exact proposal bytes",
    )
    defaults.update(overrides)
    return ProposalArtifactLineage(**defaults)


# -- VERIFIED requires lineage; every failure forbids it -----------------------------


def test_verified_requires_lineage() -> None:
    with pytest.raises(ValidationError):
        ProposalArtifactLineageReadResult(status=ProposalArtifactLineageStatus.VERIFIED, lineage=None)


def test_verified_with_lineage_is_constructible() -> None:
    result = ProposalArtifactLineageReadResult(status=ProposalArtifactLineageStatus.VERIFIED, lineage=_lineage())
    assert result.lineage is not None


@pytest.mark.parametrize(
    "status",
    [s for s in ProposalArtifactLineageStatus if s != ProposalArtifactLineageStatus.VERIFIED],
)
def test_every_failure_status_forbids_lineage(status: ProposalArtifactLineageStatus) -> None:
    with pytest.raises(ValidationError):
        ProposalArtifactLineageReadResult(status=status, lineage=_lineage())


def test_invalid_request_never_carries_lineage() -> None:
    result = ProposalArtifactLineageReadResult(
        status=ProposalArtifactLineageStatus.INVALID_REQUEST, error_message="bad args"
    )
    assert result.lineage is None


# -- ProposalArtifactLineage validators ------------------------------------------------


def test_lineage_rejects_revision_below_one() -> None:
    with pytest.raises(ValidationError):
        _lineage(proposal_revision=0)


def test_lineage_rejects_malformed_sha256_fields() -> None:
    with pytest.raises(ValidationError):
        _lineage(artifact_fingerprint="not-a-sha256")


def test_lineage_rejects_empty_bytes() -> None:
    with pytest.raises(ValidationError):
        _lineage(docx_bytes=b"")


def test_lineage_is_frozen() -> None:
    lineage = _lineage()
    with pytest.raises(ValidationError):
        lineage.proposal_revision = 2  # type: ignore[misc]


def test_lineage_read_result_is_extra_forbid() -> None:
    with pytest.raises(ValidationError):
        ProposalArtifactLineageReadResult(
            status=ProposalArtifactLineageStatus.NOT_FOUND, unexpected_field="x"  # type: ignore[call-arg]
        )


# -- validate_lineage_request never raises, closed precondition check ---------------


def test_validate_lineage_request_accepts_well_formed_arguments() -> None:
    assert validate_lineage_request(
        artifact_fingerprint=_sha("a"),
        expected_owner_key_fingerprint=_sha("b"),
        expected_proposal_revision=1,
        expected_generated_docx_sha256=_sha("c"),
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"artifact_fingerprint": "not-hex"},
        {"artifact_fingerprint": "A" * 64},  # uppercase not allowed
        {"expected_owner_key_fingerprint": "short"},
        {"expected_generated_docx_sha256": ""},
        {"expected_proposal_revision": 0},
        {"expected_proposal_revision": -1},
        {"expected_proposal_revision": "1"},
        {"expected_proposal_revision": True},
        {"expected_proposal_revision": 1.5},
        {"artifact_fingerprint": None},
    ],
)
def test_validate_lineage_request_rejects_malformed_arguments_without_raising(overrides: dict) -> None:
    base = dict(
        artifact_fingerprint=_sha("a"),
        expected_owner_key_fingerprint=_sha("b"),
        expected_proposal_revision=1,
        expected_generated_docx_sha256=_sha("c"),
    )
    base.update(overrides)
    # Must never raise -- only ever return False for bad input.
    assert validate_lineage_request(**base) is False


# -- Protocol is runtime-checkable ----------------------------------------------------


class _FakeLineageReader:
    def read_verified_proposal_artifact_lineage(
        self,
        *,
        artifact_fingerprint: str,
        expected_owner_key_fingerprint: str,
        expected_proposal_revision: int,
        expected_generated_docx_sha256: str,
    ) -> ProposalArtifactLineageReadResult:
        return ProposalArtifactLineageReadResult(status=ProposalArtifactLineageStatus.NOT_FOUND)


def test_protocol_is_runtime_checkable_and_satisfied_by_a_conforming_fake() -> None:
    assert isinstance(_FakeLineageReader(), ProposalArtifactLineageReader)


class _NonConformingReader:
    def some_other_method(self) -> None: ...


def test_protocol_rejects_a_non_conforming_object() -> None:
    assert not isinstance(_NonConformingReader(), ProposalArtifactLineageReader)


# -- lineage-status mapping completeness (against the filesystem reader's own dict) ---


def test_lineage_status_completeness_against_filesystem_reader_mapping() -> None:
    from app.services.cv_document_final_source_filesystem_reader import _LINEAGE_STATUS_TO_READ_STATUS

    non_verified = {s for s in ProposalArtifactLineageStatus if s != ProposalArtifactLineageStatus.VERIFIED}
    assert non_verified == set(_LINEAGE_STATUS_TO_READ_STATUS)
