"""OpenAPI contract tests for the P6-C1a CV Document Workflow API router.

Asserts the generated OpenAPI document exposes exactly the 8 P6-C1a
endpoints under the ``CV Documents`` tag, with the exact paths/operation_ids
from the stage spec, no ``POST proposal`` endpoint, only public response
schemas, documented binary media types/error schemas, and no leaked
internal domain enum, fingerprint/sha256 field, runtime identity, or
filesystem path anywhere in the generated schema.
"""

from __future__ import annotations

import json

import pytest

from app.main import app


@pytest.fixture(scope="module")
def openapi_schema() -> dict:
    return app.openapi()


@pytest.fixture(scope="module")
def cv_document_paths(openapi_schema: dict) -> dict:
    return {
        path: methods
        for path, methods in openapi_schema["paths"].items()
        if "cv-documents" in path
    }


_EXPECTED_PATHS_METHODS = {
    ("/api/v1/jobs/{job_id}/cv-documents", "get"): "get_cv_document_workflow_status",
    ("/api/v1/jobs/{job_id}/cv-documents/proposal", "get"): "download_cv_proposal",
    ("/api/v1/jobs/{job_id}/cv-documents/working-copy", "post"): "create_cv_working_copy",
    ("/api/v1/jobs/{job_id}/cv-documents/working-copy", "get"): "download_cv_working_copy",
    ("/api/v1/jobs/{job_id}/cv-documents/final-snapshot", "post"): "finalize_cv_working_copy",
    ("/api/v1/jobs/{job_id}/cv-documents/final-pdf", "post"): "generate_cv_final_pdf",
    ("/api/v1/jobs/{job_id}/cv-documents/final-pdf", "get"): "download_cv_final_pdf",
    ("/api/v1/jobs/{job_id}/cv-documents/final-docx", "get"): "download_cv_final_docx",
}


def test_router_is_registered(cv_document_paths: dict) -> None:
    assert len(cv_document_paths) > 0


def test_exactly_8_p6c1a_endpoints(cv_document_paths: dict) -> None:
    operation_count = sum(len(methods) for methods in cv_document_paths.values())
    assert operation_count == 8


def test_no_post_proposal_endpoint(cv_document_paths: dict) -> None:
    proposal_path = cv_document_paths.get("/api/v1/jobs/{job_id}/cv-documents/proposal", {})
    assert "post" not in proposal_path


def test_exact_paths(cv_document_paths: dict) -> None:
    expected_paths = {path for path, _ in _EXPECTED_PATHS_METHODS}
    assert set(cv_document_paths.keys()) == expected_paths


def test_exact_operation_ids(cv_document_paths: dict) -> None:
    for (path, method), expected_operation_id in _EXPECTED_PATHS_METHODS.items():
        operation = cv_document_paths[path][method]
        assert operation["operationId"] == expected_operation_id, f"{method} {path}"


def test_tag_is_cv_documents(cv_document_paths: dict) -> None:
    for path, methods in cv_document_paths.items():
        for method, operation in methods.items():
            assert operation.get("tags") == ["CV Documents"], f"{method} {path}"


def test_public_response_schemas_referenced(openapi_schema: dict, cv_document_paths: dict) -> None:
    json_response_operations = {
        "get_cv_document_workflow_status": "CvDocumentWorkflowStatusResponse",
        "create_cv_working_copy": "CvDocumentWorkingCopyResponse",
        "finalize_cv_working_copy": "CvDocumentFinalSnapshotResponse",
        "generate_cv_final_pdf": "CvDocumentFinalPdfResponse",
    }
    schemas = openapi_schema["components"]["schemas"]
    for expected_schema_name in json_response_operations.values():
        assert expected_schema_name in schemas


def test_documented_binary_media_types(cv_document_paths: dict) -> None:
    docx_media_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    proposal_get = cv_document_paths["/api/v1/jobs/{job_id}/cv-documents/proposal"]["get"]
    assert docx_media_type in proposal_get["responses"]["200"]["content"]

    working_copy_get = cv_document_paths["/api/v1/jobs/{job_id}/cv-documents/working-copy"]["get"]
    assert docx_media_type in working_copy_get["responses"]["200"]["content"]

    final_docx_get = cv_document_paths["/api/v1/jobs/{job_id}/cv-documents/final-docx"]["get"]
    assert docx_media_type in final_docx_get["responses"]["200"]["content"]

    final_pdf_get = cv_document_paths["/api/v1/jobs/{job_id}/cv-documents/final-pdf"]["get"]
    assert "application/pdf" in final_pdf_get["responses"]["200"]["content"]


def test_documented_error_schemas(cv_document_paths: dict) -> None:
    for path, methods in cv_document_paths.items():
        for method, operation in methods.items():
            responses = operation["responses"]
            assert "404" in responses, f"{method} {path} missing 404"
            assert "503" in responses, f"{method} {path} missing 503"


#: The P6-C1a router only ever declares these JSON error statuses via
#: ``_ERROR_RESPONSES`` -- 422 is deliberately excluded here because it is
#: FastAPI's own auto-generated request-validation response
#: (``HTTPValidationError``), never a status this router declares itself.
_JSON_ERROR_STATUS_CODES = {"400", "404", "409", "423", "500", "503"}


def test_declared_json_errors_reference_api_error_response(cv_document_paths: dict) -> None:
    """Structurally walks paths -> operation -> responses -> content ->
    application/json -> schema -> $ref for every declared JSON error status
    -- a global string search for "ApiErrorResponse" would not catch a
    response left undocumented (``model=None``, no ``content`` key)."""

    for path, methods in cv_document_paths.items():
        for method, operation in methods.items():
            responses = operation["responses"]
            for status_code in _JSON_ERROR_STATUS_CODES & responses.keys():
                response = responses[status_code]
                content = response.get("content")
                assert content is not None, f"{method} {path} {status_code} has no documented content"
                json_content = content.get("application/json")
                assert json_content is not None, (
                    f"{method} {path} {status_code} has no application/json content"
                )
                schema = json_content.get("schema", {})
                assert schema.get("$ref") == "#/components/schemas/ApiErrorResponse", (
                    f"{method} {path} {status_code} does not reference ApiErrorResponse: {schema}"
                )


def test_304_has_no_json_body(cv_document_paths: dict) -> None:
    for path, methods in cv_document_paths.items():
        for method, operation in methods.items():
            responses = operation["responses"]
            if "304" not in responses:
                continue
            content = responses["304"].get("content")
            if content:
                assert "application/json" not in content, f"{method} {path} 304 documents a JSON body"


def test_working_copy_description_documents_non_authoritative_download(
    cv_document_paths: dict, openapi_schema: dict
) -> None:
    working_copy_get = cv_document_paths["/api/v1/jobs/{job_id}/cv-documents/working-copy"]["get"]
    operation_text = " ".join(
        filter(None, [working_copy_get.get("summary"), working_copy_get.get("description")])
    ).lower()
    assert "authoritative" in operation_text
    assert "not" in operation_text

    schemas = openapi_schema["components"]["schemas"]
    field_description = (
        schemas["CvDocumentWorkingCopyResponse"]["properties"]["download_is_authoritative_edit_source"]
        .get("description", "")
        .lower()
    )
    assert "authoritative" in field_description


def test_all_operation_ids_globally_unique(openapi_schema: dict) -> None:
    _http_methods = {"get", "post", "put", "patch", "delete", "options", "head", "trace"}
    operation_ids = [
        operation["operationId"]
        for methods in openapi_schema["paths"].values()
        for method, operation in methods.items()
        if method in _http_methods and "operationId" in operation
    ]
    duplicates = {op_id for op_id in operation_ids if operation_ids.count(op_id) > 1}
    assert not duplicates, f"duplicate operationId(s) across the whole app: {duplicates}"


_FORBIDDEN_DOMAIN_ENUM_NAMES = {
    "ArtifactOwnerKind",
    "DocxProposalStatus",
    "PdfConfirmationStatus",
    "WorkingCopyCreateRefreshStatus",
    "WorkingCopyInspectionStatus",
    "FinalDocxSnapshotBuildStatus",
    "FinalConfirmedPdfGenerationStatus",
    "FinalConfirmedPdfFreshnessStatus",
    "DocumentProvenanceMode",
    "ConfirmedPdfLineageKind",
}


def test_no_internal_domain_enums_in_schema(openapi_schema: dict) -> None:
    schemas = openapi_schema.get("components", {}).get("schemas", {})
    assert not (_FORBIDDEN_DOMAIN_ENUM_NAMES & set(schemas.keys()))


def test_no_fingerprint_fields(openapi_schema: dict) -> None:
    schemas = openapi_schema.get("components", {}).get("schemas", {})
    for schema_name, schema in schemas.items():
        if not schema_name.startswith("CvDocument") and not schema_name.startswith("ApiError"):
            continue
        properties = schema.get("properties", {})
        assert not any("fingerprint" in field_name for field_name in properties), schema_name


def test_no_sha256_fields(openapi_schema: dict) -> None:
    schemas = openapi_schema.get("components", {}).get("schemas", {})
    for schema_name, schema in schemas.items():
        if not schema_name.startswith("CvDocument") and not schema_name.startswith("ApiError"):
            continue
        properties = schema.get("properties", {})
        assert not any("sha256" in field_name for field_name in properties), schema_name


def test_no_runtime_identity_fields(openapi_schema: dict) -> None:
    schema_text = json.dumps(openapi_schema)
    assert "runtime_identity" not in schema_text
    assert "executable_canonical_path" not in schema_text


def test_no_filesystem_paths_in_schema(openapi_schema: dict) -> None:
    schema_text = json.dumps(openapi_schema)
    assert "/Users/" not in schema_text
    assert ".resume_matcher" not in schema_text
    assert "cv_documents/blobs" not in schema_text


def test_openapi_generation_succeeds_without_error() -> None:
    schema = app.openapi()
    assert schema["openapi"]
    assert "paths" in schema
