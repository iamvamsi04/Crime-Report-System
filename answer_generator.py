"""Grounded answer wording over a fixed evidence bundle only."""

from __future__ import annotations

import json
from typing import Protocol

from pydantic import ValidationError

from app.core.exceptions import EvidenceUnavailableError
from app.core.models import Conflict, CsvAnalysisResult, EvidenceItem, GroundedAnswerDraft, QueryPlan
from app.llm.gemini_client import GeminiClient


class AnswerGenerator(Protocol):
    def generate(
        self,
        question: str,
        plan: QueryPlan,
        context: str,
        evidence: list[EvidenceItem],
        csv_results: list[CsvAnalysisResult],
        conflicts: list[Conflict],
    ) -> GroundedAnswerDraft: ...


class GeminiAnswerGenerator:
    """Ask Gemini to word an answer without supplying any unselected document content."""

    def __init__(self, client: GeminiClient) -> None:
        self.client = client

    def generate(
        self,
        question: str,
        plan: QueryPlan,
        context: str,
        evidence: list[EvidenceItem],
        csv_results: list[CsvAnalysisResult],
        conflicts: list[Conflict],
    ) -> GroundedAnswerDraft:
        evidence_payload = [
            {
                "source_id": item.evidence_id,
                "text": item.text,
                "source": item.source.model_dump(mode="json"),
            }
            for item in evidence
        ]
        csv_payload = [
            {
                "source_id": item.source.source_id,
                "filename": item.filename,
                "result_rows": item.result_rows[:100],
                "calculation_details": item.calculation_details,
            }
            for item in csv_results
        ]
        prompt = f"""Answer a user only from the evidence bundle below. Do not use outside knowledge.
If the bundle cannot support a requested fact, say: "The requested information was not found in the available documents."
Do not state a number unless it appears in an evidence text or CSV result. Do not silently resolve conflicts.
Return only JSON matching:
{{"answer":"concise answer", "cited_source_ids":["source IDs used"],
  "conflicts":[{{"topic":"...", "description":"...", "source_ids":["at least two evidence source IDs"]}}]}}

Question: {question}
Resolved question: {plan.rewritten_question}
Plan intent: {plan.intent.value}
Prior session context: {context}
Deterministically detected conflicts: {json.dumps([item.model_dump() for item in conflicts])}
Text evidence: {json.dumps(evidence_payload)}
CSV calculation evidence: {json.dumps(csv_payload)}
"""
        try:
            return GroundedAnswerDraft.model_validate(self.client.generate_json(prompt))
        except (ValidationError, ValueError, TypeError) as exc:
            raise EvidenceUnavailableError("A grounded answer could not be generated safely.") from exc
