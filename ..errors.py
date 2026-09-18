from __future__ import annotations

from fastapi import HTTPException


class AppError(Exception):
    """
    Base exception for expected application-level failures.

    `message` is safe to expose through the API.
    """

    status_code = 500
    default_message = "An application error occurred."

    def __init__(
        self,
        message: str | None = None,
        *,
        status_code: int | None = None,
    ) -> None:
        self.message = (
            message
            or self.default_message
        )

        if status_code is not None:
            self.status_code = (
                status_code
            )

        super().__init__(
            self.message
        )


class FileValidationError(AppError):
    status_code = 400
    default_message = (
        "The uploaded file is invalid."
    )


class DuplicateDocumentError(AppError):
    status_code = 409
    default_message = (
        "This document has already been uploaded."
    )


class NotFoundError(AppError):
    status_code = 404
    default_message = (
        "The requested resource was not found."
    )


class AnalysisError(AppError):
    """
    Base class for structured-analysis failures.

    chat.py generally handles this internally and converts it into grounded
    missing-information behavior rather than exposing database internals to
    the frontend.
    """

    status_code = 422
    default_message = (
        "The requested analysis could not be completed."
    )


class QueryGenerationError(
    AnalysisError
):
    default_message = (
        "A structured analysis query could not be generated."
    )


class QueryValidationError(
    AnalysisError
):
    default_message = (
        "The generated structured query was not allowed."
    )


class QueryExecutionError(
    AnalysisError
):
    default_message = (
        "The structured query could not be executed."
    )


class GeminiError(AppError):
    status_code = 502
    default_message = (
        "The language model service returned an error."
    )


class EmbeddingError(AppError):
    status_code = 502
    default_message = (
        "The embedding service returned an error."
    )


class StorageError(AppError):
    status_code = 500
    default_message = (
        "A storage operation failed."
    )


def http_error(
    exc: Exception,
) -> HTTPException:
    """
    Convert application exceptions into FastAPI HTTP errors.

    Unknown exceptions deliberately expose only a generic 500 message.
    Internal exception details should stay in server logs.
    """

    if isinstance(
        exc,
        HTTPException,
    ):
        return exc

    if isinstance(
        exc,
        AppError,
    ):
        return HTTPException(
            status_code=(
                exc.status_code
            ),
            detail=exc.message,
        )

    return HTTPException(
        status_code=500,
        detail=(
            "An unexpected server error occurred."
        ),
    )
