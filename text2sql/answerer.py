"""Deterministic answers grounded only in executed query results."""

from __future__ import annotations

from .models import QueryPlan, QueryResult
from .semantic_catalog import RATIO_METRICS, SemanticCatalog


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
    "mom_change": "环比变动",
    "yoy_change": "同比变动",
    "previous_rank": "原排名",
    "current_rank": "当前排名",
    "rank_change": "排名变化",
    "rank_type": "排名分组",
}


def _generic_answer(
    plan: QueryPlan,
    rows: list[dict[str, object]],
    catalog: SemanticCatalog,
) -> str:
    fallback_org = catalog.organizations.get(plan.organizations[0], "") if len(plan.organizations) == 1 else ""
    answers: list[str] = []
    for row in rows:
        org_name = str(row.get("org_name") or fallback_org)
        fields: list[str] = []
        for key, value in row.items():
            if key in {"org_id", "org_name", "unit"}:
                continue
            if key == "metric_id" and isinstance(value, str) and value in catalog.metrics:
                value = f"{catalog.metrics[value].name}（{value}）"
            label = _COLUMN_LABELS.get(key.lower(), key)
            fields.append(f"{label}={_number(value)}")
        if not fields and org_name:
            answers.append(org_name)
            continue
        body = "，".join(fields) if fields else "查询成功"
        answers.append(f"{org_name}：{body}" if org_name else body)
    return "；".join(answers)


def format_answer(plan: QueryPlan, result: QueryResult, catalog: SemanticCatalog) -> str:
    rows = result.dictionaries()
    unit = catalog.unit_for_plan(plan.metrics, plan.derived_formula)
    required_columns = {
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
    }.get(plan.operation)
    if required_columns and any(not required_columns.issubset(row) for row in rows):
        return _generic_answer(plan, rows, catalog)
    if plan.operation == "value":
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
        key = "change_value"
        pieces = []
        for row in rows:
            if row.get("current_value") is None or row.get("comparison_value") is None or row.get(key) is None:
                missing = "当前期" if row.get("current_value") is None else "比较期"
                pieces.append(f"{row['org_name']}：无法计算，缺少{missing}数据")
                continue
            change = float(row[key])
            direction = "增加" if change > 0 else "下降" if change < 0 else "持平"
            change_unit = "个百分点" if plan.metrics[0] in RATIO_METRICS else str(row.get("unit") or unit)
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
            f"{row['org_name']}：{_value(row['derived_value'], unit)}" for row in rows
        )
    if plan.operation == "province_average_compare":
        pieces = []
        for row in rows:
            difference = float(row["difference_from_average"])
            relation = "高于" if difference > 0 else "低于" if difference < 0 else "等于"
            metric_id = str(row.get("metric_id") or plan.metrics[0])
            difference_unit = "个百分点" if metric_id in RATIO_METRICS else str(row.get("unit") or unit)
            pieces.append(
                f"{row['org_name']}：{_value(row['metric_value'], str(row.get('unit') or unit))}，"
                f"{relation}全省均值{_number(abs(difference))}{difference_unit}"
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
        return "；".join(
            f"{row['org_name']}：日均{_value(row['average_value'], str(row.get('unit') or unit))}"
            for row in rows
        )
    if plan.operation == "quarterly_trend":
        pieces = [
            f"{row['org_name']} {row['data_date']}：{_value(row['metric_value'], str(row.get('unit') or unit))}"
            for row in rows
        ]
        if rows:
            highest = max(rows, key=lambda row: float(row["metric_value"]))
            pieces.append(
                f"最高季度为{highest['data_date']}："
                f"{_value(highest['metric_value'], str(highest.get('unit') or unit))}"
            )
        return "；".join(pieces)
    if plan.operation == "annual_average_extrema":
        return "；".join(
            f"{row['rank_group']}{_number(row['group_rank'], 0)}名 {row['org_name']}："
            f"年均{_value(row['average_value'], str(row.get('unit') or unit))}"
            for row in rows
        )
    if plan.operation == "multi_metric_rank_change":
        pieces = []
        for row in rows:
            change = row.get("rank_change")
            if change is None:
                pieces.append(f"{row['metric_name']}：缺少排名比较数据")
                continue
            change_value = int(change)
            movement = "提升" if change_value < 0 else "下降" if change_value > 0 else "不变"
            movement_text = movement if change_value == 0 else f"{movement}{abs(change_value)}名"
            pieces.append(
                f"{row['metric_name']}：第{_number(row['previous_rank'], 0)}名→"
                f"第{_number(row['current_rank'], 0)}名，排名{movement_text}"
            )
        return "；".join(pieces)
    if plan.operation == "metric_profile_rank":
        return "；".join(
            f"{row['metric_name']}：{_value(row['metric_value'], str(row.get('unit') or ''))}，"
            f"第{_number(row['metric_rank'], 0)}名（{row['performance']}）"
            for row in rows
        )
    if plan.operation == "mom_yoy_difference":
        pieces = []
        for row in rows:
            change_unit = "个百分点" if row["metric_id"] in RATIO_METRICS else str(row.get("unit") or "")
            if row.get("mom_change") is None or row.get("yoy_change") is None:
                pieces.append(f"{row['org_name']} {row['metric_name']}：缺少上月或去年同期数据")
                continue
            pieces.append(
                f"{row['org_name']} {row['metric_name']}：环比{_number(row['mom_change'])}{change_unit}，"
                f"同比{_number(row['yoy_change'])}{change_unit}"
            )
        return "；".join(pieces)
    if plan.operation == "period_global_extrema":
        return "；".join(
            f"单日{row['extrema_type']}：{row['org_name']}，{row['data_date']}，"
            f"{_value(row['metric_value'], str(row.get('unit') or unit))}"
            for row in rows
        )
    if plan.operation == "period_average_summary":
        return "；".join(
            f"{row['org_name']}：日均{_value(row['average_value'], str(row.get('unit') or unit))}，"
            f"最高日{row['highest_date']}为{_value(row['highest_value'], str(row.get('unit') or unit))}，"
            f"最低日{row['lowest_date']}为{_value(row['lowest_value'], str(row.get('unit') or unit))}"
            for row in rows
        )
    if plan.operation == "component_shares":
        return "；".join(
            f"{row['org_name']}：{row['first_metric_name']}占比{_number(row['first_share'])}%，"
            f"{row['second_metric_name']}占比{_number(row['second_share'])}%"
            for row in rows
        )
    if plan.operation == "organization_sum":
        row = rows[0]
        return (
            f"{_number(row['organization_count'], 0)}家机构{row['metric_name']}合计："
            f"{_value(row['total_value'], str(row.get('unit') or unit))}"
        )
    if plan.operation == "cross_organization_difference":
        row = rows[0]
        difference = float(row["difference_value"])
        first = str(row["first_org_name"])
        second = str(row["second_org_name"])
        higher, lower = (first, second) if difference >= 0 else (second, first)
        difference_unit = "个百分点" if plan.metrics[0] in RATIO_METRICS else str(row.get("unit") or unit)
        return (
            f"{higher}高于{lower}{_number(abs(difference))}{difference_unit}"
            f"（{first}{_value(row['first_value'], str(row.get('unit') or unit))}，"
            f"{second}{_value(row['second_value'], str(row.get('unit') or unit))}）"
        )
    if plan.operation == "component_sum":
        row = rows[0]
        details = "，".join(
            f"{item['metric_name']}{_value(item['metric_value'], str(item.get('unit') or ''))}"
            for item in rows
        )
        return f"{row['org_name']}：{details}，合计{_value(row['total_value'], str(row.get('unit') or ''))}"
    if plan.operation == "component_sum_compare":
        row = rows[0]
        relation = "等于" if row["is_equal"] else "不等于"
        return (
            f"{row['org_name']}：分项合计{_value(row['component_sum'], str(row.get('unit') or unit))}，"
            f"{row['metric_name']}{_value(row['target_value'], str(row.get('unit') or unit))}，{relation}，"
            f"差额{_value(row['difference_value'], str(row.get('unit') or unit))}"
        )
    if plan.operation == "province_average_count":
        row = rows[0]
        relation = "低于" if plan.filters[0].operator == "<" else "高于"
        return (
            f"共有{_number(row['organization_count'], 0)}家，{relation}全省均值"
            f"（全省均值{_value(row['province_average'], unit)}）"
        )
    if plan.operation in {"period_growth_rank", "period_decline_rank"}:
        key = "decline_value" if plan.operation == "period_decline_rank" else "growth_rate"
        value_unit = "个百分点" if plan.operation == "period_decline_rank" else "%"
        label = "下降" if plan.operation == "period_decline_rank" else "增幅"
        return "；".join(
            f"第{_number(row['metric_rank'], 0)}名 {row['org_name']}："
            f"{label}{_number(row[key])}{value_unit}"
            for row in rows
        )
    if plan.operation == "metric_difference":
        row = rows[0]
        difference = float(row["difference_value"])
        first = str(row["first_metric_name"])
        second = str(row["second_metric_name"])
        relation = "高于" if difference > 0 else "低于" if difference < 0 else "等于"
        return (
            f"{row['org_name']}：{first}{relation}{second}"
            f"{_number(abs(difference))}个百分点"
            f"（{first}{_number(row['first_value'])}%，{second}{_number(row['second_value'])}%）"
        )
    if plan.operation == "days_vs_average":
        row = rows[0]
        relation = "低于" if plan.filters[0].operator == "<" else "高于"
        return (
            f"{row['org_name']}：全年共{_number(row['matching_days'], 0)}天{relation}全省均值"
            f"（统计{_number(row['total_days'], 0)}天）"
        )
    if plan.operation == "multi_metric_difference":
        pieces = []
        for row in rows:
            change = row.get("change_value")
            if change is None:
                pieces.append(f"{row['metric_name']}：缺少比较数据")
                continue
            change_value = float(change)
            direction = "增加" if change_value > 0 else "下降" if change_value < 0 else "持平"
            change_unit = "个百分点" if row["metric_id"] in RATIO_METRICS else str(row.get("unit") or "")
            pieces.append(f"{row['metric_name']}：{direction}{_number(abs(change_value))}{change_unit}")
        return "；".join(pieces)
    return _generic_answer(plan, rows, catalog)
