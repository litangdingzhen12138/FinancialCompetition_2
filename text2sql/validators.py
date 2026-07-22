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
    "extrema", "count_condition", "sum", "composition", "mom_yoy",
    "count_vs_average", "profile",
    "cross_difference", "period_rank_extremes", "period_extrema",
    "multi_period_change", "period_change_rank", "multi_rank",
    "multi_rank_change", "three_dimension_profile", "reconcile",
    "multi_metric_province_compare",
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
        if plan.operation in {
            "value", "rank", "ratio", "province_average_compare", "threshold",
            "sum", "composition", "profile",
            "cross_difference", "multi_rank",
            "multi_rank_change", "three_dimension_profile", "reconcile",
            "multi_metric_province_compare",
        } and not plan.current_date:
            raise ValidationError("查询缺少当前日期")
        if plan.operation in {"difference", "growth"} and (not plan.current_date or not plan.comparison_date):
            raise ValidationError("期间比较缺少当前日期或比较日期")
        if plan.operation == "growth" and any(metric in RATIO_METRICS for metric in plan.metrics):
            raise ValidationError("比率类指标不计算增幅，应计算百分点差")
        if plan.operation == "ratio" and (len(plan.metrics) != 2 or not plan.derived_formula):
            raise ValidationError("派生比率必须包含分子、分母和公式标识")
        if plan.operation == "composition" and len(plan.metrics) < 3:
            raise ValidationError("组成占比必须包含分项和合计指标")
        if plan.operation == "mom_yoy" and (not plan.current_date or not plan.comparison_date):
            raise ValidationError("环比同比查询缺少当前日期或环比日期")
        if plan.operation in {
            "daily_average", "quarterly_trend", "period_rank_extremes", "period_extrema",
        } and (
            not plan.start_date or not plan.end_date
        ):
            raise ValidationError("期间统计缺少开始或结束日期")
        if plan.operation == "count_vs_average" and not plan.current_date and (
            not plan.start_date or not plan.end_date
        ):
            raise ValidationError("省均值计数缺少日期或期间")
        if plan.operation in {"multi_period_change", "period_change_rank", "multi_rank_change"} and (
            not plan.current_date or not plan.comparison_date
        ):
            raise ValidationError("期间变化查询缺少当前日期或比较日期")
        if plan.operation in {"multi_rank", "multi_rank_change", "three_dimension_profile"} and plan.organization_scope != "selected":
            raise ValidationError("多指标排名必须指定目标机构")
        if plan.operation == "count_vs_average" and (
            len(plan.filters) != 1
            or plan.filters[0].reference != "province_average"
            or plan.filters[0].operator not in {">", ">=", "<", "<="}
        ):
            raise ValidationError("期间省均值计数缺少有效比较条件")
        if plan.operation == "multi_metric_province_compare" and (
            plan.organization_scope != "all"
            or len(plan.metrics) < 2
            or len(plan.filters) != len(plan.metrics)
            or {item.field for item in plan.filters} != set(plan.metrics)
            or any(
                item.reference != "province_average"
                or item.operator not in {">", ">=", "<", "<="}
                for item in plan.filters
            )
        ):
            raise ValidationError("多指标省均值联合条件不完整")
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
        required_dates: list[str | None] = [plan.current_date]
        if plan.operation in {"difference", "growth"} or (
            plan.operation == "multi_condition" and plan.comparison_date
        ):
            required_dates.append(plan.comparison_date)
        if plan.operation in {
            "daily_average", "quarterly_trend", "extrema", "count_vs_average",
            "period_rank_extremes", "period_extrema",
        } or not plan.current_date:
            required_dates.extend((plan.start_date, plan.end_date))
        if plan.operation in {
            "mom_yoy", "profile", "multi_period_change", "period_change_rank",
            "multi_rank_change",
        } and plan.comparison_date:
            required_dates.append(plan.comparison_date)
        for value in required_dates:
            if value and value not in sql:
                raise ValidationError(f"SQL遗漏日期条件：{value}")
        if plan.operation == "rank":
            orders = list(expression.find_all(exp.Ordered))
            if not orders:
                raise ValidationError("排名SQL缺少ORDER BY")
            expected_desc = plan.sort_direction == "desc"
            if not any(bool(item.args.get("desc")) == expected_desc for item in orders):
                raise ValidationError("SQL排序方向与指标口径不一致")
            if plan.limit is not None:
                has_limit = any(
                    isinstance(limit.expression, exp.Literal)
                    and limit.expression.is_int
                    and int(limit.expression.this) <= plan.limit
                    for limit in expression.find_all(exp.Limit)
                )
                has_rank_filter = any(
                    isinstance(predicate.expression, exp.Literal)
                    and predicate.expression.is_int
                    and int(predicate.expression.this) <= plan.limit
                    and any(token in predicate.this.sql().lower() for token in ("rank", "row_num", "rn"))
                    for predicate in expression.find_all(exp.LTE)
                )
                has_strict_rank_filter = any(
                    isinstance(predicate.expression, exp.Literal)
                    and predicate.expression.is_int
                    and int(predicate.expression.this) <= plan.limit + 1
                    and any(token in predicate.this.sql().lower() for token in ("rank", "row_num", "rn"))
                    for predicate in expression.find_all(exp.LT)
                )
                if not (has_limit or has_rank_filter or has_strict_rank_filter):
                    raise ValidationError("排名SQL遗漏Top-N限制")
        if plan.operation in {"province_average_compare", "multi_metric_province_compare"} and "AVG" not in upper:
            raise ValidationError("省均值比较SQL未计算AVG")
        if plan.operation == "growth" and "NULLIF" not in upper:
            raise ValidationError("增幅SQL缺少除零保护")
        if plan.operation == "ratio" and "NULLIF" not in upper:
            raise ValidationError("派生比率SQL缺少除零保护")


class ResultValidator:
    def validate(self, plan: QueryPlan, result: QueryResult) -> None:
        if not result.rows and not plan.allow_empty:
            raise ResultValidationError("查询结果为空，可能是日期、机构或指标条件不匹配")
        if plan.expected_shape == "single_row" and len(result.rows) != 1:
            raise ResultValidationError(f"期望单行结果，实际返回{len(result.rows)}行")
        if plan.operation == "rank" and plan.limit is not None and len(result.rows) > plan.limit:
            if "metric_rank" not in result.columns:
                raise ResultValidationError("结果行数超过QueryPlan约定")
            rank_index = result.columns.index("metric_rank")
            if any(row[rank_index] is None or int(row[rank_index]) > plan.limit for row in result.rows):
                raise ResultValidationError("结果包含Top-N范围之外的排名")
        if plan.source == "rule" and plan.operation == "rank" and "metric_rank" not in result.columns:
            raise ResultValidationError("排名结果缺少metric_rank")
        if plan.source == "rule" and plan.operation == "ratio" and "derived_value" not in result.columns:
            raise ResultValidationError("派生比率结果缺少derived_value")
        if plan.source == "rule" and plan.operation == "province_average_compare" and "province_average" not in result.columns:
            raise ResultValidationError("省均值比较结果不完整")
        required_result_columns = {
            "sum": {"metric_value", "total_value"},
            "composition": {"metric_id", "derived_value"},
            "mom_yoy": {"current_value", "mom_change", "yoy_change"},
            "count_vs_average": (
                {"matching_days", "total_days", "matching_percentage"}
                if plan.start_date else {"matching_organizations", "total_organizations"}
            ),
            "profile": {"metric_id", "metric_value", "metric_rank"},
            "cross_difference": {"metric_id", "metric_value", "difference_value"},
            "period_rank_extremes": {"average_value", "rank_type", "metric_rank"},
            "period_extrema": {"data_date", "metric_value", "extrema_type"},
            "multi_period_change": {"metric_id", "comparison_value", "current_value", "change_value"},
            "period_change_rank": {"result_value", "result_rank"},
            "multi_rank": {"metric_id", "metric_value", "metric_rank"},
            "multi_rank_change": {"metric_id", "previous_rank", "current_rank", "rank_change"},
            "three_dimension_profile": {"metric_id", "metric_value", "metric_rank"},
            "reconcile": {"corporate_value", "personal_value", "total_value", "difference_value", "is_equal"},
            "multi_metric_province_compare": {
                "org_name", "metric_id", "metric_value", "province_average"
            },
        }.get(plan.operation)
        if plan.source == "rule" and required_result_columns and not required_result_columns.issubset(result.columns):
            raise ResultValidationError(f"{plan.operation}结果字段不完整")
        if plan.source == "rule" and plan.operation == "profile" and (
            "performance_profile" in plan.assumptions
            and "performance_label" not in result.columns
        ):
            raise ResultValidationError("表现画像结果缺少分类字段")
        if "metric_id" in result.columns:
            metric_index = result.columns.index("metric_id")
            returned_metrics = {str(row[metric_index]) for row in result.rows if row[metric_index] is not None}
            unexpected_metrics = returned_metrics - set(plan.metrics)
            if unexpected_metrics:
                raise ResultValidationError(f"查询结果包含计划外指标：{sorted(unexpected_metrics)}")
        if plan.organization_scope == "selected" and "org_id" in result.columns:
            org_index = result.columns.index("org_id")
            returned_orgs = {str(row[org_index]) for row in result.rows if row[org_index] is not None}
            unexpected_orgs = returned_orgs - set(plan.organizations)
            if unexpected_orgs:
                raise ResultValidationError(f"查询结果包含计划外机构：{sorted(unexpected_orgs)}")
        if "require_organization_list" in plan.assumptions and not ({"org_id", "org_name"} & set(result.columns)):
            raise ResultValidationError("问题同时要求机构名单和数量，但结果缺少机构列表")
