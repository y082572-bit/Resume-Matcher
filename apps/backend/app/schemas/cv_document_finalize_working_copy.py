"""Explicit Provenance Stage P6-B2b-B: the ``finalize_working_copy_to_final_docx_snapshot``
composition-root result.

``FinalizeWorkingCopyResult`` is deliberately a separate, additive model --
never reused from, and never a superset that could be mistaken for, the
existing P6-A ``FinalDocxSnapshotBuildResult``. Only ``FINALIZED`` ever
carries a ``snapshot``; every failure status carries neither a ``snapshot``
nor a ``source_proposal_is_current`` -- a failure can never look like a
partial success.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.cv_document_final_snapshot import FinalDocxSnapshot, FinalDocxSnapshotBuildStatus


class FinalizeWorkingCopyResult(BaseModel):
    """The single return value of
    ``finalize_working_copy_to_final_docx_snapshot``.

    ``source_proposal_is_current`` is best-effort and purely informational:
    ``True``/``False`` when the post-finalization current-proposal lookup
    succeeded, ``None`` when that lookup was unavailable. It never blocks
    finalization and is never present outside a ``FINALIZED`` result.
    """

    model_config = ConfigDict(extra="forbid")

    status: FinalDocxSnapshotBuildStatus
    snapshot: FinalDocxSnapshot | None = None
    source_proposal_is_current: bool | None = None
    diagnostics: tuple[str, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _status_consistency(self) -> "FinalizeWorkingCopyResult":
        if self.status == FinalDocxSnapshotBuildStatus.FINALIZED:
            if self.snapshot is None:
                raise ValueError("FINALIZED requires a snapshot")
        else:
            if self.snapshot is not None:
                raise ValueError(f"{self.status} must carry no snapshot")
            if self.source_proposal_is_current is not None:
                raise ValueError(f"{self.status} must carry no source_proposal_is_current")
        return self
