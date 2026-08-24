"""Shared immutable request, plan, result and session models."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


PlanSource = Literal["rule", "llm"]
AnswerMode = Literal["rule", "llm"]
ResultShape = Literal["single_value", "single_row", "multi_row", "time_series"]


@dataclass(frozen=True, slots=True)
class PlanFilter:
    field: str
    operator: str
    value: str | float | int | None = None
    reference: str | None = None


@dataclass(frozen=True, slots=True)
class DataAccessScope:
    """Database-enforced metric and organization boundaries for one query."""

    metric_ids: tuple[str, ...]
    organization_ids: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class QueryPlan:
    source: PlanSource
    query_type: str
    operation: str
    organizations: tuple[str, ...]
    organization_scope: Literal["selected", "all"]
    metrics: tuple[str, ...]
    current_date: str | None
    comparison_date: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    dimensions: tuple[str, ...] = ()
    filters: tuple[PlanFilter, ...] = ()
    sort_direction: Literal["asc", "desc"] | None = None
    limit: int | None = None
    expected_shape: ResultShape = "multi_row"
    allow_empty: bool = False
    derived_formula: str | None = None
    rule_id: str | None = None
    confidence: float = 0.0
    assumptions: tuple[str, ...] = ()
    sql: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class QueryResult:
    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]
    truncated: bool = False

    @property
    def row_count(self) -> int:
        return len(self.rows)

    def dictionaries(self) -> list[dict[str, Any]]:
        return [dict(zip(self.columns, row, strict=True)) for row in self.rows]


@dataclass(slots=True)
class SessionState:
    last_organizations: tuple[str, ...] = ()
    last_metrics: tuple[str, ...] = ()
    last_date: str | None = None
    last_comparison_date: str | None = None
    last_operation: str | None = None
    last_query_type: str | None = None
    last_result_organizations: tuple[str, ...] = ()
    recent_turns: tuple["TurnMemory", ...] = ()
    pending_query: "PendingQuery | None" = None


@dataclass(frozen=True, slots=True)
class PendingQuery:
    """An incomplete query waiting for explicit slot values from the user."""

    original_question: str
    working_question: str
    organizations: tuple[str, ...] = ()
    metrics: tuple[str, ...] = ()
    current_date: str | None = None
    comparison_date: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    organization_scope: Literal["selected", "all"] = "selected"
    missing_slots: tuple[str, ...] = ()
    supplements: tuple[str, ...] = ()
    clarification_count: int = 0


@dataclass(frozen=True, slots=True)
class TurnMemory:
    question: str
    plan: QueryPlan
    result: QueryResult
    answer: str


@dataclass(frozen=True, slots=True)
class QueryResponse:
    answer: str
    answer_mode: AnswerMode
    session_id: str
    route: PlanSource
    sql: str
    plan: dict[str, Any]
    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
