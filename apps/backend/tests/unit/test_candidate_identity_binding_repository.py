"""Unit tests for the read-only ``CandidateIdentityBindingRepository``
Protocol, ``InMemoryCandidateIdentityBindingRepository``, and
``compute_binding_fingerprint``.
"""

from __future__ import annotations

import inspect
from uuid import uuid4

from app.schemas.candidate_identity_binding import CandidateIdentityBinding
from app.services.candidate_identity_binding_repository import (
    CandidateIdentityBindingRepository,
    InMemoryCandidateIdentityBindingRepository,
    compute_binding_fingerprint,
)


def _binding(person_entity_id=None, master_resume_id: str = "resume-1") -> CandidateIdentityBinding:
    person_id = person_entity_id or uuid4()
    fingerprint = compute_binding_fingerprint(
        person_entity_id=person_id,
        master_resume_id=master_resume_id,
        binding_schema_version="candidate-identity-binding-schema-v1",
    )
    return CandidateIdentityBinding(
        binding_fingerprint=fingerprint,
        person_entity_id=person_id,
        master_resume_id=master_resume_id,
        created_at="2026-01-01T00:00:00+00:00",
    )


# -- Protocol shape (item 38: no update/delete) ----------------------------------------


def test_protocol_is_runtime_checkable_and_matches_in_memory_double() -> None:
    assert isinstance(InMemoryCandidateIdentityBindingRepository(), CandidateIdentityBindingRepository)


def test_repository_protocol_has_exactly_three_read_methods_no_write() -> None:
    protocol_methods = {
        name
        for name in vars(CandidateIdentityBindingRepository)
        if not name.startswith("_")
    }
    assert protocol_methods == {
        "get_binding_by_person",
        "get_binding_by_master_resume",
        "list_all_bindings",
    }
    forbidden = {"create_binding", "update_binding", "delete_binding", "replace_binding", "rebind"}
    assert not (protocol_methods & forbidden)


def test_in_memory_double_public_surface_has_no_write_method() -> None:
    public_methods = {
        name
        for name, _ in inspect.getmembers(
            InMemoryCandidateIdentityBindingRepository(), predicate=inspect.ismethod
        )
        if not name.startswith("_")
    }
    assert public_methods == {
        "get_binding_by_person",
        "get_binding_by_master_resume",
        "list_all_bindings",
    }


# -- InMemory read behavior ----------------------------------------------------------------


def test_in_memory_double_reads_seeded_bindings() -> None:
    binding = _binding()
    repo = InMemoryCandidateIdentityBindingRepository((binding,))

    assert repo.get_binding_by_person(binding.person_entity_id) == binding
    assert repo.get_binding_by_master_resume(binding.master_resume_id) == binding
    assert repo.list_all_bindings() == (binding,)


def test_in_memory_double_returns_none_for_unknown_lookup() -> None:
    repo = InMemoryCandidateIdentityBindingRepository()
    assert repo.get_binding_by_person(uuid4()) is None
    assert repo.get_binding_by_master_resume("unknown") is None
    assert repo.list_all_bindings() == ()


def test_in_memory_double_returns_defensive_copies() -> None:
    binding = _binding()
    repo = InMemoryCandidateIdentityBindingRepository((binding,))

    first_read = repo.get_binding_by_person(binding.person_entity_id)
    second_read = repo.get_binding_by_person(binding.person_entity_id)
    assert first_read is not second_read
    assert first_read == second_read


# -- compute_binding_fingerprint -------------------------------------------------------------


def test_fingerprint_is_deterministic() -> None:
    person_id = uuid4()
    fp1 = compute_binding_fingerprint(
        person_entity_id=person_id,
        master_resume_id="resume-1",
        binding_schema_version="candidate-identity-binding-schema-v1",
    )
    fp2 = compute_binding_fingerprint(
        person_entity_id=person_id,
        master_resume_id="resume-1",
        binding_schema_version="candidate-identity-binding-schema-v1",
    )
    assert fp1 == fp2
    assert len(fp1) == 64


def test_fingerprint_changes_with_person() -> None:
    fp1 = compute_binding_fingerprint(
        person_entity_id=uuid4(), master_resume_id="resume-1", binding_schema_version="v1"
    )
    fp2 = compute_binding_fingerprint(
        person_entity_id=uuid4(), master_resume_id="resume-1", binding_schema_version="v1"
    )
    assert fp1 != fp2


def test_fingerprint_changes_with_resume() -> None:
    person_id = uuid4()
    fp1 = compute_binding_fingerprint(
        person_entity_id=person_id, master_resume_id="resume-1", binding_schema_version="v1"
    )
    fp2 = compute_binding_fingerprint(
        person_entity_id=person_id, master_resume_id="resume-2", binding_schema_version="v1"
    )
    assert fp1 != fp2


def test_fingerprint_changes_with_schema_version() -> None:
    person_id = uuid4()
    fp1 = compute_binding_fingerprint(
        person_entity_id=person_id, master_resume_id="resume-1", binding_schema_version="v1"
    )
    fp2 = compute_binding_fingerprint(
        person_entity_id=person_id, master_resume_id="resume-1", binding_schema_version="v2"
    )
    assert fp1 != fp2


def test_fingerprint_excludes_created_at() -> None:
    """created_at is never a fingerprint input -- two bindings differing
    only in created_at must be classified identical by any comparison that
    relies on binding_fingerprint."""

    person_id = uuid4()
    fingerprint = compute_binding_fingerprint(
        person_entity_id=person_id,
        master_resume_id="resume-1",
        binding_schema_version="candidate-identity-binding-schema-v1",
    )
    binding_a = CandidateIdentityBinding(
        binding_fingerprint=fingerprint,
        person_entity_id=person_id,
        master_resume_id="resume-1",
        created_at="2026-01-01T00:00:00+00:00",
    )
    binding_b = binding_a.model_copy(update={"created_at": "2027-06-15T12:00:00+00:00"})
    assert binding_a.binding_fingerprint == binding_b.binding_fingerprint
    assert binding_a != binding_b
