"""Layered plan, AST safety, plan/SQL alignment and result validation."""

from __future__ import annotations

from datetime import date
import re

from sqlglot import exp, parse
from sqlglot.errors import ParseError

from .errors import ResultValidationError, SQLSafetyError, ValidationError
from .models import QueryPlan, QueryResult
from .semantic_catalog import APPROVED_TABLES, RATIO_METRICS, SemanticCatalog


ALLOWED_OPERATIONS = {
    "value", "rank", "difference", "growth", "ratio", "province_average_compare",
    "threshold", "daily_average", "quarterly_trend", "multi_condition",
    "extrema", "count_condition", "annual_average_extrema", "multi_metric_rank_change",
    "metric_profile_rank", "mom_yoy_difference", "period_global_extrema", "period_average_summary",
    "component_shares", "organization_sum", "cross_organization_difference",
    "component_sum", "component_sum_compare", "province_average_count",
    "period_growth_rank", "period_decline_rank", "metric_difference",
    "days_vs_average", "multi_metric_difference",
    "profitability_assessment", "multi_metric_average_condition",
}
EXTERNAL_ACCESS = re.compile(
    r"\b(read_csv(?:_auto)?|read_parquet|read_json(?:_auto)?|read_xlsx|sqlite_scan|postgres_scan|httpfs|glob)\s*\(",
    re.IGNORECASE,
)


class PlanValidator:
    def __init__(self, catalog: SemanticCatalog) -> None:
        self.catalog = catalog

    def validate(self, plan: QueryPlan) -> None:
        if plan.operation not in ALLOWED_OPERATIONS:
            raise ValidationError(f"不支持的查询操作：{plan.operation}")
        if plan.source == "rule" and plan.confidence < 0.95:
            raise ValidationError("规则置信度不足")
        if not plan.metrics or any(metric not in self.catalog.metrics for metric in plan.metrics):
            raise ValidationError("查询指标缺失或不在指标白名单")
        if plan.organization_scope == "selected":
            if not plan.organizations:
                raise ValidationError("缺少机构范围")
            if any(org not in self.catalog.organizations for org in plan.organizations):
                raise ValidationError("查询包含未知机构")
        for value in (plan.current_date, plan.comparison_date, plan.start_date, plan.end_date):
            if value:
                try:
                    date.fromisoformat(value)
                except ValueError as exc:
                    raise ValidationError(f"非法日期：{value}") from exc
        if plan.operation in {"value", "rank", "ratio", "province_average_compare", "threshold"} and not plan.current_date:
            raise ValidationError("查询缺少当前日期")
        if plan.operation in {"difference", "growth"} and (not plan.current_date or not plan.comparison_date):
            raise ValidationError("期间比较缺少当前日期或比较日期")
        if plan.operation == "growth" and any(metric in RATIO_METRICS for metric in plan.metrics):
            raise ValidationError("比率类指标不计算增幅，应计算百分点差")
        if plan.operation == "ratio" and (len(plan.metrics) != 2 or not plan.derived_formula):
            raise ValidationError("派生比率必须包含分子、分母和公式标识")
        if plan.operation in {"daily_average", "period_average_summary", "quarterly_trend"} and (
            not plan.start_date or not plan.end_date
        ):
            raise ValidationError("期间统计缺少开始或结束日期")
        if plan.operation == "annual_average_extrema" and (not plan.start_date or not plan.end_date):
            raise ValidationError("年度均值排名缺少开始或结束日期")
        if plan.operation == "multi_metric_rank_change" and (not plan.current_date or not plan.comparison_date):
            raise ValidationError("多指标排名变化缺少当前日期或比较日期")
        if plan.operation == "metric_profile_rank" and not plan.current_date:
            raise ValidationError("指标画像排名缺少查询日期")
        if plan.operation == "mom_yoy_difference" and (
            not plan.current_date or not plan.comparison_date or not plan.start_date
        ):
            raise ValidationError("环比同比查询缺少当前、上月或去年同期日期")
        if plan.operation == "period_global_extrema" and (not plan.start_date or not plan.end_date):
            raise ValidationError("区间单日极值缺少开始或结束日期")
        if plan.operation in {
            "component_shares", "organization_sum", "cross_organization_difference",
            "component_sum", "component_sum_compare", "province_average_count",
            "metric_difference",
        } and not plan.current_date:
            raise ValidationError("查询缺少当前日期")
        if plan.operation in {"period_growth_rank", "period_decline_rank"} and (
            not plan.current_date or not plan.comparison_date
        ):
            raise ValidationError("期间变化排名缺少当前日期或比较日期")
        if plan.operation == "days_vs_average" and (not plan.start_date or not plan.end_date):
            raise ValidationError("按日比较全省均值缺少开始或结束日期")
        if plan.operation == "multi_metric_difference" and (
            not plan.current_date or not plan.comparison_date
        ):
            raise ValidationError("多指标期间比较缺少当前日期或比较日期")
        if plan.operation == "profitability_assessment" and (
            not plan.current_date or not plan.comparison_date
        ):
            raise ValidationError("盈利能力评估缺少当前日期或年初日期")
        if plan.operation == "multi_metric_average_condition" and not plan.current_date:
            raise ValidationError("多指标均值条件查询缺少当前日期")
        if plan.operation == "rank" and plan.sort_direction not in {"asc", "desc"}:
            raise ValidationError("排名查询缺少排序方向")
        if plan.limit is not None and not 1 <= plan.limit <= 1000:
            raise ValidationError("返回数量不合法")


class SQLGuard:
    def __init__(self, default_limit: int = 200, hard_limit: int = 1000) -> None:
        self.default_limit = default_limit
        self.hard_limit = hard_limit

    def parse_one(self, sql: str) -> exp.Expression:
        if not isinstance(sql, str) or not sql.strip():
            raise SQLSafetyError("SQL为空")
        if EXTERNAL_ACCESS.search(sql) or re.search(r"https?://|file://|[A-Za-z]:[/\\]", sql, re.IGNORECASE):
            raise SQLSafetyError("禁止外部文件或网络访问")
        try:
            statements = [item for item in parse(sql, read="duckdb") if item is not None]
        except ParseError as exc:
            raise SQLSafetyError("SQL语法无法解析") from exc
        if len(statements) != 1:
            raise SQLSafetyError("只允许一条SQL语句")
        expression = statements[0]
        if not isinstance(expression, exp.Query):
            raise SQLSafetyError("只允许SELECT或WITH查询")
        forbidden_types = tuple(
            cls for cls in (
                getattr(exp, "Insert", None), getattr(exp, "Update", None), getattr(exp, "Delete", None),
                getattr(exp, "Create", None), getattr(exp, "Drop", None), getattr(exp, "Alter", None),
                getattr(exp, "Merge", None), getattr(exp, "Command", None), getattr(exp, "Copy", None),
            ) if cls is not None
        )
        if any(isinstance(node, forbidden_types) for node in expression.walk()):
            raise SQLSafetyError("SQL包含禁止的写入或管理操作")
        cte_names = {cte.alias_or_name.lower() for cte in expression.find_all(exp.CTE) if cte.alias_or_name}
        for table in expression.find_all(exp.Table):
            name = table.name.lower()
            if name not in cte_names and name not in APPROVED_TABLES:
                raise SQLSafetyError(f"禁止访问非白名单表：{name}")
        if sum(1 for _ in expression.find_all(exp.Join)) > 8:
            raise SQLSafetyError("JOIN数量超过安全上限")
        return expression

    def validate_and_limit(self, sql: str) -> tuple[str, exp.Expression]:
        expression = self.parse_one(sql)
        existing_limit = expression.args.get("limit")
        if existing_limit is not None:
            value = existing_limit.expression
            if not isinstance(value, exp.Literal) or not value.is_int:
                raise SQLSafetyError("LIMIT必须是整数常量")
            if int(value.this) > self.hard_limit:
                expression.set("limit", exp.Limit(expression=exp.Literal.number(self.hard_limit)))
            return expression.sql(dialect="duckdb"), expression
        single_aggregate = (
            isinstance(expression, exp.Select)
            and expression.args.get("group") is None
            and any(True for _ in expression.find_all(exp.AggFunc))
            and not any(True for _ in expression.find_all(exp.Window))
        )
        normalized = expression.sql(dialect="duckdb")
        if single_aggregate:
            return normalized, expression
        limited = f"SELECT * FROM (\n{normalized}\n) AS __text2sql_result\nLIMIT {self.default_limit}"
        return limited, expression


class AlignmentValidator:
    """Conservative checks that the SQL contains the plan's explicit business constraints."""

    def __init__(self, catalog: SemanticCatalog) -> None:
        self.catalog = catalog

    def validate(self, plan: QueryPlan, sql: str, expression: exp.Expression) -> None:
        upper = sql.upper()
        querying_every_metric = set(plan.metrics) == set(self.catalog.metrics)
        if not querying_every_metric:
            for metric in plan.metrics:
                if metric.upper() not in upper:
                    raise ValidationError(f"SQL遗漏指标：{metric}")
        if plan.organization_scope == "selected":
            for org in plan.organizations:
                if org.upper() not in upper:
                    raise ValidationError(f"SQL遗漏机构：{org}")
        for value in (plan.current_date, plan.comparison_date, plan.start_date, plan.end_date):
            if value and value not in sql:
                raise ValidationError(f"SQL遗漏日期条件：{value}")
        if plan.operation == "rank":
            orders = list(expression.find_all(exp.Ordered))
            if not orders:
                raise ValidationError("排名SQL缺少ORDER BY")
            expected_desc = plan.sort_direction == "desc"
            if not any(bool(item.args.get("desc")) == expected_desc for item in orders):
                raise ValidationError("SQL排序方向与指标口径不一致")
            if plan.limit is not None and f"LIMIT {plan.limit}" not in upper:
                raise ValidationError("排名SQL遗漏Top-N限制")
        if plan.operation == "province_average_compare" and "AVG" not in upper:
            raise ValidationError("省均值比较SQL未计算AVG")
        if plan.operation == "growth" and "NULLIF" not in upper:
            raise ValidationError("增幅SQL缺少除零保护")
        if plan.operation == "ratio" and plan.derived_formula != "combined_npl_overdue_rate" and "NULLIF" not in upper:
            raise ValidationError("派生比率SQL缺少除零保护")


class ResultValidator:
    def validate(self, plan: QueryPlan, result: QueryResult) -> None:
        if not result.rows and not plan.allow_empty:
            raise ResultValidationError("查询结果为空，可能是日期、机构或指标条件不匹配")
        if plan.expected_shape == "single_row" and len(result.rows) != 1:
            raise ResultValidationError(f"期望单行结果，实际返回{len(result.rows)}行")
        if plan.operation == "rank" and plan.limit is not None and len(result.rows) > plan.limit:
            raise ResultValidationError("结果行数超过QueryPlan约定")
        if plan.source == "rule" and plan.operation == "rank" and "metric_rank" not in result.columns:
            raise ResultValidationError("排名结果缺少metric_rank")
        if plan.source == "rule" and plan.operation == "ratio" and "derived_value" not in result.columns:
            raise ResultValidationError("派生比率结果缺少derived_value")
        if plan.source == "rule" and plan.operation == "province_average_compare" and "province_average" not in result.columns:
            raise ResultValidationError("省均值比较结果不完整")
