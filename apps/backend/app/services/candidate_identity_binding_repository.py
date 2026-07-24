"""Explicit Provenance Stage P6-B2A-I1: the read-only
``CandidateIdentityBindingRepository`` Protocol, its in-memory test double,
and the ``binding_fingerprint`` computation.

This Protocol is deliberately read-only -- it has no ``create_binding``,
``update_binding``, ``delete_binding``, ``replace_binding``, or ``rebind``
method. Write authority over ``candidate_identity_bindings`` belongs
exclusively to
``candidate_identity_binding_sql_repository.CandidateIdentityBindingSqlService.create_binding``,
which never delegates to (and is never itself implemented in terms of) this
Protocol.
"""

from __future__ import annotations

import hashlib
from typing import Protocol, runtime_checkable
from uuid import UUID

from app.schemas.candidate_identity_binding import CandidateIdentityBinding
from app.services.truth_fingerprint import canonical_json_bytes


BINDING_FINGERPRINT_VERSION = "candidate-identity-binding-v1"


def compute_binding_fingerprint(
    *,
    person_entity_id: UUID,
    master_resume_id: str,
    binding_schema_version: str,
) -> str:
    """SHA-256 over exactly ``person_entity_id``/``master_resume_id``/
    ``binding_schema_version`` -- never ``created_at``, never current time,
    never a filesystem path, never a random UUID."""

    content = {
        "fingerprint_version": BINDING_FINGERPRINT_VERSION,
        "content": {
            "person_entity_id": str(person_entity_id),
            "master_resume_id": master_resume_id,
            "binding_schema_version": binding_schema_version,
        },
    }
    return hashlib.sha256(canonical_json_bytes(content)).hexdigest()


@runtime_checkable
class CandidateIdentityBindingRepository(Protocol):
    """Read-only access to ``candidate_identity_bindings``. Never offers a
    way to create, update, delete, replace, or rebind a row."""

    def get_binding_by_person(self, person_entity_id: UUID) -> CandidateIdentityBinding | None: ...

    def get_binding_by_master_resume(
        self, master_resume_id: str
    ) -> CandidateIdentityBinding | None: ...

    def list_all_bindings(self) -> tuple[CandidateIdentityBinding, ...]: ...


class InMemoryCandidateIdentityBindingRepository:
    """Read-only test double / reconciliation double.

    Seeded once at construction time from an already-built collection of
    ``CandidateIdentityBinding`` values -- there is no public method on this
    class that inserts, updates, or deletes a binding afterward. Every read
    returns a fresh ``model_copy()``, never the internally stored reference,
    so a caller can never mutate this double's state through a returned
    value.
    """

    def __init__(self, bindings: tuple[CandidateIdentityBinding, ...] = ()) -> None:
        self._by_fingerprint: dict[str, CandidateIdentityBinding] = {
            binding.binding_fingerprint: binding.model_copy() for binding in bindings
        }

    def get_binding_by_person(self, person_entity_id: UUID) -> CandidateIdentityBinding | None:
        for binding in self._by_fingerprint.values():
            if binding.person_entity_id == person_entity_id:
                return binding.model_copy()
        return None

    def get_binding_by_master_resume(
        self, master_resume_id: str
    ) -> CandidateIdentityBinding | None:
        for binding in self._by_fingerprint.values():
            if binding.master_resume_id == master_resume_id:
                return binding.model_copy()
        return None

    def list_all_bindings(self) -> tuple[CandidateIdentityBinding, ...]:
        return tuple(binding.model_copy() for binding in self._by_fingerprint.values())
