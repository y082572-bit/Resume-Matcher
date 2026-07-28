"""Explicit Provenance Stage P6-C1a: closed, server-side owner resolution
for the CV Document Workflow API.

The only public input is ``job_id``. Every other identity component
(``person_entity_id``, ``master_resume_id``, ``owner_kind``,
``owner_reference_id``, ``owner_key_fingerprint``) is always derived
server-side -- a caller can never supply, override, or influence any of
them.

Resolution flow:

1. verify the job exists (``app.database.db.get_job``),
2. find the current Master Resume (``db.get_master_resume``),
3. find the ``CandidateIdentityBinding`` for that Master Resume
   (``CandidateIdentityBindingSqlRepository.get_binding_by_master_resume`` --
   synchronous SQL, always run through a threadpool, never on the event
   loop),
4. build the owner key via the existing ``build_owner_key`` with
   ``owner_kind=ArtifactOwnerKind.JOB`` and ``owner_reference_id=job_id``.

A missing job and a missing identity binding are deliberately
indistinguishable to the caller -- both resolve to ``NOT_FOUND``. They are
only ever distinguished in the server-side log line this module emits.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict
from starlette.concurrency import run_in_threadpool

from app.schemas.cv_document_artifact import ArtifactOwnerKind, JobArtifactOwnerKey
from app.services.candidate_identity_binding_sql_repository import (
    CandidateIdentityBindingSqlRepository,
)
from app.services.cv_document_owner_identity import build_owner_key

logger = logging.getLogger(__name__)


class OwnerResolutionStatus(StrEnum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"


class OwnerResolutionResult(BaseModel):
    """The single closed return value of ``resolve_owner_for_job``. Only
    ``FOUND`` ever carries an ``owner_key``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: OwnerResolutionStatus
    owner_key: JobArtifactOwnerKey | None = None


class _JobLookup(Protocol):
    async def get_job(self, job_id: str) -> dict | None: ...

    async def get_master_resume(self) -> dict | None: ...


class CvDocumentWorkflowOwnerResolver:
    """The single, closed owner resolver for the CV Document Workflow API.

    Depends only on the existing async ``Database`` facade (for job/master
    resume lookups) and a synchronous ``CandidateIdentityBindingSqlRepository``
    (for the identity-binding lookup) -- never on a router, a request object,
    or FastAPI itself.
    """

    def __init__(
        self, *, database: _JobLookup, binding_repository: CandidateIdentityBindingSqlRepository
    ) -> None:
        self._database = database
        self._binding_repository = binding_repository

    async def resolve_owner_for_job(self, job_id: str) -> OwnerResolutionResult:
        job = await self._database.get_job(job_id)
        if job is None:
            logger.info("cv document workflow: job not found (job_id=%s)", job_id)
            return OwnerResolutionResult(status=OwnerResolutionStatus.NOT_FOUND)

        master_resume = await self._database.get_master_resume()
        if master_resume is None:
            logger.info(
                "cv document workflow: no master resume exists (job_id=%s)", job_id
            )
            return OwnerResolutionResult(status=OwnerResolutionStatus.NOT_FOUND)

        master_resume_id = master_resume["resume_id"]
        binding = await run_in_threadpool(
            self._binding_repository.get_binding_by_master_resume, master_resume_id
        )
        if binding is None:
            logger.info(
                "cv document workflow: no identity binding for master_resume_id=%s (job_id=%s)",
                master_resume_id,
                job_id,
            )
            return OwnerResolutionResult(status=OwnerResolutionStatus.NOT_FOUND)

        owner_key = build_owner_key(
            person_entity_id=binding.person_entity_id,
            owner_kind=ArtifactOwnerKind.JOB,
            owner_reference_id=job_id,
        )
        return OwnerResolutionResult(status=OwnerResolutionStatus.FOUND, owner_key=owner_key)
