"""Versioned product-facing models around the core Text2SQL response."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


UserRole = Literal["viewer", "analyst", "admin"]
BusinessRole = Literal[
    "head_office_manager",
    "branch_manager",
    "business_staff",
    "risk_compliance",
    "finance_staff",
    "system_admin",
]
OrganizationAccess = Literal["auto", "all", "restricted", "none"]
ChartType = Literal["metric", "line", "bar", "donut", "table"]
TimeGranularity = Literal["year", "quarter", "month", "day"]
AnswerMode = Literal["rule", "llm"]
AnswerStatus = Literal["pending", "streaming", "completed", "failed"]

ALL_METRIC_IDS = tuple(f"ZB{index:03d}" for index in range(1, 22))
BUSINESS_ROLE_LABELS: dict[BusinessRole, str] = {
    "head_office_manager": "总行管理层",
    "branch_manager": "分支行管理层",
    "business_staff": "业务条线人员",
    "risk_compliance": "风险合规人员",
    "finance_staff": "财务人员",
    "system_admin": "系统管理员",
}
BUSINESS_ROLE_METRICS: dict[BusinessRole, tuple[str, ...]] = {
    "head_office_manager": ALL_METRIC_IDS,
    "branch_manager": ALL_METRIC_IDS,
    "business_staff": (
        "ZB003",
        "ZB004",
        "ZB005",
        "ZB006",
        "ZB018",
        "ZB019",
        "ZB020",
        "ZB021",
    ),
    "risk_compliance": tuple(f"ZB{index:03d}" for index in range(13, 18)),
    "finance_staff": (
        "ZB001",
        "ZB002",
        "ZB007",
        "ZB008",
        "ZB009",
        "ZB010",
        "ZB011",
        "ZB012",
    ),
    "system_admin": (),
}


@dataclass(frozen=True, slots=True)
class UserContext:
    user_id: str
    role: UserRole = "analyst"
    allowed_organizations: tuple[str, ...] = ()
    business_role: BusinessRole | None = None
    organization_access: OrganizationAccess = "auto"

    @property
    def effective_business_role(self) -> BusinessRole | None:
        if self.business_role is not None:
            return self.business_role
        if self.role == "admin":
            return "system_admin"
        if self.role == "analyst":
            return "head_office_manager"
        return None

    @property
    def effective_organization_access(self) -> OrganizationAccess:
        if self.organization_access != "auto":
            return self.organization_access
        if self.allowed_organizations:
            return "restricted"
        if self.effective_business_role in {"branch_manager", "business_staff"}:
            return "restricted"
        if self.effective_business_role == "system_admin":
            return "none"
        return "all"

    @property
    def allowed_metric_ids(self) -> tuple[str, ...]:
        business_role = self.effective_business_role
        if business_role is None:
            return ALL_METRIC_IDS
        return BUSINESS_ROLE_METRICS[business_role]

    @property
    def can_query_data(self) -> bool:
        return self.role == "analyst" and bool(self.allowed_metric_ids)

    @property
    def can_view_sql(self) -> bool:
        return self.can_query_data

    @property
    def can_export(self) -> bool:
        return self.can_query_data

    @property
    def can_share(self) -> bool:
        return self.can_query_data

    @property
    def can_view_admin(self) -> bool:
        return self.role == "admin"

    @property
    def can_view_all_queries(self) -> bool:
        return self.can_view_admin

    @property
    def can_clear_sessions(self) -> bool:
        return self.can_view_admin

    @property
    def mask_sensitive_values(self) -> bool:
        return self.role == "viewer"

    @property
    def mask_all_values(self) -> bool:
        return self.effective_business_role == "system_admin"


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
