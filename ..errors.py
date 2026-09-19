from __future__ import annotations


class AppError(Exception):
    """Base exception for application-level errors."""


class FileValidationError(AppError):
    """Raised when an uploaded file is invalid or unsupported."""


class DuplicateDocumentError(AppError):
    """Raised when a document with the same content already exists."""


class NotFoundError(AppError):
    """Raised when a requested resource does not exist."""


class AnalysisError(AppError):
    """Raised when structured document analysis fails."""


class GeminiError(AppError):
    """Raised when Gemini generation or embedding fails."""


class EmbeddingError(AppError):
    """Raised when document embedding fails."""


class StorageError(AppError):
    """Raised when persistent storage operations fail."""
