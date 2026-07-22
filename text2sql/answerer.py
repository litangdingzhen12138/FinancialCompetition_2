"""Format answers strictly from executed query results.

The rule path has a few stable renderers.  LLM queries use a schema-agnostic
renderer so new SQL shapes do not require adding a question-specific branch.
"""

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
}


def _generic_answer(
    plan: QueryPlan,
    rows: list[dict[str, object]],
    catalog: SemanticCatalog,
) -> str:
    """Render arbitrary read-only result shapes without interpreting new semantics."""
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
            elif any(token in normalized for token in ("rate", "ratio", "share", "percent", "pct")):
                field_unit = "%"
            if field_unit and isinstance(value, (int, float)) and not isinstance(value, bool) and not is_dimension:
                rendered = _value(value, field_unit)
            elif normalized.endswith("rate") and value is not None:
                rendered = _value(value, "%")
            fields.append(f"{label}={rendered}")
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
    if plan.operation not in {
        "value", "rank", "difference", "growth", "ratio",
        "province_average_compare", "threshold", "daily_average", "quarterly_trend",
    }:
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
        pieces = []
        for row in rows:
            if row.get("current_value") is None or row.get("comparison_value") is None or row.get("change_value") is None:
                missing = "当前期" if row.get("current_value") is None else "比较期"
                pieces.append(f"{row['org_name']}：无法计算，缺少{missing}数据")
                continue
            change = float(row["change_value"])
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
        return "；".join(f"{row['org_name']}：{_value(row['derived_value'], unit)}" for row in rows)
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
    return "；".join(
        f"{row['org_name']} {row['data_date']}：{_value(row['metric_value'], str(row.get('unit') or unit))}"
        for row in rows
    )
