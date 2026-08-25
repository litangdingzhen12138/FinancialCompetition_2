"""OpenAI-compatible LLM planner used only when deterministic rules cannot finish."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

import requests  # Compatibility alias for existing integrations/tests.

from .business_rules import derived_rule_prompt
from .config import Settings
from .errors import ConfigurationError, PlanningError
from .llm_client import (
    ChatModelClient,
    LLMResponseError,
    LLMTransportError,
    create_llm_client,
)
from .models import PlanFilter, QueryPlan, SessionState
from .semantic_catalog import DERIVED_METRICS


SYSTEM_PROMPT = f"""你是银行指标Text2SQL规划器。只输出一个JSON对象，不要Markdown。
只能使用提供的DuckDB Schema，不能发明表、列、指标或机构。SQL只能是一条SELECT或WITH...SELECT。
必须同时输出结构化QueryPlan和sql。数值必须由数据库查询，不能凭记忆回答。
比率指标的变化使用百分点差，不计算增幅；不良贷款率、逾期贷款率、成本收入比越低越好。
衍生维度必须严格服从下列业务契约，不得根据年份或问题自行改写：
{derived_rule_prompt()}
JSON字段：query_type, operation, organizations, organization_scope, metrics, current_date,
comparison_date, start_date, end_date, dimensions, filters, sort_direction, limit,
expected_shape, allow_empty, derived_formula, confidence, assumptions, sql。
filters元素字段为field, operator, value, reference。
operation只能是value, rank, difference, growth, ratio, province_average_compare,
threshold, daily_average, quarterly_trend, multi_condition, extrema, count_condition之一。
organizations、metrics、dimensions、assumptions必须是字符串数组；confidence必须是0到1的数字。
SQL结果必须使用清晰稳定的列别名；金额或比率结果应同时返回unit列。优先使用org_id、org_name、
metric_id、metric_name、unit、metric_value、result_value、count_value、metric_rank、data_date等通用别名，
使结果无需针对具体问题编写格式化代码。多指标且单位不同时，优先每行返回一个指标的长表结构；
如必须横向展开，每个数值字段必须有对应的<字段前缀>_unit列。
派生比率必须服从指标目录约定，并保证公式与unit一致。“净利润/存款比”应先把万元换算为亿元，
再用净利润（亿元）/存款（亿元）×100%计算；其他跨单位金额比率同样应先统一量纲。
CTE和表别名必须使用非保留英文名称（如base_data、aggregated_values、pv）；禁止把DuckDB保留字
用作未加引号的标识符，尤其禁止使用pivot、unpivot作为裸CTE名或表别名。
必须覆盖问题中的每个子问题：若同时询问“哪些机构”和“一共有几家”，结果必须同时返回机构列表和总数；
若同时询问两家谁高和相差多少，结果必须包含两家数值及差值；若询问趋势的最高和最低，必须返回可确定极值的完整序列。
只返回用户要求的指标，不得额外返回其他指标。计算某机构的全省排名时，必须先对全省机构做窗口排名，
再在外层筛选目标机构，禁止先筛选目标机构再计算排名。"""


REWRITE_SYSTEM_PROMPT = """你是多轮问数问题改写器。只输出一个JSON对象，不要Markdown，也不要生成SQL。
任务是把当前问题改写为脱离历史也能理解的完整问题。
只能用提供的历史补充当前问题省略的机构、指标、日期、比较期和查询操作；不得编造历史中不存在的信息。
当前问题明确给出的内容优先于历史，不得被历史覆盖。必须保留当前问题的全部比较、排名、差值、阈值、
计数、趋势、极值和综合判断要求，不得只改写其中一部分。
如果历史中存在多个候选而无法唯一确定，不能猜测：rewritten_question返回空字符串，并在
unresolved_references中列出需要用户明确的引用。
输出字段：rewritten_question（字符串）、used_turn_indexes（整数数组）、unresolved_references（字符串数组）。"""


@dataclass(frozen=True, slots=True)
class ContextRewrite:
    rewritten_question: str
    used_turn_indexes: tuple[int, ...]
    unresolved_references: tuple[str, ...]


OPERATION_ALIASES = {
    "query": "value",
    "fetch": "value",
    "compute": "value",
    "calculate": "value",
    "proportion": "value",
    "subtraction": "value",
    "compare": "value",
    "compare_with_industry": "province_average_compare",
    "find_extrema": "extrema",
}


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


def _optional_string(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def parse_context_rewrite(content: str) -> ContextRewrite:
    value = _extract_json(content)
    rewritten = value.get("rewritten_question")
    if not isinstance(rewritten, str):
        raise PlanningError("上下文改写缺少rewritten_question")
    raw_indexes = value.get("used_turn_indexes") or []
    if not isinstance(raw_indexes, list) or not all(
        isinstance(item, int) and not isinstance(item, bool) for item in raw_indexes
    ):
        raise PlanningError("上下文改写的used_turn_indexes格式错误")
    unresolved = _strings(value.get("unresolved_references"))
    return ContextRewrite(
        rewritten_question=rewritten.strip(),
        used_turn_indexes=tuple(raw_indexes),
        unresolved_references=unresolved,
    )


def parse_llm_plan(content: str) -> QueryPlan:
    value = _extract_json(content)
    organizations = _strings(value.get("organizations"))
    metrics = _strings(value.get("metrics"))
    filters: list[PlanFilter] = []
    raw_filters = value.get("filters") or []
    if not isinstance(raw_filters, list):
        raise PlanningError("filters必须是数组")
    for item in raw_filters:
        if not isinstance(item, dict) or not isinstance(item.get("field"), str) or not isinstance(item.get("operator"), str):
            raise PlanningError("filter格式错误")
        filters.append(PlanFilter(item["field"], item["operator"], item.get("value"), item.get("reference")))
    sql = value.get("sql")
    if not isinstance(sql, str) or not sql.strip():
        raise PlanningError("LLM计划缺少SQL")
    query_type = value.get("query_type")
    if not isinstance(query_type, str) or not query_type.strip():
        query_type = "llm_query"
    operation = value.get("operation")
    if not isinstance(operation, str) or not operation.strip():
        operation = "value"
    operation = OPERATION_ALIASES.get(operation.strip().lower(), operation.strip().lower())
    current_date = _optional_string(value.get("current_date"))
    comparison_date = _optional_string(value.get("comparison_date"))
    start_date = _optional_string(value.get("start_date"))
    end_date = _optional_string(value.get("end_date"))
    derived_formula = _optional_string(value.get("derived_formula"))
    recognized_formula = derived_formula in DERIVED_METRICS
    if operation == "value" and recognized_formula and len(metrics) == 2:
        operation = "ratio"
    elif operation == "ratio" and not recognized_formula:
        operation = "multi_condition"
        derived_formula = None
    elif operation in {"difference", "growth"} and (
        comparison_date is None or derived_formula is not None
    ):
        operation = "multi_condition"
        derived_formula = None
    elif operation == "rank" and len(metrics) > 1:
        operation = "multi_condition"
    organization_scope = value.get("organization_scope")
    if not isinstance(organization_scope, str) or organization_scope not in {"selected", "all"}:
        organization_scope = "selected" if organizations else "all"
    expected_shape = value.get("expected_shape")
    if not isinstance(expected_shape, str) or expected_shape not in {
        "single_value", "single_row", "multi_row", "time_series"
    }:
        expected_shape = "multi_row"
    try:
        raw_confidence = value.get("confidence")
        confidence = 0.0 if raw_confidence is None else float(raw_confidence)
    except (TypeError, ValueError) as exc:
        raise PlanningError("confidence格式错误") from exc
    limit_value = value.get("limit")
    limit = int(limit_value) if limit_value is not None else None
    return QueryPlan(
        source="llm",
        query_type=query_type,
        operation=operation,
        organizations=organizations,
        organization_scope=organization_scope,
        metrics=metrics,
        current_date=current_date,
        comparison_date=comparison_date,
        start_date=start_date,
        end_date=end_date,
        dimensions=_strings(value.get("dimensions")),
        filters=tuple(filters),
        sort_direction=_optional_string(value.get("sort_direction")),
        limit=limit,
        expected_shape=expected_shape,
        allow_empty=bool(value.get("allow_empty", False)),
        derived_formula=derived_formula,
        confidence=confidence,
        assumptions=_assumptions(value.get("assumptions")),
        sql=sql.strip(),
    )


class LLMPlanner:
    def __init__(
        self,
        settings: Settings,
        client: ChatModelClient | None = None,
    ) -> None:
        self.settings = settings
        self.client = client or create_llm_client(settings)

    @property
    def available(self) -> bool:
        return self.client.available

    def plan(
        self,
        question: str,
        schema_context: str,
        rule_candidates: dict[str, object],
        state: SessionState,
        feedback: str | None = None,
        trace: list[dict[str, Any]] | None = None,
        *,
        include_history: bool = False,
        history_limit: int = 10,
    ) -> QueryPlan:
        payload_context = {
            "schema": schema_context,
            "question": question,
            "rule_candidates": rule_candidates,
            "previous_failure": feedback,
        }
        if include_history:
            payload_context["conversation_context"] = self._history_context(
                state,
                history_limit,
            )
        raw_content = self._request(
            system_prompt=SYSTEM_PROMPT,
            payload=payload_context,
            trace=trace,
            stage_prefix="llm",
        )
        return parse_llm_plan(raw_content)

    def rewrite(
        self,
        question: str,
        state: SessionState,
        *,
        history_limit: int,
        feedback: str | None = None,
        trace: list[dict[str, Any]] | None = None,
    ) -> ContextRewrite:
        payload_context = {
            "question": question,
            "conversation_context": self._history_context(state, history_limit),
            "previous_failure": feedback,
        }
        raw_content = self._request(
            system_prompt=REWRITE_SYSTEM_PROMPT,
            payload=payload_context,
            trace=trace,
            stage_prefix="context_rewrite",
        )
        return parse_context_rewrite(raw_content)

    @staticmethod
    def _history_context(state: SessionState, history_limit: int) -> dict[str, object]:
        limit = 20 if history_limit >= 20 else 10
        selected_turns = state.recent_turns[-limit:]
        return {
            "focus_state": {
                "last_organizations": state.last_organizations,
                "last_result_organizations": state.last_result_organizations,
                "last_metrics": state.last_metrics,
                "last_date": state.last_date,
                "last_comparison_date": state.last_comparison_date,
                "last_operation": state.last_operation,
            },
            "recent_turns": [
                {
                    "turn_index": index,
                    "question": turn.question,
                    "answer": turn.answer[:1000],
                    "plan": {
                        "organizations": turn.plan.organizations,
                        "metrics": turn.plan.metrics,
                        "current_date": turn.plan.current_date,
                        "comparison_date": turn.plan.comparison_date,
                        "operation": turn.plan.operation,
                    },
                }
                for index, turn in enumerate(selected_turns, start=1)
            ],
        }

    def _request(
        self,
        *,
        system_prompt: str,
        payload: dict[str, object],
        trace: list[dict[str, Any]] | None,
        stage_prefix: str,
    ) -> str:
        if not self.available:
            raise ConfigurationError(
                "LLM兜底未配置；请设置TEXT2SQL_LLM_URL和TEXT2SQL_LLM_API_KEY"
            )
        request_stage = f"{stage_prefix}_request"
        response_stage = f"{stage_prefix}_response"
        if trace is not None:
            trace.append({"stage": request_stage, "context": payload})
        try:
            result = self.client.complete_json(
                system_prompt=system_prompt,
                payload=payload,
            )
        except LLMTransportError as exc:
            if trace is not None:
                trace.append(
                    {
                        "stage": f"{stage_prefix}_transport_error",
                        "error": str(exc),
                    }
                )
            raise PlanningError(f"LLM网络调用失败：{exc.cause_type}") from exc
        except LLMResponseError as exc:
            if trace is not None:
                trace.append(
                    {
                        "stage": f"{stage_prefix}_response_error",
                        "error": str(exc),
                    }
                )
            raise PlanningError(f"LLM调用失败：{exc.cause_type}") from exc
        raw_content = result.content
        if trace is not None:
            trace.append(
                {
                    "stage": response_stage,
                    "status_code": result.status_code,
                    "content": raw_content,
                }
            )
        return raw_content
