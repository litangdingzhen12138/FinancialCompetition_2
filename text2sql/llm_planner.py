"""OpenAI-compatible LLM planner used only when deterministic rules cannot finish."""

from __future__ import annotations

import json
from typing import Any

import requests

from .config import Settings
from .errors import ConfigurationError, PlanningError
from .models import PlanFilter, QueryPlan, SessionState


SYSTEM_PROMPT = """你是银行指标Text2SQL规划器。只输出一个JSON对象，不要Markdown。
只能使用提供的DuckDB Schema，不能发明表、列、指标或机构。SQL只能是一条SELECT或WITH...SELECT。
必须同时输出结构化QueryPlan和sql。数值必须由数据库查询，不能凭记忆回答。
比率指标的变化使用百分点差，不计算增幅；不良贷款率、逾期贷款率、成本收入比越低越好。
JSON字段：query_type, operation, organizations, organization_scope, metrics, current_date,
comparison_date, start_date, end_date, dimensions, filters, sort_direction, limit,
expected_shape, allow_empty, derived_formula, confidence, assumptions, sql。
filters元素字段为field, operator, value, reference。"""


def _extract_json(content: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    for index, character in enumerate(content):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(content[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise PlanningError("LLM未返回有效JSON")


def _strings(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise PlanningError("LLM计划数组字段格式错误")
    return tuple(value)


def _assumptions(value: Any) -> tuple[str, ...]:
    """Normalize the non-executable assumptions field from different LLMs."""
    if value is None:
        return ()
    if isinstance(value, str):
        value = value.strip()
        return (value,) if value else ()
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return tuple(item.strip() for item in value if item.strip())
    raise PlanningError("LLM计划assumptions字段格式错误")


def parse_llm_plan(content: str) -> QueryPlan:
    value = _extract_json(content)
    filters: list[PlanFilter] = []
    raw_filters = value.get("filters") or []
    if not isinstance(raw_filters, list):
        raise PlanningError("filters必须是数组")
    for item in raw_filters:
        if not isinstance(item, dict) or not isinstance(item.get("field"), str) or not isinstance(item.get("operator"), str):
            raise PlanningError("filter格式错误")
        filters.append(PlanFilter(item["field"], item["operator"], item.get("value"), item.get("reference")))
    required = ("query_type", "operation", "organization_scope", "expected_shape", "sql")
    if any(not isinstance(value.get(field), str) or not value[field].strip() for field in required):
        raise PlanningError("LLM计划缺少关键字符串字段")
    try:
        confidence = float(value.get("confidence", 0.0))
    except (TypeError, ValueError) as exc:
        raise PlanningError("confidence格式错误") from exc
    limit_value = value.get("limit")
    limit = int(limit_value) if limit_value is not None else None
    return QueryPlan(
        source="llm",
        query_type=value["query_type"],
        operation=value["operation"],
        organizations=_strings(value.get("organizations")),
        organization_scope=value["organization_scope"],
        metrics=_strings(value.get("metrics")),
        current_date=value.get("current_date"),
        comparison_date=value.get("comparison_date"),
        start_date=value.get("start_date"),
        end_date=value.get("end_date"),
        dimensions=_strings(value.get("dimensions")),
        filters=tuple(filters),
        sort_direction=value.get("sort_direction"),
        limit=limit,
        expected_shape=value["expected_shape"],
        allow_empty=bool(value.get("allow_empty", False)),
        derived_formula=value.get("derived_formula"),
        confidence=confidence,
        assumptions=_assumptions(value.get("assumptions")),
        sql=value["sql"].strip(),
    )


class LLMPlanner:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def available(self) -> bool:
        return bool(self.settings.llm_url and self.settings.llm_api_key)

    def plan(
        self,
        question: str,
        schema_context: str,
        rule_candidates: dict[str, object],
        state: SessionState,
        feedback: str | None = None,
    ) -> QueryPlan:
        if not self.available:
            raise ConfigurationError(
                "LLM兜底未配置；请设置TEXT2SQL_LLM_URL和TEXT2SQL_LLM_API_KEY"
            )
        payload_context = {
            "schema": schema_context,
            "question": question,
            "rule_candidates": rule_candidates,
            "session_state": {
                "last_organizations": state.last_organizations,
                "last_result_organizations": state.last_result_organizations,
                "last_metrics": state.last_metrics,
                "last_date": state.last_date,
            },
            "previous_failure": feedback,
        }
        response = requests.post(
            self.settings.llm_url,
            headers={
                "Authorization": f"Bearer {self.settings.llm_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.settings.llm_model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(payload_context, ensure_ascii=False)},
                ],
                "temperature": 0,
                "stream": False,
            },
            timeout=self.settings.llm_timeout_seconds,
        )
        try:
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
        except Exception as exc:
            raise PlanningError(f"LLM调用失败：{type(exc).__name__}") from exc
        return parse_llm_plan(str(content))
