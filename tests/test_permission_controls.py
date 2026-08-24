from __future__ import annotations

from typing import Any

import pytest

from text2sql import api
from text2sql.errors import SQLSafetyError
from text2sql.executor import DuckDBExecutor
from text2sql.models import DataAccessScope, QueryPlan, QueryResult
from text2sql.product_auth import ProductAuthService
from text2sql.product_models import (
    ALL_METRIC_IDS,
    BUSINESS_ROLE_METRICS,
    BusinessRole,
    UserContext,
)
from text2sql.product_service import ProductQueryService
from text2sql.product_store import ProductStore


ROLE_METRIC_CASES: tuple[tuple[BusinessRole, set[str]], ...] = (
    ("head_office_manager", set(ALL_METRIC_IDS)),
    ("branch_manager", set(ALL_METRIC_IDS)),
    (
        "business_staff",
        {"ZB003", "ZB004", "ZB005", "ZB006", "ZB018", "ZB019", "ZB020", "ZB021"},
    ),
    ("risk_compliance", {"ZB013", "ZB014", "ZB015", "ZB016", "ZB017"}),
    (
        "finance_staff",
        {"ZB001", "ZB002", "ZB007", "ZB008", "ZB009", "ZB010", "ZB011", "ZB012"},
    ),
    ("system_admin", set()),
)


@pytest.fixture
def product(service, tmp_path) -> ProductQueryService:
    return ProductQueryService(service, ProductStore(tmp_path / "permissions.sqlite3"))


def _user(
    business_role: BusinessRole,
    *,
    user_id: str | None = None,
    organizations: tuple[str, ...] = (),
) -> UserContext:
    return UserContext(
        user_id=user_id or business_role,
        role="admin" if business_role == "system_admin" else "analyst",
        allowed_organizations=organizations,
        business_role=business_role,
    )


def _plan(
    metric: str,
    *,
    organizations: tuple[str, ...] = ("ORG001",),
    organization_scope: str = "selected",
    operation: str = "value",
) -> dict[str, Any]:
    return {
        "source": "rule",
        "query_type": "metric_value",
        "operation": operation,
        "organizations": list(organizations),
        "organization_scope": organization_scope,
        "metrics": [metric],
        "current_date": "2026-03-31",
    }


@pytest.mark.parametrize(("business_role", "expected_metrics"), ROLE_METRIC_CASES)
def test_six_business_roles_enforce_exact_metric_whitelists(
    product: ProductQueryService,
    business_role: BusinessRole,
    expected_metrics: set[str],
) -> None:
    organizations = (
        ("ORG001",) if business_role in {"branch_manager", "business_staff"} else ()
    )
    user = _user(business_role, organizations=organizations)

    assert set(BUSINESS_ROLE_METRICS[business_role]) == expected_metrics
    assert set(user.allowed_metric_ids) == expected_metrics

    if not expected_metrics:
        with pytest.raises(PermissionError, match="无数据查询权限"):
            product._authorize_plan(_plan("ZB001"), user)
        return

    allowed_metric = sorted(expected_metrics)[0]
    access_scope = product._authorize_plan(_plan(allowed_metric), user)
    assert set(access_scope.metric_ids) == {allowed_metric}

    denied_metrics = set(ALL_METRIC_IDS) - expected_metrics
    if denied_metrics:
        with pytest.raises(PermissionError, match="无权访问指标"):
            product._authorize_plan(_plan(sorted(denied_metrics)[0]), user)


@pytest.mark.parametrize("business_role", ["branch_manager", "business_staff"])
def test_restricted_roles_only_accept_configured_organizations(
    product: ProductQueryService,
    business_role: BusinessRole,
) -> None:
    metric = "ZB001" if business_role == "branch_manager" else "ZB003"
    user = _user(business_role, organizations=("ORG001", "ORG002"))

    scope = product._authorize_plan(_plan(metric, organizations=("ORG001",)), user)
    assert scope.organization_ids == ("ORG001",)

    with pytest.raises(PermissionError, match="无权访问的机构"):
        product._authorize_plan(_plan(metric, organizations=("ORG003",)), user)

    with pytest.raises(PermissionError, match="超出机构数据权限"):
        product._authorize_plan(
            _plan(metric, organizations=(), organization_scope="all"),
            user,
        )

    unconfigured = _user(business_role)
    with pytest.raises(PermissionError, match="尚未配置可访问机构"):
        product._authorize_plan(_plan(metric), unconfigured)


def _history_record() -> dict[str, Any]:
    return {
        "query_id": "sensitive-query",
        "user_id": "business-owner",
        "session_id": "sensitive-session",
        "question": "查询机构指标明细",
        "route": "rule",
        "status": "completed",
        "sql": "SELECT metric_value FROM metric_values",
        "plan": {
            **_plan("ZB001"),
            "sql": "SELECT metric_value FROM metric_values",
        },
        "columns": [
            "org_name",
            "metric_value",
            "metric_rank",
            "customer_count",
            "is_valid",
        ],
        "rows": [["江苏省A市农商行", 123.45, 2, 900, True]],
        "answer": "指标值为123.45，排名第2。",
        "answer_mode": "rule",
        "answer_status": "completed",
        "visualization": {
            "chart_type": "table",
            "title": "指标明细",
            "x_field": None,
            "y_fields": [],
            "series_field": None,
            "unit": "",
            "sort": None,
            "reason": "",
            "available_time_drill": [],
        },
        "insight": None,
        "warnings": [],
        "truncated": False,
        "duration_ms": 1,
        "created_at": "2026-08-24T00:00:00+00:00",
    }


def test_system_admin_cannot_query_export_or_share_and_history_values_are_masked(
    product: ProductQueryService,
) -> None:
    admin = _user("system_admin")
    product.store.save_query(_history_record())

    history = product.history(admin)
    detail = product.get_query("sensitive-query", admin)

    with pytest.raises(PermissionError, match="无数据查询权限"):
        product.query("查询江苏省A市农商行的各项存款", "admin-session", admin)
    with pytest.raises(PermissionError, match="无导出权限"):
        product.export_record("sensitive-query", admin)
    with pytest.raises(PermissionError, match="无分享权限"):
        product.create_share("sensitive-query", admin, expires_hours=1)

    assert history[0]["answer"] == "查询结果数值已按权限隐藏"
    assert detail.sql is None
    assert "sql" not in detail.plan
    assert detail.records == (
        {
            "org_name": "***",
            "metric_value": "***",
            "metric_rank": "***",
            "customer_count": "***",
            "is_valid": "***",
        },
    )
    assert detail.insight is not None
    assert "隐藏" in detail.insight.summary


def test_plan_authorizer_denial_happens_before_preflight_and_execute(
    service,
    monkeypatch,
) -> None:
    class SpyExecutor:
        def __init__(self) -> None:
            self.preflight_calls = 0
            self.execute_calls = 0

        def preflight(
            self,
            sql: str,
            scope: DataAccessScope | None = None,
        ) -> None:
            self.preflight_calls += 1

        def execute(
            self,
            sql: str,
            scope: DataAccessScope | None = None,
        ) -> QueryResult:
            self.execute_calls += 1
            return QueryResult(("metric_value",), ((1,),))

    spy = SpyExecutor()
    monkeypatch.setattr(service, "executor", spy)
    plan = QueryPlan(
        source="rule",
        query_type="metric_value",
        operation="value",
        organizations=("ORG001",),
        organization_scope="selected",
        metrics=("ZB001",),
        current_date="2026-03-31",
        confidence=1.0,
    )
    authorization_calls: list[QueryPlan] = []

    def deny(candidate: QueryPlan) -> DataAccessScope:
        authorization_calls.append(candidate)
        raise PermissionError("测试越权阻断")

    with pytest.raises(PermissionError, match="测试越权阻断"):
        service._validated_execute(
            plan,
            "SELECT metric_value FROM metric_values",
            plan_authorizer=deny,
        )

    assert authorization_calls == [plan]
    assert spy.preflight_calls == 0
    assert spy.execute_calls == 0


def test_duckdb_data_access_scope_enforces_metric_and_organization(service) -> None:
    executor = DuckDBExecutor(service.db_path, hard_limit=10)
    scope = DataAccessScope(metric_ids=("ZB013",), organization_ids=("ORG001",))

    result = executor.execute(
        """
        SELECT COUNT(DISTINCT org_id) AS org_count,
               MIN(org_id) AS min_org,
               MAX(org_id) AS max_org,
               COUNT(DISTINCT metric_id) AS metric_count,
               MIN(metric_id) AS min_metric,
               MAX(metric_id) AS max_metric
        FROM metric_values
        """,
        scope,
    )

    assert result.rows == ((1, "ORG001", "ORG001", 1, "ZB013", "ZB013"),)


def test_plan_scope_blocks_unlisted_metric_and_organization_in_aggregate(
    service,
    tmp_path,
) -> None:
    product = ProductQueryService(
        service,
        ProductStore(tmp_path / "aggregate-scope.sqlite3"),
    )
    user = _user(
        "branch_manager",
        organizations=("ORG001", "ORG002"),
    )
    plan = QueryPlan(
        source="llm",
        query_type="metric_value",
        operation="value",
        organizations=("ORG001",),
        organization_scope="selected",
        metrics=("ZB001",),
        current_date="2026-03-31",
    )
    raw_sql = """
        SELECT SUM(metric_value) AS metric_value
        FROM metric_values
        WHERE data_date = DATE '2026-03-31'
          AND metric_id IN ('ZB001', 'ZB013')
          AND 'ORG001' = 'ORG001'
    """

    _, result = service._validated_execute(
        plan,
        raw_sql,
        plan_authorizer=lambda candidate: product._authorize_plan(
            candidate.to_dict(),
            user,
        ),
    )
    expected = service.executor.execute(
        """
        SELECT SUM(metric_value) AS metric_value
        FROM metric_values
        WHERE data_date = DATE '2026-03-31'
          AND metric_id = 'ZB001'
          AND org_id = 'ORG001'
        """
    )

    assert result.rows == expected.rows


def test_sql_guard_rejects_qualified_metric_values_name(service) -> None:
    with pytest.raises(SQLSafetyError, match="未限定表名"):
        service.sql_guard.parse_one("SELECT * FROM main.metric_values")


@pytest.mark.parametrize("operation", ["profile", "multi_condition"])
def test_restricted_user_cannot_run_cross_organization_semantics(
    product: ProductQueryService,
    operation: str,
) -> None:
    user = _user("branch_manager", organizations=("ORG001",))

    with pytest.raises(PermissionError, match="跨机构排名或省均值"):
        product._authorize_plan(
            _plan("ZB001", operation=operation),
            user,
        )


def test_authorized_execution_scope_uses_plan_minimum(
    product: ProductQueryService,
) -> None:
    user = _user("head_office_manager")

    value_scope = product._authorize_plan(
        _plan("ZB001", organizations=("ORG001",)),
        user,
    )
    rank_scope = product._authorize_plan(
        _plan("ZB001", organizations=("ORG001",), operation="rank"),
        user,
    )

    assert value_scope.metric_ids == ("ZB001",)
    assert value_scope.organization_ids == ("ORG001",)
    assert rank_scope.metric_ids == ("ZB001",)
    assert rank_scope.organization_ids is None


def test_masking_rejects_sensitive_value_aliased_as_dimension(
    product: ProductQueryService,
) -> None:
    record = _history_record()
    record["plan"]["metrics"] = ["ZB013"]
    record["columns"] = ["org_name", "metric_value"]
    record["rows"] = [["123.45", 123.45]]
    viewer = UserContext("shared-viewer", "viewer")

    response = product._response_from_record(record, viewer)

    assert response.records == ({"org_name": "***", "metric_value": "***"},)


def test_history_is_filtered_against_current_permissions(
    product: ProductQueryService,
) -> None:
    record = _history_record()
    record["user_id"] = "downgraded-user"
    product.store.save_query(record)
    downgraded = _user(
        "business_staff",
        user_id="downgraded-user",
        organizations=("ORG001",),
    )

    assert product.history(downgraded) == []
    assert product.session_history("sensitive-session", downgraded) == []


def test_history_fails_closed_when_stored_plan_has_no_metrics(
    product: ProductQueryService,
) -> None:
    record = _history_record()
    record["user_id"] = "legacy-corrupt-user"
    record["plan"]["metrics"] = []
    product.store.save_query(record)
    user = _user(
        "business_staff",
        user_id="legacy-corrupt-user",
        organizations=("ORG001",),
    )

    assert product.history(user) == []
    with pytest.raises(PermissionError, match="未声明指标"):
        product.get_query("sensitive-query", user)


def test_share_is_reauthorized_for_logged_in_recipient(
    product: ProductQueryService,
) -> None:
    product.store.save_query(_history_record())
    owner = _user("head_office_manager", user_id="business-owner")
    token = product.create_share(
        "sensitive-query",
        owner,
        expires_hours=1,
    )["token"]
    denied = _user(
        "business_staff",
        user_id="recipient-business",
        organizations=("ORG001",),
    )
    allowed = _user("finance_staff", user_id="recipient-finance")

    with pytest.raises(PermissionError, match="无权访问指标"):
        product.resolve_share(token, denied)
    assert product.resolve_share(token, allowed).query_id == "sensitive-query"


def test_bearer_context_keeps_business_role_and_organization_scope(
    monkeypatch,
) -> None:
    monkeypatch.setenv("TEXT2SQL_ANALYST_2_USERNAME", "branch-test")
    monkeypatch.setenv("TEXT2SQL_ANALYST_2_PASSWORD", "branch-secret")
    monkeypatch.setenv("TEXT2SQL_ANALYST_2_ORGS", "ORG003,ORG004")
    auth = ProductAuthService()
    token, authenticated = auth.login("branch-test", "branch-secret")
    monkeypatch.setattr(api, "get_auth_service", lambda: auth)

    context = api.user_context(
        "spoofed-admin",
        "system_admin",
        "ORG013",
        f"Bearer {token}",
    )

    assert authenticated.to_dict()["business_role"] == "branch_manager"
    assert context.user_id == "branch-test"
    assert context.role == "analyst"
    assert context.business_role == "branch_manager"
    assert context.organization_access == "restricted"
    assert context.allowed_organizations == ("ORG003", "ORG004")


def test_legacy_header_analyst_keeps_supplied_organization_scope(
    monkeypatch,
) -> None:
    monkeypatch.setenv("TEXT2SQL_ALLOW_HEADER_AUTH", "true")

    context = api.user_context("legacy-branch", "analyst", "ORG001", None)

    assert context.business_role == "branch_manager"
    assert context.effective_organization_access == "restricted"
    assert context.allowed_organizations == ("ORG001",)
