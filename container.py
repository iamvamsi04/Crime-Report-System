"""Composition root for production services; imports do not initialize a model or API call."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from app.analysis.answer_generator import GeminiAnswerGenerator
from app.analysis.conflict_detector import ConflictDetector
from app.analysis.csv_engine import CsvAnalysisEngine
from app.analysis.planner import GeminiQuestionPlanner
from app.conversation.context_manager import ContextManager
from app.conversation.session_store import SessionStore
from app.core.config import Settings, settings
from app.core.runtime import validate_backend_dependencies
from app.ingestion.catalog import DocumentCatalog
from app.ingestion.document_service import DocumentService
from app.llm.gemini_client import GeminiClient
from app.retrieval.retriever import SemanticRetriever
from app.retrieval.vector_store import LocalVectorStore
from app.services.question_service import QuestionService


@dataclass(slots=True)
class ApplicationServices:
    document_service: DocumentService
    catalog: DocumentCatalog
    context_manager: ContextManager
    question_service: QuestionService


def build_services(app_settings: Settings) -> ApplicationServices:
    """Wire the production local stores, Gemini planner, and answer generator."""
    validate_backend_dependencies()
    app_settings.ensure_directories()
    catalog = DocumentCatalog(app_settings.data_dir / "catalog.db")
    vector_store = LocalVectorStore(app_settings)
    document_service = DocumentService(catalog, vector_store)
    context_manager = ContextManager(SessionStore(app_settings.session_db_path))
    gemini = GeminiClient(app_settings)
    question_service = QuestionService(
        catalog=catalog,
        context_manager=context_manager,
        planner=GeminiQuestionPlanner(gemini),
        retriever=SemanticRetriever(vector_store, app_settings),
        csv_engine=CsvAnalysisEngine(),
        conflict_detector=ConflictDetector(),
        answer_generator=GeminiAnswerGenerator(gemini),
    )
    return ApplicationServices(
        document_service=document_service,
        catalog=catalog,
        context_manager=context_manager,
        question_service=question_service,
    )


@lru_cache(maxsize=1)
def get_services() -> ApplicationServices:
    """Create persistent services once per API process, on first non-health request."""
    return build_services(settings)
