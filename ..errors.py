from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException


@dataclass
class AppError(Exception):
    """
    Base application error.

    Domain/application code raises AppError subclasses instead of
    constructing HTTP responses directly. The API layer converts these
    errors into appropriate HTTP responses.
    """

    message: str
    code: str = "application_error"
    status_code: int = 500
    details: Any | None = None

    def __post_init__(self) -> None:
        super().__init__(self.message)


class ValidationError(AppError):
    """
    Raised when application-level input validation fails.
    """

    def __init__(
        self,
        message: str,
        details: Any | None = None,
    ) -> None:
        super().__init__(
            message=message,
            code="validation_error",
            status_code=400,
            details=details,
        )


class NotFoundError(AppError):
    """
    Raised when a requested document, conversation, or other
    application resource does not exist.
    """

    def __init__(
        self,
        message: str,
        details: Any | None = None,
    ) -> None:
        super().__init__(
            message=message,
            code="not_found",
            status_code=404,
            details=details,
        )


class ConflictError(AppError):
    """
    Raised when an operation conflicts with the current application state.
    """

    def __init__(
        self,
        message: str,
        details: Any | None = None,
    ) -> None:
        super().__init__(
            message=message,
            code="conflict",
            status_code=409,
            details=details,
        )


class IngestionError(AppError):
    """
    Raised when a document cannot be extracted, processed, embedded,
    or stored successfully.
    """

    def __init__(
        self,
        message: str,
        details: Any | None = None,
    ) -> None:
        super().__init__(
            message=message,
            code="ingestion_error",
            status_code=422,
            details=details,
        )


class RetrievalError(AppError):
    """
    Raised when document retrieval cannot be completed.
    """

    def __init__(
        self,
        message: str,
        details: Any | None = None,
    ) -> None:
        super().__init__(
            message=message,
            code="retrieval_error",
            status_code=500,
            details=details,
        )


class AnalysisError(AppError):
    """
    Raised when structured CSV/Excel analysis cannot be completed.
    """

    def __init__(
        self,
        message: str,
        details: Any | None = None,
    ) -> None:
        super().__init__(
            message=message,
            code="analysis_error",
            status_code=422,
            details=details,
        )


class GeminiError(AppError):
    """
    Raised when communication with Gemini fails or Gemini returns
    an unusable response.
    """

    def __init__(
        self,
        message: str,
        details: Any | None = None,
    ) -> None:
        super().__init__(
            message=message,
            code="gemini_error",
            status_code=502,
            details=details,
        )


class EmbeddingError(GeminiError):
    """
    Specialized Gemini error for embedding failures.
    """

    def __init__(
        self,
        message: str,
        details: Any | None = None,
    ) -> None:
        AppError.__init__(
            self,
            message=message,
            code="embedding_error",
            status_code=502,
            details=details,
        )


class StorageError(AppError):
    """
    Raised when persistent storage operations fail.
    """

    def __init__(
        self,
        message: str,
        details: Any | None = None,
    ) -> None:
        super().__init__(
            message=message,
            code="storage_error",
            status_code=500,
            details=details,
        )


class UnsupportedFileError(AppError):
    """
    Raised when the uploaded document type is not supported.
    """

    def __init__(
        self,
        message: str,
        details: Any | None = None,
    ) -> None:
        super().__init__(
            message=message,
            code="unsupported_file_type",
            status_code=400,
            details=details,
        )


def http_error(error: AppError) -> HTTPException:
    """
    Convert an application error into FastAPI's HTTPException.

    main.py uses this function inside its global AppError handler.
    """

    detail: dict[str, Any] = {
        "error": error.code,
        "message": error.message,
    }

    if error.details is not None:
        detail["details"] = error.details

    return HTTPException(
        status_code=error.status_code,
        detail=detail,
    )
