"""P3 must never couple to the legacy Truth Library bridge (P2's job alone).

These checks read the actual source of the three new P3 modules and assert
the literal absence of legacy-loader imports, legacy identity fields, list
indexing as a lookup mechanism, and any text-based fallback matching. P3
consumes only existing, stable IDs and P1 models.
"""

from pathlib import Path
from uuid import uuid4

from app.schemas.fact_selection import TargetContext
from app.schemas.truth_fact import (
    FactStatus,
    Transferability,
    TransformationOperation,
    TruthFactRead,
)
from app.services import fact_selection_policy, fact_selection_replay
from app.services.fact_selection_policy import SELECTION_POLICY_VERSION, select_fact
from datetime import datetime, timezone


_SOURCE_FILES = (
    Path(fact_selection_policy.__file__),
    Path(fact_selection_replay.__file__),
    Path(
        Path(fact_selection_policy.__file__).parent.parent
        / "schemas"
        / "fact_selection.py"
    ),
)

_FORBIDDEN_SUBSTRINGS = (
    "truth_library_loader",
    "truth_legacy_migrator",
    "truth_legacy_classification",
    "load_truth_library",
    "get_truth_library",
    "truth_library_path",
    "legacy_record_key",
    "legacy_source_path",
    "legacy_source_id",
    "TruthLegacyMigrationMap",
)


def _read_sources() -> dict[Path, str]:
    return {path: path.read_text(encoding="utf-8") for path in _SOURCE_FILES}


def test_no_legacy_loader_import() -> None:
    sources = _read_sources()
    for path, text in sources.items():
        for line in text.splitlines():
            if "import" in line:
                assert "truth_library_loader" not in line, path
                assert "truth_legacy_migrator" not in line, path
                assert "truth_legacy_classification" not in line, path


def test_no_forbidden_legacy_identifiers_anywhere_in_p3_source() -> None:
    sources = _read_sources()
    for path, text in sources.items():
        for forbidden in _FORBIDDEN_SUBSTRINGS:
            assert forbidden not in text, f"found forbidden token {forbidden!r} in {path}"


def test_no_list_index_lookup_in_p3_source() -> None:
    """P3 must select facts/permissions by explicit id equality, never by
    positional list index (e.g. ``facts[0]`` or ``permissions[i]``)."""

    sources = _read_sources()
    for path, text in sources.items():
        for line in text.splitlines():
            stripped = line.strip()
            assert "[0]" not in stripped, f"positional indexing found in {path}: {line}"
            assert "[i]" not in stripped, f"positional indexing found in {path}: {line}"


def test_fact_selection_consumes_only_explicit_ids_and_p1_models() -> None:
    """End-to-end: selection succeeds using only entity_id/fact_id/permission_id
    and real P1 schemas -- no legacy source path or record key is required
    anywhere on the call surface."""

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    fact = TruthFactRead(
        schema_version="1.0",
        revision=1,
        content_fingerprint="a" * 64,
        created_at=now,
        updated_at=now,
        archived_at=None,
        fact_id=uuid4(),
        entity_id=uuid4(),
        fact_type="SKILL",
        value_json={"text": "Python"},
        normalized_value_json={"text": "Python"},
        status=FactStatus.CONFIRMED,
        use_in_cv=True,
        requires_approval=False,
        transferability=Transferability.GLOBAL_TRANSFERABLE,
        employment_scope_entity_id=None,
        source_reference="legacy:some:path:should:be:irrelevant",
    )
    target_context = TargetContext(
        target_context_id=uuid4(),
        person_entity_id=uuid4(),
        job_or_application_id=None,
        target_role_title=None,
        target_role_family=None,
        target_seniority=None,
        target_organization_or_industry=None,
        employment_entity_id=None,
        selection_policy_version=SELECTION_POLICY_VERSION,
    )
    decision = select_fact(
        fact=fact,
        target_context=target_context,
        requested_operation=TransformationOperation.EXACT_COPY,
        requested_target_scope="SKILLS",
        permissions=[],
        evaluation_time=now,
    )
    assert decision.fact_id == fact.fact_id
    assert decision.entity_id == fact.entity_id


def test_source_reference_is_not_used_as_identity() -> None:
    """Two facts sharing the exact same source_reference text must be
    selected/decided independently -- source_reference is never an identity
    key in P3."""

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    shared_reference = "legacy:shared:reference"

    def _make(fact_id, entity_id) -> TruthFactRead:
        return TruthFactRead(
            schema_version="1.0",
            revision=1,
            content_fingerprint="a" * 64,
            created_at=now,
            updated_at=now,
            archived_at=None,
            fact_id=fact_id,
            entity_id=entity_id,
            fact_type="SKILL",
            value_json={"text": "Python"},
            normalized_value_json={"text": "Python"},
            status=FactStatus.CONFIRMED,
            use_in_cv=True,
            requires_approval=False,
            transferability=Transferability.GLOBAL_TRANSFERABLE,
            employment_scope_entity_id=None,
            source_reference=shared_reference,
        )

    target_context = TargetContext(
        target_context_id=uuid4(),
        person_entity_id=uuid4(),
        job_or_application_id=None,
        target_role_title=None,
        target_role_family=None,
        target_seniority=None,
        target_organization_or_industry=None,
        employment_entity_id=None,
        selection_policy_version=SELECTION_POLICY_VERSION,
    )

    first_id, second_id = uuid4(), uuid4()
    first = select_fact(
        fact=_make(first_id, uuid4()),
        target_context=target_context,
        requested_operation=TransformationOperation.EXACT_COPY,
        requested_target_scope="SKILLS",
        permissions=[],
        evaluation_time=now,
    )
    second = select_fact(
        fact=_make(second_id, uuid4()),
        target_context=target_context,
        requested_operation=TransformationOperation.EXACT_COPY,
        requested_target_scope="SKILLS",
        permissions=[],
        evaluation_time=now,
    )
    assert first.fact_id != second.fact_id
    assert first.decision_fingerprint != second.decision_fingerprint
