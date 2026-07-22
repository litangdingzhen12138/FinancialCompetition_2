"""Conservative deterministic planning for simple, unambiguous query families.

Rules deliberately stop at semantic boundaries.  Queries that require composing
multiple calculations, interpreting a business judgement, or choosing a complex
aggregation are handed to the LLM planner instead of being encoded per question.
"""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
import re

from .date_resolver import comparison_date, resolve_date
from .models import PlanFilter, QueryPlan, SessionState
from .semantic_catalog import (
    DERIVED_METRICS,
    RATIO_METRICS,
    SemanticCatalog,
    contains_pronoun_reference,
)


@dataclass(frozen=True, slots=True)
class RuleDecision:
    plan: QueryPlan | None
    candidates: dict[str, object]
    reason: str


def normalize_question(question: str) -> str:
    return re.sub(r"\s+", "", question.strip().replace("？", "?").replace("％", "%"))


def _top_n(question: str) -> int | None:
    match = re.search(r"(?:前|后|最高的?|最低的?|排名前|排名后)(\d+)(?:家|名|个)?", question)
    if match:
        return int(match.group(1))
    chinese = {
        "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
        "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
    }
    match = re.search(r"(?:前|后)([一二三四五六七八九十])", question)
    if match:
        return chinese[match.group(1)]
    match = re.search(r"(?:最大|最小|最高|最低)的?([一二三四五六七八九十])家", question)
    if match:
        return chinese[match.group(1)]
    if re.search(r"哪家|谁.*(?:最高|最低|最多|最少|最好|最差)|排第一|最后一名", question):
        return 1
    return None


def _threshold(question: str) -> PlanFilter | None:
    match = re.search(
        r"(超过|高于|大于|不低于|至少|低于|小于|不超过|至多)(\d+(?:\.\d+)?)%?",
        question,
    )
    if not match:
        match = re.search(r"(?:满足|达到)(\d+(?:\.\d+)?)%?的?(?:最低)?要求", question)
        if not match:
            return None
        return PlanFilter("metric_value", ">=", float(match.group(1)))
    operator = {
        "超过": ">", "高于": ">", "大于": ">", "不低于": ">=", "至少": ">=",
        "低于": "<", "小于": "<", "不超过": "<=", "至多": "<=",
    }[match.group(1)]
    return PlanFilter("metric_value", operator, float(match.group(2)))


def _period_range(question: str) -> tuple[str | None, str | None]:
    quarter_numbers = {"一": 1, "二": 2, "三": 3, "四": 4}

    def quarter_number(text: str) -> int:
        return quarter_numbers.get(text, int(text) if text.isdigit() else 1)

    def quarter_end(year: int, text: str) -> str:
        month = quarter_number(text) * 3
        return f"{year}-{month:02d}-{monthrange(year, month)[1]:02d}"

    chinese = re.findall(r"(20\d{2})年(?:第)?([一二三四1234])季度(?:末)?", question)
    if len(chinese) >= 2:
        return quarter_end(int(chinese[0][0]), chinese[0][1]), quarter_end(int(chinese[-1][0]), chinese[-1][1])
    q_style = re.findall(r"(20\d{2})[- ]?Q([1-4])(?:末)?", question, re.IGNORECASE)
    if len(q_style) >= 2:
        return quarter_end(int(q_style[0][0]), q_style[0][1]), quarter_end(int(q_style[-1][0]), q_style[-1][1])
    match = re.search(r"(20\d{2})年全年", question)
    if match:
        return f"{match.group(1)}-01-01", f"{match.group(1)}-12-31"
    match = re.search(r"(20\d{2})年(?:第)?([一二三四1234])季度", question)
    if match:
        year = int(match.group(1))
        quarter = quarter_number(match.group(2))
        first_month, last_month = (quarter - 1) * 3 + 1, quarter * 3
        return (
            f"{year}-{first_month:02d}-01",
            f"{year}-{last_month:02d}-{monthrange(year, last_month)[1]:02d}",
        )
    years = [int(value) for value in re.findall(r"20\d{2}", question)]
    if len(years) >= 2 and re.search(r"逐季|季度变化|趋势", question):
        return f"{min(years)}-01-01", f"{max(years)}-12-31"
    return None, None


def _fallback(candidates: dict[str, object], reason: str) -> RuleDecision:
    return RuleDecision(None, candidates, reason)


class RulePlanner:
    """Recognize only reusable query primitives with deterministic semantics."""

    def __init__(self, catalog: SemanticCatalog) -> None:
        self.catalog = catalog

    def plan(self, raw_question: str, state: SessionState) -> RuleDecision:
        question = normalize_question(raw_question)
        has_reference = contains_pronoun_reference(question)

        explicit_orgs = self.catalog.resolve_organizations(question)
        organizations = explicit_orgs
        inherited_orgs = False
        if not organizations and has_reference:
            organizations = state.last_result_organizations or state.last_organizations
            inherited_orgs = bool(organizations)

        explicit_metrics = self.catalog.resolve_metrics(question)
        derived_formula = self.catalog.resolve_derived_metric(question)
        metrics = explicit_metrics
        if derived_formula:
            definition = DERIVED_METRICS[derived_formula]
            metrics = (str(definition["numerator"]), str(definition["denominator"]))
        elif not metrics and has_reference:
            metrics = state.last_metrics

        current_date = resolve_date(question) or (state.last_date if has_reference else None)
        comparison_value = comparison_kind = None
        if current_date:
            comparison_value, comparison_kind = comparison_date(question, current_date)
        start_date, end_date = _period_range(question)
        top_n = _top_n(question)
        threshold = _threshold(question)
        all_scope = bool(
            re.search(r"全省|13家|哪家|哪些|多少家|排名|前\d|后\d|前三|后三|最高|最低|最多|最少", question)
        ) and not explicit_orgs
        organization_scope = "all" if all_scope else "selected"

        candidates: dict[str, object] = {
            "organizations": organizations,
            "explicit_organizations": explicit_orgs,
            "inherited_organizations": inherited_orgs,
            "metrics": metrics,
            "current_date": current_date,
            "comparison_date": comparison_value,
            "comparison_kind": comparison_kind,
            "start_date": start_date,
            "end_date": end_date,
            "derived_formula": derived_formula,
            "top_n": top_n,
            "threshold": (
                {
                    "field": threshold.field,
                    "operator": threshold.operator,
                    "value": threshold.value,
                    "reference": threshold.reference,
                }
                if threshold else None
            ),
        }

        if not metrics:
            return _fallback(candidates, "规则未唯一识别指标，交由LLM结合Schema规划")
        if not current_date and not (start_date and end_date):
            return _fallback(candidates, "规则未解析出完整日期，交由LLM规划")
        if organization_scope == "selected" and not organizations:
            return _fallback(candidates, "规则未识别机构范围，交由LLM规划")

        filters: tuple[PlanFilter, ...] = (threshold,) if threshold else ()
        query_type = "point_query"
        operation = "value"
        expected_shape = "single_row" if len(organizations) == 1 else "multi_row"
        sort_direction = None
        limit = None
        rule_id = "point_query_v1"

        is_trend = bool(re.search(r"逐季|季度变化|趋势", question))
        is_daily_average = "日均" in question
        asks_extrema = bool(re.search(r"最高日|最低日|单日最高|单日最低|哪个季度.*(?:最高|最低)", question))
        asks_joint_condition = bool(re.search(r"同时满足|且.*(?:均值|平均)", question))
        asks_province_average = bool(re.search(r"全省(?:均值|平均)|省均值|平均水平", question))
        asks_count = bool(re.search(r"多少家|有几家|多少天", question))
        asks_arithmetic = bool(
            re.search(r"合计|总额|加起来|相加|分别占|占比|比例|比重|差额|差多少|多多少|高多少|低多少", question)
        )
        asks_profile = bool(re.search(r"评估|综合|盈利能力|主要经营指标|表现较好|表现较差|三个维度|三方面", question))

        if asks_profile:
            return _fallback(candidates, "综合评价需要业务语义组合，交由LLM规划")
        if asks_joint_condition:
            return _fallback(candidates, "多指标联合条件需要组合聚合，交由LLM规划")
        if is_trend:
            if len(metrics) != 1 or not (start_date and end_date) or asks_extrema:
                return _fallback(candidates, "复合趋势分析交由LLM规划")
            query_type, operation, expected_shape, rule_id = (
                "trend", "quarterly_trend", "time_series", "trend_v1"
            )
            current_date = end_date
        elif is_daily_average:
            if len(metrics) != 1 or not (start_date and end_date) or asks_extrema or asks_count:
                return _fallback(candidates, "复合期间统计交由LLM规划")
            query_type, operation, rule_id = "period_average", "daily_average", "period_average_v1"
            current_date = end_date
        elif start_date and end_date and not current_date:
            return _fallback(candidates, "区间聚合语义超出基础规则，交由LLM规划")
        elif derived_formula:
            if len(organizations) != 1:
                return _fallback(candidates, "跨机构派生指标查询交由LLM规划")
            query_type, operation, rule_id = "derived_ratio", "ratio", "derived_ratio_v1"
        elif asks_province_average:
            if asks_count or asks_arithmetic:
                return _fallback(candidates, "全省均值计数或复合比较交由LLM规划")
            query_type, operation, rule_id = (
                "province_average_comparison", "province_average_compare", "province_average_v1"
            )
            expected_shape = "single_row" if len(organizations) == 1 and len(metrics) == 1 else "multi_row"
        elif comparison_value:
            if len(metrics) != 1:
                return _fallback(candidates, "多指标期间比较交由LLM规划")
            if "环比" in question and "同比" in question:
                return _fallback(candidates, "环比与同比联合计算交由LLM规划")
            if top_n or "排名" in question:
                return _fallback(candidates, "期间变化排名交由LLM规划")
            if re.search(r"增幅|增长率|百分之多少", question):
                if metrics[0] in RATIO_METRICS:
                    return _fallback(candidates, "比率指标应计算百分点差，交由LLM确认语义")
                query_type, operation, rule_id = "period_growth", "growth", "period_growth_v1"
            else:
                query_type, operation, rule_id = "period_difference", "difference", "period_difference_v1"
        elif threshold:
            if len(metrics) != 1:
                return _fallback(candidates, "多指标阈值条件交由LLM规划")
            query_type, operation, rule_id = "threshold", "threshold", "threshold_v1"
        elif top_n or re.search(r"排名|排第几|谁.*(?:多|高|低)|最高|最低|最好|最差", question):
            if len(metrics) != 1:
                return _fallback(candidates, "多指标排名交由LLM规划")
            query_type, operation, rule_id = "ranking", "rank", "ranking_v1"
            metric = self.catalog.metrics[metrics[0]]
            sort_direction = metric.sort_direction
            if re.search(r"最高|最多|最大", question):
                sort_direction = "desc"
            if re.search(r"最低|最少", question):
                sort_direction = "asc"
            if "最后" in question or re.search(r"排名后|后三|表现较差|最差", question):
                sort_direction = "desc" if metric.sort_direction == "asc" else "asc"
            limit = top_n or (None if "第几" in question else 1)
            expected_shape = "single_row" if limit == 1 else "multi_row"
        elif asks_arithmetic:
            return _fallback(candidates, "跨指标或跨机构计算交由LLM规划")

        if query_type == "point_query" and len(metrics) > 1:
            expected_shape = "multi_row"
            rule_id = "multi_metric_point_v1"

        if operation not in {"daily_average", "quarterly_trend"}:
            start_date = end_date = None
        plan = QueryPlan(
            source="rule",
            query_type=query_type,
            operation=operation,
            organizations=organizations,
            organization_scope=organization_scope,
            metrics=metrics,
            current_date=current_date,
            comparison_date=comparison_value,
            start_date=start_date,
            end_date=end_date,
            dimensions=("organization",),
            filters=filters,
            sort_direction=sort_direction,
            limit=limit,
            expected_shape=expected_shape,  # type: ignore[arg-type]
            derived_formula=derived_formula,
            rule_id=rule_id,
            confidence=0.97,
            assumptions=tuple(
                filter(
                    None,
                    (
                        comparison_kind,
                        "rank_population_all" if "第几" in question and organization_scope == "selected" else None,
                    ),
                )
            ),
        )
        return RuleDecision(plan, candidates, "基础规则完整命中")
