from __future__ import annotations

from fastapi import HTTPException


class AppError(Exception):
    code = "application_error"
    status_code = 500

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code


class FileValidationError(AppError):
    code = "file_validation_error"
    status_code = 400


class DuplicateDocumentError(AppError):
    code = "duplicate_document"
    status_code = 409

    def __init__(self, message: str, document_id: str) -> None:
        super().__init__(message)
        self.document_id = document_id


class NotFoundError(AppError):
    code = "not_found"
    status_code = 404


class AnalysisError(AppError):
    code = "analysis_error"
    status_code = 400


class GeminiError(AppError):
    code = "gemini_unavailable"
    status_code = 503


class EmbeddingError(AppError):
    code = "embedding_unavailable"
    status_code = 503


class StorageError(AppError):
    code = "storage_unavailable"
    status_code = 503


def http_error(exc: AppError) -> HTTPException:
    detail: dict[str, str] = {"error": exc.code, "message": exc.message}
    if isinstance(exc, DuplicateDocumentError):
        detail["document_id"] = exc.document_id
    return HTTPException(status_code=exc.status_code, detail=detail)

