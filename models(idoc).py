from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


# ============================================================
# Enums
# ============================================================


class Intent(str, Enum):
    FACTUAL_LOOKUP = "FACTUAL_LOOKUP"
    NUMERICAL_LOOKUP = "NUMERICAL_LOOKUP"
    CSV_AGGREGATION = "CSV_AGGREGATION"
    SUM = "SUM"
    AVERAGE = "AVERAGE"
    MINIMUM = "MINIMUM"
    MAXIMUM = "MAXIMUM"
    RANKING = "RANKING"
    SORTING = "SORTING"
    FILTERING = "FILTERING"
    PERCENTAGE_CHANGE = "PERCENTAGE_CHANGE"
    YEAR_OVER_YEAR_COMPARISON = "YEAR_OVER_YEAR_COMPARISON"
    ENTITY_COMPARISON = "ENTITY_COMPARISON"
    DOCUMENT_COMPARISON = "DOCUMENT_COMPARISON"
    MULTI_DOCUMENT_QUESTION = "MULTI_DOCUMENT_QUESTION"
    FOLLOW_UP_QUESTION = "FOLLOW_UP_QUESTION"
    MISSING_INFORMATION = "MISSING_INFORMATION"
    CONFLICTING_INFORMATION = "CONFLICTING_INFORMATION"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"
    AMBIGUOUS = "AMBIGUOUS"


class ChatStatus(str, Enum):
    ANSWERED = "answered"
    MISSING_INFORMATION = "missing_information"
    CONFLICTING_INFORMATION = "conflicting_information"
    OUT_OF_SCOPE = "out_of_scope"
    AMBIGUOUS = "ambiguous"


# ============================================================
# Planning models
# ============================================================


class PlanOp(BaseModel):
    """
    One executable operation produced by the planner.
    """

    model_config = ConfigDict(populate_by_name=True)

    op: str
    target: str | None = None
    filename_hint: str | None = None

    column: str | None = None
    value: Any | None = None
    value_column: str | None = None

    year_column: str | None = None

    from_year: int | None = Field(
        default=None,
        alias="from",
    )

    to_year: int | None = Field(
        default=None,
        alias="to",
    )

    ascending: bool | None = None
    n: int | None = None
    agg: str | None = None


class QueryPlan(BaseModel):
    """
    Normalized execution plan created from the user's question.

    The plan is deliberately independent of any particular execution
    engine. CSV/Excel operations can later be executed by the analysis
    layer, while PDF/TXT retrieval is handled by the retrieval layer.
    """

    model_config = ConfigDict(populate_by_name=True)

    intent: Intent = Intent.FACTUAL_LOOKUP

    is_follow_up: bool = False
    conversation_intent: str | None = None

    resolved_question: str = ""
    history_target: str | None = None

    out_of_scope: bool = False

    entities: list[str] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)
    years: list[int] = Field(default_factory=list)

    filters: list[PlanOp] = Field(default_factory=list)

    document_ids: list[str] = Field(default_factory=list)
    document_hints: list[str] = Field(default_factory=list)

    operations: list[PlanOp] = Field(default_factory=list)

    ambiguous: bool = False
    ambiguity_reason: str | None = None

    retrieval_query: str = ""


# ============================================================
# Retrieval / evidence models
# ============================================================


class Evidence(BaseModel):
    """
    A grounded piece of evidence retrieved from a PDF or TXT document.
    """

    document_id: str
    filename: str
    document_type: str

    chunk_id: str | None = None
    text: str

    similarity: float | None = None

    page_number: int | None = None
    section: str | None = None
    source_reference: str | None = None

    start_line: int | None = None
    end_line: int | None = None

    row_start: int | None = None
    row_end: int | None = None

    columns: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)

    year: int | None = None


class Source(BaseModel):
    """
    User-facing citation information returned with an answer.
    """

    filename: str
    document_type: str

    source_reference: str | None = None
    excerpt: str | None = None

    page_number: int | None = None
    section: str | None = None

    columns: list[str] = Field(default_factory=list)
    rows: list[str] = Field(default_factory=list)


# ============================================================
# Structured analysis models
# ============================================================


class AnalysisResult(BaseModel):
    """
    Result of a structured CSV/Excel analysis operation.
    """

    operation: str

    value: Any | None = None
    table: list[dict[str, Any]] = Field(default_factory=list)

    inputs: dict[str, Any] = Field(default_factory=dict)
    formula: str | None = None

    source_file: str | None = None

    rows_used: int | None = None
    columns_used: list[str] = Field(default_factory=list)


# ============================================================
# Conversation models
# ============================================================


class ConversationContext(BaseModel):
    """
    Persistent context used to resolve follow-up questions.
    """

    last_question: str | None = None
    last_answer: str | None = None

    last_intent: str | None = None
    last_resolved_question: str | None = None

    entities: list[str] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)
    years: list[int] = Field(default_factory=list)

    filters: list[PlanOp] = Field(default_factory=list)

    document_ids: list[str] = Field(default_factory=list)
    document_hints: list[str] = Field(default_factory=list)

    operations: list[PlanOp] = Field(default_factory=list)

    last_analysis: list[AnalysisResult] = Field(
        default_factory=list,
    )


# ============================================================
# Document API models
# ============================================================


class DocumentOut(BaseModel):
    """
    Public representation of an uploaded document.
    """

    document_id: str = Field(alias="id")
    filename: str
    stored_name: str | None = None
    file_type: str
    file_hash: str | None = None

    status: str

    chunk_count: int = 0
    page_count: int = 0

    csv_profile: dict[str, Any] | None = None
    error_message: str | None = None

    created_at: str | datetime | None = None
    updated_at: str | datetime | None = None

    model_config = ConfigDict(
        populate_by_name=True,
    )


class UploadResponse(BaseModel):
    """
    Response returned after document ingestion.
    """

    document: DocumentOut


# ============================================================
# Chat API models
# ============================================================


class ChatRequest(BaseModel):
    """
    Request accepted by POST /chat.
    """

    question: str = Field(
        min_length=1,
        max_length=20_000,
    )

    conversation_id: str | None = None


class ChatResponse(BaseModel):
    """
    Complete response returned by the chat endpoint.
    """

    conversation_id: str
    answer: str
    status: ChatStatus

    sources: list[Source] = Field(default_factory=list)

    execution_flow: list[str] = Field(default_factory=list)

    query_plan: dict[str, Any] | None = None


# ============================================================
# Message / conversation API models
# ============================================================


class MessageOut(BaseModel):
    """
    Public representation of one stored conversation message.
    """

    message_id: str

    role: str
    content: str
    status: str | None = None

    sources: list[Source] = Field(default_factory=list)
    execution_flow: list[str] = Field(default_factory=list)

    query_plan: dict[str, Any] | None = None

    created_at: str | datetime | None = None


class ConversationOut(BaseModel):
    """
    Complete conversation returned by the conversation endpoint.
    """

    conversation_id: str

    context: ConversationContext

    messages: list[MessageOut] = Field(
        default_factory=list,
    )

    created_at: str | datetime | None = None
    updated_at: str | datetime | None = None


# ============================================================
# Health API model
# ============================================================


class HealthResponse(BaseModel):
    status: str
    service: str
    model: str
