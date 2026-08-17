"""Compile the small deterministic QueryPlan subset to DuckDB SQL."""

from __future__ import annotations

from .business_rules import PERFORMANCE_BAD_COUNT, PERFORMANCE_GOOD_MAX_RANK
from .models import QueryPlan
from .date_resolver import same_period_last_year
from .semantic_catalog import COMPOSITION_METRICS, DERIVED_METRICS, LOWER_IS_BETTER


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
    rank_all_then_filter = "rank_population_all" in plan.assumptions
    org_filter = "" if rank_all_then_filter else _organization_filter(plan)
    outside_conditions: list[str] = []
    if rank_all_then_filter:
        outside_conditions.append(f"org_id IN ({_literals(plan.organizations)})")
    if plan.limit:
        if "select_bottom_rank" in plan.assumptions:
            outside_conditions.append(f"metric_rank >= population_size - {plan.limit - 1}")
        elif "select_highest_value" in plan.assumptions or "select_lowest_value" in plan.assumptions:
            outside_conditions.append(f"selection_rank <= {plan.limit}")
        else:
            outside_conditions.append(f"metric_rank <= {plan.limit}")
    outside_filter = f"WHERE {' AND '.join(outside_conditions)}" if outside_conditions else ""
    selection_direction = "ASC" if "select_lowest_value" in plan.assumptions else "DESC"
    order_expression = (
        "metric_rank DESC, org_id"
        if "select_bottom_rank" in plan.assumptions
        else "selection_rank, org_id"
        if "select_highest_value" in plan.assumptions or "select_lowest_value" in plan.assumptions
        else "metric_rank, org_id"
    )
    return f"""
        WITH ranked AS (
            SELECT o.org_id, o.org_name, m.metric_name, m.unit, v.metric_value,
                   RANK() OVER (ORDER BY v.metric_value {direction}) AS metric_rank,
                   RANK() OVER (ORDER BY v.metric_value {selection_direction}) AS selection_rank,
                   COUNT(*) OVER () AS population_size
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
        ORDER BY {order_expression}
    """


def _multi_metric_province_compare_sql(plan: QueryPlan) -> str:
    predicates = " AND ".join(
        "MAX(CASE WHEN metric_id = "
        f"'{item.field.replace(chr(39), chr(39) * 2)}' "
        f"AND metric_value {item.operator} province_average THEN 1 ELSE 0 END) = 1"
        for item in plan.filters
    )
    return f"""
        WITH values_with_average AS (
            SELECT v.org_id, v.metric_id, v.metric_value,
                   AVG(v.metric_value) OVER (PARTITION BY v.metric_id) AS province_average
            FROM metric_values v
            WHERE v.data_date = DATE '{plan.current_date}'
              AND v.metric_id IN ({_literals(plan.metrics)})
        ), matching_organizations AS (
            SELECT org_id
            FROM values_with_average
            GROUP BY org_id
            HAVING {predicates}
        )
        SELECT v.org_id, o.org_name, v.metric_id, m.metric_name, m.unit,
               v.metric_value, v.province_average
        FROM values_with_average v
        JOIN matching_organizations x ON x.org_id = v.org_id
        JOIN organizations o ON o.org_id = v.org_id
        JOIN metrics m ON m.metric_id = v.metric_id
        ORDER BY v.org_id, v.metric_id
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
    definition = DERIVED_METRICS.get(plan.derived_formula or "", {})
    multiplier = str(definition.get("multiplier", "100.0"))
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
    lower_metrics = _literals(tuple(metric for metric in plan.metrics if metric in LOWER_IS_BETTER)) or "''"
    return f"""
        WITH base AS (
            SELECT v.org_id, v.metric_id, v.metric_value,
                   AVG(v.metric_value) OVER (PARTITION BY v.metric_id) AS province_average,
                   RANK() OVER (
                       PARTITION BY v.metric_id
                       ORDER BY
                           CASE WHEN v.metric_id IN ({lower_metrics}) THEN v.metric_value END ASC,
                           CASE WHEN v.metric_id NOT IN ({lower_metrics}) THEN v.metric_value END DESC
                   ) AS metric_rank
            FROM metric_values v
            WHERE v.data_date = DATE '{plan.current_date}'
              AND v.metric_id IN ({_literals(plan.metrics)})
        )
        SELECT o.org_id, o.org_name, m.metric_id, m.metric_name, m.unit,
               b.metric_value, b.province_average,
               b.metric_value - b.province_average AS difference_from_average,
               b.metric_rank
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


def _threshold_count_sql(plan: QueryPlan) -> str:
    condition = plan.filters[0]
    return f"""
        SELECT COUNT(*) FILTER (
                   WHERE v.metric_value {condition.operator} {float(condition.value)}
               ) AS matching_organizations,
               COUNT(*) AS total_organizations
        FROM metric_values v
        WHERE v.data_date = DATE '{plan.current_date}'
          AND v.metric_id = '{plan.metrics[0]}'
    """


def _daily_average_sql(plan: QueryPlan) -> str:
    if "include_extrema" in plan.assumptions:
        return f"""
            SELECT o.org_id, o.org_name, m.metric_name, m.unit,
                   AVG(v.metric_value) AS average_value,
                   MAX(v.metric_value) AS maximum_value,
                   ARG_MAX(v.data_date, v.metric_value) AS maximum_date,
                   MIN(v.metric_value) AS minimum_value,
                   ARG_MIN(v.data_date, v.metric_value) AS minimum_date
            FROM metric_values v
            JOIN organizations o ON o.org_id = v.org_id
            JOIN metrics m ON m.metric_id = v.metric_id
            WHERE v.data_date BETWEEN DATE '{plan.start_date}' AND DATE '{plan.end_date}'
              AND v.metric_id = '{plan.metrics[0]}'
              {_organization_filter(plan)}
            GROUP BY o.org_id, o.org_name, m.metric_name, m.unit
            ORDER BY o.org_id
        """
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


def _sum_sql(plan: QueryPlan) -> str:
    return f"""
        WITH base AS (
            SELECT o.org_id, o.org_name, m.metric_id, m.metric_name, m.unit, v.metric_value
            FROM metric_values v
            JOIN organizations o ON o.org_id = v.org_id
            JOIN metrics m ON m.metric_id = v.metric_id
            WHERE v.data_date = DATE '{plan.current_date}'
              AND v.metric_id IN ({_literals(plan.metrics)})
              {_organization_filter(plan)}
        )
        SELECT org_id, org_name, metric_id, metric_name, unit, metric_value,
               SUM(metric_value) OVER () AS total_value
        FROM base
        ORDER BY org_id, metric_id
    """


def _composition_sql(plan: QueryPlan) -> str:
    composition_name = next(name for name in plan.assumptions if name in COMPOSITION_METRICS)
    definition = COMPOSITION_METRICS[composition_name]
    components = tuple(str(value) for value in definition["components"])
    denominator = str(definition["denominator"])
    return f"""
        WITH base AS (
            SELECT v.org_id, v.metric_id, v.metric_value
            FROM metric_values v
            WHERE v.data_date = DATE '{plan.current_date}'
              AND v.metric_id IN ({_literals((*components, denominator))})
              {_organization_filter(plan)}
        ), denominator AS (
            SELECT org_id, metric_value AS denominator_value
            FROM base
            WHERE metric_id = '{denominator}'
        )
        SELECT o.org_id, o.org_name, m.metric_id, m.metric_name,
               b.metric_value AS component_value, d.denominator_value,
               b.metric_value / NULLIF(d.denominator_value, 0) * 100.0 AS derived_value,
               '%' AS unit
        FROM base b
        JOIN denominator d ON d.org_id = b.org_id
        JOIN organizations o ON o.org_id = b.org_id
        JOIN metrics m ON m.metric_id = b.metric_id
        WHERE b.metric_id IN ({_literals(components)})
        ORDER BY o.org_id, m.metric_id
    """


def _mom_yoy_sql(plan: QueryPlan) -> str:
    yoy_date = same_period_last_year(str(plan.current_date))
    return f"""
        WITH values_by_org AS (
            SELECT v.org_id,
                   MAX(CASE WHEN v.data_date = DATE '{plan.current_date}' THEN v.metric_value END) AS current_value,
                   MAX(CASE WHEN v.data_date = DATE '{plan.comparison_date}' THEN v.metric_value END) AS mom_value,
                   MAX(CASE WHEN v.data_date = DATE '{yoy_date}' THEN v.metric_value END) AS yoy_value
            FROM metric_values v
            WHERE v.metric_id = '{plan.metrics[0]}'
              AND v.data_date IN (
                  DATE '{plan.current_date}', DATE '{plan.comparison_date}', DATE '{yoy_date}'
              )
              {_organization_filter(plan)}
            GROUP BY v.org_id
        )
        SELECT o.org_id, o.org_name, m.metric_name, m.unit,
               x.current_value, x.mom_value, x.yoy_value,
               (x.current_value - x.mom_value) / NULLIF(x.mom_value, 0) * 100.0 AS mom_change,
               (x.current_value - x.yoy_value) / NULLIF(x.yoy_value, 0) * 100.0 AS yoy_change
        FROM values_by_org x
        JOIN organizations o ON o.org_id = x.org_id
        JOIN metrics m ON m.metric_id = '{plan.metrics[0]}'
        ORDER BY o.org_id
    """


def _count_vs_average_sql(plan: QueryPlan) -> str:
    operator = plan.filters[0].operator
    if plan.start_date and plan.end_date:
        return f"""
            WITH daily AS (
                SELECT v.data_date, v.org_id, v.metric_value,
                       AVG(v.metric_value) OVER (PARTITION BY v.data_date) AS province_average
                FROM metric_values v
                WHERE v.metric_id = '{plan.metrics[0]}'
                  AND v.data_date BETWEEN DATE '{plan.start_date}' AND DATE '{plan.end_date}'
            )
            SELECT o.org_id, o.org_name, m.metric_name,
                   COUNT(*) FILTER (WHERE d.metric_value {operator} d.province_average) AS matching_days,
                   COUNT(*) AS total_days,
                   COUNT(*) FILTER (WHERE d.metric_value {operator} d.province_average)
                       * 100.0 / NULLIF(COUNT(*), 0) AS matching_percentage
            FROM daily d
            JOIN organizations o ON o.org_id = d.org_id
            JOIN metrics m ON m.metric_id = '{plan.metrics[0]}'
            WHERE d.org_id IN ({_literals(plan.organizations)})
            GROUP BY o.org_id, o.org_name, m.metric_name
            ORDER BY o.org_id
        """
    return f"""
        WITH compared AS (
            SELECT v.org_id, v.metric_value,
                   AVG(v.metric_value) OVER () AS province_average
            FROM metric_values v
            WHERE v.metric_id = '{plan.metrics[0]}'
              AND v.data_date = DATE '{plan.current_date}'
        )
        SELECT COUNT(*) FILTER (WHERE metric_value {operator} province_average) AS matching_organizations,
               COUNT(*) AS total_organizations
        FROM compared
    """


def _cross_difference_sql(plan: QueryPlan) -> str:
    return f"""
        WITH base AS (
            SELECT o.org_id, o.org_name, m.metric_id, m.metric_name, m.unit, v.metric_value
            FROM metric_values v
            JOIN organizations o ON o.org_id = v.org_id
            JOIN metrics m ON m.metric_id = v.metric_id
            WHERE v.data_date = DATE '{plan.current_date}'
              AND v.metric_id IN ({_literals(plan.metrics)})
              {_organization_filter(plan)}
        )
        SELECT org_id, org_name, metric_id, metric_name, unit, metric_value,
               MAX(metric_value) OVER () - MIN(metric_value) OVER () AS difference_value
        FROM base
        ORDER BY org_id, metric_id
    """


def _reconcile_sql(plan: QueryPlan) -> str:
    return f"""
        WITH values_by_org AS (
            SELECT v.org_id,
                   MAX(CASE WHEN v.metric_id = 'ZB003' THEN v.metric_value END) AS corporate_value,
                   MAX(CASE WHEN v.metric_id = 'ZB004' THEN v.metric_value END) AS personal_value,
                   MAX(CASE WHEN v.metric_id = 'ZB001' THEN v.metric_value END) AS total_value
            FROM metric_values v
            WHERE v.data_date = DATE '{plan.current_date}'
              AND v.metric_id IN ('ZB001', 'ZB003', 'ZB004')
              {_organization_filter(plan)}
            GROUP BY v.org_id
        )
        SELECT x.org_id, o.org_name, '亿元' AS unit,
               x.corporate_value, x.personal_value, x.total_value,
               x.corporate_value + x.personal_value AS component_sum,
               x.corporate_value + x.personal_value - x.total_value AS difference_value,
               ABS(x.corporate_value + x.personal_value - x.total_value) < 0.000001 AS is_equal
        FROM values_by_org x
        JOIN organizations o ON o.org_id = x.org_id
        ORDER BY x.org_id
    """


def _period_rank_extremes_sql(plan: QueryPlan) -> str:
    best_direction = (plan.sort_direction or "desc").upper()
    worst_direction = "ASC" if best_direction == "DESC" else "DESC"
    return f"""
        WITH averages AS (
            SELECT v.org_id, AVG(v.metric_value) AS average_value
            FROM metric_values v
            WHERE v.metric_id = '{plan.metrics[0]}'
              AND v.data_date BETWEEN DATE '{plan.start_date}' AND DATE '{plan.end_date}'
            GROUP BY v.org_id
        ), grouped AS (
            SELECT org_id, average_value, '前' AS rank_type,
                   RANK() OVER (ORDER BY average_value {best_direction}) AS metric_rank
            FROM averages
            UNION ALL
            SELECT org_id, average_value, '后' AS rank_type,
                   RANK() OVER (ORDER BY average_value {worst_direction}) AS metric_rank
            FROM averages
        )
        SELECT g.org_id, o.org_name, m.metric_name, m.unit,
               g.average_value, g.rank_type, g.metric_rank
        FROM grouped g
        JOIN organizations o ON o.org_id = g.org_id
        JOIN metrics m ON m.metric_id = '{plan.metrics[0]}'
        WHERE g.metric_rank <= {plan.limit or 3}
        ORDER BY CASE g.rank_type WHEN '前' THEN 1 ELSE 2 END, g.metric_rank, g.org_id
    """


def _period_extrema_sql(plan: QueryPlan) -> str:
    return f"""
        WITH ranked AS (
            SELECT v.data_date, v.org_id, v.metric_value,
                   RANK() OVER (ORDER BY v.metric_value DESC) AS maximum_rank,
                   RANK() OVER (ORDER BY v.metric_value ASC) AS minimum_rank
            FROM metric_values v
            WHERE v.metric_id = '{plan.metrics[0]}'
              AND v.data_date BETWEEN DATE '{plan.start_date}' AND DATE '{plan.end_date}'
        ), extrema AS (
            SELECT data_date, org_id, metric_value, '最高' AS extrema_type
            FROM ranked WHERE maximum_rank = 1
            UNION ALL
            SELECT data_date, org_id, metric_value, '最低' AS extrema_type
            FROM ranked WHERE minimum_rank = 1
        )
        SELECT e.org_id, o.org_name, e.data_date, m.metric_name, m.unit,
               e.metric_value, e.extrema_type
        FROM extrema e
        JOIN organizations o ON o.org_id = e.org_id
        JOIN metrics m ON m.metric_id = '{plan.metrics[0]}'
        ORDER BY CASE e.extrema_type WHEN '最高' THEN 1 ELSE 2 END, e.org_id
    """


def _multi_period_change_sql(plan: QueryPlan) -> str:
    return f"""
        WITH values_by_metric AS (
            SELECT v.org_id, v.metric_id,
                   MAX(CASE WHEN v.data_date = DATE '{plan.current_date}' THEN v.metric_value END) AS current_value,
                   MAX(CASE WHEN v.data_date = DATE '{plan.comparison_date}' THEN v.metric_value END) AS comparison_value
            FROM metric_values v
            WHERE v.metric_id IN ({_literals(plan.metrics)})
              AND v.data_date IN (DATE '{plan.current_date}', DATE '{plan.comparison_date}')
              {_organization_filter(plan)}
            GROUP BY v.org_id, v.metric_id
        )
        SELECT x.org_id, o.org_name, x.metric_id, m.metric_name, m.unit,
               x.comparison_value, x.current_value,
               x.current_value - x.comparison_value AS change_value
        FROM values_by_metric x
        JOIN organizations o ON o.org_id = x.org_id
        JOIN metrics m ON m.metric_id = x.metric_id
        ORDER BY x.metric_id
    """


def _period_change_rank_sql(plan: QueryPlan) -> str:
    is_decrease = "rank_decrease" in plan.assumptions
    is_absolute = "rank_absolute_change" in plan.assumptions
    if is_decrease:
        result_expression = "comparison_value - current_value"
        result_unit = "个百分点"
    elif is_absolute:
        result_expression = "current_value - comparison_value"
        result_unit = None
    else:
        result_expression = "(current_value - comparison_value) / NULLIF(comparison_value, 0) * 100.0"
        result_unit = "%"
    outside_filter = (
        f"WHERE r.org_id IN ({_literals(plan.organizations)})"
        if plan.organization_scope == "selected"
        else f"WHERE r.result_rank <= {plan.limit or 3}"
    )
    unit_expression = "m.unit" if result_unit is None else f"'{result_unit}'"
    return f"""
        WITH values_by_org AS (
            SELECT v.org_id,
                   MAX(CASE WHEN v.data_date = DATE '{plan.current_date}' THEN v.metric_value END) AS current_value,
                   MAX(CASE WHEN v.data_date = DATE '{plan.comparison_date}' THEN v.metric_value END) AS comparison_value
            FROM metric_values v
            WHERE v.metric_id = '{plan.metrics[0]}'
              AND v.data_date IN (DATE '{plan.current_date}', DATE '{plan.comparison_date}')
            GROUP BY v.org_id
        ), ranked AS (
            SELECT *, {result_expression} AS result_value,
                   RANK() OVER (ORDER BY {result_expression} DESC) AS result_rank
            FROM values_by_org
            WHERE current_value IS NOT NULL AND comparison_value IS NOT NULL
        )
        SELECT r.org_id, o.org_name, m.metric_name,
               r.comparison_value, r.current_value, r.result_value,
               {unit_expression} AS unit, r.result_rank
        FROM ranked r
        JOIN organizations o ON o.org_id = r.org_id
        JOIN metrics m ON m.metric_id = '{plan.metrics[0]}'
        {outside_filter}
        ORDER BY r.result_rank, r.org_id
    """


def _profitability_profile_sql(plan: QueryPlan) -> str:
    lower_metrics = _literals(tuple(metric for metric in plan.metrics if metric in LOWER_IS_BETTER)) or "''"
    return f"""
        WITH current_ranked AS (
            SELECT v.org_id, v.metric_id, v.metric_value,
                   RANK() OVER (
                       PARTITION BY v.metric_id
                       ORDER BY
                           CASE WHEN v.metric_id IN ({lower_metrics}) THEN v.metric_value END ASC,
                           CASE WHEN v.metric_id NOT IN ({lower_metrics}) THEN v.metric_value END DESC
                   ) AS metric_rank
            FROM metric_values v
            WHERE v.data_date = DATE '{plan.current_date}'
              AND v.metric_id IN ({_literals(plan.metrics)})
        ), comparison AS (
            SELECT v.org_id, v.metric_id, v.metric_value AS comparison_value
            FROM metric_values v
            WHERE v.data_date = DATE '{plan.comparison_date}'
              AND v.metric_id IN ({_literals(plan.metrics)})
        )
        SELECT c.org_id, o.org_name, c.metric_id, m.metric_name, m.unit,
               c.metric_value, c.metric_rank, p.comparison_value,
               c.metric_value - p.comparison_value AS change_value
        FROM current_ranked c
        JOIN organizations o ON o.org_id = c.org_id
        JOIN metrics m ON m.metric_id = c.metric_id
        LEFT JOIN comparison p ON p.org_id = c.org_id AND p.metric_id = c.metric_id
        WHERE c.org_id IN ({_literals(plan.organizations)})
        ORDER BY c.metric_id
    """


def _performance_profile_sql(plan: QueryPlan) -> str:
    lower_metrics = _literals(tuple(metric for metric in plan.metrics if metric in LOWER_IS_BETTER)) or "''"
    return f"""
        WITH ranked AS (
            SELECT v.org_id, v.metric_id, v.metric_value,
                   AVG(v.metric_value) OVER (PARTITION BY v.metric_id) AS province_average,
                   RANK() OVER (
                       PARTITION BY v.metric_id
                       ORDER BY
                           CASE WHEN v.metric_id IN ({lower_metrics}) THEN v.metric_value END ASC,
                           CASE WHEN v.metric_id NOT IN ({lower_metrics}) THEN v.metric_value END DESC
                   ) AS metric_rank,
                   COUNT(*) OVER (PARTITION BY v.metric_id) AS population_size
            FROM metric_values v
            WHERE v.data_date = DATE '{plan.current_date}'
              AND v.metric_id IN ({_literals(plan.metrics)})
        )
        SELECT r.org_id, o.org_name, r.metric_id, m.metric_name, m.unit,
               r.metric_value, r.province_average, r.metric_rank,
               CASE
                   WHEN r.metric_rank <= {PERFORMANCE_GOOD_MAX_RANK} THEN '较好'
                   WHEN r.metric_rank >= r.population_size - {PERFORMANCE_BAD_COUNT - 1} THEN '较差'
                   ELSE '中性'
               END AS performance_label
        FROM ranked r
        JOIN organizations o ON o.org_id = r.org_id
        JOIN metrics m ON m.metric_id = r.metric_id
        WHERE r.org_id IN ({_literals(plan.organizations)})
        ORDER BY r.metric_id
    """


def _major_operating_profile_sql(plan: QueryPlan) -> str:
    lower_metrics = _literals(tuple(metric for metric in plan.metrics if metric in LOWER_IS_BETTER)) or "''"
    return f"""
        WITH current_ranked AS (
            SELECT v.org_id, v.metric_id, v.metric_value,
                   AVG(v.metric_value) OVER (PARTITION BY v.metric_id) AS province_average,
                   RANK() OVER (
                       PARTITION BY v.metric_id
                       ORDER BY
                           CASE WHEN v.metric_id IN ({lower_metrics}) THEN v.metric_value END ASC,
                           CASE WHEN v.metric_id NOT IN ({lower_metrics}) THEN v.metric_value END DESC
                   ) AS metric_rank,
                   COUNT(*) OVER (PARTITION BY v.metric_id) AS population_size
            FROM metric_values v
            WHERE v.data_date = DATE '{plan.current_date}'
              AND v.metric_id IN ({_literals(plan.metrics)})
        ), comparison AS (
            SELECT v.org_id, v.metric_id, v.metric_value AS comparison_value
            FROM metric_values v
            WHERE v.data_date = DATE '{plan.comparison_date}'
              AND v.metric_id IN ({_literals(plan.metrics)})
        )
        SELECT c.org_id, o.org_name, c.metric_id, m.metric_name, m.unit,
               c.metric_value, c.province_average, c.metric_rank,
               p.comparison_value, c.metric_value - p.comparison_value AS change_value,
               CASE
                   WHEN c.metric_rank <= {PERFORMANCE_GOOD_MAX_RANK} THEN '较好'
                   WHEN c.metric_rank >= c.population_size - {PERFORMANCE_BAD_COUNT - 1} THEN '较差'
                   ELSE '中性'
               END AS performance_label
        FROM current_ranked c
        JOIN organizations o ON o.org_id = c.org_id
        JOIN metrics m ON m.metric_id = c.metric_id
        LEFT JOIN comparison p ON p.org_id = c.org_id AND p.metric_id = c.metric_id
        WHERE c.org_id IN ({_literals(plan.organizations)})
        ORDER BY c.metric_id
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


def compile_global_rank_sql(plan: QueryPlan) -> str:
    """Compile a validated semantic plan for selected organizations' province-wide ranks."""
    metrics = _literals(plan.metrics)
    lower_metrics = _literals(tuple(metric for metric in plan.metrics if metric in LOWER_IS_BETTER)) or "''"
    dates = (plan.current_date,) if not plan.comparison_date else (plan.comparison_date, plan.current_date)
    date_literals = ", ".join(f"DATE '{value}'" for value in dates if value)
    selected_orgs = _literals(plan.organizations)
    if plan.comparison_date:
        return f"""
            WITH ranked AS (
                SELECT v.data_date, v.metric_id, v.org_id, v.metric_value,
                       RANK() OVER (
                           PARTITION BY v.data_date, v.metric_id
                           ORDER BY
                               CASE WHEN v.metric_id IN ({lower_metrics}) THEN v.metric_value END ASC,
                               CASE WHEN v.metric_id NOT IN ({lower_metrics}) THEN v.metric_value END DESC
                       ) AS metric_rank
                FROM metric_values v
                WHERE v.data_date IN ({date_literals})
                  AND v.metric_id IN ({metrics})
            )
            SELECT r.org_id, o.org_name, r.metric_id, m.metric_name, m.unit,
                   MAX(CASE WHEN r.data_date = DATE '{plan.comparison_date}' THEN r.metric_value END) AS comparison_value,
                   MAX(CASE WHEN r.data_date = DATE '{plan.current_date}' THEN r.metric_value END) AS current_value,
                   MAX(CASE WHEN r.data_date = DATE '{plan.comparison_date}' THEN r.metric_rank END) AS previous_rank,
                   MAX(CASE WHEN r.data_date = DATE '{plan.current_date}' THEN r.metric_rank END) AS current_rank,
                   MAX(CASE WHEN r.data_date = DATE '{plan.comparison_date}' THEN r.metric_rank END)
                     - MAX(CASE WHEN r.data_date = DATE '{plan.current_date}' THEN r.metric_rank END) AS rank_change
            FROM ranked r
            JOIN organizations o ON o.org_id = r.org_id
            JOIN metrics m ON m.metric_id = r.metric_id
            WHERE r.org_id IN ({selected_orgs})
            GROUP BY r.org_id, o.org_name, r.metric_id, m.metric_name, m.unit
            ORDER BY r.org_id, r.metric_id
        """
    return f"""
        WITH ranked AS (
            SELECT v.metric_id, v.org_id, v.metric_value,
                   RANK() OVER (
                       PARTITION BY v.metric_id
                       ORDER BY
                           CASE WHEN v.metric_id IN ({lower_metrics}) THEN v.metric_value END ASC,
                           CASE WHEN v.metric_id NOT IN ({lower_metrics}) THEN v.metric_value END DESC
                   ) AS metric_rank
            FROM metric_values v
            WHERE v.data_date = DATE '{plan.current_date}'
              AND v.metric_id IN ({metrics})
        )
        SELECT r.org_id, o.org_name, r.metric_id, m.metric_name, m.unit,
               r.metric_value, r.metric_rank
        FROM ranked r
        JOIN organizations o ON o.org_id = r.org_id
        JOIN metrics m ON m.metric_id = r.metric_id
        WHERE r.org_id IN ({selected_orgs})
        ORDER BY r.org_id, r.metric_id
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
    if plan.operation == "multi_metric_province_compare":
        return _multi_metric_province_compare_sql(plan)
    if plan.operation == "threshold":
        return _threshold_sql(plan)
    if plan.operation == "count_condition":
        return _threshold_count_sql(plan)
    if plan.operation == "daily_average":
        return _daily_average_sql(plan)
    if plan.operation == "sum":
        return _sum_sql(plan)
    if plan.operation == "composition":
        return _composition_sql(plan)
    if plan.operation == "mom_yoy":
        return _mom_yoy_sql(plan)
    if plan.operation == "count_vs_average":
        return _count_vs_average_sql(plan)
    if plan.operation == "profile":
        if "major_operating_profile" in plan.assumptions:
            return _major_operating_profile_sql(plan)
        if "profitability_profile" in plan.assumptions:
            return _profitability_profile_sql(plan)
        if "performance_profile" in plan.assumptions or "risk_profile" in plan.assumptions:
            return _performance_profile_sql(plan)
        raise ValueError("规则画像缺少类型标识")
    if plan.operation == "cross_difference":
        return _cross_difference_sql(plan)
    if plan.operation == "period_rank_extremes":
        return _period_rank_extremes_sql(plan)
    if plan.operation == "period_extrema":
        return _period_extrema_sql(plan)
    if plan.operation == "multi_period_change":
        return _multi_period_change_sql(plan)
    if plan.operation == "period_change_rank":
        return _period_change_rank_sql(plan)
    if plan.operation == "multi_rank":
        return compile_global_rank_sql(plan)
    if plan.operation == "multi_rank_change":
        return compile_global_rank_sql(plan)
    if plan.operation == "three_dimension_profile":
        return compile_global_rank_sql(plan)
    if plan.operation == "reconcile":
        return _reconcile_sql(plan)
    if plan.operation == "quarterly_trend":
        return _trend_sql(plan)
    if plan.operation != "value":
        raise ValueError(f"规则编译器不支持操作：{plan.operation}")
    return _point_sql(plan)
