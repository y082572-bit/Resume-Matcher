"""End-to-end proof of the Explicit Provenance Stage P6-A document
artifact lifecycle -- ApprovedCvContentResult -> current DOCX proposal ->
validated snapshot -> confirmed PDF -- plus the source-level isolation
matrix every P6-A core module must satisfy.

All facts, plans, and documents are synthetic; no real candidate or
employer data is used. P6-A ships no real filesystem repository, no real
DOCX renderer, no real PDF converter, no database, no API, and no
frontend -- every adapter exercised here is a deterministic in-memory
fake, proving the core lifecycle contracts independent of any real I/O.

The ``ApprovedCvContentResult`` exercised here is built through the shared
``build_ready_integrated_context``/``build_document_input`` factory in
``tests.unit.test_cv_document_input_validation`` -- a real
PRE-P4 -> P3(strategy) -> P4(``ROLE_STRATEGY_INTEGRATED``) -> P5a -> P5.5
pipeline -- since P6-A accepts no other ``CvContentPlan.plan_mode``.
"""

from __future__ import annotations

import hashlib
import inspect
import re

import pytest

from app.schemas.cv_document_artifact import (
    CvDocxRenderingResult,
    CvPdfConversionAttemptResult,
    DocumentProvenanceMode,
    DocxRenderingStatus,
    DocxStructuralValidationResult,
    DocxStructuralValidationStatus,
    PdfConfirmationBuildStatus,
    PdfConversionStatus,
    ProposalBuildStatus,
    SnapshotBuildStatus,
)
from app.services import (
    cv_document_adapters,
    cv_document_input_validation,
    cv_document_manual_edit_detector,
    cv_document_owner_identity,
    cv_document_pdf_confirmation_builder,
    cv_document_proposal_builder,
    cv_document_replay,
    cv_document_repository_protocol,
    cv_document_snapshot_builder,
)
from app.services.cv_document_adapters import DocxTemplateHandle
from app.services.cv_document_pdf_confirmation_builder import build_and_confirm_pdf
from app.services.cv_document_proposal_builder import build_current_docx_proposal
from app.services.cv_document_repository_protocol import InMemoryCvDocumentArtifactRepository
from app.services.cv_document_snapshot_builder import build_validated_docx_snapshot
from tests.unit.test_cv_document_input_validation import build_document_input, build_ready_integrated_context


VALIDATION_POLICY_VERSION = "cv-document-structural-validation-policy-integration-v1"
RENDERING_POLICY_VERSION = "cv-document-rendering-policy-integration-v1"
CONVERSION_POLICY_VERSION = "cv-document-pdf-conversion-policy-integration-v1"

build_ready_context = build_ready_integrated_context


class _FakeTemplateProvider:
    def __init__(self, template_bytes: bytes = b"FAKE-DOCX-TEMPLATE-BYTES-INTEGRATION-V1") -> None:
        self._template_bytes = template_bytes

    def get_template(self) -> DocxTemplateHandle:
        return DocxTemplateHandle(
            template_bytes=self._template_bytes,
            template_fingerprint=hashlib.sha256(self._template_bytes).hexdigest(),
            template_adapter_id="fake-template-provider-v1",
        )


class _FakeRenderer:
    renderer_adapter_id = "fake-renderer-v1"

    def render(self, *, approved_content_result, content_plan, template_bytes, template_fingerprint, rendering_policy_version) -> CvDocxRenderingResult:
        output = template_bytes + b"::" + approved_content_result.result_fingerprint.encode()
        return CvDocxRenderingResult(
            status=DocxRenderingStatus.SUCCEEDED,
            renderer_adapter_id=self.renderer_adapter_id,
            rendering_policy_version=rendering_policy_version,
            docx_bytes=output,
            output_sha256=hashlib.sha256(output).hexdigest(),
        )


class _FakeStructuralValidator:
    def validate(self, *, exact_bytes: bytes, expected_sha256: str, validation_policy_version: str) -> DocxStructuralValidationResult:
        return DocxStructuralValidationResult(status=DocxStructuralValidationStatus.VALID, validated_sha256=expected_sha256)


class _FakePdfConversionAdapter:
    adapter_id = "fake-pdf-adapter-v1"

    def convert(self, *, snapshot_bytes: bytes, snapshot_fingerprint: str, conversion_policy_version: str) -> CvPdfConversionAttemptResult:
        output = b"%PDF-fake::" + snapshot_fingerprint.encode()
        return CvPdfConversionAttemptResult(
            status=PdfConversionStatus.SUCCEEDED,
            adapter_id=self.adapter_id,
            conversion_policy_version=conversion_policy_version,
            pdf_bytes=output,
            pdf_sha256=hashlib.sha256(output).hexdigest(),
            attempt_fingerprint=hashlib.sha256(output).hexdigest(),
        )


@pytest.mark.asyncio
async def test_full_document_lifecycle_end_to_end() -> None:
    ctx = await build_ready_context()
    document_input = build_document_input(ctx)
    repository = InMemoryCvDocumentArtifactRepository()

    proposal_result = build_current_docx_proposal(
        document_input=document_input,
        template_provider=_FakeTemplateProvider(),
        renderer=_FakeRenderer(),
        repository=repository,
        rendering_policy_version=RENDERING_POLICY_VERSION,
        expected_previous_revision=None,
    )
    assert proposal_result.status == ProposalBuildStatus.GENERATED
    owner_key = proposal_result.artifact.owner_key

    snapshot_result = build_validated_docx_snapshot(
        owner_key=owner_key,
        repository=repository,
        structural_validator=_FakeStructuralValidator(),
        validation_policy_version=VALIDATION_POLICY_VERSION,
    )
    assert snapshot_result.status == SnapshotBuildStatus.SNAPSHOT_CREATED
    assert snapshot_result.snapshot.provenance_mode == DocumentProvenanceMode.PIPELINE_UNMODIFIED_DOCUMENT

    pdf_result = build_and_confirm_pdf(
        owner_key=owner_key,
        snapshot=snapshot_result.snapshot,
        repository=repository,
        conversion_adapter=_FakePdfConversionAdapter(),
        conversion_policy_version=CONVERSION_POLICY_VERSION,
        expected_previous_revision=None,
    )
    assert pdf_result.status == PdfConfirmationBuildStatus.CONFIRMED

    # the full fingerprint chain links end to end
    assert proposal_result.artifact.approved_cv_content_fingerprint == ctx["result"].result_fingerprint
    assert snapshot_result.snapshot.proposal_artifact_fingerprint == proposal_result.artifact.artifact_fingerprint
    assert pdf_result.artifact.source_validated_docx_snapshot_fingerprint == snapshot_result.snapshot.snapshot_fingerprint
    assert pdf_result.artifact.source_docx_sha256 == snapshot_result.snapshot.exact_docx_sha256
    assert pdf_result.artifact.approved_cv_content_fingerprint == ctx["result"].result_fingerprint
    assert pdf_result.artifact.content_plan_fingerprint == ctx["plan"].content_plan_fingerprint

    # both slots present, independently
    assert repository.get_current_proposal(owner_key) is not None
    assert repository.get_current_pdf(owner_key) is not None


@pytest.mark.asyncio
async def test_pdf_conversion_failure_never_touches_proposal_or_existing_pdf() -> None:
    ctx = await build_ready_context()
    document_input = build_document_input(ctx)
    repository = InMemoryCvDocumentArtifactRepository()

    proposal_result = build_current_docx_proposal(
        document_input=document_input,
        template_provider=_FakeTemplateProvider(),
        renderer=_FakeRenderer(),
        repository=repository,
        rendering_policy_version=RENDERING_POLICY_VERSION,
        expected_previous_revision=None,
    )
    owner_key = proposal_result.artifact.owner_key
    snapshot_result = build_validated_docx_snapshot(
        owner_key=owner_key,
        repository=repository,
        structural_validator=_FakeStructuralValidator(),
        validation_policy_version=VALIDATION_POLICY_VERSION,
    )

    class _FailingAdapter:
        adapter_id = "failing-pdf-adapter-v1"

        def convert(self, *, snapshot_bytes, snapshot_fingerprint, conversion_policy_version):
            from app.schemas.cv_document_artifact import PdfConversionFailureCode

            return CvPdfConversionAttemptResult(
                status=PdfConversionStatus.FAILED,
                adapter_id=self.adapter_id,
                conversion_policy_version=conversion_policy_version,
                failure_code=PdfConversionFailureCode.ADAPTER_ERROR,
                attempt_fingerprint=hashlib.sha256(b"failure").hexdigest(),
            )

    pdf_result = build_and_confirm_pdf(
        owner_key=owner_key,
        snapshot=snapshot_result.snapshot,
        repository=repository,
        conversion_adapter=_FailingAdapter(),
        conversion_policy_version=CONVERSION_POLICY_VERSION,
        expected_previous_revision=None,
    )
    assert pdf_result.status == PdfConfirmationBuildStatus.CONVERSION_FAILED
    assert repository.get_current_pdf(owner_key) is None
    assert repository.get_current_proposal(owner_key).artifact_fingerprint == proposal_result.artifact.artifact_fingerprint


# -- Isolation matrix ----------------------------------------------------------


CORE_MODULES = [
    cv_document_adapters,
    cv_document_input_validation,
    cv_document_manual_edit_detector,
    cv_document_owner_identity,
    cv_document_pdf_confirmation_builder,
    cv_document_proposal_builder,
    cv_document_replay,
    cv_document_repository_protocol,
    cv_document_snapshot_builder,
]


def _import_lines(module) -> list[str]:
    return [
        line.strip()
        for line in inspect.getsource(module).splitlines()
        if line.strip().startswith("import ") or line.strip().startswith("from ")
    ]


@pytest.mark.parametrize("module", CORE_MODULES)
def test_core_module_never_imports_llm_or_litellm(module) -> None:
    for line in _import_lines(module):
        assert "app.llm" not in line
        assert "litellm" not in line


@pytest.mark.parametrize("module", CORE_MODULES)
def test_core_module_never_imports_database_or_sqlalchemy(module) -> None:
    for line in _import_lines(module):
        assert "sqlalchemy" not in line.lower()
        assert "aiosqlite" not in line.lower()
        assert "app.database" not in line
        assert "app.db_engine" not in line
        assert "app.models" not in line


@pytest.mark.parametrize("module", CORE_MODULES)
def test_core_module_never_imports_routers_or_fastapi(module) -> None:
    for line in _import_lines(module):
        assert "app.routers" not in line
        assert "fastapi" not in line.lower()


@pytest.mark.parametrize("module", CORE_MODULES)
def test_core_module_never_imports_frontend(module) -> None:
    for line in _import_lines(module):
        assert "apps.frontend" not in line
        assert "next" not in line.lower()


@pytest.mark.parametrize("module", CORE_MODULES)
def test_core_module_never_imports_real_filesystem_word_or_pdf_adapters(module) -> None:
    for line in _import_lines(module):
        assert not re.search(r"^\s*(import|from)\s+docx\b", line)
        assert "playwright" not in line.lower()
        assert "pathlib" not in line.lower()


def _code_lines_excluding_docstrings_and_comments(module) -> list[str]:
    """Best-effort: drop full-line comments and lines inside triple-quoted
    docstrings, so a mention of a forbidden term *in prose explaining the
    ban* doesn't itself trip the ban."""

    lines = inspect.getsource(module).splitlines()
    code_lines: list[str] = []
    in_docstring = False
    for line in lines:
        stripped = line.strip()
        if not in_docstring and (stripped.startswith('"""') or stripped.startswith("'''")):
            quote = stripped[:3]
            if stripped.count(quote) >= 2 and len(stripped) > 3:
                continue  # single-line docstring
            in_docstring = True
            continue
        if in_docstring:
            if line.strip().endswith('"""') or line.strip().endswith("'''"):
                in_docstring = False
            continue
        if stripped.startswith("#"):
            continue
        code_lines.append(line)
    return code_lines


@pytest.mark.parametrize("module", CORE_MODULES)
def test_core_module_never_uses_subprocess_or_os_open_calls(module) -> None:
    code = "\n".join(_code_lines_excluding_docstrings_and_comments(module))
    assert "subprocess" not in code
    assert "os.startfile" not in code
    assert "osascript" not in code.lower()
    assert "applescript" not in code.lower()
    assert "xdg-open" not in code.lower()


@pytest.mark.parametrize("module", CORE_MODULES)
def test_core_module_never_uses_datetime_now_or_python_hash(module) -> None:
    source = inspect.getsource(module)
    assert "datetime.now(" not in source
    assert re.search(r"(?<!hashlib\.)(?<!\.)\bhash\(", source) is None


@pytest.mark.parametrize("module", CORE_MODULES)
def test_core_module_never_uses_uuid4_in_fingerprint_computation(module) -> None:
    """``uuid4()`` may only ever be used as an ``artifact_id`` locator --
    never fed into a fingerprint computation. A blanket ban on ``uuid4()``
    entirely inside modules whose only job is fingerprinting/pure-logic
    (never minting artifact locators) proves this for those modules."""

    if module in (cv_document_owner_identity, cv_document_manual_edit_detector, cv_document_replay, cv_document_input_validation):
        assert "uuid4()" not in inspect.getsource(module)


def test_no_path_or_url_identity_fields_in_schema() -> None:
    from app.schemas import cv_document_artifact

    source = inspect.getsource(cv_document_artifact)
    for forbidden_field in ("file_path:", "filesystem_path:", "url:", "employer_name:", "job_title:"):
        assert forbidden_field not in source


def test_fingerprint_helpers_use_canonical_json_bytes() -> None:
    for module in (
        cv_document_owner_identity,
        cv_document_input_validation,
        cv_document_proposal_builder,
        cv_document_snapshot_builder,
        cv_document_pdf_confirmation_builder,
    ):
        source = inspect.getsource(module)
        assert "canonical_json_bytes" in source


def test_byte_hashing_uses_raw_sha256() -> None:
    source = inspect.getsource(cv_document_manual_edit_detector)
    assert "hashlib.sha256(content)" in source or "hashlib.sha256(observed_bytes)" in source or "hashlib.sha256(" in source
