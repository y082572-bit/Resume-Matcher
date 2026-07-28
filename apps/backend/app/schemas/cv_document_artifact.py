"""Pydantic contracts for Explicit Provenance Stage P6-A: the document
artifact lifecycle core.

P6-A is a pure, deterministic, fail-closed transformation of an already
``READY``/fresh ``ApprovedCvContentResult`` (Stage P5.5) into a chain of
immutable, content-addressed document artifacts:

    ApprovedCvContentResult
    -> CvDocxProposalArtifact (current proposal slot)
    -> ValidatedCvDocxSnapshot (immutable, content-addressed)
    -> ConfirmedCvPdfArtifact (current PDF slot)

Every model here is a closed, replayable Pydantic value object. Identity is
always expressed through content-derived SHA-256 fingerprints -- never a
random UUID, a filesystem path, a filename, a URL, or an implicit
``datetime.now()``. A locator (``artifact_id``) may exist for addressing an
artifact in a repository, but it never participates in a fingerprint.

P6-A accepts only P5.5 content whose ``source_content_plan.plan_mode`` is
``CvContentPlanMode.ROLE_STRATEGY_INTEGRATED`` -- a ``LEGACY``
``CvContentPlan`` is always rejected fail-closed by
``cv_document_input_validation.validate_approved_content_document_input``,
never silently accepted. See ``docs/explicit-provenance-stage-p6a.md`` for
the full invariant.

Final domain records that a repository stores or returns
(``JobArtifactOwnerKey``, ``CvDocxProposalArtifact``,
``ValidatedCvDocxSnapshot``, ``ConfirmedCvPdfArtifact``,
``ManualDocumentSnapshotConfirmation``, ``CvPdfConversionAttemptResult``,
``DocumentLifecycleView``) are ``frozen=True`` -- once built, never mutated
in place. A repository is expected to additionally hand out its own
defensive copies on ingress/egress; ``frozen=True`` alone only blocks a
normal attribute assignment, it does not by itself prove no shared mutable
reference exists between caller and repository.

P6-A implements no filesystem repository, no real DOCX renderer, no real
PDF converter, no database, no API, and no frontend -- see
``docs/explicit-provenance-stage-p6a.md`` for the full scope boundary.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, UUID4, model_validator

from app.schemas.cv_content_approval import (
    ApprovedCvContentReplayInput,
    ApprovedCvContentResult,
    ProposalApprovalDecision,
    ProposalSemanticValidationReplayInput,
)
from app.schemas.cv_content_plan import CvContentPlan
from app.schemas.truth_entity import SHA256_PATTERN


CV_DOCUMENT_ARTIFACT_SCHEMA_VERSION = "cv-document-artifact-schema-v1"
DOCUMENT_INPUT_SCHEMA_VERSION = "cv-document-input-schema-v1"
DOCUMENT_INPUT_POLICY_VERSION = "cv-document-input-policy-v1"
OWNER_KEY_SCHEMA_VERSION = "cv-document-owner-key-schema-v1"
MANUAL_CONFIRMATION_POLICY_VERSION = "cv-document-manual-confirmation-policy-v1"


# -- Owner identity -----------------------------------------------------------


class ArtifactOwnerKind(StrEnum):
    """Which caller-facing entity a document artifact slot belongs to.

    A raw ``job_or_application_id`` string never disambiguates the two on
    its own -- the same string value could in principle be issued by either
    namespace, so ``owner_kind`` must always be supplied explicitly by the
    caller, never inferred.
    """

    JOB = "JOB"
    APPLICATION = "APPLICATION"


class JobArtifactOwnerKey(BaseModel):
    """The stable identity of one document artifact owner (one job or one
    application), never a URL, employer name, job title, filename,
    filesystem path, or content-plan fingerprint.

    ``content_plan_fingerprint`` changes between document versions of the
    very same owner and must never be folded into this identity.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    person_entity_id: UUID4
    owner_kind: ArtifactOwnerKind
    owner_reference_id: str = Field(min_length=1, max_length=255)
    owner_key_schema_version: Literal["cv-document-owner-key-schema-v1"] = (
        OWNER_KEY_SCHEMA_VERSION
    )
    owner_key_fingerprint: str = Field(pattern=SHA256_PATTERN)


# -- Approved document input ---------------------------------------------------


class ApprovedContentDocumentInput(BaseModel):
    """The raw, untrusted envelope a caller hands to P6-A.

    Every field here is independently re-derived and re-checked by
    ``cv_document_input_validation.validate_approved_content_document_input``
    -- nothing on this model (including ``document_input_fingerprint``
    itself) is trusted at face value.
    """

    model_config = ConfigDict(extra="forbid")

    approved_content_result: ApprovedCvContentResult
    source_content_plan: CvContentPlan
    current_p55_replay_input: ApprovedCvContentReplayInput
    semantic_validation_replay_input: ProposalSemanticValidationReplayInput
    approval_decisions: tuple[ProposalApprovalDecision, ...] = Field(default_factory=tuple)
    owner_kind: ArtifactOwnerKind
    document_input_schema_version: Literal["cv-document-input-schema-v1"] = (
        DOCUMENT_INPUT_SCHEMA_VERSION
    )
    document_input_policy_version: Literal["cv-document-input-policy-v1"] = (
        DOCUMENT_INPUT_POLICY_VERSION
    )
    document_input_fingerprint: str = Field(pattern=SHA256_PATTERN)


class DocumentInputValidationStatus(StrEnum):
    VALID = "VALID"
    INVALID = "INVALID"


class DocumentInputViolationCode(StrEnum):
    """Closed set of reasons ``ApprovedContentDocumentInput`` can be
    rejected -- always re-derived structurally, never guessed."""

    RESULT_FINGERPRINT_MISMATCH = "RESULT_FINGERPRINT_MISMATCH"
    RESULT_NOT_READY = "RESULT_NOT_READY"
    RESULT_HAS_PENDING_APPROVALS = "RESULT_HAS_PENDING_APPROVALS"
    RESULT_HAS_UNRESOLVED_SEMANTIC_VALIDATION = "RESULT_HAS_UNRESOLVED_SEMANTIC_VALIDATION"
    RESULT_NOT_FRESH = "RESULT_NOT_FRESH"
    CONTENT_PLAN_FINGERPRINT_MISMATCH = "CONTENT_PLAN_FINGERPRINT_MISMATCH"
    CONTENT_PLAN_FINGERPRINT_DOES_NOT_MATCH_RESULT = (
        "CONTENT_PLAN_FINGERPRINT_DOES_NOT_MATCH_RESULT"
    )
    DOCUMENT_INPUT_FINGERPRINT_MISMATCH = "DOCUMENT_INPUT_FINGERPRINT_MISMATCH"
    #: ``source_content_plan.plan_mode`` is not
    #: ``CvContentPlanMode.ROLE_STRATEGY_INTEGRATED``. P6-A never accepts a
    #: ``LEGACY`` plan -- there is no fallback path that admits one.
    LEGACY_CONTENT_PLAN_NOT_ALLOWED = "LEGACY_CONTENT_PLAN_NOT_ALLOWED"
    #: ``plan_mode == ROLE_STRATEGY_INTEGRATED`` but at least one of
    #: ``role_strategy_context_fingerprint`` / ``strategy_selection_result_fingerprint``
    #: / ``strategy_ranking_input_fingerprint`` / ``strategy_integration_policy_version``
    #: is ``None``. Checked independently of ``CvContentPlan``'s own model
    #: validator, which a ``model_copy(update=...)``-created plan can bypass.
    CONTENT_PLAN_STRATEGY_FIELDS_INCOMPLETE = "CONTENT_PLAN_STRATEGY_FIELDS_INCOMPLETE"


class DocumentInputViolation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    violation_code: DocumentInputViolationCode
    detail: str = Field(min_length=1, max_length=1000)


class ApprovedContentDocumentInputValidationResult(BaseModel):
    """The single return value of
    ``validate_approved_content_document_input``."""

    model_config = ConfigDict(extra="forbid")

    status: DocumentInputValidationStatus
    owner_key: JobArtifactOwnerKey | None = None
    recomputed_document_input_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)
    violations: tuple[DocumentInputViolation, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _status_consistency(self) -> "ApprovedContentDocumentInputValidationResult":
        if self.status == DocumentInputValidationStatus.VALID:
            if self.owner_key is None or self.recomputed_document_input_fingerprint is None:
                raise ValueError("VALID requires owner_key and recomputed_document_input_fingerprint")
            if self.violations:
                raise ValueError("VALID must carry no violations")
        else:
            if not self.violations:
                raise ValueError("INVALID must carry at least one violation")
        return self


# -- DOCX rendering ------------------------------------------------------------


class DocxRenderingStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class DocxRenderingFailureCode(StrEnum):
    RENDERER_ERROR = "RENDERER_ERROR"
    TEMPLATE_INVALID = "TEMPLATE_INVALID"
    CONTENT_INCOMPATIBLE = "CONTENT_INCOMPATIBLE"
    UNKNOWN = "UNKNOWN"


class CvDocxRenderingResult(BaseModel):
    """The closed return value of one ``CvDocxRenderingAdapter`` call.

    ``output_sha256`` is the adapter's own claim about its output bytes --
    the proposal builder always independently recomputes
    ``hashlib.sha256(docx_bytes)`` and requires an exact match before ever
    trusting this result.
    """

    model_config = ConfigDict(extra="forbid")

    status: DocxRenderingStatus
    renderer_adapter_id: str = Field(min_length=1, max_length=255)
    rendering_policy_version: str = Field(min_length=1, max_length=64)
    docx_bytes: bytes | None = None
    output_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    failure_code: DocxRenderingFailureCode | None = None
    diagnostics: tuple[str, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _status_consistency(self) -> "CvDocxRenderingResult":
        if self.status == DocxRenderingStatus.SUCCEEDED:
            if self.docx_bytes is None or self.output_sha256 is None:
                raise ValueError("SUCCEEDED requires docx_bytes and output_sha256")
            if not self.docx_bytes:
                raise ValueError("SUCCEEDED requires non-empty docx_bytes")
            if self.failure_code is not None:
                raise ValueError("SUCCEEDED must carry no failure_code")
        else:
            if self.docx_bytes is not None or self.output_sha256 is not None:
                raise ValueError("FAILED must carry no docx_bytes/output_sha256")
            if self.failure_code is None:
                raise ValueError("FAILED requires a failure_code")
        return self


# -- Proposal artifact ----------------------------------------------------------


class DocxProposalProvenanceMode(StrEnum):
    """How a ``CvDocxProposalArtifact``'s bytes were produced. P6-A defines
    exactly one route: a controlled renderer call."""

    GENERATED_BY_RENDERER = "GENERATED_BY_RENDERER"


class CvDocxProposalArtifact(BaseModel):
    """One generated DOCX proposal for a given owner. Never carries a
    filesystem path -- ``artifact_id`` is a repository locator only and
    never participates in ``artifact_fingerprint``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: UUID4
    owner_key: JobArtifactOwnerKey
    approved_cv_content_fingerprint: str = Field(pattern=SHA256_PATTERN)
    content_plan_fingerprint: str = Field(pattern=SHA256_PATTERN)
    role_strategy_context_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)
    document_schema_version: str = Field(min_length=1, max_length=64)
    rendering_policy_version: str = Field(min_length=1, max_length=64)
    renderer_adapter_id: str = Field(min_length=1, max_length=255)
    template_fingerprint: str = Field(pattern=SHA256_PATTERN)
    generation_input_fingerprint: str = Field(pattern=SHA256_PATTERN)
    generated_docx_content_hash: str = Field(pattern=SHA256_PATTERN)
    current_file_hash: str = Field(pattern=SHA256_PATTERN)
    validated_file_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)
    proposal_revision: int = Field(ge=1)
    superseded: bool
    artifact_fingerprint: str = Field(pattern=SHA256_PATTERN)
    provenance_mode: DocxProposalProvenanceMode = DocxProposalProvenanceMode.GENERATED_BY_RENDERER


# -- Proposal build result -------------------------------------------------------


class ProposalBuildStatus(StrEnum):
    GENERATED = "GENERATED"
    INPUT_INVALID = "INPUT_INVALID"
    STALE_REVISION = "STALE_REVISION"
    RENDERING_FAILED = "RENDERING_FAILED"
    RENDERER_OUTPUT_HASH_MISMATCH = "RENDERER_OUTPUT_HASH_MISMATCH"
    TEMPLATE_HASH_CHANGED = "TEMPLATE_HASH_CHANGED"


class CvDocxProposalBuildResult(BaseModel):
    """The single return value of ``build_current_docx_proposal``. Only
    ``GENERATED`` ever carries a new current proposal artifact/bytes -- any
    other status leaves the repository's current proposal slot untouched."""

    model_config = ConfigDict(extra="forbid")

    status: ProposalBuildStatus
    artifact: CvDocxProposalArtifact | None = None
    docx_bytes: bytes | None = None
    input_violations: tuple[DocumentInputViolation, ...] = Field(default_factory=tuple)
    diagnostics: tuple[str, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _status_consistency(self) -> "CvDocxProposalBuildResult":
        if self.status == ProposalBuildStatus.GENERATED:
            if self.artifact is None or self.docx_bytes is None:
                raise ValueError("GENERATED requires artifact and docx_bytes")
        else:
            if self.artifact is not None or self.docx_bytes is not None:
                raise ValueError(f"{self.status} must carry no artifact/docx_bytes")
        return self


# -- Manual edit detection ------------------------------------------------------


class ManualEditStatus(StrEnum):
    UNMODIFIED = "UNMODIFIED"
    USER_EDITED = "USER_EDITED"
    FILE_MISSING = "FILE_MISSING"
    FILE_CHANGED_DURING_READ = "FILE_CHANGED_DURING_READ"
    FRESHNESS_NOT_VERIFIED = "FRESHNESS_NOT_VERIFIED"


class ManualEditDetectionResult(BaseModel):
    """Raw SHA-256 bytes comparison only -- never mtime, filesize,
    filename, path, or Word metadata."""

    model_config = ConfigDict(extra="forbid")

    status: ManualEditStatus
    expected_sha256: str = Field(pattern=SHA256_PATTERN)
    observed_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def _status_consistency(self) -> "ManualEditDetectionResult":
        if self.status == ManualEditStatus.UNMODIFIED:
            if self.observed_sha256 != self.expected_sha256:
                raise ValueError("UNMODIFIED requires observed_sha256 == expected_sha256")
        elif self.status == ManualEditStatus.USER_EDITED:
            if self.observed_sha256 is None or self.observed_sha256 == self.expected_sha256:
                raise ValueError("USER_EDITED requires an observed_sha256 different from expected")
        elif self.status in (
            ManualEditStatus.FILE_MISSING,
            ManualEditStatus.FRESHNESS_NOT_VERIFIED,
        ):
            if self.observed_sha256 is not None:
                raise ValueError(f"{self.status} must carry no observed_sha256")
        elif self.status == ManualEditStatus.FILE_CHANGED_DURING_READ:
            if self.observed_sha256 is not None:
                raise ValueError("FILE_CHANGED_DURING_READ must carry no single observed_sha256")
        return self


# -- Manual document snapshot confirmation --------------------------------------


class DocumentProvenanceMode(StrEnum):
    """Whether a ``ValidatedCvDocxSnapshot``/``ConfirmedCvPdfArtifact`` was
    built from the pipeline's own unmodified proposal bytes, or from bytes
    the user manually edited and then explicitly confirmed. Never implies
    the two are semantically equivalent to ``ApprovedCvContent``."""

    PIPELINE_UNMODIFIED_DOCUMENT = "PIPELINE_UNMODIFIED_DOCUMENT"
    USER_CONFIRMED_MANUAL_DOCUMENT = "USER_CONFIRMED_MANUAL_DOCUMENT"


class ManualDocumentSnapshotConfirmation(BaseModel):
    """An explicit, user-sourced confirmation that a manually edited DOCX
    should be snapshotted anyway.

    The system never infers this on the user's behalf: for
    ``USER_CONFIRMED_MANUAL_DOCUMENT`` a literal ``CONFIRMED`` decision is
    required, and it must name the exact bytes hash and proposal revision
    it applies to -- a confirmation can never be silently reused for a
    different hash or an older revision.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    owner_key_fingerprint: str = Field(pattern=SHA256_PATTERN)
    proposal_artifact_fingerprint: str = Field(pattern=SHA256_PATTERN)
    proposal_revision: int = Field(ge=1)
    exact_docx_sha256: str = Field(pattern=SHA256_PATTERN)
    confirmation_mode: DocumentProvenanceMode
    user_confirmation_decision: Literal["CONFIRMED"] | None = None
    confirmation_policy_version: str = Field(
        min_length=1, max_length=64, default=MANUAL_CONFIRMATION_POLICY_VERSION
    )
    confirmation_fingerprint: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def _mode_requires_decision(self) -> "ManualDocumentSnapshotConfirmation":
        if self.confirmation_mode == DocumentProvenanceMode.USER_CONFIRMED_MANUAL_DOCUMENT:
            if self.user_confirmation_decision != "CONFIRMED":
                raise ValueError(
                    "USER_CONFIRMED_MANUAL_DOCUMENT requires an explicit CONFIRMED decision"
                )
        else:
            if self.user_confirmation_decision is not None:
                raise ValueError("PIPELINE_UNMODIFIED_DOCUMENT must carry no user_confirmation_decision")
        return self


# -- Structural validation -------------------------------------------------------


class DocxStructuralValidationStatus(StrEnum):
    VALID = "VALID"
    INVALID = "INVALID"


class DocxStructuralViolationCode(StrEnum):
    BYTES_EMPTY = "BYTES_EMPTY"
    SHA256_MISMATCH = "SHA256_MISMATCH"
    INVALID_ZIP_STRUCTURE = "INVALID_ZIP_STRUCTURE"
    MISSING_REQUIRED_PART = "MISSING_REQUIRED_PART"
    EMPTY_BODY = "EMPTY_BODY"
    UNRESOLVED_TEMPLATE_PLACEHOLDER = "UNRESOLVED_TEMPLATE_PLACEHOLDER"
    HASH_CHANGED_DURING_VALIDATION = "HASH_CHANGED_DURING_VALIDATION"


class DocxStructuralValidationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: DocxStructuralValidationStatus
    validated_sha256: str = Field(pattern=SHA256_PATTERN)
    violations: tuple[DocxStructuralViolationCode, ...] = Field(default_factory=tuple)
    diagnostics: tuple[str, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _status_consistency(self) -> "DocxStructuralValidationResult":
        if self.status == DocxStructuralValidationStatus.VALID and self.violations:
            raise ValueError("VALID must carry no violations")
        if self.status == DocxStructuralValidationStatus.INVALID and not self.violations:
            raise ValueError("INVALID requires at least one violation")
        return self


# -- Validated snapshot -----------------------------------------------------------


class ValidatedCvDocxSnapshot(BaseModel):
    """An immutable, content-addressed DOCX snapshot. Never built from a
    live proposal path -- only from exact bytes re-read from the
    repository and re-hashed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    owner_key: JobArtifactOwnerKey
    proposal_artifact_fingerprint: str = Field(pattern=SHA256_PATTERN)
    proposal_revision: int = Field(ge=1)
    exact_docx_sha256: str = Field(pattern=SHA256_PATTERN)
    approved_cv_content_fingerprint: str = Field(pattern=SHA256_PATTERN)
    content_plan_fingerprint: str = Field(pattern=SHA256_PATTERN)
    template_fingerprint: str = Field(pattern=SHA256_PATTERN)
    validation_policy_version: str = Field(min_length=1, max_length=64)
    structural_validation_result: DocxStructuralValidationResult
    manual_confirmation_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)
    provenance_mode: DocumentProvenanceMode
    snapshot_fingerprint: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def _provenance_requires_confirmation(self) -> "ValidatedCvDocxSnapshot":
        if self.provenance_mode == DocumentProvenanceMode.USER_CONFIRMED_MANUAL_DOCUMENT:
            if self.manual_confirmation_fingerprint is None:
                raise ValueError(
                    "USER_CONFIRMED_MANUAL_DOCUMENT requires manual_confirmation_fingerprint"
                )
        else:
            if self.manual_confirmation_fingerprint is not None:
                raise ValueError(
                    "PIPELINE_UNMODIFIED_DOCUMENT must carry no manual_confirmation_fingerprint"
                )
        return self


class SnapshotBuildStatus(StrEnum):
    SNAPSHOT_CREATED = "SNAPSHOT_CREATED"
    NO_CURRENT_PROPOSAL = "NO_CURRENT_PROPOSAL"
    PROPOSAL_BYTES_MISSING = "PROPOSAL_BYTES_MISSING"
    MANUAL_CONFIRMATION_REQUIRED = "MANUAL_CONFIRMATION_REQUIRED"
    MANUAL_CONFIRMATION_MISMATCH = "MANUAL_CONFIRMATION_MISMATCH"
    STRUCTURAL_VALIDATION_FAILED = "STRUCTURAL_VALIDATION_FAILED"
    HASH_CHANGED_DURING_VALIDATION = "HASH_CHANGED_DURING_VALIDATION"
    REPOSITORY_SAVE_FAILED = "REPOSITORY_SAVE_FAILED"
    REPOSITORY_INTEGRITY_FAILURE = "REPOSITORY_INTEGRITY_FAILURE"


class CvDocxSnapshotBuildResult(BaseModel):
    """The single return value of ``build_validated_docx_snapshot``. Only
    ``SNAPSHOT_CREATED`` ever carries a snapshot."""

    model_config = ConfigDict(extra="forbid")

    status: SnapshotBuildStatus
    snapshot: ValidatedCvDocxSnapshot | None = None
    diagnostics: tuple[str, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _status_consistency(self) -> "CvDocxSnapshotBuildResult":
        if self.status == SnapshotBuildStatus.SNAPSHOT_CREATED:
            if self.snapshot is None:
                raise ValueError("SNAPSHOT_CREATED requires a snapshot")
        else:
            if self.snapshot is not None:
                raise ValueError(f"{self.status} must carry no snapshot")
        return self


# -- PDF conversion ---------------------------------------------------------------


class PdfConversionStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class PdfConversionFailureCode(StrEnum):
    ADAPTER_ERROR = "ADAPTER_ERROR"
    TIMEOUT = "TIMEOUT"
    INVALID_INPUT = "INVALID_INPUT"
    UNSUPPORTED_DOCUMENT = "UNSUPPORTED_DOCUMENT"
    UNKNOWN = "UNKNOWN"


class CvPdfConversionAttemptResult(BaseModel):
    """One conversion attempt. A ``FAILED`` attempt is never itself a
    document artifact -- it never creates or replaces the current PDF."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: PdfConversionStatus
    adapter_id: str = Field(min_length=1, max_length=255)
    conversion_policy_version: str = Field(min_length=1, max_length=64)
    pdf_bytes: bytes | None = None
    pdf_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    failure_code: PdfConversionFailureCode | None = None
    diagnostics: tuple[str, ...] = Field(default_factory=tuple)
    attempt_fingerprint: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def _status_consistency(self) -> "CvPdfConversionAttemptResult":
        if self.status == PdfConversionStatus.SUCCEEDED:
            if self.pdf_bytes is None or self.pdf_sha256 is None:
                raise ValueError("SUCCEEDED requires pdf_bytes and pdf_sha256")
            if not self.pdf_bytes:
                raise ValueError("SUCCEEDED requires non-empty pdf_bytes")
            if self.failure_code is not None:
                raise ValueError("SUCCEEDED must carry no failure_code")
        else:
            if self.pdf_bytes is not None or self.pdf_sha256 is not None:
                raise ValueError("FAILED must carry no pdf_bytes/pdf_sha256")
            if self.failure_code is None:
                raise ValueError("FAILED requires a failure_code")
        return self


class ConfirmedCvPdfArtifact(BaseModel):
    """A confirmed PDF -- can never represent a failed conversion attempt.
    A failed attempt stays exactly a ``CvPdfConversionAttemptResult`` and
    is never promoted into this model."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: UUID4
    owner_key: JobArtifactOwnerKey
    source_validated_docx_snapshot_fingerprint: str = Field(pattern=SHA256_PATTERN)
    source_docx_sha256: str = Field(pattern=SHA256_PATTERN)
    approved_cv_content_fingerprint: str = Field(pattern=SHA256_PATTERN)
    content_plan_fingerprint: str = Field(pattern=SHA256_PATTERN)
    pdf_sha256: str = Field(pattern=SHA256_PATTERN)
    conversion_adapter_id: str = Field(min_length=1, max_length=255)
    conversion_policy_version: str = Field(min_length=1, max_length=64)
    provenance_mode: DocumentProvenanceMode
    artifact_fingerprint: str = Field(pattern=SHA256_PATTERN)
    pdf_revision: int = Field(ge=1)
    superseded: bool


class PdfConfirmationBuildStatus(StrEnum):
    CONFIRMED = "CONFIRMED"
    SNAPSHOT_NOT_FOUND = "SNAPSHOT_NOT_FOUND"
    SNAPSHOT_MISMATCH = "SNAPSHOT_MISMATCH"
    CONVERSION_FAILED = "CONVERSION_FAILED"
    TAMPERED_PDF_HASH = "TAMPERED_PDF_HASH"
    STALE_REVISION = "STALE_REVISION"
    #: Explicit Provenance Stage P6-B3b-A: the legacy
    #: ``cv_document_pdf_slots`` table has been permanently frozen by the
    #: Final-Confirmed-PDF cutover -- never a transient condition, never
    #: mapped from/onto ``STALE_REVISION``.
    LEGACY_CURRENT_PDF_CUTOVER_FROZEN = "LEGACY_CURRENT_PDF_CUTOVER_FROZEN"


class CvPdfConfirmationBuildResult(BaseModel):
    """The single return value of ``build_and_confirm_pdf``. Only
    ``CONFIRMED`` ever carries a ``ConfirmedCvPdfArtifact`` -- a failed
    conversion attempt is surfaced through ``attempt`` and never promoted
    into an artifact, never replaces an existing current PDF, and never
    changes the PDF revision."""

    model_config = ConfigDict(extra="forbid")

    status: PdfConfirmationBuildStatus
    artifact: ConfirmedCvPdfArtifact | None = None
    attempt: CvPdfConversionAttemptResult | None = None
    diagnostics: tuple[str, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _status_consistency(self) -> "CvPdfConfirmationBuildResult":
        if self.status == PdfConfirmationBuildStatus.CONFIRMED:
            if self.artifact is None:
                raise ValueError("CONFIRMED requires an artifact")
        else:
            if self.artifact is not None:
                raise ValueError(f"{self.status} must carry no artifact")
        return self


# -- Lifecycle status -------------------------------------------------------------


class DocxProposalStatus(StrEnum):
    NO_PROPOSAL = "NO_PROPOSAL"
    PROPOSAL_CURRENT = "PROPOSAL_CURRENT"
    PROPOSAL_EXTERNALLY_MODIFIED = "PROPOSAL_EXTERNALLY_MODIFIED"
    PROPOSAL_STALE = "PROPOSAL_STALE"
    PROPOSAL_VALIDATED = "PROPOSAL_VALIDATED"
    PROPOSAL_FILE_MISSING = "PROPOSAL_FILE_MISSING"


class PdfConfirmationStatus(StrEnum):
    NO_PDF = "NO_PDF"
    PDF_CURRENT = "PDF_CURRENT"
    PDF_CURRENT_BASED_ON_OLDER_PROPOSAL = "PDF_CURRENT_BASED_ON_OLDER_PROPOSAL"


class DocumentLifecycleView(BaseModel):
    """A read-only, computed view over a document owner's proposal/PDF
    slots -- never a single collapsed ``is_confirmed`` boolean."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    proposal_status: DocxProposalStatus
    pdf_status: PdfConfirmationStatus
    current_proposal_revision: int | None = None
    current_pdf_revision: int | None = None
    pdf_source_proposal_revision: int | None = None


# -- Local document open protocol result -----------------------------------------


class LocalDocumentOpenOutcome(StrEnum):
    """P6-A defines the contract only. No implementation ever runs in this
    stage -- see ``docs/explicit-provenance-stage-p6a.md``."""

    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"


class LocalDocumentOpenRequest(BaseModel):
    """Never carries a filesystem path -- only a content-addressed
    locator. ``suggested_filename`` is presentation-only and never
    participates in any fingerprint or identity check."""

    model_config = ConfigDict(extra="forbid")

    exact_sha256: str = Field(pattern=SHA256_PATTERN)
    suggested_filename: str | None = None


class LocalDocumentOpenResult(BaseModel):
    """Never carries a path or suggested filename as part of identity --
    both are presentation-only and excluded from any fingerprint."""

    model_config = ConfigDict(extra="forbid")

    outcome: LocalDocumentOpenOutcome
    exact_sha256: str = Field(pattern=SHA256_PATTERN)
    diagnostics: tuple[str, ...] = Field(default_factory=tuple)


# -- Replay / freshness -----------------------------------------------------------


class DocumentReplayFreshnessStatus(StrEnum):
    FRESH = "FRESH"
    STALE = "STALE"
    FRESHNESS_NOT_VERIFIED = "FRESHNESS_NOT_VERIFIED"
    FILE_MISSING = "FILE_MISSING"
    FILE_CHANGED = "FILE_CHANGED"
    SNAPSHOT_MISMATCH = "SNAPSHOT_MISMATCH"
    CONVERSION_MISMATCH = "CONVERSION_MISMATCH"


class ApprovedDocumentInputReplayInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approved_content_result_fingerprint: str = Field(pattern=SHA256_PATTERN)
    content_plan_fingerprint: str = Field(pattern=SHA256_PATTERN)
    owner_key_fingerprint: str = Field(pattern=SHA256_PATTERN)
    document_input_fingerprint: str = Field(pattern=SHA256_PATTERN)


class ProposalGenerationReplayInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owner_key_fingerprint: str = Field(pattern=SHA256_PATTERN)
    approved_cv_content_fingerprint: str = Field(pattern=SHA256_PATTERN)
    content_plan_fingerprint: str = Field(pattern=SHA256_PATTERN)
    role_strategy_context_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)
    template_fingerprint: str = Field(pattern=SHA256_PATTERN)
    renderer_adapter_id: str = Field(min_length=1, max_length=255)
    rendering_policy_version: str = Field(min_length=1, max_length=64)
    document_schema_version: str = Field(min_length=1, max_length=64)
    generation_input_fingerprint: str = Field(pattern=SHA256_PATTERN)


class ValidatedSnapshotReplayInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_artifact_fingerprint: str = Field(pattern=SHA256_PATTERN)
    proposal_revision: int = Field(ge=1)
    validation_policy_version: str = Field(min_length=1, max_length=64)
    manual_confirmation_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)
    provenance_mode: DocumentProvenanceMode
    snapshot_fingerprint: str = Field(pattern=SHA256_PATTERN)


class PdfConversionReplayInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    validated_docx_snapshot_fingerprint: str = Field(pattern=SHA256_PATTERN)
    exact_docx_sha256: str = Field(pattern=SHA256_PATTERN)
    conversion_adapter_id: str = Field(min_length=1, max_length=255)
    conversion_policy_version: str = Field(min_length=1, max_length=64)


class ConfirmedPdfReplayInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_validated_docx_snapshot_fingerprint: str = Field(pattern=SHA256_PATTERN)
    source_docx_sha256: str = Field(pattern=SHA256_PATTERN)
    pdf_sha256: str = Field(pattern=SHA256_PATTERN)
    conversion_adapter_id: str = Field(min_length=1, max_length=255)
    conversion_policy_version: str = Field(min_length=1, max_length=64)
    artifact_fingerprint: str = Field(pattern=SHA256_PATTERN)


class DocumentReplayResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    freshness_status: DocumentReplayFreshnessStatus
    stale_fields: tuple[str, ...] = Field(default_factory=tuple)
