"""Format answers strictly from executed query results.

The rule path has a few stable renderers.  LLM queries use a schema-agnostic
renderer so new SQL shapes do not require adding a question-specific branch.
"""

from __future__ import annotations

import re

from .models import QueryPlan, QueryResult, TurnMemory
from .semantic_catalog import LOWER_IS_BETTER, RATIO_METRICS, SemanticCatalog


def _number(value: object, digits: int = 2) -> str:
    if isinstance(value, bool):
        return "是" if value else "否"
    if not isinstance(value, (int, float)):
        return str(value)
    rounded = round(float(value), digits)
    if rounded.is_integer():
        return str(int(rounded))
    return f"{rounded:.{digits}f}".rstrip("0").rstrip(".")


def _value(value: object, unit: str) -> str:
    return f"{_number(value)}{unit}"


def _difference_number(value: float) -> str:
    """Keep small non-zero gaps visible instead of rendering them as zero."""
    return _number(value, 4 if value and round(abs(value), 2) == 0 else 2)


def _value_requested_before_change(question: str) -> bool:
    """Whether the user asks for the current value before asking for its change."""
    clauses = [
        clause
        for clause in re.split(r"[？?；;。！!，,]", re.sub(r"\s+", "", question))
        if clause
    ]
    saw_value = False
    for clause in clauses:
        asks_change = bool(
            re.search(
                r"(?:相比|比|较|变化|变动|增长|增加|下降|减少|回落|上升|升高|降低)"
                r".*(?:多少|如何|怎么样|怎么变)",
                clause,
            )
        )
        if asks_change:
            return saw_value
        if re.search(r"(?:是多少|有多少|为多少|多少)$", clause):
            saw_value = True
    return False


_COLUMN_LABELS = {
    "metric_id": "指标编码",
    "metric_name": "指标",
    "metric_value": "指标值",
    "current_value": "当前值",
    "comparison_value": "比较期值",
    "change_value": "变动值",
    "growth_rate": "增幅",
    "derived_value": "计算值",
    "average_value": "平均值",
    "avg_value": "平均值",
    "avg_val": "平均值",
    "org_value": "本机构值",
    "self_value": "本机构值",
    "province_average": "全省均值",
    "province_avg": "全省均值",
    "difference_from_average": "与全省均值之差",
    "difference": "差值",
    "sum_value": "合计",
    "total_value": "合计",
    "count": "数量",
    "organization_count": "机构数",
    "matching_days": "符合天数",
    "mom_change": "环比变动",
    "yoy_change": "同比变动",
    "previous_rank": "原排名",
    "current_rank": "当前排名",
    "metric_rank": "排名",
    "rank_change": "排名变化",
    "rank_type": "排名分组",
    "matching_percentage": "符合占比",
    "maximum_value": "最高值",
    "minimum_value": "最低值",
}

_REQUIRED_COLUMNS_BY_OPERATION = {
    "value": {"org_name", "metric_value"},
    "rank": {"org_name", "metric_value", "metric_rank"},
    "difference": {"org_name", "current_value", "comparison_value", "change_value"},
    "growth": {"org_name", "current_value", "comparison_value", "growth_rate"},
    "ratio": {"org_name", "derived_value"},
    "province_average_compare": {
        "org_name", "metric_value", "province_average", "difference_from_average"
    },
    "threshold": {"org_name", "metric_value", "threshold_met"},
    "daily_average": {"org_name", "average_value"},
    "quarterly_trend": {"org_name", "data_date", "metric_value"},
    "sum": {"metric_name", "metric_value", "total_value"},
    "composition": {"metric_name", "derived_value"},
    "mom_yoy": {"org_name", "current_value", "mom_change", "yoy_change"},
    "count_vs_average": {"matching_organizations", "total_organizations"},
    "count_condition": {"matching_organizations", "total_organizations"},
    "profile": {"org_name", "metric_id", "metric_name", "metric_value", "metric_rank"},
    "cross_difference": {"metric_name", "metric_value", "difference_value"},
    "period_rank_extremes": {"org_name", "average_value", "rank_type", "metric_rank"},
    "period_extrema": {"org_name", "data_date", "metric_value", "extrema_type"},
    "multi_period_change": {"metric_name", "comparison_value", "current_value", "change_value"},
    "multi_rank_change": {
        "metric_name", "comparison_value", "current_value", "previous_rank", "current_rank"
    },
    "period_change_rank": {"org_name", "result_value", "result_rank"},
    "multi_rank": {"metric_name", "metric_value", "metric_rank"},
    "three_dimension_profile": {"metric_id", "metric_name", "metric_value", "metric_rank"},
    "reconcile": {"corporate_value", "personal_value", "total_value", "difference_value", "is_equal"},
    "multi_metric_province_compare": {
        "org_name", "metric_id", "metric_name", "metric_value", "province_average"
    },
}
_RULE_ANSWER_OPERATIONS = frozenset(_REQUIRED_COLUMNS_BY_OPERATION)


def has_deterministic_answer(plan: QueryPlan, result: QueryResult) -> bool:
    """Return whether an explicit answer template covers this executed result."""
    rows = result.dictionaries()
    if not rows or plan.operation not in _RULE_ANSWER_OPERATIONS:
        return False
    required_columns = _REQUIRED_COLUMNS_BY_OPERATION[plan.operation]
    if plan.operation == "count_vs_average" and plan.start_date:
        required_columns = {"matching_days", "total_days", "matching_percentage"}
    return all(required_columns.issubset(row) for row in rows)


def _generic_answer(
    plan: QueryPlan,
    rows: list[dict[str, object]],
    catalog: SemanticCatalog,
) -> str:
    """Render arbitrary read-only result shapes without interpreting new semantics."""
    if len(rows) > 1 and all(
        row.get("metric_name") is not None and row.get("result_value") is not None
        for row in rows
    ):
        pieces = []
        for row in rows:
            row_unit = str(row.get("unit") or "")
            value = row["result_value"]
            digits = (
                4
                if row_unit == "%" and isinstance(value, (int, float)) and 0 < abs(float(value)) < 0.1
                else 2
            )
            pieces.append(f"{row['metric_name']}为{_number(value, digits)}{row_unit}")
        fallback_org = catalog.organizations.get(plan.organizations[0], "") if len(plan.organizations) == 1 else ""
        return f"{fallback_org}：{'；'.join(pieces)}"
    if rows and all(
        "metric_name" in row and any(key.lower() == "condition_met" for key in row)
        for row in rows
    ):
        pieces: list[str] = []
        statuses: list[bool] = []
        for row in rows:
            metric_id = str(row.get("metric_id") or "")
            value = row.get("metric_value", row.get("result_value"))
            row_unit = str(row.get("unit") or (
                catalog.metrics[metric_id].unit if metric_id in catalog.metrics else ""
            ))
            average = next(
                (
                    row.get(key)
                    for key in ("province_average", "province_avg", "average_value")
                    if row.get(key) is not None
                ),
                None,
            )
            text = f"{row['metric_name']}{_value(value, row_unit)}"
            if average is not None and value is not None:
                relation = "低于" if float(value) < float(average) else "高于"
                text += f"（{relation}全省均值{_value(average, row_unit)}）"
            raw_status = next(value for key, value in row.items() if key.lower() == "condition_met")
            met = raw_status is True or str(raw_status).strip().lower() in {
                "true", "1", "是", "满足", "达标",
            }
            statuses.append(met)
            pieces.append(f"{text}，{'满足' if met else '不满足'}条件")
        fallback_org = catalog.organizations.get(plan.organizations[0], "") if len(plan.organizations) == 1 else ""
        conclusion = "同时满足全部条件" if all(statuses) else "未同时满足全部条件"
        return f"{fallback_org}：{'；'.join(pieces)}；综合判定：{conclusion}"
    all_condition_aliases = {"meetsallconditions", "allconditionsmet"}
    if len(rows) == 1 and any(
        key.lower().replace("_", "") in all_condition_aliases for key in rows[0]
    ):
        normalized = {key.lower().replace("_", ""): value for key, value in rows[0].items()}

        column_aliases = {
            "ZB013": ("zb013", "npl", "nonperformingloan", "buliang"),
            "ZB015": ("zb015", "provisioncoverage", "coverage", "bobei"),
            "ZB016": ("zb016", "capitaladequacy", "capital", "ziben"),
            "ZB012": ("zb012", "costincome", "cir", "chengben"),
        }

        def metric_value(metric_id: str, suffix: str = "value") -> object | None:
            exact = normalized.get(f"{metric_id.lower()}{suffix.lower()}")
            if exact is not None:
                return exact
            aliases = column_aliases[metric_id]
            for key, value in normalized.items():
                if not any(alias in key for alias in aliases):
                    continue
                is_average = any(token in key for token in ("provinceavg", "provinceaverage", "average", "avg"))
                if suffix == "value" and not is_average and (
                    key in aliases or key.endswith(("value", "rate", "ratio"))
                ):
                    return value
                if suffix == "provinceavg" and is_average:
                    return value
            return None

        pieces: list[str] = []
        for metric_id, label in (
            ("ZB013", "不良贷款率"),
            ("ZB015", "拨备覆盖率"),
            ("ZB016", "资本充足率"),
            ("ZB012", "成本收入比"),
        ):
            value = metric_value(metric_id)
            if value is None:
                continue
            text = f"{label}{_value(value, '%')}"
            province_average = metric_value(metric_id, "provinceavg")
            if province_average is not None:
                relation = "低于" if float(value) < float(province_average) else "高于"
                text += f"（{relation}全省均值{_value(province_average, '%')}）"
            pieces.append(text)
        raw_status = next(
            value
            for key, value in rows[0].items()
            if key.lower().replace("_", "") in all_condition_aliases
        )
        meets = raw_status is True or str(raw_status).strip().lower() in {"true", "1", "是", "满足", "达标"}
        org_name = str(rows[0].get("org_name") or (
            catalog.organizations.get(plan.organizations[0], "") if len(plan.organizations) == 1 else ""
        ))
        details = "，".join(pieces)
        conclusion = "同时满足全部四项条件" if meets else "未同时满足全部四项条件"
        if details:
            return f"{org_name}：{details}；综合判定：{conclusion}"
    if "rank_population_all" in plan.assumptions:
        column_names = set(rows[0]) if rows else set()
        if {"metric_name", "current_rank", "previous_rank"}.issubset(column_names):
            pieces: list[str] = []
            for row in rows:
                previous_rank = int(row["previous_rank"])
                current_rank = int(row["current_rank"])
                change = previous_rank - current_rank
                movement = f"提升{change}名" if change > 0 else f"下降{abs(change)}名" if change < 0 else "不变"
                pieces.append(
                    f"{row['metric_name']}：第{previous_rank}名→第{current_rank}名，排名{movement}"
                )
            return "；".join(pieces)
        if {"metric_name", "metric_value", "metric_rank"}.issubset(column_names):
            return "；".join(
                f"{row['metric_name']}：{_value(row['metric_value'], str(row.get('unit') or ''))}，"
                f"全省第{_number(row['metric_rank'], 0)}名"
                for row in rows
            )
    fallback_org = catalog.organizations.get(plan.organizations[0], "") if len(plan.organizations) == 1 else ""
    fallback_unit = catalog.unit_for_plan(plan.metrics, plan.derived_formula)
    answers: list[str] = []
    for row in rows:
        org_name = str(row.get("org_name") or fallback_org)
        row_unit = str(row.get("unit") or fallback_unit) if len(plan.metrics) == 1 else ""
        normalized_row = {key.lower(): value for key, value in row.items()}
        named_units = {
            key.removesuffix("_unit"): str(value)
            for key, value in normalized_row.items()
            if key.endswith("_unit") and value
        }
        fields: list[str] = []
        for key, value in row.items():
            normalized = key.lower()
            if normalized in {"org_id", "org_name", "unit"} or normalized.endswith("_unit"):
                continue
            if normalized == "metric_id" and isinstance(value, str) and value in catalog.metrics:
                value = f"{catalog.metrics[value].name}（{value}）"
            label = _COLUMN_LABELS.get(normalized, key)
            rendered = _number(value)
            is_dimension = any(token in normalized for token in ("count", "rank", "date", "year", "month", "day"))
            field_unit = row_unit
            matching_units = [
                (len(prefix), candidate_unit)
                for prefix, candidate_unit in named_units.items()
                if prefix and prefix in normalized
            ]
            if matching_units:
                field_unit = max(matching_units)[1]
            elif normalized in {
                "result_value", "metric_value", "current_value", "comparison_value", "change_value"
            } and row.get("unit"):
                field_unit = str(row["unit"])
            elif any(token in normalized for token in ("rate", "ratio", "share", "percent", "pct")):
                field_unit = "%"
            if field_unit and isinstance(value, (int, float)) and not isinstance(value, bool) and not is_dimension:
                digits = (
                    4
                    if (
                        field_unit == "%"
                        or any(token in normalized for token in ("rate", "ratio", "share", "percent", "pct"))
                    )
                    and 0 < abs(float(value)) < 0.1
                    else 2
                )
                rendered = f"{_number(value, digits)}{field_unit}"
            elif normalized.endswith("rate") and value is not None:
                rendered = _value(value, "%")
            fields.append(f"{label}={rendered}")
        if not fields and org_name:
            answers.append(org_name)
            continue
        body = "，".join(fields) if fields else "查询成功"
        answers.append(f"{org_name}：{body}" if org_name else body)
    answer = "；".join(answers)
    if "require_organization_list" in plan.assumptions:
        organization_count = len({str(row.get("org_id") or row.get("org_name")) for row in rows if row.get("org_id") or row.get("org_name")})
        if organization_count:
            answer = f"共有{organization_count}家，分别为：{answer}"
    return answer


def _format_answer_core(
    plan: QueryPlan,
    result: QueryResult,
    catalog: SemanticCatalog,
    question: str = "",
) -> str:
    rows = result.dictionaries()
    unit = catalog.unit_for_plan(plan.metrics, plan.derived_formula)
    required_columns = _REQUIRED_COLUMNS_BY_OPERATION.get(plan.operation)
    if plan.operation == "count_vs_average" and plan.start_date:
        required_columns = {"matching_days", "total_days", "matching_percentage"}
    if required_columns and any(not required_columns.issubset(row) for row in rows):
        return _generic_answer(plan, rows, catalog)
    if plan.operation not in _RULE_ANSWER_OPERATIONS:
        return _generic_answer(plan, rows, catalog)
    if plan.operation == "multi_metric_province_compare":
        relation_by_metric = {
            item.field: "高于" if item.operator in {">", ">="} else "低于"
            for item in plan.filters
        }
        organizations: dict[str, list[str]] = {}
        for row in rows:
            metric_id = str(row["metric_id"])
            organizations.setdefault(str(row["org_name"]), []).append(
                f"{row['metric_name']}{_value(row['metric_value'], str(row.get('unit') or ''))}"
                f"（{relation_by_metric[metric_id]}全省均值"
                f"{_value(row['province_average'], str(row.get('unit') or ''))}）"
            )
        answer = "；".join(
            f"{org_name}：{'，'.join(values)}"
            for org_name, values in organizations.items()
        )
        if "require_organization_count" in plan.assumptions:
            answer = f"共有{len(organizations)}家，分别为：{answer}"
        return answer
    if plan.operation == "sum":
        pieces = []
        by_organization = len(plan.organizations) > 1 and len(plan.metrics) == 1
        for row in rows:
            label = str(row["org_name"] if by_organization else row["metric_name"])
            pieces.append(f"{label}{_value(row['metric_value'], str(row.get('unit') or unit))}")
        total_unit = str(rows[0].get("unit") or unit)
        return f"{'，'.join(pieces)}，合计{_value(rows[0]['total_value'], total_unit)}"
    if plan.operation == "composition":
        return "，".join(
            f"{str(row['metric_name']).removesuffix('余额')}占比{_value(row['derived_value'], '%')}"
            for row in rows
        )
    if plan.operation == "mom_yoy":
        row = rows[0]
        def movement(value: object) -> str:
            number = float(value)
            return f"{'增长' if number >= 0 else '下降'}{_number(abs(number))}%"
        return (
            f"{row['org_name']}：环比{movement(row['mom_change'])}，"
            f"同比{movement(row['yoy_change'])}，当前值"
            f"{_value(row['current_value'], str(row.get('unit') or unit))}"
        )
    if plan.operation == "count_vs_average":
        row = rows[0]
        relation = "高于" if plan.filters[0].operator in {">", ">="} else "低于"
        if not plan.start_date:
            return (
                f"{_number(row['matching_organizations'], 0)}家机构{relation}全省均值"
                f"（共{_number(row['total_organizations'], 0)}家）"
            )
        return (
            f"{row['org_name']}：{_number(row['matching_days'], 0)}天{relation}全省均值"
            f"（共{_number(row['total_days'], 0)}天，占比{_number(row['matching_percentage'])}%）"
        )
    if plan.operation == "count_condition":
        row = rows[0]
        condition = plan.filters[0]
        metric_name = catalog.metrics[plan.metrics[0]].name
        condition_text = {"<": "低于", "<=": "不高于", ">": "高于", ">=": "不低于"}[condition.operator]
        return (
            f"{metric_name}{condition_text}{_number(condition.value)}%的共有"
            f"{_number(row['matching_organizations'], 0)}家（全省"
            f"{_number(row['total_organizations'], 0)}家）"
        )
    if plan.operation == "profile":
        by_metric = {str(row["metric_id"]): row for row in rows}
        if "major_operating_profile" in plan.assumptions:
            deposit, loan = by_metric["ZB001"], by_metric["ZB002"]
            npl, coverage = by_metric["ZB013"], by_metric["ZB015"]
            capital, overdue = by_metric["ZB016"], by_metric["ZB017"]
            profit, cost = by_metric["ZB011"], by_metric["ZB012"]
            deposit_loan_ratio = float(loan["metric_value"]) / float(deposit["metric_value"]) * 100.0
            good = [row for row in rows if row.get("performance_label") == "较好"]
            bad = [row for row in rows if row.get("performance_label") == "较差"]
            good_text = "、".join(f"{row['metric_name']}（第{_number(row['metric_rank'], 0)}名）" for row in good) or "无"
            bad_text = "、".join(f"{row['metric_name']}（第{_number(row['metric_rank'], 0)}名）" for row in bad) or "无"
            npl_change = float(npl["change_value"])
            profit_change = float(profit["change_value"])
            npl_relation = "低于" if float(npl["metric_value"]) < float(npl["province_average"]) else "高于"
            cost_relation = "低于" if float(cost["metric_value"]) < float(cost["province_average"]) else "高于"
            return (
                f"存款{_value(deposit['metric_value'], str(deposit.get('unit') or ''))}"
                f"（第{_number(deposit['metric_rank'], 0)}名），"
                f"贷款{_value(loan['metric_value'], str(loan.get('unit') or ''))}"
                f"（第{_number(loan['metric_rank'], 0)}名），存贷比{_number(deposit_loan_ratio)}%；"
                f"不良贷款率{_value(npl['metric_value'], '%')}，拨备覆盖率{_value(coverage['metric_value'], '%')}，"
                f"资本充足率{_value(capital['metric_value'], '%')}，逾期贷款率{_value(overdue['metric_value'], '%')}；"
                f"净利润{_value(profit['metric_value'], str(profit.get('unit') or ''))}，"
                f"成本收入比{_value(cost['metric_value'], '%')}。"
                f"较年初：不良贷款率{'上升' if npl_change > 0 else '下降' if npl_change < 0 else '持平'}"
                f"{_number(abs(npl_change))}个百分点，净利润{'增长' if profit_change >= 0 else '下降'}"
                f"{_number(abs(profit_change))}{profit.get('unit') or ''}。"
                f"表现较好：{good_text}；表现较差：{bad_text}。"
                f"不良贷款率{npl_relation}全省均值，成本收入比{cost_relation}全省均值"
            )
        if "profitability_profile" in plan.assumptions:
            pieces = []
            for metric_id in ("ZB011", "ZB012", "ZB008", "ZB007"):
                row = by_metric.get(metric_id)
                if row:
                    rank = f"（第{_number(row['metric_rank'], 0)}名）" if metric_id in {"ZB011", "ZB012"} else ""
                    pieces.append(
                        f"{row['metric_name']}{_value(row['metric_value'], str(row.get('unit') or ''))}{rank}"
                    )
            profit = by_metric.get("ZB011")
            if profit and profit.get("change_value") is not None:
                change = float(profit["change_value"])
                pieces.append(
                    f"较年初净利润{'增长' if change >= 0 else '下降'}"
                    f"{_number(abs(change))}{profit.get('unit') or ''}"
                )
            return "，".join(pieces)
        good = [row for row in rows if row.get("performance_label") == "较好"]
        bad = [row for row in rows if row.get("performance_label") == "较差"]
        values = "，".join(
            f"{row['metric_name']}{_value(row['metric_value'], str(row.get('unit') or ''))}"
            f"（第{_number(row['metric_rank'], 0)}名）"
            for row in rows
        )
        good_text = "、".join(f"{row['metric_name']}（第{_number(row['metric_rank'], 0)}名）" for row in good) or "无"
        bad_text = "、".join(f"{row['metric_name']}（第{_number(row['metric_rank'], 0)}名）" for row in bad) or "无"
        return f"{values}。表现较好：{good_text}；表现较差：{bad_text}"
    if plan.operation == "cross_difference":
        ratio_difference = all(str(row.get("unit") or "") == "%" for row in rows)
        difference_unit = "个百分点" if ratio_difference else str(rows[0].get("unit") or unit)
        compare_organizations = len(plan.organizations) > 1 and len(plan.metrics) == 1
        values = "，".join(
            f"{row['org_name'] if compare_organizations else row['metric_name']}"
            f"{_value(row['metric_value'], str(row.get('unit') or unit))}"
            for row in rows
        )
        return f"{values}，相差{_number(rows[0]['difference_value'])}{difference_unit}"
    if plan.operation == "period_rank_extremes":
        groups = {"前": [], "后": []}
        for row in rows:
            item = f"{row['org_name']}（{_value(row['average_value'], str(row.get('unit') or unit))}）"
            if row["rank_type"] == "后":
                groups["后"].insert(0, item)
            else:
                groups["前"].append(item)
        return f"前{plan.limit or 3}名：{'、'.join(groups['前'])}；后{plan.limit or 3}名：{'、'.join(groups['后'])}"
    if plan.operation == "period_extrema":
        return "；".join(
            f"{row['extrema_type']}值：{row['org_name']}在{row['data_date']}达到"
            f"{_value(row['metric_value'], str(row.get('unit') or unit))}"
            for row in rows
        )
    if plan.operation == "multi_period_change":
        pieces = []
        for row in rows:
            change = float(row["change_value"])
            direction = "上升" if change > 0 else "下降" if change < 0 else "持平"
            pieces.append(
                f"{row['metric_name']}{direction}（"
                f"{_value(row['comparison_value'], str(row.get('unit') or ''))}→"
                f"{_value(row['current_value'], str(row.get('unit') or ''))}）"
            )
        return "，".join(pieces)
    if plan.operation == "multi_rank_change":
        pieces = []
        for row in rows:
            current = float(row["current_value"])
            comparison = float(row["comparison_value"])
            change = current - comparison
            lower_is_better = str(row.get("metric_id") or plan.metrics[0]) in LOWER_IS_BETTER
            improved = change < 0 if lower_is_better else change > 0
            assessment = "改善" if improved else "恶化" if change else "持平"
            previous_rank = int(row["previous_rank"])
            current_rank = int(row["current_rank"])
            movement = previous_rank - current_rank
            rank_text = (
                f"提升{movement}名" if movement > 0
                else f"下降{abs(movement)}名" if movement < 0
                else "排名不变"
            )
            metric_id = str(row.get("metric_id") or plan.metrics[0])
            change_unit = "个百分点" if metric_id in RATIO_METRICS else str(row.get("unit") or unit)
            pieces.append(
                f"{row['metric_name']}：第{previous_rank}名→第{current_rank}名，"
                f"{'排名' if movement else ''}{rank_text}（"
                f"{_value(row['comparison_value'], str(row.get('unit') or unit))}→"
                f"{_value(row['current_value'], str(row.get('unit') or unit))}，"
                f"{assessment}{_number(abs(change))}{change_unit}）"
            )
        return "；".join(pieces)
    if plan.operation == "period_change_rank":
        pieces = []
        for row in rows:
            value = float(row["result_value"])
            if "rank_decrease" in plan.assumptions:
                action = "下降"
            elif "rank_absolute_change" in plan.assumptions:
                action = "增加" if value >= 0 else "减少"
            else:
                action = "增长" if value >= 0 else "下降"
            pieces.append(
                f"第{_number(row['result_rank'], 0)}名 {row['org_name']}："
                f"{action}{_value(abs(value), str(row.get('unit') or ''))}"
            )
        return "；".join(pieces)
    if plan.operation == "multi_rank":
        return "；".join(
            f"{row['metric_name']}：{_value(row['metric_value'], str(row.get('unit') or ''))}，"
            f"全省第{_number(row['metric_rank'], 0)}名"
            for row in rows
        )
    if plan.operation == "three_dimension_profile":
        by_metric = {str(row["metric_id"]): row for row in rows}
        deposit = by_metric["ZB001"]
        loan = by_metric["ZB002"]
        npl = by_metric["ZB013"]
        profit = by_metric["ZB011"]
        deposit_loan_ratio = float(loan["metric_value"]) / float(deposit["metric_value"]) * 100.0
        return (
            f"规模：存款{_value(deposit['metric_value'], str(deposit.get('unit') or ''))}"
            f"（第{_number(deposit['metric_rank'], 0)}名），"
            f"贷款{_value(loan['metric_value'], str(loan.get('unit') or ''))}"
            f"（第{_number(loan['metric_rank'], 0)}名），存贷比{_number(deposit_loan_ratio)}%；"
            f"资产质量：不良贷款率{_value(npl['metric_value'], str(npl.get('unit') or ''))}"
            f"（第{_number(npl['metric_rank'], 0)}名）；"
            f"盈利能力：净利润{_value(profit['metric_value'], str(profit.get('unit') or ''))}"
            f"（第{_number(profit['metric_rank'], 0)}名）"
        )
    if plan.operation == "reconcile":
        row = rows[0]
        unit = str(row.get("unit") or "")
        return (
            f"对公存款{_value(row['corporate_value'], unit)}+"
            f"个人存款{_value(row['personal_value'], unit)}="
            f"{_value(row['component_sum'], unit)}，"
            f"{'等于' if row['is_equal'] else '不等于'}各项存款"
            f"{_value(row['total_value'], unit)}，差额{_value(abs(float(row['difference_value'])), unit)}"
        )
    if plan.operation == "value":
        if "pairwise_difference" in plan.assumptions and len(rows) == 2:
            first, second = rows
            first_value, second_value = float(first["metric_value"]), float(second["metric_value"])
            wants_lower = "pairwise_lower" in plan.assumptions
            winner = min(rows, key=lambda row: float(row["metric_value"])) if wants_lower else max(
                rows, key=lambda row: float(row["metric_value"])
            )
            difference_unit = "个百分点" if plan.metrics[0] in RATIO_METRICS else str(winner.get("unit") or unit)
            return (
                f"{winner['org_name']}{'更低' if wants_lower else '更高'}，"
                f"{_value(winner['metric_value'], str(winner.get('unit') or unit))}；"
                f"两家相差{_number(abs(first_value - second_value))}{difference_unit}"
            )
        if len(plan.metrics) > 1:
            return "；".join(
                f"{row['metric_name']}：{_value(row['metric_value'], str(row.get('unit') or unit))}"
                for row in rows
            )
        return "；".join(
            f"{row['org_name']}：{_value(row['metric_value'], str(row.get('unit') or unit))}"
            for row in rows
        )
    if plan.operation == "rank":
        return "；".join(
            f"第{_number(row['metric_rank'], 0)}名 {row['org_name']}："
            f"{_value(row['metric_value'], str(row.get('unit') or unit))}"
            for row in rows
        )
    if plan.operation == "difference":
        pieces = []
        current_value_first = _value_requested_before_change(question)
        metric_name = (
            catalog.metrics[plan.metrics[0]].name
            if len(plan.metrics) == 1 and plan.metrics[0] in catalog.metrics
            else "当前值"
        )
        for row in rows:
            if row.get("current_value") is None or row.get("comparison_value") is None or row.get("change_value") is None:
                missing = "当前期" if row.get("current_value") is None else "比较期"
                pieces.append(f"{row['org_name']}：无法计算，缺少{missing}数据")
                continue
            change = float(row["change_value"])
            direction = "增加" if change > 0 else "下降" if change < 0 else "持平"
            change_unit = "个百分点" if plan.metrics[0] in RATIO_METRICS else str(row.get("unit") or unit)
            if current_value_first:
                pieces.append(
                    f"{row['org_name']}：{metric_name}"
                    f"{_value(row['current_value'], str(row.get('unit') or unit))}；"
                    f"{direction}{_number(abs(change))}{change_unit}"
                    f"（比较期{_value(row['comparison_value'], str(row.get('unit') or unit))}）"
                )
            else:
                pieces.append(
                    f"{row['org_name']}：{direction}{_number(abs(change))}{change_unit}"
                    f"（当前{_value(row['current_value'], str(row.get('unit') or unit))}，"
                    f"比较期{_value(row['comparison_value'], str(row.get('unit') or unit))}）"
                )
        return "；".join(pieces)
    if plan.operation == "growth":
        pieces = []
        for row in rows:
            if row.get("current_value") is None or row.get("comparison_value") is None or row.get("growth_rate") is None:
                missing = "当前期" if row.get("current_value") is None else "比较期"
                pieces.append(f"{row['org_name']}：无法计算，缺少{missing}数据")
                continue
            growth_rate = float(row["growth_rate"])
            pieces.append(
                f"{row['org_name']}：{'增长' if growth_rate >= 0 else '下降'}"
                f"{_number(abs(growth_rate))}%，当前值"
                f"{_value(row['current_value'], str(row.get('unit') or unit))}"
            )
        return "；".join(pieces)
    if plan.operation == "ratio":
        return "；".join(
            f"{row['org_name']}："
            f"{_number(row['derived_value'], 4 if unit == '%' and 0 < abs(float(row['derived_value'])) < 0.1 else 2)}{unit}"
            for row in rows
        )
    if plan.operation == "province_average_compare":
        pieces = []
        for row in rows:
            difference = float(row["difference_from_average"])
            relation = "高于" if difference > 0 else "低于" if difference < 0 else "等于"
            metric_id = str(row.get("metric_id") or plan.metrics[0])
            difference_unit = "个百分点" if metric_id in RATIO_METRICS else str(row.get("unit") or unit)
            rank_text = (
                f"全省第{_number(row['metric_rank'], 0)}名，"
                if "rank_population_all" in plan.assumptions and row.get("metric_rank") is not None
                else ""
            )
            pieces.append(
                f"{row['org_name']}：{_value(row['metric_value'], str(row.get('unit') or unit))}，{rank_text}"
                f"{relation}全省均值{_difference_number(abs(difference))}{difference_unit}"
                f"（全省均值{_value(row['province_average'], str(row.get('unit') or unit))}）"
            )
        return "；".join(pieces)
    if plan.operation == "threshold":
        return "；".join(
            f"{row['org_name']}：{'满足' if row['threshold_met'] else '不满足'}，"
            f"实际值{_value(row['metric_value'], str(row.get('unit') or unit))}"
            for row in rows
        )
    if plan.operation == "daily_average":
        pieces = []
        for row in rows:
            text = f"{row['org_name']}：日均{_value(row['average_value'], str(row.get('unit') or unit))}"
            if "include_extrema" in plan.assumptions:
                text += (
                    f"，最高日{row['maximum_date']}为"
                    f"{_value(row['maximum_value'], str(row.get('unit') or unit))}"
                    f"，最低日{row['minimum_date']}为"
                    f"{_value(row['minimum_value'], str(row.get('unit') or unit))}"
                )
            pieces.append(text)
        return "；".join(pieces)
    pieces = [
        f"{row['org_name']} {row['data_date']}：{_value(row['metric_value'], str(row.get('unit') or unit))}"
        for row in rows
    ]
    if rows and "include_extrema" in plan.assumptions:
        highest = max(rows, key=lambda row: float(row["metric_value"]))
        lowest = min(rows, key=lambda row: float(row["metric_value"]))
        pieces.extend(
            (
                f"最高为{highest['data_date']}：{_value(highest['metric_value'], str(highest.get('unit') or unit))}",
                f"最低为{lowest['data_date']}：{_value(lowest['metric_value'], str(lowest.get('unit') or unit))}",
            )
        )
    return "；".join(pieces)


def _facts_from_result(
    plan: QueryPlan,
    result: QueryResult,
    catalog: SemanticCatalog,
) -> list[dict[str, object]]:
    name_to_id = {definition.name: metric_id for metric_id, definition in catalog.metrics.items()}
    facts: list[dict[str, object]] = []
    for row in result.dictionaries():
        metric_id = str(row.get("metric_id") or "")
        if metric_id not in catalog.metrics:
            metric_id = plan.metrics[0] if len(plan.metrics) == 1 else name_to_id.get(str(row.get("metric_name") or ""), "")
        if metric_id not in catalog.metrics:
            continue
        value = next(
            (
                row.get(key)
                for key in ("metric_value", "current_value", "result_value", "derived_value")
                if row.get(key) is not None
            ),
            None,
        )
        facts.append(
            {
                "metric_id": metric_id,
                "value": value,
                "unit": str(row.get("unit") or catalog.metrics[metric_id].unit),
                "rank": next(
                    (
                        row.get(key)
                        for key in ("metric_rank", "current_rank", "result_rank")
                        if row.get(key) is not None
                    ),
                    None,
                ),
                "province_average": next(
                    (
                        row.get(key)
                        for key in (
                            "province_average", "province_avg", "province_avg_value",
                            "province_average_value", "average_value",
                        )
                        if row.get(key) is not None
                    ),
                    None,
                ),
                "org_id": str(row.get("org_id") or (plan.organizations[0] if len(plan.organizations) == 1 else "")),
            }
        )
    return facts


def _collect_metric_facts(
    plan: QueryPlan,
    result: QueryResult,
    history: tuple[TurnMemory, ...],
    catalog: SemanticCatalog,
) -> dict[str, dict[str, object]]:
    target_org = plan.organizations[0] if len(plan.organizations) == 1 else ""
    collected: dict[str, dict[str, object]] = {}
    sources = [(turn.plan, turn.result) for turn in history] + [(plan, result)]
    for source_plan, source_result in sources:
        for fact in _facts_from_result(source_plan, source_result, catalog):
            if target_org and fact["org_id"] and fact["org_id"] != target_org:
                continue
            metric_id = str(fact["metric_id"])
            previous = collected.get(metric_id, {})
            collected[metric_id] = {
                key: value if value is not None and value != "" else previous.get(key)
                for key, value in {**previous, **fact}.items()
            }
    return collected


def _risk_summary(facts: dict[str, dict[str, object]]) -> str:
    npl = facts.get("ZB013")
    coverage = facts.get("ZB015")
    capital = facts.get("ZB016")
    pieces: list[str] = []
    if npl and npl.get("value") is not None:
        rank = f"、全省第{_number(npl['rank'], 0)}名" if npl.get("rank") is not None else ""
        pieces.append(f"不良贷款率{_value(npl['value'], '%')}{rank}，低于5%监管上限")
    if coverage and coverage.get("value") is not None:
        rank = f"、全省第{_number(coverage['rank'], 0)}名" if coverage.get("rank") is not None else ""
        status = "达到" if float(coverage["value"]) >= 150 else "未达到"
        pieces.append(f"拨备覆盖率{_value(coverage['value'], '%')}{rank}，{status}150%要求")
    if capital and capital.get("value") is not None:
        rank = f"、全省第{_number(capital['rank'], 0)}名" if capital.get("rank") is not None else ""
        status = "达到" if float(capital["value"]) >= 10.5 else "未达到"
        pieces.append(f"资本充足率{_value(capital['value'], '%')}{rank}，{status}10.5%要求")
    ranks = [int(fact["rank"]) for fact in (npl, coverage) if fact and fact.get("rank") is not None]
    if len(ranks) == 2 and max(ranks) <= 3:
        conclusion = "是全省风控表现最优的机构之一"
    elif len(ranks) == 2 and min(ranks) >= 11:
        conclusion = "两项核心风险指标虽达监管要求，但在全省排名靠后，风险抵补能力相对较弱"
    elif coverage and coverage.get("rank") == 1:
        conclusion = "拨备覆盖率全省领先，风险抵补能力突出"
    else:
        conclusion = "整体风险指标处于可控范围"
    return "，".join(pieces) + f"；{conclusion}" if pieces else conclusion


def _overall_summary(facts: dict[str, dict[str, object]]) -> str:
    parts: list[str] = []
    for metric_id in ("ZB001", "ZB011"):
        fact = facts.get(metric_id)
        if fact and fact.get("value") is not None:
            parts.append(
                f"{'存款' if metric_id == 'ZB001' else '净利润'}"
                f"{_value(fact['value'], str(fact.get('unit') or ''))}"
            )
    for metric_id, label in (("ZB013", "不良率"), ("ZB015", "拨备"), ("ZB016", "资本")):
        fact = facts.get(metric_id)
        if fact and fact.get("rank") is not None:
            parts.append(f"{label}全省第{_number(fact['rank'], 0)}名")
    cost = facts.get("ZB012")
    if cost and cost.get("value") is not None:
        relation = "低于" if cost.get("province_average") is not None and float(cost["value"]) < float(cost["province_average"]) else "高于"
        parts.append(f"成本收入比{_value(cost['value'], '%')}{relation}全省均值")
    return "、".join(parts) + "，风控与经营效率兼优，综合经营质量较为均衡"


def format_answer(
    plan: QueryPlan,
    result: QueryResult,
    catalog: SemanticCatalog,
    *,
    question: str = "",
    history: tuple[TurnMemory, ...] = (),
) -> str:
    answer = _format_answer_core(plan, result, catalog, question)
    facts = _collect_metric_facts(plan, result, history, catalog)
    if re.search(r"达标|监管(?:线|要求|标准|上限)", question) and len(plan.metrics) == 1:
        thresholds = {"ZB013": ("低于", 5.0, "上限"), "ZB015": ("不低于", 150.0, "要求"), "ZB016": ("不低于", 10.5, "要求")}
        rule = thresholds.get(plan.metrics[0])
        fact = facts.get(plan.metrics[0])
        if rule and fact and fact.get("value") is not None:
            operator, threshold, label = rule
            value = float(fact["value"])
            met = value < threshold if plan.metrics[0] == "ZB013" else value >= threshold
            answer += f"；{'达到' if met else '未达到'}监管标准（{operator}{_number(threshold)}%{label}）"
    if re.search(r"风控|风险画像|风控表现", question):
        answer += f"。综合判断：{_risk_summary(facts)}"
    if re.search(r"一句话总结|整体画像", question):
        answer += f"。一句话：{_overall_summary(facts)}"
    return answer
