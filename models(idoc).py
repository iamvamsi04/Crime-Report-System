from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class Intent(StrEnum):
    FACTUAL_LOOKUP = "factual_lookup"
    NUMERICAL_LOOKUP = "numerical_lookup"
    CSV_AGGREGATION = "csv_aggregation"
    SUM = "sum"
    AVERAGE = "average"
    MINIMUM = "minimum"
    MAXIMUM = "maximum"
    RANKING = "ranking"
    SORTING = "sorting"
    FILTERING = "filtering"
    PERCENTAGE_CHANGE = "percentage_change"
    YEAR_OVER_YEAR_COMPARISON = "year_over_year_comparison"
    ENTITY_COMPARISON = "entity_comparison"
    DOCUMENT_COMPARISON = "document_comparison"
    MULTI_DOCUMENT_QUESTION = "multi_document_question"
    FOLLOW_UP_QUESTION = "follow_up_question"
    MISSING_INFORMATION = "missing_information"
    CONFLICTING_INFORMATION = "conflicting_information"
    OUT_OF_SCOPE = "out_of_scope"
    AMBIGUOUS = "ambiguous"


class ChatStatus(StrEnum):
    ANSWERED = "answered"
    MISSING_INFORMATION = "missing_information"
    CONFLICTING_INFORMATION = "conflicting_information"
    OUT_OF_SCOPE = "out_of_scope"
    AMBIGUOUS = "ambiguous"


ALLOWED_OPS = (
    "retrieve",
    "load_csv",
    "sum",
    "average",
    "min",
    "max",
    "count",
    "sort",
    "rank",
    "filter",
    "groupby",
    "percentage_change",
    "yoy",
    "compare",
)


class PlanOp(BaseModel):
    op: Literal[
        "retrieve",
        "load_csv",
        "sum",
        "average",
        "min",
        "max",
        "count",
        "sort",
        "rank",
        "filter",
        "groupby",
        "percentage_change",
        "yoy",
        "compare",
    ]

    target: str | None = None
    filename_hint: str | None = None
    column: str | None = None
    value: Any = None
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

    ascending: bool = False
    n: int | None = None
    agg: str | None = None

    model_config = {
        "populate_by_name": True,
        "extra": "ignore",
    }


class QueryPlan(BaseModel):
    intent: Intent = Intent.FACTUAL_LOOKUP

    # Normal / follow-up / history / repeat conversation handling
    is_follow_up: bool = False
    conversation_intent: str = "normal"
    resolved_question: str | None = None
    history_target: str = "none"

    out_of_scope: bool = False

    entities: list[str] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)
    years: list[int] = Field(default_factory=list)

    filters: dict[str, Any] = Field(default_factory=dict)

    document_ids: list[str] = Field(default_factory=list)
    document_hints: list[str] = Field(default_factory=list)

    operations: list[PlanOp] = Field(default_factory=list)

    ambiguous: bool = False
    ambiguity_reason: str | None = None

    retrieval_query: str | None = None

    model_config = {
        "extra": "ignore",
    }

    @field_validator("operations", mode="before")
    @classmethod
    def _coerce_ops(cls, value: Any) -> Any:
        if not isinstance(value, list):
            return []

        coerced = []

        for item in value:
            if isinstance(item, PlanOp):
                coerced.append(item)

            elif isinstance(item, str):
                op = item.strip().lower()

                if op in ALLOWED_OPS:
                    coerced.append({"op": op})

            elif isinstance(item, dict):
                if item.get("op") in ALLOWED_OPS:
                    coerced.append(item)

        return coerced


class Evidence(BaseModel):
    document_id: str
    filename: str
    document_type: str
    chunk_id: str
    text: str
    similarity: float

    page_number: int = 0
    section: str = ""
    source_reference: str = ""

    start_line: int = 0
    end_line: int = 0

    row_start: int = 0
    row_end: int = 0

    columns: str = ""
    entities: str = ""
    year: int = 0


class Source(BaseModel):
    filename: str
    document_type: str
    source_reference: str
    excerpt: str

    page_number: int | None = None
    section: str | None = None
    columns: str | None = None
    rows: str | None = None


class AnalysisResult(BaseModel):
    operation: str

    value: Any = None
    table: list[dict[str, Any]] | None = None

    inputs: dict[str, Any] = Field(
        default_factory=dict
    )

    formula: str | None = None

    source_file: str | None = None

    rows_used: int = 0

    columns_used: list[str] = Field(
        default_factory=list
    )


class NumericClaim(BaseModel):
    metric: str
    entity: str | None = None
    year: int | None = None
    value: float
    unit: str | None = None

    source_filename: str
    source_reference: str
    excerpt: str


class ConflictReport(BaseModel):
    genuine: bool = False

    explanation: str = ""

    claims: list[NumericClaim] = Field(
        default_factory=list
    )


class ConversationContext(BaseModel):
    last_intent: str | None = None
    last_question: str | None = None
    last_answer: str | None = None

    entities: list[str] = Field(
        default_factory=list
    )

    metrics: list[str] = Field(
        default_factory=list
    )

    years: list[int] = Field(
        default_factory=list
    )

    document_ids: list[str] = Field(
        default_factory=list
    )

    last_plan_summary: str | None = None

    last_numeric_results: list[dict[str, Any]] = Field(
        default_factory=list
    )


class DocumentOut(BaseModel):
    document_id: str
    filename: str
    file_type: str
    file_hash: str
    status: str
    chunk_count: int
    page_count: int | None = None
    csv_profile: dict[str, Any] | None = None

    created_at: str
    updated_at: str


class UploadResponse(DocumentOut):
    pass


class ChatRequest(BaseModel):
    question: str = Field(
        min_length=1,
        max_length=8000,
    )

    conversation_id: str | None = None


class ChatResponse(BaseModel):
    conversation_id: str

    answer: str

    status: ChatStatus

    sources: list[Source] = Field(
        default_factory=list
    )

    execution_flow: list[str] = Field(
        default_factory=list
    )

    query_plan: dict[str, Any] = Field(
        default_factory=dict
    )


class MessageOut(BaseModel):
    id: str
    role: str
    content: str

    status: str | None = None

    sources: list[Source] | None = None

    execution_flow: list[str] | None = None

    created_at: str


class ConversationOut(BaseModel):
    conversation_id: str

    context: ConversationContext

    messages: list[MessageOut]

    created_at: str
    updated_at: str


class HealthResponse(BaseModel):
    status: str
    sqlite: str
    chroma: str
    gemini_configured: bool
