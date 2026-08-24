"""Layered plan, AST safety, plan/SQL alignment and result validation."""

from __future__ import annotations

from datetime import date
from functools import lru_cache
import re

import duckdb
from sqlglot import exp, parse
from sqlglot.errors import ParseError

from .business_rules import (
    PERFORMANCE_BAD_COUNT,
    PERFORMANCE_GOOD_MAX_RANK,
    PROVINCE_ORGANIZATION_COUNT,
)
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


ANSWER_OBLIGATION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("rank", re.compile(r"第几|排名|排第|位列|前\d|后\d|前三|后三|哪家.*(?:最高|最低|最多|最少)|谁.*(?:最高|最低)")),
    ("province_average", re.compile(r"全省(?:均值|平均)|省均值|平均水平")),
    ("growth", re.compile(r"增幅|增长率|增长百分比")),
    ("difference", re.compile(r"差多少|相差|变化了多少|变动了多少|增减了多少|增长了多少钱|回落了多少|高多少|低多少|多多少|少多少")),
    ("threshold", re.compile(r"达标|监管(?:线|要求|上限|下限)|满足.*要求|是否.*(?:高于|低于|达到|超过|不超过).*\d+(?:\.\d+)?%?")),
    ("count", re.compile(r"多少家|有几家|一共有几家|多少天")),
    ("profile", re.compile(r"综合(?:评价|判断|来看)|整体(?:画像|风控|风险)|画像|一句话总结|最优.*之一")),
    ("extrema", re.compile(r"最高日|最低日|单日最高|单日最低|最高.*最低|最低.*最高")),
    ("trend", re.compile(r"逐季(?:变化|趋势|数据|如何|怎么样|是)|按季(?:展示|列出|分析)|季度(?:序列|趋势)|趋势")),
    ("sum", re.compile(r"合计|总额|加起来|相加|总和")),
)


def answer_obligations(question: str) -> tuple[str, ...]:
    """Extract only high-confidence semantic duties; unknown wording stays with LLM."""
    normalized = re.sub(r"\s+", "", question)
    obligations = [
        name for name, pattern in ANSWER_OBLIGATION_PATTERNS if pattern.search(normalized)
    ]
    if (
        "extrema" in obligations
        and "rank" in obligations
        and not re.search(r"第几|排名|排第|位列|前\d|后\d|前三|后三", normalized)
    ):
        obligations.remove("rank")
    return tuple(dict.fromkeys(obligations))


class AnswerCoverageValidator:
    """Reject plans that complete only a visible subset of the user's request."""

    _RANK_OPERATIONS = {
        "rank", "period_change_rank", "multi_rank", "multi_rank_change",
        "period_rank_extremes", "profile", "three_dimension_profile",
    }
    _AVERAGE_OPERATIONS = {
        "province_average_compare", "count_vs_average", "multi_metric_province_compare",
    }
    _DIFFERENCE_OPERATIONS = {
        "difference", "growth", "province_average_compare", "cross_difference",
        "mom_yoy", "multi_period_change", "period_change_rank", "multi_rank_change",
    }
    _THRESHOLD_OPERATIONS = {
        "threshold", "count_condition", "multi_metric_province_compare",
    }
    _COUNT_OPERATIONS = {
        "count_condition", "count_vs_average", "multi_metric_province_compare",
    }
    _PROFILE_OPERATIONS = {"profile", "three_dimension_profile"}
    _EXTREMA_OPERATIONS = {"extrema", "period_extrema", "period_rank_extremes"}

    def analyze(self, question: str, plan: QueryPlan) -> tuple[tuple[str, ...], tuple[str, ...]]:
        required = answer_obligations(question)
        supported = {"value"}
        operation = plan.operation

        if operation == "multi_condition":
            # The LLM uses this operation for genuinely composite plans; SQL and
            # result validation remain responsible for its concrete columns.
            supported.update(required)
        if operation in self._RANK_OPERATIONS or "rank_population_all" in plan.assumptions:
            supported.add("rank")
        if operation in self._AVERAGE_OPERATIONS:
            supported.add("province_average")
        if operation in self._DIFFERENCE_OPERATIONS or plan.query_type in {
            "pairwise_difference", "cross_difference"
        }:
            supported.add("difference")
        if operation == "growth" or (
            operation == "period_change_rank"
            and "rank_decrease" not in plan.assumptions
            and "rank_absolute_change" not in plan.assumptions
        ):
            supported.add("growth")
        if operation in self._THRESHOLD_OPERATIONS or "regulatory_check" in plan.assumptions:
            supported.add("threshold")
        if operation in self._COUNT_OPERATIONS or "require_organization_count" in plan.assumptions:
            supported.add("count")
        if operation in self._PROFILE_OPERATIONS or "needs_summary" in plan.assumptions:
            supported.add("profile")
        if operation in self._EXTREMA_OPERATIONS or "include_extrema" in plan.assumptions:
            supported.add("extrema")
        if operation == "quarterly_trend":
            supported.add("trend")
        if operation in {"sum", "reconcile", "composition"}:
            supported.add("sum")

        missing = tuple(item for item in required if item not in supported)
        return required, missing

    def validate(self, question: str, plan: QueryPlan) -> None:
        required, missing = self.analyze(question, plan)
        if missing:
            raise ValidationError(
                "查询计划未覆盖全部回答义务："
                f"要求={','.join(required) or 'value'}；遗漏={','.join(missing)}"
            )


def _is_zero_literal(node: exp.Expression) -> bool:
    return isinstance(node, exp.Literal) and node.is_number and float(node.this) == 0.0


def _division_is_zero_safe(division: exp.Div) -> bool:
    """Accept NULLIF or an enclosing CASE that guards this denominator at zero."""
    denominator = division.expression
    if any(True for _ in denominator.find_all(exp.Nullif)):
        return True
    denominator_columns = {
        column.name.lower() for column in denominator.find_all(exp.Column) if column.name
    }
    ancestor = division.parent
    while ancestor is not None:
        if isinstance(ancestor, exp.Case):
            for equality in ancestor.find_all(exp.EQ):
                if _is_zero_literal(equality.this):
                    guarded_side = equality.expression
                elif _is_zero_literal(equality.expression):
                    guarded_side = equality.this
                else:
                    continue
                guarded_columns = {
                    column.name.lower()
                    for column in guarded_side.find_all(exp.Column)
                    if column.name
                }
                if denominator_columns & guarded_columns:
                    return True
            return False
        ancestor = ancestor.parent
    return False


def _validate_safe_divisions(expression: exp.Expression, operation_label: str) -> None:
    divisions = list(expression.find_all(exp.Div))
    if not divisions:
        raise ValidationError(f"{operation_label}SQL缺少除法计算")
    if any(not _division_is_zero_safe(division) for division in divisions):
        raise ValidationError(f"{operation_label}SQL缺少分母为零保护")


@lru_cache(maxsize=1)
def _duckdb_reserved_keywords() -> frozenset[str]:
    """Use the installed DuckDB version as the source of truth for reserved words."""
    connection = duckdb.connect(":memory:")
    try:
        rows = connection.execute(
            "SELECT keyword_name FROM duckdb_keywords() WHERE keyword_category = 'reserved'"
        ).fetchall()
    finally:
        connection.close()
    # Keep the two table-operator keywords guarded across DuckDB version metadata changes.
    return frozenset(str(row[0]).lower() for row in rows) | {"pivot", "unpivot"}


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
        if plan.operation == "rank":
            if len(plan.metrics) != 1:
                raise ValidationError("单次排名只能包含一个指标")
            expected_direction = self.catalog.metrics[plan.metrics[0]].sort_direction
            if plan.sort_direction != expected_direction:
                raise ValidationError(
                    "排名方向违反衍生维度说明："
                    f"{plan.metrics[0]}必须使用{expected_direction}"
                )
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
        reserved_aliases = {
            alias.this.name.lower()
            for alias in expression.find_all(exp.TableAlias)
            if isinstance(alias.this, exp.Identifier)
            and not alias.this.args.get("quoted")
            and alias.this.name.lower() in _duckdb_reserved_keywords()
        }
        if reserved_aliases:
            names = "、".join(sorted(reserved_aliases))
            raise SQLSafetyError(
                f"CTE或表别名“{names}”是未加引号的DuckDB保留字；"
                "请改用非保留名称（如pv、base_data），或对名称加双引号。"
            )
        cte_names = {cte.alias_or_name.lower() for cte in expression.find_all(exp.CTE) if cte.alias_or_name}
        for table in expression.find_all(exp.Table):
            name = table.name.lower()
            if name not in cte_names and name not in APPROVED_TABLES:
                raise SQLSafetyError(f"禁止访问非白名单表：{name}")
            if name == "metric_values" and (table.args.get("db") or table.args.get("catalog")):
                raise SQLSafetyError("metric_values必须使用未限定表名，以应用数据权限范围")
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
            if not any(True for _ in expression.find_all(exp.Rank)):
                raise ValidationError("排名SQL必须按照衍生维度说明使用RANK算法")
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
                has_bottom_rank_filter = (
                    "select_bottom_rank" in plan.assumptions
                    and any(
                        "rank" in predicate.this.sql().lower()
                        and "population" in predicate.expression.sql().lower()
                        for predicate in expression.find_all(exp.GTE)
                    )
                )
                if not (
                    has_limit or has_rank_filter or has_strict_rank_filter or has_bottom_rank_filter
                ):
                    raise ValidationError("排名SQL遗漏Top-N限制")
        if plan.operation in {"province_average_compare", "multi_metric_province_compare"} and "AVG" not in upper:
            raise ValidationError("省均值比较SQL未计算AVG")
        if plan.operation == "growth":
            _validate_safe_divisions(expression, "增幅")
        if plan.operation == "ratio":
            _validate_safe_divisions(expression, "派生比率")


class ResultValidator:
    def validate(self, plan: QueryPlan, result: QueryResult) -> None:
        if not result.rows and not plan.allow_empty:
            raise ResultValidationError("查询结果为空，可能是日期、机构或指标条件不匹配")
        if plan.expected_shape == "single_row" and len(result.rows) != 1:
            raise ResultValidationError(f"期望单行结果，实际返回{len(result.rows)}行")
        if plan.operation == "rank" and plan.limit is not None and len(result.rows) > plan.limit:
            preserves_boundary_ties = any(
                assumption in plan.assumptions
                for assumption in ("select_highest_value", "select_lowest_value", "select_bottom_rank")
            )
            if not preserves_boundary_ties:
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
            unexpected_metrics = {
                metric
                for metric in returned_metrics - set(plan.metrics)
                if re.fullmatch(r"ZB\d+", metric, re.IGNORECASE)
            }
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
        if "performance_label" in result.columns:
            if "metric_rank" not in result.columns:
                raise ResultValidationError("表现分类缺少metric_rank，无法验证前三/后四口径")
            rank_index = result.columns.index("metric_rank")
            label_index = result.columns.index("performance_label")
            bad_rank_minimum = PROVINCE_ORGANIZATION_COUNT - PERFORMANCE_BAD_COUNT + 1
            for row in result.rows:
                rank = int(row[rank_index])
                expected_label = (
                    "较好"
                    if rank <= PERFORMANCE_GOOD_MAX_RANK
                    else "较差"
                    if rank >= bad_rank_minimum
                    else "中性"
                )
                if row[label_index] != expected_label:
                    raise ResultValidationError(
                        f"表现分类违反前三/后四口径：第{rank}名应为{expected_label}，"
                        f"实际为{row[label_index]}"
                    )
