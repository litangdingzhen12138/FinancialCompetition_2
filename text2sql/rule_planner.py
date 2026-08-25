"""Conservative deterministic planning for simple, unambiguous query families.

Rules deliberately stop at semantic boundaries.  Queries that require composing
multiple calculations, interpreting a business judgement, or choosing a complex
aggregation are handed to the LLM planner instead of being encoded per question.
"""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass, replace
import re

from .context_router import ContextRouter, ORGANIZATION_LIST_FOLLOWUP, PLURAL_REFERENCE
from .date_resolver import comparison_date, resolve_date, year_beginning
from .models import PlanFilter, QueryPlan, SessionState
from .semantic_catalog import (
    COMPOSITION_METRICS,
    DERIVED_METRICS,
    PERFORMANCE_PROFILE_METRICS,
    PROFITABILITY_METRICS,
    RATIO_METRICS,
    RISK_PROFILE_METRICS,
    THREE_DIMENSION_METRICS,
    MAJOR_OPERATING_METRICS,
    METRIC_ALIASES,
    SemanticCatalog,
)


@dataclass(frozen=True, slots=True)
class RuleDecision:
    plan: QueryPlan | None
    candidates: dict[str, object]
    reason: str


def normalize_question(question: str) -> str:
    return re.sub(r"\s+", "", question.strip().replace("？", "?").replace("％", "%"))


def _top_n(question: str) -> int | None:
    match = re.search(r"(?:前|后|靠前|靠后|最高的?|最低的?|排名前|排名后)(\d+)(?:家|名|个)?", question)
    if match:
        return int(match.group(1))
    chinese = {
        "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
        "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
    }
    match = re.search(r"(?:前|后)([一二三四五六七八九十])", question)
    if match:
        return chinese[match.group(1)]
    match = re.search(r"(?:靠前|靠后|最好的?|相对靠后的?)(?:的)?([一二三四五六七八九十])家", question)
    if match:
        return chinese[match.group(1)]
    match = re.search(r"(?:最大|最小|最高|最低)的?([一二三四五六七八九十])家", question)
    if match:
        return chinese[match.group(1)]
    match = re.search(r"(?:最后|最末|倒数)的?([一二三四五六七八九十])家", question)
    if match:
        return chinese[match.group(1)]
    match = re.search(r"(?:最后|最末|倒数)的?(\d+)(?:家|名|个)?", question)
    if match:
        return int(match.group(1))
    if re.search(r"哪家|谁.*(?:最高|最低|最多|最少|最好|最差)|排第一|最后一名", question):
        return 1
    return None


def _threshold(question: str) -> PlanFilter | None:
    match = re.search(
        r"(超过|高于|大于|不低于|至少|低于|小于|不超过|至多|达到)(\d+(?:\.\d+)?)%?",
        question,
    )
    if not match:
        controlled = re.search(r"控制在(\d+(?:\.\d+)?)%?以内", question)
        if controlled:
            return PlanFilter("metric_value", "<=", float(controlled.group(1)))
        match = re.search(r"(?:满足|达到)(\d+(?:\.\d+)?)%?的?(?:最低|监管)?要求", question)
        if not match:
            return None
        return PlanFilter("metric_value", ">=", float(match.group(1)))
    operator = {
        "超过": ">", "高于": ">", "大于": ">", "不低于": ">=", "至少": ">=",
        "低于": "<", "小于": "<", "不超过": "<=", "至多": "<=", "达到": ">=",
    }[match.group(1)]
    return PlanFilter("metric_value", operator, float(match.group(2)))


def _joint_province_average_filters(
    question: str,
    catalog: SemanticCatalog,
) -> tuple[PlanFilter, ...]:
    """Resolve reusable per-metric predicates such as ``A低于省均值且B高于省均值``."""
    operators = {
        "低于": "<", "小于": "<", "不超过": "<=",
        "高于": ">", "超过": ">", "大于": ">", "不低于": ">=",
    }
    resolved: dict[str, PlanFilter] = {}
    for clause in re.split(r"(?:同时满足|并且|且|以及)", question):
        relation = re.search(r"(不低于|不超过|低于|小于|高于|超过|大于).*?(?:全省|省)(?:均值|平均)", clause)
        clause_metrics = catalog.resolve_metrics(clause)
        if not relation or len(clause_metrics) != 1:
            continue
        metric = clause_metrics[0]
        resolved[metric] = PlanFilter(
            field=metric,
            operator=operators[relation.group(1)],
            reference="province_average",
        )
    return tuple(resolved.values())


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


def _focus_metric_for_rank(
    question: str,
    metrics: tuple[str, ...],
    catalog: SemanticCatalog,
) -> tuple[str, ...]:
    """Prefer the metric nearest a singular rank request over background metrics."""
    if len(metrics) <= 1 or not re.search(
        r"第几|排名如何|排名上|最高|最低|前[一二三\d]|后[一二三\d]", question
    ):
        return metrics
    if re.search(r"各自|分别", question):
        return metrics
    rank_position = max(
        (
            position
            for token in ("第几", "排名如何", "排名上", "最高", "最低", "前三", "后三")
            if (position := question.rfind(token)) >= 0
        ),
        default=len(question),
    )
    candidates: list[tuple[int, str]] = []
    for metric_id in metrics:
        aliases = (catalog.metrics[metric_id].name,)
        positions = [question.rfind(alias, 0, rank_position) for alias in aliases]
        position = max(positions, default=-1)
        if position >= 0:
            candidates.append((position, metric_id))
    return (max(candidates)[1],) if candidates else metrics


def _focus_metric_for_threshold(
    question: str,
    metrics: tuple[str, ...],
) -> tuple[str, ...]:
    """Bind a single threshold to the metric mentioned nearest before it."""
    threshold_match = re.search(
        r"(?:超过|高于|大于|不低于|至少|低于|小于|不超过|至多|达到)\d+(?:\.\d+)?%?",
        question,
    )
    if not threshold_match:
        return metrics
    candidates: list[tuple[int, str]] = []
    for metric_id in metrics:
        positions = [
            question.rfind(alias, 0, threshold_match.start())
            for alias in METRIC_ALIASES.get(metric_id, ())
        ]
        position = max(positions, default=-1)
        if position >= 0:
            candidates.append((position, metric_id))
    return (max(candidates)[1],) if candidates else metrics


class RulePlanner:
    """Recognize only reusable query primitives with deterministic semantics."""

    def __init__(self, catalog: SemanticCatalog) -> None:
        self.catalog = catalog
        self.context_router = ContextRouter(catalog)

    def plan(
        self,
        raw_question: str,
        state: SessionState,
        *,
        use_context: bool | None = None,
    ) -> RuleDecision:
        if use_context is None:
            context = self.context_router.classify(raw_question, state)
            if context.needs_history and not context.allow_context_rule:
                return _fallback(
                    {
                        "context_dependency": context.dependency,
                        "history_limit": context.history_limit,
                    },
                    context.reason,
                )
            use_context = context.allow_context_rule
        question = normalize_question(raw_question)

        if use_context and ORGANIZATION_LIST_FOLLOWUP.fullmatch(question):
            previous_plan = state.recent_turns[-1].plan if state.recent_turns else None
            if previous_plan and previous_plan.source == "rule" and previous_plan.operation in {
                "count_condition",
                "condition_members",
                "multi_metric_province_compare",
            }:
                operation = (
                    "condition_members"
                    if previous_plan.operation in {"count_condition", "condition_members"}
                    else previous_plan.operation
                )
                plan = replace(
                    previous_plan,
                    query_type="condition_members",
                    operation=operation,
                    expected_shape="multi_row",
                    allow_empty=True,
                    assumptions=tuple(
                        dict.fromkeys(
                            assumption
                            for assumption in (
                                *previous_plan.assumptions,
                                "organization_list_only",
                                "require_organization_list",
                            )
                            if assumption != "require_organization_count"
                        )
                    ),
                    rule_id="condition_members_followup_v1",
                    confidence=0.99,
                    sql=None,
                )
                return RuleDecision(
                    plan,
                    {
                        "organizations": plan.organizations,
                        "organization_scope": plan.organization_scope,
                        "metrics": plan.metrics,
                        "current_date": plan.current_date,
                        "context_used": True,
                    },
                    "上一轮条件查询的机构名单投影",
                )

        explicit_orgs = self.catalog.resolve_organizations(question)
        population_intent = bool(
            re.search(r"哪家|哪些|多少家|有几家|前\d|后\d|前三|后三|最高的?[一二三\d]|最低的?[一二三\d]", question)
        )

        organizations = explicit_orgs
        inherited_orgs = False
        linked_explicit_org = bool(
            explicit_orgs
            and re.search(
                r"(?:它|该行|这个机构).*(?:和|与|跟)|(?:和|与|跟).*(?:它|该行|这个机构)",
                question,
            )
        )
        if use_context and linked_explicit_org:
            focus = state.last_organizations or state.last_result_organizations
            if len(focus) == 1:
                organizations = tuple(dict.fromkeys((*focus, *explicit_orgs)))
                inherited_orgs = len(organizations) > len(explicit_orgs)
        elif not organizations and use_context:
            organizations = state.last_result_organizations or state.last_organizations
            inherited_orgs = bool(organizations)

        explicit_metrics = self.catalog.resolve_metrics(question)
        derived_formula = self.catalog.resolve_derived_metric(question)
        composition_formula = self.catalog.resolve_composition(question)
        sum_metric_group = self.catalog.resolve_sum_metric_group(question)
        is_profitability_profile = "盈利能力" in question and "收入结构" in question
        is_three_dimension_profile = all(token in question for token in ("规模", "资产质量", "盈利能力"))
        is_major_operating_profile = "主要经营指标" in question and "排名" in question
        is_performance_profile = bool(
            re.search(r"表现较好.*(?:表现)?较差|哪些表现较好.*哪些表现较差", question)
        ) and "主要经营指标" not in question
        is_risk_profile = bool(
            re.search(r"整体(?:风控|风险)画像|(?:风控|风险)表现最优|综合来看.*(?:风控|风险)", question)
        )
        metrics = _focus_metric_for_rank(question, explicit_metrics, self.catalog)
        if composition_formula:
            definition = COMPOSITION_METRICS[composition_formula]
            metrics = (*definition["components"], str(definition["denominator"]))
        elif sum_metric_group:
            metrics = sum_metric_group
        elif derived_formula:
            definition = DERIVED_METRICS[derived_formula]
            metrics = (str(definition["numerator"]), str(definition["denominator"]))
        elif is_profitability_profile:
            metrics = PROFITABILITY_METRICS
        elif is_three_dimension_profile:
            metrics = THREE_DIMENSION_METRICS
        elif is_major_operating_profile:
            metrics = MAJOR_OPERATING_METRICS
        elif is_performance_profile and not metrics:
            metrics = PERFORMANCE_PROFILE_METRICS
        elif is_risk_profile:
            metrics = RISK_PROFILE_METRICS
        elif not metrics and use_context:
            metrics = state.last_metrics

        reference_date = state.last_date if use_context else None
        current_date = resolve_date(question, reference_date) or reference_date
        comparison_value = comparison_kind = None
        if current_date:
            comparison_value, comparison_kind = comparison_date(
                question,
                current_date,
                reference_date,
            )
        if (
            use_context
            and
            not comparison_value
            and state.last_comparison_date
            and re.search(r"这个增量|同期变化", question)
        ):
            comparison_value = state.last_comparison_date
            comparison_kind = "inherited_comparison_period"
        start_date, end_date = _period_range(question)
        top_n = _top_n(question)
        threshold = _threshold(question)
        if (
            threshold
            and len(metrics) > 1
            and not is_risk_profile
            and len(re.findall(r"\d+(?:\.\d+)?%", question)) <= 1
        ):
            metrics = _focus_metric_for_threshold(question, metrics)
        joint_average_filters = _joint_province_average_filters(question, self.catalog)
        asks_population = bool(
            re.search(r"哪家|哪些|多少家|前\d|后\d|前三|后三|靠前|靠后|最高的?[三二一\d]|最低的?[三二一\d]", question)
        )
        asks_selected_rank = bool(organizations) and bool(
            re.search(r"第几|排名如何|排名中|排名上", question)
        )
        previous_plan = state.recent_turns[-1].plan if use_context and state.recent_turns else None
        previous_selected_group = bool(
            previous_plan
            and previous_plan.organization_scope == "selected"
            and len(previous_plan.organizations) >= 2
        )
        bounded_candidate_scope = bool(
            len(organizations) >= 2
            and (
                len(explicit_orgs) >= 2
                or linked_explicit_org
                or (
                    inherited_orgs
                    and (PLURAL_REFERENCE.search(question) or previous_selected_group)
                )
            )
        )
        all_scope = (asks_population and not bounded_candidate_scope) or (
            bool(re.search(r"全省|13家", question))
            and not organizations
            and not asks_selected_rank
        )
        organization_scope = "all" if all_scope else "selected"

        candidates: dict[str, object] = {
            "organizations": organizations,
            "organization_scope": organization_scope,
            "explicit_organizations": explicit_orgs,
            "inherited_organizations": inherited_orgs,
            "context_used": use_context,
            "metrics": metrics,
            "current_date": current_date,
            "comparison_date": comparison_value,
            "comparison_kind": comparison_kind,
            "start_date": start_date,
            "end_date": end_date,
            "derived_formula": derived_formula,
            "composition_formula": composition_formula,
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
            "joint_average_filters": tuple(
                {"metric": item.field, "operator": item.operator, "reference": item.reference}
                for item in joint_average_filters
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

        is_trend = bool(
            re.search(r"逐季(?:是|变|列|展示|情况|数据|多少)|按季|季度(?:序列|趋势|变化)|趋势", question)
        )
        is_daily_average = "日均" in question
        asks_extrema = bool(
            re.search(r"最高日|最低日|单日最高|单日最低|哪个季度.*(?:最高|最低)|最高.*最低|最低.*最高", question)
        )
        asks_joint_condition = bool(re.search(r"同时满足|且.*(?:均值|平均)", question))
        asks_province_average = bool(re.search(r"全省(?:均值|平均)|省均值|平均水平", question))
        asks_count = bool(re.search(r"多少家|有几家|多少天", question))
        asks_organization_list = bool(
            re.search(r"哪几家|哪些机构|分别.*(?:哪家|谁)|都是(?:哪|那)几家", question)
        )
        asks_sum = bool(re.search(r"合计|总额|加起来|相加|总和", question))
        asks_difference = bool(
            re.search(r"差多少|相差|多多少|少多少|高多少|低多少|回落.*多少|减少.*多少|比.*(?:多|少|高|低).*多少", question)
        )
        asks_mean_rank_extremes = bool(
            start_date and end_date and re.search(r"均值|平均", question)
            and re.search(r"前(?:三|\d).*(?:后|最后)|后(?:三|\d).*(?:前|最前)", question)
        )
        asks_arithmetic = bool(
            re.search(r"合计|总额|加起来|相加|分别占|占比|比例|比重|差额|差多少|多多少|高多少|低多少", question)
        )
        asks_profile = bool(
            re.search(
                r"评估|综合|画像|一句话总结|风控如何|盈利能力|主要经营指标|"
                r"表现较好|表现较差|最优的?之一|三个维度|三方面",
                question,
            )
        )
        asks_reconciliation = bool(
            re.search(r"(?:是否|是不是)?等于|差额", question)
            and {"ZB001", "ZB003", "ZB004"}.issubset(metrics)
        )
        asks_npl_reconciliation = bool(
            re.search(r"(?:是否|是不是)?等于|除以", question)
            and {"ZB002", "ZB013", "ZB014"}.issubset(metrics)
        )
        asks_period_values = bool(
            comparison_value
            and re.search(r"分别(?:是|为)?多少|分别是多少|各(?:是|为)?多少", question)
            and not re.search(r"变化|变动|增长|增幅|增量|增加|下降|减少|回落|相差|相比|较", question)
        )
        asks_pairwise_comparison = bool(
            len(organizations) == 2
            and len(metrics) == 1
            and re.search(
                r"(?:谁|哪家|哪个|那个).*(?:更高|更多|更低|更少|高|低)|"
                r"(?:和|与|跟).*(?:比).*(?:高|低)",
                question,
            )
        )
        allow_empty = False

        if is_risk_profile:
            query_type, operation, expected_shape, rule_id = (
                "risk_profile", "profile", "multi_row", "risk_profile_v1"
            )
        elif is_profitability_profile:
            query_type, operation, expected_shape, rule_id = (
                "profitability_profile", "profile", "multi_row", "profitability_profile_v1"
            )
            comparison_value = comparison_value or year_beginning(current_date)
        elif is_major_operating_profile:
            query_type, operation, expected_shape, rule_id = (
                "major_operating_profile", "profile", "multi_row", "major_operating_profile_v1"
            )
            comparison_value = year_beginning(current_date)
        elif is_three_dimension_profile:
            query_type, operation, expected_shape, rule_id = (
                "three_dimension_profile", "three_dimension_profile", "multi_row", "three_dimension_profile_v1"
            )
        elif is_performance_profile:
            query_type, operation, expected_shape, rule_id = (
                "performance_profile", "profile", "multi_row", "performance_profile_v1"
            )
        elif asks_profile and len(metrics) > 1 and "排名" in question:
            query_type, operation, expected_shape, rule_id = (
                "multi_rank", "multi_rank", "multi_row", "multi_rank_v1"
            )
        elif asks_profile and not (
            comparison_value or derived_formula or threshold or re.search(r"排名|第几", question)
        ):
            return _fallback(candidates, "综合评价需要业务语义组合，交由LLM规划")
        elif asks_joint_condition:
            if (
                asks_province_average
                and len(metrics) >= 2
                and {item.field for item in joint_average_filters} == set(metrics)
            ):
                filters = joint_average_filters
                query_type, operation, expected_shape, rule_id = (
                    "joint_province_average", "multi_metric_province_compare", "multi_row",
                    "multi_metric_province_compare_v1",
                )
            else:
                return _fallback(candidates, "多指标联合条件未能完整绑定指标与比较方向，交由LLM规划")
        elif is_trend:
            if len(metrics) != 1 or not (start_date and end_date):
                return _fallback(candidates, "复合趋势分析交由LLM规划")
            query_type, operation, expected_shape, rule_id = (
                "trend", "quarterly_trend", "time_series", "trend_v1"
            )
            current_date = end_date
        elif asks_mean_rank_extremes and len(metrics) == 1 and organization_scope == "all":
            query_type, operation, expected_shape, rule_id = (
                "period_rank_extremes", "period_rank_extremes", "multi_row", "period_rank_extremes_v1"
            )
            sort_direction = self.catalog.metrics[metrics[0]].sort_direction
            limit = top_n or 3
        elif asks_extrema and start_date and end_date and organization_scope == "all":
            query_type, operation, expected_shape, rule_id = (
                "period_extrema", "period_extrema", "multi_row", "period_extrema_v1"
            )
        elif is_daily_average:
            if len(metrics) != 1 or not (start_date and end_date) or asks_count:
                return _fallback(candidates, "复合期间统计交由LLM规划")
            query_type, operation, rule_id = "period_average", "daily_average", "period_average_v1"
            current_date = end_date
        elif (
            start_date and end_date and len(metrics) == 1 and len(organizations) == 1
            and asks_count and asks_province_average
        ):
            average_operator = ">" if re.search(r"高于|超过|大于", question) else "<"
            filters = (PlanFilter("metric_value", average_operator, reference="province_average"),)
            query_type, operation, expected_shape, rule_id = (
                "period_count_vs_average", "count_vs_average", "single_row", "count_vs_average_v1"
            )
        elif start_date and end_date and not current_date:
            return _fallback(candidates, "区间聚合语义超出基础规则，交由LLM规划")
        elif composition_formula:
            query_type, operation, expected_shape, rule_id = (
                "composition", "composition", "multi_row", "composition_v1"
            )
        elif asks_reconciliation or asks_npl_reconciliation:
            query_type, operation, expected_shape, rule_id = (
                "metric_reconciliation",
                "reconcile",
                "single_row" if len(organizations) == 1 else "multi_row",
                "reconcile_v1",
            )
        elif derived_formula and asks_difference and start_date and end_date:
            return _fallback(candidates, "复合期间差值与派生比率交由LLM规划")
        elif derived_formula:
            if len(organizations) != 1:
                return _fallback(candidates, "跨机构派生指标查询交由LLM规划")
            query_type, operation, rule_id = "derived_ratio", "ratio", "derived_ratio_v1"
        elif asks_province_average:
            if asks_count:
                average_operator = ">" if re.search(r"高于|超过|大于", question) else "<"
                filters = (PlanFilter("metric_value", average_operator, reference="province_average"),)
                query_type, operation, expected_shape, rule_id = (
                    "point_count_vs_average", "count_vs_average", "single_row", "count_vs_average_v1"
                )
            else:
                query_type, operation, rule_id = (
                    "province_average_comparison", "province_average_compare", "province_average_v1"
                )
                expected_shape = "single_row" if len(organizations) == 1 and len(metrics) == 1 else "multi_row"
        elif asks_period_values:
            query_type, operation, expected_shape, rule_id = (
                "period_values", "period_values", "multi_row", "period_values_v1"
            )
        elif comparison_value:
            if re.search(r"排名变化", question) and organization_scope == "selected":
                query_type, operation, expected_shape, rule_id = (
                    "metric_rank_change", "multi_rank_change", "multi_row", "metric_rank_change_v1"
                )
            elif len(metrics) != 1:
                if "排名" in question and re.search(r"变化|变动|怎么变", question):
                    query_type, operation, expected_shape, rule_id = (
                        "multi_rank_change", "multi_rank_change", "multi_row", "multi_rank_change_v1"
                    )
                elif re.search(r"变动方向|分别.*(?:上升|下降)|方向", question):
                    query_type, operation, expected_shape, rule_id = (
                        "multi_period_change", "multi_period_change", "multi_row", "multi_period_change_v1"
                    )
                else:
                    return _fallback(candidates, "多指标期间比较交由LLM规划")
            elif "环比" in question and "同比" in question:
                query_type, operation, expected_shape, rule_id = (
                    "mom_yoy", "mom_yoy", "single_row", "mom_yoy_v1"
                )
            elif top_n or re.search(r"排名|排第几|位列第几", question):
                query_type, operation, expected_shape, rule_id = (
                    "period_change_rank", "period_change_rank", "multi_row", "period_change_rank_v1"
                )
                sort_direction = "desc"
                limit = top_n or (None if organization_scope == "selected" else 3)
            elif re.search(r"增幅|增长率|百分之多少", question):
                if metrics[0] in RATIO_METRICS:
                    return _fallback(candidates, "比率指标应计算百分点差，交由LLM确认语义")
                query_type, operation, rule_id = "period_growth", "growth", "period_growth_v1"
            else:
                query_type, operation, rule_id = "period_difference", "difference", "period_difference_v1"
        elif threshold and asks_selected_rank:
            if len(metrics) != 1:
                return _fallback(candidates, "多指标排名与阈值组合交由LLM规划")
            query_type, operation, expected_shape, rule_id = (
                "rank_threshold", "rank_threshold", "multi_row", "rank_threshold_v1"
            )
            sort_direction = self.catalog.metrics[metrics[0]].sort_direction
        elif threshold:
            if len(metrics) != 1:
                return _fallback(candidates, "多指标阈值条件交由LLM规划")
            if asks_count and asks_organization_list:
                query_type, operation, expected_shape, rule_id = (
                    "condition_members", "condition_members", "multi_row", "condition_members_v1"
                )
                allow_empty = True
            elif asks_count:
                query_type, operation, expected_shape, rule_id = (
                    "threshold_count", "count_condition", "single_row", "threshold_count_v1"
                )
            else:
                query_type, operation, rule_id = "threshold", "threshold", "threshold_v1"
        elif asks_pairwise_comparison:
            query_type, operation, expected_shape, rule_id = (
                (
                    "pairwise_difference" if asks_difference else "pairwise_comparison"
                ),
                "value",
                "multi_row",
                "pairwise_comparison_v1",
            )
        elif asks_difference and (
            (len(organizations) == 2 and len(metrics) == 1)
            or (len(organizations) == 1 and len(metrics) == 2)
        ):
            query_type, operation, expected_shape, rule_id = (
                "cross_difference", "cross_difference", "multi_row", "cross_difference_v1"
            )
        elif (
            len(metrics) > 1 and "排名" in question and organization_scope == "selected"
        ):
            query_type, operation, expected_shape, rule_id = (
                "multi_rank", "multi_rank", "multi_row", "multi_rank_v1"
            )
        elif top_n or re.search(r"第几|排名|排第几|谁.*(?:多|高|低)|最高|最低|最好|最差|靠前|靠后", question):
            if len(metrics) != 1:
                return _fallback(candidates, "多指标排名交由LLM规划")
            query_type, operation, rule_id = "ranking", "rank", "ranking_v1"
            metric = self.catalog.metrics[metrics[0]]
            sort_direction = metric.sort_direction
            limit = top_n or (None if organization_scope == "selected" else 1)
            expected_shape = "multi_row" if organization_scope == "all" else (
                "single_row" if limit == 1 else "multi_row"
            )
        elif asks_sum and (
            len(metrics) > 1 or len(organizations) > 1
        ) and len({self.catalog.metrics[metric].unit for metric in metrics}) == 1:
            query_type, operation, expected_shape, rule_id = (
                "multi_metric_sum", "sum", "multi_row", "sum_v1"
            )
        elif asks_arithmetic:
            return _fallback(candidates, "跨指标或跨机构计算交由LLM规划")

        if query_type == "point_query" and len(metrics) > 1:
            expected_shape = "multi_row"
            rule_id = "multi_metric_point_v1"

        if operation not in {
            "daily_average", "quarterly_trend", "count_vs_average",
            "period_rank_extremes", "period_extrema",
        }:
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
            allow_empty=allow_empty,
            derived_formula=derived_formula,
            rule_id=rule_id,
            confidence=0.97,
            assumptions=tuple(
                filter(
                    None,
                    (
                        comparison_kind,
                        "pairwise_comparison"
                        if query_type in {"pairwise_comparison", "pairwise_difference"}
                        else None,
                        "pairwise_difference" if query_type == "pairwise_difference" else None,
                        "pairwise_lower"
                        if query_type in {"pairwise_comparison", "pairwise_difference"}
                        and re.search(r"更低|更少|哪个低|那个低|谁低|哪家.*低", question)
                        else None,
                        "include_extrema" if is_trend and asks_extrema else None,
                        "include_extrema" if is_daily_average and asks_extrema else None,
                        composition_formula,
                        "profitability_profile" if is_profitability_profile else None,
                        "major_operating_profile" if is_major_operating_profile else None,
                        "performance_profile" if is_performance_profile else None,
                        "performance_profile" if is_risk_profile else None,
                        "risk_profile" if is_risk_profile else None,
                        "rank_population_all" if "第几" in question and organization_scope == "selected" else None,
                        "rank_population_all"
                        if operation == "rank"
                        and organization_scope == "selected"
                        and len(organizations) == 1
                        else None,
                        "rank_population_all" if operation == "multi_rank" else None,
                        "rank_population_all" if operation == "multi_rank_change" else None,
                        "rank_population_all" if operation == "rank_threshold" else None,
                        "rank_decrease" if operation == "period_change_rank" and "下降" in question else None,
                        "rank_absolute_change" if operation == "period_change_rank" and "增量" in question else None,
                        "select_highest_value" if operation == "rank" and re.search(r"最高|最多|最大", question) else None,
                        "select_lowest_value" if operation == "rank" and re.search(r"最低|最少", question) else None,
                        "select_bottom_rank" if operation == "rank" and (
                            "最后" in question or re.search(r"排名后|后三|表现较差|最差|靠后", question)
                        ) else None,
                        "needs_summary" if asks_profile else None,
                        "regulatory_check" if "达标" in question or "监管" in question else None,
                        "require_organization_count" if asks_count and operation == "multi_metric_province_compare" else None,
                        "require_organization_count" if asks_count and operation == "condition_members" else None,
                        "require_organization_list" if asks_organization_list else None,
                        "npl_reconciliation" if asks_npl_reconciliation else None,
                    ),
                )
            ),
        )
        return RuleDecision(plan, candidates, "基础规则完整命中")
