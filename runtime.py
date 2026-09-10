"""Dependency preflight checks shared by the API server and explicit runtime verification."""

from __future__ import annotations

import importlib
from dataclasses import dataclass

from app.core.exceptions import RuntimeConfigurationError


@dataclass(frozen=True, slots=True)
class DependencyCheck:
    """A single package import check, kept safe to expose through startup diagnostics."""

    distribution: str
    import_name: str
    required_for: str


BACKEND_DEPENDENCIES = (
    DependencyCheck("FastAPI", "fastapi", "the REST API"),
    DependencyCheck("Uvicorn", "uvicorn", "the REST API server"),
    DependencyCheck("PyMuPDF", "pymupdf", "PDF extraction"),
    DependencyCheck("pandas", "pandas", "local CSV analysis"),
    DependencyCheck("ChromaDB", "chromadb", "local semantic retrieval"),
    DependencyCheck("sentence-transformers", "sentence_transformers", "local all-MiniLM-L6-v2 embeddings"),
    DependencyCheck("google-genai", "google.genai", "Gemini planning and grounded answer wording"),
    DependencyCheck("python-dotenv", "dotenv", ".env configuration"),
    DependencyCheck("Pydantic", "pydantic", "plan and API validation"),
    DependencyCheck("python-multipart", "python_multipart", "file uploads"),
    DependencyCheck("httpx", "httpx", "the Streamlit API client"),
)

UI_DEPENDENCY = DependencyCheck("Streamlit", "streamlit", "the web UI")


def missing_dependencies(include_ui: bool = False) -> list[DependencyCheck]:
    """Import each required package so a broken transitive installation is also detected."""
    checks = BACKEND_DEPENDENCIES + ((UI_DEPENDENCY,) if include_ui else ())
    missing: list[DependencyCheck] = []
    for check in checks:
        try:
            importlib.import_module(check.import_name)
        except (ImportError, ModuleNotFoundError):
            missing.append(check)
    return missing


def validate_backend_dependencies() -> None:
    """Fail early with the exact install action instead of waiting for a request to fail."""
    missing = missing_dependencies()
    if not missing:
        return
    package_names = ", ".join(check.distribution for check in missing)
    capabilities = "; ".join(f"{check.distribution} ({check.required_for})" for check in missing)
    raise RuntimeConfigurationError(
        f"Required runtime dependency package(s) are unavailable: {package_names}. "
        f"Missing capabilities: {capabilities}. "
        "Create the documented Python 3.11 virtual environment and run "
        "'python -m pip install -r requirements.txt'."
    )


def validate_ui_dependency() -> None:
    """Give the same clear preflight error when launching the optional Streamlit client."""
    missing = missing_dependencies(include_ui=True)
    if UI_DEPENDENCY not in missing:
        return
    raise RuntimeConfigurationError(
        "Streamlit is not installed. Activate the documented Python 3.11 virtual environment and run "
        "'python -m pip install -r requirements.txt'."
    )
