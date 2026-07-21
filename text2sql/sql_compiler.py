"""Compile the small deterministic QueryPlan subset to DuckDB SQL."""

from __future__ import annotations

from .models import QueryPlan


def _literals(values: tuple[str, ...]) -> str:
    return ", ".join("'" + value.replace("'", "''") + "'" for value in values)


def _organization_filter(plan: QueryPlan, alias: str = "v") -> str:
    if plan.organization_scope == "all":
        return ""
    return f" AND {alias}.org_id IN ({_literals(plan.organizations)})"


def _point_sql(plan: QueryPlan) -> str:
    return f"""
        SELECT o.org_id, o.org_name, m.metric_name, m.unit, v.metric_value
        FROM metric_values v
        JOIN organizations o ON o.org_id = v.org_id
        JOIN metrics m ON m.metric_id = v.metric_id
        WHERE v.data_date = DATE '{plan.current_date}'
          AND v.metric_id IN ({_literals(plan.metrics)})
          {_organization_filter(plan)}
        ORDER BY o.org_id, v.metric_id
    """


def _ranking_sql(plan: QueryPlan) -> str:
    direction = (plan.sort_direction or "desc").upper()
    limit = f"LIMIT {plan.limit}" if plan.limit else ""
    rank_all_then_filter = "rank_population_all" in plan.assumptions
    org_filter = "" if rank_all_then_filter else _organization_filter(plan)
    outside_filter = (
        f"WHERE org_id IN ({_literals(plan.organizations)})" if rank_all_then_filter else ""
    )
    return f"""
        WITH ranked AS (
            SELECT o.org_id, o.org_name, m.metric_name, m.unit, v.metric_value,
                   RANK() OVER (ORDER BY v.metric_value {direction}) AS metric_rank
            FROM metric_values v
            JOIN organizations o ON o.org_id = v.org_id
            JOIN metrics m ON m.metric_id = v.metric_id
            WHERE v.data_date = DATE '{plan.current_date}'
              AND v.metric_id = '{plan.metrics[0]}'
              {org_filter}
        )
        SELECT org_id, org_name, metric_name, unit, metric_value, metric_rank
        FROM ranked
        {outside_filter}
        ORDER BY metric_value {direction}, org_id
        {limit}
    """


def _period_sql(plan: QueryPlan) -> str:
    expression = (
        "(current_value - comparison_value) / NULLIF(comparison_value, 0) * 100.0"
        if plan.operation == "growth"
        else "current_value - comparison_value"
    )
    result_name = "growth_rate" if plan.operation == "growth" else "change_value"
    return f"""
        WITH values_by_org AS (
            SELECT v.org_id,
                   MAX(CASE WHEN v.data_date = DATE '{plan.current_date}' THEN v.metric_value END) AS current_value,
                   MAX(CASE WHEN v.data_date = DATE '{plan.comparison_date}' THEN v.metric_value END) AS comparison_value
            FROM metric_values v
            WHERE v.metric_id = '{plan.metrics[0]}'
              AND v.data_date IN (DATE '{plan.current_date}', DATE '{plan.comparison_date}')
              {_organization_filter(plan)}
            GROUP BY v.org_id
        )
        SELECT o.org_id, o.org_name, m.metric_name, m.unit,
               x.current_value, x.comparison_value, {expression} AS {result_name}
        FROM values_by_org x
        JOIN organizations o ON o.org_id = x.org_id
        JOIN metrics m ON m.metric_id = '{plan.metrics[0]}'
        ORDER BY o.org_id
    """


def _ratio_sql(plan: QueryPlan) -> str:
    numerator, denominator = plan.metrics
    multiplier = {
        "profit_per_employee": "1.0",
        "deposit_per_branch": "10000.0",
    }.get(plan.derived_formula, "100.0")
    return f"""
        WITH components AS (
            SELECT v.org_id,
                   MAX(CASE WHEN v.metric_id = '{numerator}' THEN v.metric_value END) AS numerator,
                   MAX(CASE WHEN v.metric_id = '{denominator}' THEN v.metric_value END) AS denominator
            FROM metric_values v
            WHERE v.data_date = DATE '{plan.current_date}'
              AND v.metric_id IN ('{numerator}', '{denominator}')
              {_organization_filter(plan)}
            GROUP BY v.org_id
        )
        SELECT o.org_id, o.org_name, c.numerator, c.denominator,
               c.numerator / NULLIF(c.denominator, 0) * {multiplier} AS derived_value
        FROM components c
        JOIN organizations o ON o.org_id = c.org_id
        ORDER BY o.org_id
    """


def _province_average_sql(plan: QueryPlan) -> str:
    outside_filter = ""
    if plan.organization_scope == "selected":
        outside_filter = f"WHERE b.org_id IN ({_literals(plan.organizations)})"
    return f"""
        WITH base AS (
            SELECT v.org_id, v.metric_id, v.metric_value,
                   AVG(v.metric_value) OVER (PARTITION BY v.metric_id) AS province_average
            FROM metric_values v
            WHERE v.data_date = DATE '{plan.current_date}'
              AND v.metric_id IN ({_literals(plan.metrics)})
        )
        SELECT o.org_id, o.org_name, m.metric_id, m.metric_name, m.unit,
               b.metric_value, b.province_average,
               b.metric_value - b.province_average AS difference_from_average
        FROM base b
        JOIN organizations o ON o.org_id = b.org_id
        JOIN metrics m ON m.metric_id = b.metric_id
        {outside_filter}
        ORDER BY o.org_id, m.metric_id
    """


def _threshold_sql(plan: QueryPlan) -> str:
    condition = plan.filters[0]
    return f"""
        SELECT o.org_id, o.org_name, m.metric_name, m.unit, v.metric_value,
               v.metric_value {condition.operator} {float(condition.value)} AS threshold_met
        FROM metric_values v
        JOIN organizations o ON o.org_id = v.org_id
        JOIN metrics m ON m.metric_id = v.metric_id
        WHERE v.data_date = DATE '{plan.current_date}'
          AND v.metric_id = '{plan.metrics[0]}'
          {_organization_filter(plan)}
        ORDER BY o.org_id
    """


def _daily_average_sql(plan: QueryPlan) -> str:
    return f"""
        SELECT o.org_id, o.org_name, m.metric_name, m.unit,
               AVG(v.metric_value) AS average_value
        FROM metric_values v
        JOIN organizations o ON o.org_id = v.org_id
        JOIN metrics m ON m.metric_id = v.metric_id
        WHERE v.data_date BETWEEN DATE '{plan.start_date}' AND DATE '{plan.end_date}'
          AND v.metric_id = '{plan.metrics[0]}'
          {_organization_filter(plan)}
        GROUP BY o.org_id, o.org_name, m.metric_name, m.unit
        ORDER BY o.org_id
    """


def _trend_sql(plan: QueryPlan) -> str:
    return f"""
        SELECT o.org_id, o.org_name, v.data_date, m.metric_name, m.unit, v.metric_value
        FROM metric_values v
        JOIN organizations o ON o.org_id = v.org_id
        JOIN metrics m ON m.metric_id = v.metric_id
        WHERE v.data_date BETWEEN DATE '{plan.start_date}' AND DATE '{plan.end_date}'
          AND v.metric_id = '{plan.metrics[0]}'
          AND EXTRACT(month FROM v.data_date) IN (3, 6, 9, 12)
          AND v.data_date = LAST_DAY(v.data_date)
          {_organization_filter(plan)}
        ORDER BY o.org_id, v.data_date
    """


def compile_rule_sql(plan: QueryPlan) -> str:
    """Compile only operations owned by the conservative rule layer."""
    if plan.operation == "rank":
        return _ranking_sql(plan)
    if plan.operation in {"difference", "growth"}:
        return _period_sql(plan)
    if plan.operation == "ratio":
        return _ratio_sql(plan)
    if plan.operation == "province_average_compare":
        return _province_average_sql(plan)
    if plan.operation == "threshold":
        return _threshold_sql(plan)
    if plan.operation == "daily_average":
        return _daily_average_sql(plan)
    if plan.operation == "quarterly_trend":
        return _trend_sql(plan)
    if plan.operation != "value":
        raise ValueError(f"规则编译器不支持操作：{plan.operation}")
    return _point_sql(plan)
