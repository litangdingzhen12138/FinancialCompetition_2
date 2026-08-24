"""Versioned product-facing models around the core Text2SQL response."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


UserRole = Literal["viewer", "analyst", "admin"]
ChartType = Literal["metric", "line", "bar", "donut", "table"]
TimeGranularity = Literal["year", "quarter", "month", "day"]
AnswerMode = Literal["rule", "llm"]
AnswerStatus = Literal["pending", "streaming", "completed", "failed"]


@dataclass(frozen=True, slots=True)
class UserContext:
    user_id: str
    role: UserRole = "analyst"
    allowed_organizations: tuple[str, ...] = ()

    @property
    def can_view_sql(self) -> bool:
        return self.role in {"analyst", "admin"}

    @property
    def can_export(self) -> bool:
        return self.role in {"analyst", "admin"}

    @property
    def can_share(self) -> bool:
        return self.role in {"analyst", "admin"}

    @property
    def can_view_admin(self) -> bool:
        return self.role == "admin"

    @property
    def mask_sensitive_values(self) -> bool:
        return self.role == "viewer"


@dataclass(frozen=True, slots=True)
class DataColumn:
    key: str
    label: str
    data_type: Literal["string", "number", "date", "boolean"]
    unit: str = ""
    sensitive: bool = False


@dataclass(frozen=True, slots=True)
class ChartSpec:
    chart_type: ChartType
    title: str
    x_field: str | None = None
    y_fields: tuple[str, ...] = ()
    series_field: str | None = None
    unit: str = ""
    sort: Literal["asc", "desc"] | None = None
    reason: str = ""
    available_time_drill: tuple[TimeGranularity, ...] = ()


@dataclass(frozen=True, slots=True)
class Insight:
    headline: str
    summary: str
    evidence: tuple[str, ...] = ()
    caveats: tuple[str, ...] = ()
    confidence: Literal["high", "medium", "low"] = "high"
    inference: bool = False


@dataclass(frozen=True, slots=True)
class ProductQueryResponse:
    api_version: str
    query_id: str
    session_id: str
    status: Literal["completed", "failed"]
    question: str
    route: str
    answer_mode: AnswerMode
    answer_status: AnswerStatus
    generated_at: str
    duration_ms: int
    plan: dict[str, Any]
    sql: str | None
    columns: tuple[DataColumn, ...]
    records: tuple[dict[str, Any], ...]
    row_count: int
    truncated: bool
    visualization: ChartSpec
    alternatives: tuple[ChartSpec, ...]
    insight: Insight | None
    current_time_level: TimeGranularity | None = None
    available_time_drill: tuple[TimeGranularity, ...] = ()
    drill_path: tuple[TimeGranularity, ...] = ()
    aggregation: str | None = None
    permissions: dict[str, bool] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    request_id: str | None = None
    caller_system: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
