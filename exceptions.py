"""Expected domain errors that are safe to show to users."""

from __future__ import annotations


class DocumentAnalysisError(Exception):
    """Base class for predictable application failures."""


class FileValidationError(DocumentAnalysisError):
    """Raised for empty, unsupported, corrupt, or invalid input files."""


class DocumentNotFoundError(DocumentAnalysisError):
    """Raised when a document identifier cannot be found locally."""


class PlanningError(DocumentAnalysisError):
    """Raised when a generated query plan is invalid or unsafe."""


class EvidenceUnavailableError(DocumentAnalysisError):
    """Raised when an answer cannot be supported by available evidence."""


class LLMConfigurationError(DocumentAnalysisError):
    """Raised when a Gemini request cannot be made safely."""


class RuntimeConfigurationError(DocumentAnalysisError):
    """Raised before serving requests when required local runtime components are unavailable."""
