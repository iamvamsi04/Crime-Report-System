"""Safe orchestration from question to local evidence, calculations, and response."""

from __future__ import annotations

from app.analysis.answer_generator import AnswerGenerator
from app.analysis.conflict_detector import ConflictDetector
from app.analysis.csv_engine import CsvAnalysisEngine
from app.analysis.planner import QuestionPlanner
from app.conversation.context_manager import ContextManager
from app.core.exceptions import EvidenceUnavailableError, PlanningError
from app.core.models import (
    AnswerResponse,
    Conflict,
    CsvAnalysisResult,
    DocumentRecord,
    DocumentType,
    EvidenceItem,
    ExecutionStep,
    GroundedAnswerDraft,
    QueryPlan,
    SourceReference,
)
from app.ingestion.catalog import DocumentCatalog
from app.retrieval.retriever import SemanticRetriever


NOT_FOUND_ANSWER = "The requested information was not found in the available documents."


class QuestionService:
    """Coordinates the local-first workflow without exposing private model reasoning."""

    def __init__(
        self,
        catalog: DocumentCatalog,
        context_manager: ContextManager,
        planner: QuestionPlanner,
        retriever: SemanticRetriever,
        csv_engine: CsvAnalysisEngine,
        conflict_detector: ConflictDetector,
        answer_generator: AnswerGenerator,
    ) -> None:
        self.catalog = catalog
        self.context_manager = context_manager
        self.planner = planner
        self.retriever = retriever
        self.csv_engine = csv_engine
        self.conflict_detector = conflict_detector
        self.answer_generator = answer_generator

    def ask(self, question: str, requested_session_id: str | None = None) -> AnswerResponse:
        """Answer one question from retrieved local evidence and validated calculations."""
        session_id = self.context_manager.get_or_create_session(requested_session_id)
        context = self.context_manager.context_for_planning(session_id)
        documents = self.catalog.list()
        steps = [ExecutionStep(label="Question", detail="Question received.")]
        if not documents:
            return self._unavailable(
                session_id, question, {}, steps, "No documents are currently available for retrieval or analysis."
            )

        plan = self.planner.create_plan(question, documents, context)
        steps.append(ExecutionStep(label="Intent", detail=f"Identified as {plan.intent.value.replace('_', ' ')}."))
        if plan.intent.value == "out_of_scope":
            return self._unavailable(
                session_id, question, plan.follow_up_resolved_entities, steps,
                "No relevant available document was identified.",
            )

        selected_documents = self._selected_documents(plan, documents)
        text_evidence = self._retrieve_text_evidence(plan, selected_documents)
        if plan.use_text_retrieval:
            steps.append(
                ExecutionStep(
                    label="Retrieval",
                    detail=(f"Retrieved {len(text_evidence)} relevant PDF/TXT section(s)." if text_evidence
                            else "No sufficiently relevant PDF/TXT sections were found."),
                )
            )

        csv_results = self._run_csv_analysis(plan, selected_documents)
        if plan.csv_analysis:
            total_rows = sum(result.row_count for result in csv_results)
            steps.append(
                ExecutionStep(
                    label="Data analysis",
                    detail=f"Ran deterministic local CSV analysis on {len(csv_results)} dataset(s), using {total_rows} matching row(s).",
                )
            )

        has_csv_evidence = any(result.result_rows for result in csv_results)
        if not text_evidence and not has_csv_evidence:
            return self._unavailable(
                session_id,
                question,
                plan.follow_up_resolved_entities,
                steps,
                "The retrieval and calculation results did not contain supporting information.",
            )

        conflicts = self.conflict_detector.from_csv_results(csv_results)
        steps.append(
            ExecutionStep(
                label="Validation",
                detail=(f"Detected {len(conflicts)} conflicting result(s)." if conflicts else "Validated available evidence; no deterministic numeric conflict was detected."),
            )
        )
        draft = self.answer_generator.generate(question, plan, context, text_evidence, csv_results, conflicts)
        sources, merged_conflicts = self._validate_draft(draft, text_evidence, csv_results, conflicts)
        if not sources:
            raise EvidenceUnavailableError("The generated answer did not cite any available evidence.")
        steps.append(ExecutionStep(label="Answer", detail="Generated a grounded answer and attached sources."))
        self.context_manager.record_turn(
            session_id=session_id,
            question=question,
            answer=draft.answer,
            resolved_entities=plan.follow_up_resolved_entities,
            cited_source_ids=[source.source_id for source in sources],
        )
        return AnswerResponse(
            answer=draft.answer,
            sources=sources,
            conflicts=merged_conflicts,
            execution_flow=steps,
            session_id=session_id,
            evidence_available=True,
        )

    def _selected_documents(self, plan: QueryPlan, all_documents: list[DocumentRecord]) -> list[DocumentRecord]:
        allowed = {document.document_id: document for document in all_documents}
        plan_ids = set(plan.document_ids)
        if plan.csv_analysis:
            plan_ids.update(plan.csv_analysis.document_ids)
        if not plan_ids:
            # Semantic retrieval across the local collection remains safely bounded by relevance.
            return all_documents
        unknown = plan_ids - set(allowed)
        if unknown:
            raise PlanningError(f"The plan references unavailable documents: {', '.join(sorted(unknown))}.")
        return [document for document in all_documents if document.document_id in plan_ids]

    def _retrieve_text_evidence(self, plan: QueryPlan, selected_documents: list[DocumentRecord]) -> list[EvidenceItem]:
        if not plan.use_text_retrieval:
            return []
        text_document_ids = [
            document.document_id
            for document in selected_documents
            if document.document_type in {DocumentType.PDF, DocumentType.TXT}
        ]
        if not text_document_ids:
            return []
        return self.retriever.retrieve(plan.retrieval_query or plan.rewritten_question, text_document_ids)

    def _run_csv_analysis(self, plan: QueryPlan, selected_documents: list[DocumentRecord]) -> list[CsvAnalysisResult]:
        if not plan.csv_analysis:
            return []
        selected_ids = set(plan.csv_analysis.document_ids or [document.document_id for document in selected_documents])
        csv_documents = [
            document
            for document in selected_documents
            if document.document_id in selected_ids and document.document_type is DocumentType.CSV
        ]
        if not csv_documents:
            raise PlanningError("The CSV plan did not select an available CSV dataset.")
        return [self.csv_engine.analyze(document, plan.csv_analysis) for document in csv_documents]

    def _validate_draft(
        self,
        draft: GroundedAnswerDraft,
        evidence: list[EvidenceItem],
        csv_results: list[CsvAnalysisResult],
        deterministic_conflicts: list[Conflict],
    ) -> tuple[list[SourceReference], list[Conflict]]:
        sources_by_id = {item.evidence_id: item.source for item in evidence}
        sources_by_id.update({item.source.source_id: item.source for item in csv_results})
        cited = list(dict.fromkeys(draft.cited_source_ids))
        unknown_citations = set(cited) - set(sources_by_id)
        if unknown_citations:
            raise EvidenceUnavailableError("The generated answer cited evidence that was not retrieved locally.")

        validated_conflicts = list(deterministic_conflicts)
        for conflict in draft.conflicts:
            if len(conflict.source_ids) < 2 or not set(conflict.source_ids).issubset(sources_by_id):
                continue
            if conflict.description.strip():
                validated_conflicts.append(conflict)
                cited.extend(conflict.source_ids)
        unique_conflicts = list({(item.topic, item.description): item for item in validated_conflicts}.values())
        return [sources_by_id[source_id] for source_id in dict.fromkeys(cited)], unique_conflicts

    def _unavailable(
        self,
        session_id: str,
        question: str,
        resolved_entities: dict[str, str],
        steps: list[ExecutionStep],
        detail: str,
    ) -> AnswerResponse:
        steps.append(ExecutionStep(label="Validation", detail=detail))
        steps.append(ExecutionStep(label="Answer", detail="Returned an evidence-unavailable response; no unsupported answer was generated."))
        self.context_manager.record_turn(
            session_id=session_id,
            question=question,
            answer=NOT_FOUND_ANSWER,
            resolved_entities=resolved_entities,
            cited_source_ids=[],
        )
        return AnswerResponse(
            answer=NOT_FOUND_ANSWER,
            session_id=session_id,
            execution_flow=steps,
            evidence_available=False,
        )
