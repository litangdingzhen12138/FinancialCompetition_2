"""Deterministic chart recommendations from result shape and business semantics."""

from __future__ import annotations

from typing import Any

from .models import QueryPlan, QueryResult
from .product_models import ChartSpec
from .semantic_catalog import RATIO_METRICS, SemanticCatalog


TIME_FIELDS = ("period_start", "data_date", "date", "month", "quarter", "year")
CATEGORY_FIELDS = ("org_name", "metric_name", "category", "label")
VALUE_FIELDS = (
    "metric_value",
    "current_value",
    "comparison_value",
    "growth_rate",
    "change_value",
    "derived_value",
    "result_value",
    "province_average",
)
COMPOSITION_OPERATIONS = {"composition", "deposit_composition", "customer_composition"}


class ChartRecommender:
    def __init__(self, catalog: SemanticCatalog) -> None:
        self.catalog = catalog

    def recommend(
        self,
        plan: QueryPlan | dict[str, Any],
        result: QueryResult,
    ) -> tuple[ChartSpec, tuple[ChartSpec, ...]]:
        plan_data = plan.to_dict() if isinstance(plan, QueryPlan) else plan
        columns = tuple(result.columns)
        operation = str(plan_data.get("operation") or "")
        metrics = tuple(plan_data.get("metrics") or ())
        metric_names = [
            self.catalog.metrics[metric].name
            for metric in metrics
            if metric in self.catalog.metrics
        ]
        title = "、".join(metric_names) or "查询结果"
        unit = self._unit(metrics, result)
        time_field = next((field for field in TIME_FIELDS if field in columns), None)
        category_field = next((field for field in CATEGORY_FIELDS if field in columns), None)
        numeric_fields = tuple(field for field in VALUE_FIELDS if field in columns)
        preferred_value = {
            "difference": "change_value",
            "growth": "growth_rate",
        }.get(operation)
        primary_value = (
            preferred_value
            if preferred_value in columns
            else numeric_fields[0] if numeric_fields else self._first_numeric_column(result)
        )
        if operation == "difference":
            title = f"{title}变动"
            if metrics and metrics[0] in RATIO_METRICS:
                unit = "个百分点"
        elif operation == "growth":
            title = f"{title}增幅"
            unit = "%"

        table = ChartSpec(
            chart_type="table",
            title=f"{title}明细",
            reason="表格用于保留完整字段和精确数值",
        )
        if result.row_count <= 1 and primary_value:
            return (
                ChartSpec(
                    chart_type="metric",
                    title=title,
                    y_fields=(primary_value,),
                    unit=unit,
                    reason="结果为单条核心指标，优先使用指标卡",
                ),
                (table,),
            )
        if operation in COMPOSITION_OPERATIONS and category_field and primary_value:
            return (
                ChartSpec(
                    chart_type="donut",
                    title=f"{title}构成",
                    x_field=category_field,
                    y_fields=(primary_value,),
                    unit=unit,
                    reason="结果表达部分与整体关系，适合环形图",
                ),
                (table,),
            )
        if time_field and primary_value:
            return (
                ChartSpec(
                    chart_type="line",
                    title=f"{title}趋势",
                    x_field=time_field,
                    y_fields=(primary_value,),
                    series_field="org_name" if "org_name" in columns else None,
                    unit=unit,
                    reason="结果包含连续时间维度，适合展示变化趋势",
                    available_time_drill=("year", "quarter", "month", "day"),
                ),
                (table,),
            )
        if category_field and primary_value and result.row_count <= 30:
            return (
                ChartSpec(
                    chart_type="bar",
                    title=f"{title}对比",
                    x_field=category_field,
                    y_fields=(primary_value,),
                    unit=unit,
                    sort=str(plan_data.get("sort_direction") or "desc"),
                    reason="结果以机构或指标为分类，适合比较大小与排名",
                ),
                (table,),
            )
        return table, ()

    def _unit(self, metrics: tuple[str, ...], result: QueryResult) -> str:
        if len(metrics) == 1 and metrics[0] in self.catalog.metrics:
            return self.catalog.metrics[metrics[0]].unit
        if "unit" in result.columns:
            index = result.columns.index("unit")
            units = {str(row[index]) for row in result.rows if row[index]}
            if len(units) == 1:
                return units.pop()
        return ""

    @staticmethod
    def _first_numeric_column(result: QueryResult) -> str | None:
        for index, column in enumerate(result.columns):
            values = [row[index] for row in result.rows if row[index] is not None]
            if values and all(isinstance(value, (int, float)) for value in values):
                return column
        return None
