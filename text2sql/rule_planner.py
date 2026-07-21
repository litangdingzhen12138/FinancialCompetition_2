"""High-confidence deterministic parsing for the competition's common query families."""

from __future__ import annotations

from dataclasses import dataclass
import re

from .date_resolver import comparison_date, resolve_date
from .models import PlanFilter, QueryPlan, SessionState
from .semantic_catalog import DERIVED_METRICS, RATIO_METRICS, SemanticCatalog, contains_pronoun_reference


@dataclass(frozen=True, slots=True)
class RuleDecision:
    plan: QueryPlan | None
    candidates: dict[str, object]
    reason: str


def normalize_question(question: str) -> str:
    return re.sub(r"\s+", "", question.strip().replace("？", "?").replace("％", "%"))


def _top_n(question: str) -> int | None:
    match = re.search(r"(?:前|后|最高的?|最低的?|排名前|排名后)\s*(\d+)\s*(?:家|名|个)?", question)
    if match:
        return int(match.group(1))
    chinese = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    match = re.search(r"(?:前|后)([一二三四五六七八九十])", question)
    if match:
        return chinese[match.group(1)]
    if re.search(r"哪家|谁.*(?:最高|最低|最多|最少|最好|最差)|排第一|最后一名", question):
        return 1
    return None


def _threshold(question: str) -> PlanFilter | None:
    match = re.search(r"(超过|高于|大于|不低于|至少|低于|小于|不超过|至多)\s*(\d+(?:\.\d+)?)\s*(%)?", question)
    if not match:
        return None
    operator = {
        "超过": ">", "高于": ">", "大于": ">", "不低于": ">=", "至少": ">=",
        "低于": "<", "小于": "<", "不超过": "<=", "至多": "<=",
    }[match.group(1)]
    return PlanFilter("metric_value", operator, float(match.group(2)))


def _year_range(question: str) -> tuple[str | None, str | None]:
    match = re.search(r"(20\d{2})年全年", question)
    if match:
        year = match.group(1)
        return f"{year}-01-01", f"{year}-12-31"
    years = [int(value) for value in re.findall(r"20\d{2}", question)]
    if len(years) >= 2 and re.search(r"逐季|季度变化|趋势", question):
        return f"{min(years)}-01-01", f"{max(years)}-12-31"
    return None, None


class RulePlanner:
    def __init__(self, catalog: SemanticCatalog) -> None:
        self.catalog = catalog

    def plan(self, raw_question: str, state: SessionState) -> RuleDecision:
        question = normalize_question(raw_question)
        explicit_orgs = self.catalog.resolve_organizations(question)
        organizations = explicit_orgs
        inherited_orgs = False
        if not organizations and contains_pronoun_reference(question):
            organizations = state.last_result_organizations or state.last_organizations
            inherited_orgs = bool(organizations)

        explicit_metrics = self.catalog.resolve_metrics(question)
        derived_formula = self.catalog.resolve_derived_metric(question)
        metrics = explicit_metrics
        if derived_formula:
            definition = DERIVED_METRICS[derived_formula]
            metrics = (str(definition["numerator"]), str(definition["denominator"]))
        elif not metrics and contains_pronoun_reference(question):
            metrics = state.last_metrics

        current_date = resolve_date(question) or (state.last_date if contains_pronoun_reference(question) else None)
        comp_date = comp_kind = None
        if current_date:
            comp_date, comp_kind = comparison_date(question, current_date)
        start_date, end_date = _year_range(question)
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
            "comparison_date": comp_date,
            "comparison_kind": comp_kind,
            "derived_formula": derived_formula,
            "top_n": top_n,
            "threshold": (
                {
                    "field": threshold.field,
                    "operator": threshold.operator,
                    "value": threshold.value,
                    "reference": threshold.reference,
                }
                if threshold
                else None
            ),
        }

        if not metrics:
            return RuleDecision(None, candidates, "未唯一识别指标")
        if not current_date and not (start_date and end_date):
            return RuleDecision(None, candidates, "缺少可解析日期")
        if organization_scope == "selected" and not organizations:
            return RuleDecision(None, candidates, "缺少机构范围")

        filters: tuple[PlanFilter, ...] = (threshold,) if threshold else ()
        query_type = "point_query"
        operation = "value"
        expected_shape = "single_row" if len(organizations) == 1 else "multi_row"
        sort_direction = None
        limit = None
        confidence = 0.97
        rule_id = "point_query_v1"

        if re.search(r"逐季|季度变化|趋势", question):
            query_type, operation, expected_shape, rule_id = "trend", "quarterly_trend", "time_series", "trend_v1"
            current_date = end_date
        elif "日均" in question:
            query_type, operation, rule_id = "period_average", "daily_average", "period_average_v1"
            current_date = end_date
        elif derived_formula:
            query_type, operation, rule_id = "derived_ratio", "ratio", "derived_ratio_v1"
        elif re.search(r"同时满足|且.*(?:均值|平均)", question) and len(metrics) >= 2:
            return RuleDecision(None, candidates, "多指标联合条件交由LLM规划")
        elif re.search(r"全省(?:均值|平均)|省均值|平均水平", question):
            query_type, operation, rule_id = "province_average_comparison", "province_average_compare", "province_average_v1"
            expected_shape = "single_row" if len(organizations) == 1 else "multi_row"
        elif comp_date:
            if "环比" in question and "同比" in question:
                return RuleDecision(None, candidates, "同时计算环比和同比交由LLM规划")
            if re.search(r"增幅|增长率", question):
                if len(metrics) != 1 or metrics[0] in RATIO_METRICS:
                    return RuleDecision(None, candidates, "比率指标或多指标增幅交由LLM规划")
                query_type, operation, rule_id = "period_growth", "growth", "period_growth_v1"
            else:
                query_type, operation, rule_id = "period_difference", "difference", "period_difference_v1"
        elif threshold:
            query_type, operation, rule_id = "threshold", "threshold", "threshold_v1"
        elif top_n or re.search(r"排名|排第几|谁.*(?:多|高|低)|最高|最低|最好|最差", question):
            if len(metrics) != 1:
                return RuleDecision(None, candidates, "排名问题包含多个指标")
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

        if query_type == "point_query" and len(metrics) > 1:
            return RuleDecision(None, candidates, "普通点查包含多个指标")
        plan = QueryPlan(
            source="rule",
            query_type=query_type,
            operation=operation,
            organizations=organizations,
            organization_scope=organization_scope,
            metrics=metrics,
            current_date=current_date,
            comparison_date=comp_date,
            start_date=start_date,
            end_date=end_date,
            dimensions=("organization",),
            filters=filters,
            sort_direction=sort_direction,
            limit=limit,
            expected_shape=expected_shape,  # type: ignore[arg-type]
            derived_formula=derived_formula,
            rule_id=rule_id,
            confidence=confidence,
            assumptions=tuple(
                filter(
                    None,
                    (
                        comp_kind,
                        "rank_population_all" if "第几" in question and organization_scope == "selected" else None,
                    ),
                )
            ),
        )
        return RuleDecision(plan, candidates, "规则完整命中")
