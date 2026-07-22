"""Rule-first orchestration with a bounded LLM fallback and one validation path."""

from __future__ import annotations

from dataclasses import replace
import re
import uuid

from .answerer import format_answer
from .business_rules import PROVINCE_ORGANIZATION_COUNT
from .config import Settings
from .data_builder import ensure_database
from .date_resolver import comparison_date
from .errors import (
    ConfigurationError,
    ResultValidationError,
    Text2SQLError,
    ValidationError,
    retry_feedback,
)
from .executor import DuckDBExecutor
from .llm_planner import LLMPlanner
from .models import QueryPlan, QueryResponse, QueryResult, SessionState
from .rule_planner import RuleDecision, RulePlanner
from .semantic_catalog import LOWER_IS_BETTER, SemanticCatalog
from .session_store import InMemorySessionStore
from .sql_compiler import compile_global_rank_sql, compile_rule_sql
from .validators import AlignmentValidator, PlanValidator, ResultValidator, SQLGuard


class Text2SQLService:
    def __init__(
        self,
        settings: Settings | None = None,
        session_store: InMemorySessionStore | None = None,
        llm_planner: LLMPlanner | None = None,
    ) -> None:
        self.settings = settings or Settings.from_env()
        self.db_path = ensure_database(self.settings.xlsx_path, self.settings.db_path)
        self.catalog = SemanticCatalog(self.db_path)
        self.sessions = session_store or InMemorySessionStore()
        self.rule_planner = RulePlanner(self.catalog)
        self.llm_planner = llm_planner or LLMPlanner(self.settings)
        self.plan_validator = PlanValidator(self.catalog)
        self.sql_guard = SQLGuard(self.settings.default_row_limit, self.settings.hard_row_limit)
        self.alignment_validator = AlignmentValidator(self.catalog)
        self.result_validator = ResultValidator()
        self.executor = DuckDBExecutor(self.db_path, self.settings.hard_row_limit)

    def _validated_execute(
        self,
        plan: QueryPlan,
        raw_sql: str,
        trace: list[dict[str, object]] | None = None,
    ) -> tuple[str, QueryResult]:
        if trace is not None:
            trace.append({"stage": "plan_validation", "plan": plan.to_dict()})
        self.plan_validator.validate(plan)
        approved_sql, expression = self.sql_guard.validate_and_limit(raw_sql)
        if trace is not None:
            trace.append({"stage": "sql_guard", "raw_sql": raw_sql, "approved_sql": approved_sql})
        self.alignment_validator.validate(plan, raw_sql, expression)
        if trace is not None:
            trace.append({"stage": "alignment_validation", "status": "ok"})
        self.executor.preflight(approved_sql)
        if trace is not None:
            trace.append({"stage": "sql_preflight", "status": "ok"})
        result = self.executor.execute(approved_sql)
        if trace is not None:
            trace.append(
                {
                    "stage": "sql_execution",
                    "columns": result.columns,
                    "row_count": result.row_count,
                    "rows": result.rows[:20],
                    "rows_truncated_in_trace": result.row_count > 20,
                }
            )
        self.result_validator.validate(plan, result)
        self._validate_rank_semantics(plan, result)
        self._validate_province_average_semantics(plan, result)
        if trace is not None:
            trace.append({"stage": "result_validation", "status": "ok"})
        return approved_sql, result

    @staticmethod
    def _augment_llm_plan(question: str, plan: QueryPlan) -> QueryPlan:
        if plan.current_date:
            expected_comparison, comparison_kind = comparison_date(question, plan.current_date)
            if comparison_kind in {
                "year_beginning",
                "previous_month",
                "previous_quarter",
                "same_period_last_year",
            } and plan.comparison_date != expected_comparison:
                raise ValidationError(
                    f"{comparison_kind}比较期违反衍生维度说明："
                    f"应为{expected_comparison}，实际为{plan.comparison_date}"
                )
        assumptions = list(plan.assumptions)
        if re.search(r"(?:各自在全省|全省\d*家里).*排第几|排名.*(?:怎么变|变化)", question):
            assumptions.append("rank_population_all")
        if "哪些" in question and re.search(r"一共有几家|有几家|多少家", question):
            assumptions.append("require_organization_list")
        return replace(plan, assumptions=tuple(dict.fromkeys(assumptions)))

    def _validate_rank_semantics(self, plan: QueryPlan, result: QueryResult) -> None:
        """Verify selected-organization ranks against the full organization population."""
        if plan.source != "llm" or "rank_population_all" not in plan.assumptions:
            return
        column_map = {name.lower(): index for index, name in enumerate(result.columns)}
        metric_index = column_map.get("metric_id")
        org_index = column_map.get("org_id")
        if metric_index is None:
            raise ResultValidationError("全省排名结果缺少metric_id，无法验证排名口径")
        current_rank_index = next(
            (column_map[name] for name in ("current_rank", "metric_rank", "rank") if name in column_map),
            None,
        )
        comparison_rank_index = next(
            (column_map[name] for name in ("previous_rank", "comparison_rank", "base_rank") if name in column_map),
            None,
        )
        if current_rank_index is None:
            raise ResultValidationError("问题要求全省排名，但结果缺少可验证的排名列")

        quoted_metrics = ", ".join(f"'{metric}'" for metric in plan.metrics)
        lower_metrics = tuple(metric for metric in plan.metrics if metric in LOWER_IS_BETTER)
        quoted_lower = ", ".join(f"'{metric}'" for metric in lower_metrics) or "''"

        def expected_ranks(data_date: str) -> dict[tuple[str, str], int]:
            sql = f"""
                WITH ranked AS (
                    SELECT org_id, metric_id,
                           RANK() OVER (
                               PARTITION BY metric_id
                               ORDER BY
                                   CASE WHEN metric_id IN ({quoted_lower}) THEN metric_value END ASC,
                                   CASE WHEN metric_id NOT IN ({quoted_lower}) THEN metric_value END DESC
                           ) AS expected_rank
                    FROM metric_values
                    WHERE data_date = DATE '{data_date}'
                      AND metric_id IN ({quoted_metrics})
                )
                SELECT org_id, metric_id, expected_rank FROM ranked
            """
            oracle = self.executor.execute(sql)
            return {(str(org), str(metric)): int(rank) for org, metric, rank in oracle.rows}

        def validate_column(data_date: str | None, rank_index: int | None, label: str) -> None:
            if not data_date or rank_index is None:
                return
            oracle = expected_ranks(data_date)
            for row in result.rows:
                metric = str(row[metric_index])
                org = str(row[org_index]) if org_index is not None else (
                    plan.organizations[0] if len(plan.organizations) == 1 else ""
                )
                actual = row[rank_index]
                expected = oracle.get((org, metric))
                if expected is None or actual is None or int(actual) != expected:
                    raise ResultValidationError(
                        f"{label}排名口径错误：{org}/{metric}返回{actual}，全省排名应为{expected}"
                    )

        validate_column(plan.current_date, current_rank_index, "当前期")
        validate_column(plan.comparison_date, comparison_rank_index, "比较期")

    def _validate_province_average_semantics(
        self, plan: QueryPlan, result: QueryResult
    ) -> None:
        """Verify every returned province average against all 13 organizations."""
        if not plan.current_date or "province_average" not in result.columns:
            return
        column_map = {name.lower(): index for index, name in enumerate(result.columns)}
        average_index = column_map["province_average"]
        metric_index = column_map.get("metric_id")
        if metric_index is None and len(plan.metrics) != 1:
            raise ResultValidationError("全省均值结果缺少metric_id，无法验证13家机构口径")
        quoted_metrics = ", ".join(f"'{metric}'" for metric in plan.metrics)
        oracle = self.executor.execute(
            f"""
            SELECT metric_id, AVG(metric_value) AS province_average,
                   COUNT(DISTINCT org_id) AS organization_count
            FROM metric_values
            WHERE data_date = DATE '{plan.current_date}'
              AND metric_id IN ({quoted_metrics})
            GROUP BY metric_id
            """
        )
        expected = {
            str(metric): (float(average), int(count))
            for metric, average, count in oracle.rows
        }
        for row in result.rows:
            metric = str(row[metric_index]) if metric_index is not None else plan.metrics[0]
            expected_average, organization_count = expected.get(metric, (float("nan"), 0))
            actual_average = row[average_index]
            if organization_count != PROVINCE_ORGANIZATION_COUNT:
                raise ResultValidationError(
                    f"{metric}全省均值仅覆盖{organization_count}家机构，必须覆盖"
                    f"{PROVINCE_ORGANIZATION_COUNT}家"
                )
            if actual_average is None or abs(float(actual_average) - expected_average) > max(
                1e-9, abs(expected_average) * 1e-9
            ):
                raise ResultValidationError(
                    f"{metric}全省均值口径错误：返回{actual_average}，"
                    f"13家机构算数平均值应为{expected_average}"
                )

    def _response(
        self,
        session_id: str,
        question: str,
        state: SessionState,
        plan: QueryPlan,
        sql: str,
        result: QueryResult,
        warnings: tuple[str, ...] = (),
    ) -> QueryResponse:
        answer = format_answer(
            plan,
            result,
            self.catalog,
            question=question,
            history=state.recent_turns,
        )
        self.sessions.update(session_id, plan, result, question=question, answer=answer)
        return QueryResponse(
            answer=answer,
            session_id=session_id,
            route=plan.source,
            sql=sql,
            plan=plan.to_dict(),
            columns=result.columns,
            rows=result.rows,
            warnings=warnings,
        )

    def _llm_attempts(
        self,
        question: str,
        session_id: str,
        state: SessionState,
        decision: RuleDecision,
        initial_feedback: str | None,
        trace: list[dict[str, object]],
    ) -> QueryResponse:
        feedback = initial_feedback
        last_error: Exception | None = None
        for attempt in range(1, self.settings.llm_retries + 2):
            try:
                trace.append({"stage": "llm_attempt", "attempt": attempt, "feedback": feedback})
                plan = self.llm_planner.plan(
                    question=question,
                    schema_context=self.catalog.schema_context(question),
                    rule_candidates=decision.candidates,
                    state=state,
                    feedback=feedback,
                    trace=trace,
                )
                plan = self._augment_llm_plan(question, plan)
                if (
                    "rank_population_all" in plan.assumptions
                    and plan.organization_scope == "selected"
                    and plan.organizations
                    and plan.current_date
                ):
                    plan = replace(plan, sql=compile_global_rank_sql(plan))
                    trace.append({"stage": "deterministic_rank_compilation", "sql": plan.sql})
                trace.append({"stage": "llm_plan", "attempt": attempt, "plan": plan.to_dict()})
                if not plan.sql:
                    raise ConfigurationError("LLM计划缺少SQL")
                sql, result = self._validated_execute(plan, plan.sql, trace)
                warnings = (f"规则路径降级：{decision.reason}",) if decision.plan else ()
                return self._response(session_id, question, state, plan, sql, result, warnings)
            except Text2SQLError as exc:
                last_error = exc
                feedback = retry_feedback(exc)
                trace.append(
                    {
                        "stage": "llm_attempt_error",
                        "attempt": attempt,
                        "error": f"{type(exc).__name__}: {exc}",
                        "retry_feedback": feedback,
                    }
                )
        if last_error:
            raise last_error
        raise ConfigurationError("LLM兜底未能生成有效查询")

    def ask(self, question: str, session_id: str | None = None) -> QueryResponse:
        if not isinstance(question, str) or not question.strip():
            raise ValueError("问题不能为空")
        session_id = session_id or uuid.uuid4().hex
        trace: list[dict[str, object]] = [
            {"stage": "request", "question": question, "session_id": session_id}
        ]
        try:
            return self._ask(question, session_id, trace)
        except Exception as exc:
            trace.append({"stage": "failed", "error": f"{type(exc).__name__}: {exc}"})
            exc.diagnostics = trace
            raise

    def _ask(
        self,
        question: str,
        session_id: str,
        trace: list[dict[str, object]],
    ) -> QueryResponse:
        state = self.sessions.get(session_id)
        trace.append(
            {
                "stage": "session_state",
                "last_organizations": state.last_organizations,
                "last_result_organizations": state.last_result_organizations,
                "last_metrics": state.last_metrics,
                "last_date": state.last_date,
                "last_comparison_date": state.last_comparison_date,
                "last_operation": state.last_operation,
                "recent_turn_count": len(state.recent_turns),
            }
        )
        decision = self.rule_planner.plan(question, state)
        trace.append(
            {
                "stage": "rule_decision",
                "reason": decision.reason,
                "candidates": decision.candidates,
                "plan": decision.plan.to_dict() if decision.plan else None,
            }
        )
        rule_error: Exception | None = None
        if decision.plan is not None:
            try:
                raw_sql = compile_rule_sql(decision.plan)
                trace.append({"stage": "rule_sql", "sql": raw_sql})
                plan = replace(decision.plan, sql=raw_sql)
                sql, result = self._validated_execute(plan, raw_sql, trace)
                return self._response(session_id, question, state, plan, sql, result)
            except Text2SQLError as exc:
                rule_error = exc
                trace.append({"stage": "rule_error", "error": f"{type(exc).__name__}: {exc}"})
        feedback = retry_feedback(rule_error) if rule_error else decision.reason
        return self._llm_attempts(question, session_id, state, decision, feedback, trace)

    def clear_session(self, session_id: str) -> None:
        self.sessions.clear(session_id)
