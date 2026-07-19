"""Public contract for a controlled draft generated from an approved plan."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.models import ResumeData


GenerationStatus = Literal["GENERATING", "GENERATED", "FAILED", "SUPERSEDED"]
GenerationMode = Literal[
    "COPIED",
    "COPIED_LOW_PRIORITY",
    "GENERATED",
    "EXACT_INSERT",
    "OMITTED",
    "EXCLUDED",
    "PRESERVED_AFTER_REVIEW",
    "EXCLUDED_UNRESOLVED",
    "COPIED_NO_MUTABLE_CONTENT",
    "COPIED_PROTECTED_CONTENT",
    "EXCLUDED_BY_PARENT",
]
GenerationSection = Literal["SUMMARY", "EXPERIENCE", "COMPETENCIES", "ACHIEVEMENTS", "TOOLS"]
GenerationAction = Literal["KEEP", "EMPHASIZE", "DEEMPHASIZE", "REPHRASE", "OMIT", "HUMAN_REVIEW"]
GenerationFailureCode = Literal[
    "INVALID_LLM_RESPONSE",
    "SOURCE_REFERENCE_MISMATCH",
    "NUMERIC_BOUNDARY_VIOLATION",
    "PROHIBITED_CLAIM_BOUNDARY_VIOLATION",
    "LLM_PROVIDER_ERROR",
    "DRAFT_ASSEMBLY_FAILED",
    "SOURCE_CHANGED_DURING_GENERATION",
]
SOURCE_REFERENCE_PATTERN = r"^(?:resume|positioning):(?:summary|experience|competencies|achievements|tools):\d{4}$"


def _exact_keys(value: Any, allowed: set[str], path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be an object")
    unknown = set(value) - allowed
    missing = allowed - set(value)
    if unknown or missing:
        raise ValueError(f"{path} has unknown or missing fields")
    return value


def _validate_exact_resume_data(value: Any) -> Any:
    if value is None:
        return value
    resume = _exact_keys(
        value,
        {
            "personalInfo", "summary", "workExperience", "education",
            "personalProjects", "additional", "sectionMeta", "customSections",
        },
        "draft_resume",
    )
    personal = _exact_keys(
        resume["personalInfo"],
        {"name", "title", "email", "phone", "location", "website", "linkedin", "github"},
        "draft_resume.personalInfo",
    )
    for key in ("name", "title", "email", "phone", "location"):
        if not isinstance(personal[key], str):
            raise ValueError(f"draft_resume.personalInfo.{key} must be a string")
    for key in ("website", "linkedin", "github"):
        if personal[key] is not None and not isinstance(personal[key], str):
            raise ValueError(f"draft_resume.personalInfo.{key} must be a string or null")
    if not isinstance(resume["summary"], str):
        raise ValueError("draft_resume.summary must be a string")

    additional = _exact_keys(
        resume["additional"],
        {"technicalSkills", "languages", "certificationsTraining", "awards"},
        "draft_resume.additional",
    )
    for key, values in additional.items():
        if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
            raise ValueError(f"draft_resume.additional.{key} must contain only strings")
    nested = (
        ("workExperience", {"id", "title", "company", "location", "years", "description"}),
        ("education", {"id", "institution", "degree", "years", "description"}),
        ("personalProjects", {"id", "name", "role", "years", "github", "website", "description"}),
        ("sectionMeta", {"id", "key", "displayName", "sectionType", "isDefault", "isVisible", "order"}),
    )
    for collection, keys in nested:
        if not isinstance(resume[collection], list):
            raise ValueError(f"draft_resume.{collection} must be an array")
        for index, item in enumerate(resume[collection]):
            entry = _exact_keys(item, keys, f"draft_resume.{collection}[{index}]")
            path = f"draft_resume.{collection}[{index}]"
            if collection == "workExperience":
                _strict_integer(entry["id"], f"{path}.id")
                _strict_strings(entry, ("title", "company", "years"), path)
                _strict_nullable_string(entry["location"], f"{path}.location")
                _strict_string_array(entry["description"], f"{path}.description")
            elif collection == "education":
                _strict_integer(entry["id"], f"{path}.id")
                _strict_strings(entry, ("institution", "degree", "years"), path)
                _strict_nullable_string(entry["description"], f"{path}.description")
            elif collection == "personalProjects":
                _strict_integer(entry["id"], f"{path}.id")
                _strict_strings(entry, ("name", "role", "years"), path)
                _strict_nullable_string(entry["github"], f"{path}.github")
                _strict_nullable_string(entry["website"], f"{path}.website")
                _strict_string_array(entry["description"], f"{path}.description")
            else:
                _strict_strings(entry, ("id", "key", "displayName"), path)
                if entry["sectionType"] not in {"personalInfo", "text", "itemList", "stringList"}:
                    raise ValueError(f"{path}.sectionType is invalid")
                if not isinstance(entry["isDefault"], bool) or not isinstance(entry["isVisible"], bool):
                    raise ValueError(f"{path} visibility fields must be booleans")
                _strict_integer(entry["order"], f"{path}.order")
    if not isinstance(resume["customSections"], dict):
        raise ValueError("draft_resume.customSections must be an object")
    for key, section in resume["customSections"].items():
        section_value = _exact_keys(
            section,
            {"sectionType", "items", "strings", "text"},
            f"draft_resume.customSections.{key}",
        )
        section_type = section_value["sectionType"]
        if section_type not in {"text", "itemList", "stringList"}:
            raise ValueError("custom section type is invalid")
        items, strings, text = (
            section_value["items"], section_value["strings"], section_value["text"]
        )
        if section_type == "itemList":
            if not isinstance(items, list) or strings is not None or text is not None:
                raise ValueError("itemList custom section has an invalid shape")
            for index, item in enumerate(items):
                entry = _exact_keys(
                    item,
                    {"id", "title", "subtitle", "location", "years", "description"},
                    f"draft_resume.customSections.{key}.items[{index}]",
                )
                path = f"draft_resume.customSections.{key}.items[{index}]"
                _strict_integer(entry["id"], f"{path}.id")
                _strict_strings(entry, ("title", "years"), path)
                _strict_nullable_string(entry["subtitle"], f"{path}.subtitle")
                _strict_nullable_string(entry["location"], f"{path}.location")
                _strict_string_array(entry["description"], f"{path}.description")
        elif section_type == "stringList":
            if items is not None or text is not None:
                raise ValueError("stringList custom section has an invalid shape")
            _strict_string_array(strings, f"draft_resume.customSections.{key}.strings")
        elif not isinstance(text, str) or items is not None or strings is not None:
            raise ValueError("text custom section has an invalid shape")
    return value


def _strict_integer(value: Any, path: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{path} must be an integer")


def _strict_strings(value: dict[str, Any], keys: tuple[str, ...], path: str) -> None:
    for key in keys:
        if not isinstance(value[key], str):
            raise ValueError(f"{path}.{key} must be a string")


def _strict_nullable_string(value: Any, path: str) -> None:
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{path} must be a string or null")


def _strict_string_array(value: Any, path: str) -> None:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{path} must contain only strings")


class CVTransformationGenerationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approval_id: str = Field(min_length=1, max_length=128)
    plan_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    retry_failed: bool = False


class CVTransformationProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_reference: str = Field(pattern=SOURCE_REFERENCE_PATTERN)
    section: GenerationSection
    action: GenerationAction
    decision: Literal["ACCEPT", "REJECT"]
    mode: GenerationMode
    priority: Literal["HIGH", "NORMAL", "LOW"]
    source_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    llm_used: bool
    prompt_version: str | None = None
    validation_status: Literal["NOT_RUN"]
    boundary_validation_status: Literal["PASSED", "NOT_APPLICABLE"]

    @model_validator(mode="after")
    def validate_mode_contract(self) -> "CVTransformationProvenance":
        excluded = {"OMITTED", "EXCLUDED", "EXCLUDED_UNRESOLVED", "EXCLUDED_BY_PARENT"}
        copied = {
            "COPIED", "COPIED_LOW_PRIORITY", "EXACT_INSERT",
            "PRESERVED_AFTER_REVIEW", "COPIED_NO_MUTABLE_CONTENT",
            "COPIED_PROTECTED_CONTENT",
        }
        if self.decision == "REJECT" and self.mode != "EXCLUDED":
            raise ValueError("REJECT provenance must use EXCLUDED")
        if self.decision == "ACCEPT":
            if self.mode == "EXCLUDED_BY_PARENT":
                if self.section != "ACHIEVEMENTS":
                    raise ValueError("EXCLUDED_BY_PARENT is only valid for achievements")
            else:
                expected = {
                    "KEEP": {"COPIED"},
                    "DEEMPHASIZE": {"COPIED_LOW_PRIORITY"},
                    "OMIT": {"OMITTED"},
                    "HUMAN_REVIEW": {"PRESERVED_AFTER_REVIEW", "EXCLUDED_UNRESOLVED"},
                }.get(self.action)
                if self.action in {"EMPHASIZE", "REPHRASE"}:
                    expected = (
                        {"EXACT_INSERT"}
                        if self.section in {"COMPETENCIES", "TOOLS"}
                        else {"GENERATED", "COPIED_NO_MUTABLE_CONTENT", "COPIED_PROTECTED_CONTENT"}
                        if self.section == "EXPERIENCE"
                        else {"GENERATED", "COPIED_PROTECTED_CONTENT"}
                    )
                if expected is None or self.mode not in expected:
                    raise ValueError("Action, section and mode are inconsistent")
        if self.mode == "GENERATED":
            if not self.llm_used or self.prompt_version != "1.3" or not self.output_fingerprint:
                raise ValueError("GENERATED provenance requires an LLM output")
            if self.boundary_validation_status != "PASSED":
                raise ValueError("GENERATED provenance requires passed boundary validation")
        elif self.mode in copied:
            if self.llm_used or self.prompt_version is not None or not self.output_fingerprint:
                raise ValueError("Copied provenance cannot claim LLM use")
            if self.boundary_validation_status != "NOT_APPLICABLE":
                raise ValueError("Copied provenance has no boundary validation")
        elif self.mode in excluded:
            if self.llm_used or self.prompt_version is not None or self.output_fingerprint is not None:
                raise ValueError("Excluded provenance cannot expose output")
            if self.boundary_validation_status != "NOT_APPLICABLE":
                raise ValueError("Excluded provenance has no boundary validation")
        return self


class CVTransformationGeneration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generation_id: str
    approval_id: str
    resume_id: str
    job_id: str
    plan_version: Literal["1.1"]
    plan_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    generation_version: Literal["1.3"]
    prompt_version: Literal["1.3"]
    generation_input_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: GenerationStatus
    provider: str
    model: str
    draft_resume: ResumeData | None = None
    provenance: list[CVTransformationProvenance] = Field(default_factory=list)
    failure_code: GenerationFailureCode | None = None
    attempt_count: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    requires_truth_validation: Literal[True] = True
    truth_validation_status: Literal["NOT_RUN"] = "NOT_RUN"
    applied_to_resume: Literal[False] = False

    @field_validator("draft_resume", mode="before")
    @classmethod
    def validate_exact_draft(cls, value: Any) -> Any:
        return _validate_exact_resume_data(value)

    @field_validator("created_at", "updated_at", "completed_at")
    @classmethod
    def validate_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("Generation timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_status_payload(self) -> "CVTransformationGeneration":
        if self.status == "GENERATING":
            if self.draft_resume is not None or self.provenance or self.completed_at or self.failure_code:
                raise ValueError("GENERATING cannot contain result or failure data")
        elif self.status == "GENERATED":
            if self.draft_resume is None or not self.provenance or not self.completed_at or self.failure_code:
                raise ValueError("GENERATED requires complete draft and provenance")
        elif self.status == "FAILED":
            if self.draft_resume is not None or self.provenance or not self.completed_at or not self.failure_code:
                raise ValueError("FAILED requires only a safe failure code")
        elif self.status == "SUPERSEDED" and self.draft_resume is not None:
            raise ValueError("SUPERSEDED cannot expose a current draft")
        references = [item.source_reference for item in self.provenance]
        if references != sorted(references) or len(references) != len(set(references)):
            raise ValueError("Provenance must be unique and sorted")
        if self.status == "SUPERSEDED" and self.provenance:
            raise ValueError("SUPERSEDED cannot expose provenance")
        return self


class ExperienceBulletLLMResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    index: int = Field(ge=0)
    source_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    text: str


class GenerationLLMItemResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_reference: str
    prompt_version: Literal["1.3"]
    content: str | list[ExperienceBulletLLMResponse]
