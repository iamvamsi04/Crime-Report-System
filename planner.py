"""Constrained Gemini planning with local validation of every document identifier."""

from __future__ import annotations

import json
from typing import Protocol

from pydantic import ValidationError

from app.core.exceptions import PlanningError
from app.core.models import DocumentRecord, QueryPlan
from app.llm.gemini_client import GeminiClient


class QuestionPlanner(Protocol):
    def create_plan(self, question: str, documents: list[DocumentRecord], context: str) -> QueryPlan: ...


class GeminiQuestionPlanner:
    """Ask Gemini for a safe declarative plan, never executable code."""

    def __init__(self, client: GeminiClient) -> None:
        self.client = client

    def create_plan(self, question: str, documents: list[DocumentRecord], context: str) -> QueryPlan:
        document_inventory = [
            {
                "document_id": document.document_id,
                "filename": document.filename,
                "type": document.document_type.value,
                "columns": document.columns,
                "row_count": document.row_count,
                "pages": document.page_count,
            }
            for document in documents
        ]
        prompt = f"""You are a question-planning component for a grounded local document system.
Return only one JSON object. Do not answer the user's question.

Available document inventory (metadata only):
{json.dumps(document_inventory)}

Conversation context (local summaries, not hidden reasoning):
{context}

User question: {question}

Create this JSON object exactly:
{{
  "intent": "retrieval|csv_analysis|comparison|multi_document|follow_up|out_of_scope",
  "rewritten_question": "an explicit standalone question after resolving references",
  "retrieval_query": "semantic text query or null",
  "document_ids": ["relevant IDs from the inventory only"],
  "use_text_retrieval": true,
  "csv_analysis": null or {{
    "document_ids": ["CSV IDs only"],
    "filters": [{{"column":"exact column name","operator":"eq|ne|gt|gte|lt|lte|contains|in","value":"value"}}],
    "group_by": ["exact column names"],
    "aggregations": [{{"column":"exact column name","operation":"sum|mean|max|min|count|nunique","alias":"result_name"}}],
    "sort_by":"result column or null", "sort_descending":true, "limit":null,
    "comparison": "short comparison request or null", "derived_calculations": []
  }},
  "needs_comparison": false,
  "follow_up_resolved_entities": {{"reference":"resolved value"}}
}}

Rules: use only exact document IDs and column names supplied above; never write SQL, Python, or a formula.
Use CSV analysis for numeric/tabular questions. A follow-up must resolve pronouns, periods, ranks, and entities from the supplied context.
Set intent to out_of_scope if no available document could answer the question."""
        try:
            plan = QueryPlan.model_validate(self.client.generate_json(prompt))
        except (ValidationError, ValueError, TypeError) as exc:
            raise PlanningError("Gemini returned an invalid query plan; no analysis was performed.") from exc
        self._validate_document_ids(plan, documents)
        return plan

    @staticmethod
    def _validate_document_ids(plan: QueryPlan, documents: list[DocumentRecord]) -> None:
        allowed = {document.document_id for document in documents}
        requested = set(plan.document_ids)
        if plan.csv_analysis:
            requested.update(plan.csv_analysis.document_ids)
        invalid = requested - allowed
        if invalid:
            raise PlanningError(f"The plan references unavailable documents: {', '.join(sorted(invalid))}.")
