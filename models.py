"""Pydantic models shared across ingestion, analysis, API, and UI layers."""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class DocumentType(str, Enum):
    PDF = "pdf"
    TXT = "txt"
    CSV = "csv"


class DocumentStatus(str, Enum):
    READY = "ready"
    UNSUPPORTED = "unsupported"
    INVALID = "invalid"


class DocumentRecord(BaseModel):
    document_id: str
    filename: str
    path: str
    document_type: DocumentType
    status: DocumentStatus = DocumentStatus.READY
    message: str | None = None
    page_count: int | None = None
    line_count: int | None = None
    row_count: int | None = None
    columns: list[str] = Field(default_factory=list)


class TextChunk(BaseModel):
    chunk_id: str
    document_id: str
    filename: str
    document_type: DocumentType
    text: str
    page_number: int | None = None
    line_start: int | None = None
    line_end: int | None = None
    section: str | None = None


class SourceReference(BaseModel):
    source_id: str
    filename: str
    document_type: DocumentType
    excerpt: str | None = None
    page_number: int | None = None
    line_start: int | None = None
    line_end: int | None = None
    section: str | None = None
    columns: list[str] = Field(default_factory=list)
    rows: list[int] = Field(default_factory=list)
    calculation_details: str | None = None


class EvidenceItem(BaseModel):
    evidence_id: str
    text: str
    source: SourceReference
    similarity: float | None = None


class Conflict(BaseModel):
    topic: str
    description: str
    source_ids: list[str]


class ExecutionStep(BaseModel):
    label: str
    detail: str


class AnswerResponse(BaseModel):
    answer: str
    sources: list[SourceReference] = Field(default_factory=list)
    conflicts: list[Conflict] = Field(default_factory=list)
    execution_flow: list[ExecutionStep] = Field(default_factory=list)
    session_id: str
    evidence_available: bool


class QuestionRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    session_id: str | None = None


class UploadResult(BaseModel):
    documents: list[DocumentRecord]


class Intent(str, Enum):
    RETRIEVAL = "retrieval"
    CSV_ANALYSIS = "csv_analysis"
    COMPARISON = "comparison"
    MULTI_DOCUMENT = "multi_document"
    FOLLOW_UP = "follow_up"
    OUT_OF_SCOPE = "out_of_scope"


class FilterCondition(BaseModel):
    column: str
    operator: Literal["eq", "ne", "gt", "gte", "lt", "lte", "contains", "in"]
    value: Any



class AggregationSpec(BaseModel):
    column: str
    operation: Literal["sum", "mean", "max", "min", "count", "nunique"]
    alias: str | None = None


class DerivedCalculationSpec(BaseModel):
    """A small, deterministic set of derived calculations allowed on result data."""

    operation: Literal["difference", "ratio", "percentage_change", "percentage_change_previous"]
    alias: str
    left_column: str | None = None
    right_column: str | None = None
    target_column: str | None = None


class CsvAnalysisPlan(BaseModel):
    document_ids: list[str] = Field(default_factory=list)
    filters: list[FilterCondition] = Field(default_factory=list)
    group_by: list[str] = Field(default_factory=list)
    aggregations: list[AggregationSpec] = Field(default_factory=list)
    sort_by: str | None = None
    sort_descending: bool = True
    limit: int | None = Field(default=None, ge=1, le=1000)
    comparison: str | None = None
    derived_calculations: list[DerivedCalculationSpec] = Field(default_factory=list)


class QueryPlan(BaseModel):
    intent: Intent
    rewritten_question: str
    retrieval_query: str | None = None
    document_ids: list[str] = Field(default_factory=list)
    use_text_retrieval: bool = True
    csv_analysis: CsvAnalysisPlan | None = None
    needs_comparison: bool = False
    follow_up_resolved_entities: dict[str, str] = Field(default_factory=dict)


class CsvAnalysisResult(BaseModel):
    document_id: str
    filename: str
    columns_used: list[str]
    row_count: int
    result_rows: list[dict[str, Any]]
    calculation_details: str
    source: SourceReference


class SessionTurn(BaseModel):
    turn_number: int
    question: str
    answer: str
    resolved_entities: dict[str, str] = Field(default_factory=dict)
    cited_source_ids: list[str] = Field(default_factory=list)
    summary: str = ""


class GroundedAnswerDraft(BaseModel):
    """The only answer shape accepted from Gemini before local validation."""

    answer: str = Field(min_length=1)
    cited_source_ids: list[str] = Field(default_factory=list)
    conflicts: list[Conflict] = Field(default_factory=list)
