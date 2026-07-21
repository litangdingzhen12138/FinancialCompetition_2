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


def format_answer(plan: QueryPlan, result: QueryResult, catalog: SemanticCatalog) -> str:
    rows = result.dictionaries()
    unit = catalog.unit_for_plan(plan.metrics, plan.derived_formula)
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
        return "；".join(
            f"{row['org_name']}：{'增长' if float(row['growth_rate']) >= 0 else '下降'}"
            f"{_number(abs(float(row['growth_rate'])))}%，当前值"
            f"{_value(row['current_value'], str(row.get('unit') or unit))}"
            for row in rows
        )
    if plan.operation == "ratio":
        return "；".join(
            f"{row['org_name']}：{_value(row['derived_value'], unit)}" for row in rows
        )
    if plan.operation == "province_average_compare":
        pieces = []
        for row in rows:
            difference = float(row["difference_from_average"])
            relation = "高于" if difference > 0 else "低于" if difference < 0 else "等于"
            difference_unit = "个百分点" if plan.metrics[0] in RATIO_METRICS else str(row.get("unit") or unit)
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
        return "；".join(
            f"{row['org_name']} {row['data_date']}：{_value(row['metric_value'], str(row.get('unit') or unit))}"
            for row in rows
        )
    return "；".join(str(row) for row in rows)

