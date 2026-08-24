"""Rule-first orchestration with a bounded LLM fallback and one validation path."""

from __future__ import annotations

from dataclasses import replace
import re
import uuid

from .answerer import format_answer, has_deterministic_answer
from .business_rules import PROVINCE_ORGANIZATION_COUNT
from .config import Settings
from .context_router import ContextDecision, ContextRouter
from .datasource import DataSourceAdapter, DuckDBDataSourceAdapter
from .date_resolver import comparison_date
from .errors import (
    ClarificationError,
    ConfigurationError,
    PlanningError,
    ResultValidationError,
    Text2SQLError,
    ValidationError,
    retry_feedback,
)
from .llm_planner import LLMPlanner
from .models import QueryPlan, QueryResponse, QueryResult, SessionState
from .pending_query import (
    PendingQueryResolver,
    is_slot_only_reply,
    should_start_pending,
)
from .rule_planner import RuleDecision, RulePlanner
from .semantic_catalog import LOWER_IS_BETTER
from .session_store import InMemorySessionStore
from .sql_compiler import compile_global_rank_sql, compile_rule_sql
from .validators import (
    AlignmentValidator,
    AnswerCoverageValidator,
    PlanValidator,
    ResultValidator,
    SQLGuard,
    answer_obligations,
)


class Text2SQLService:
    def __init__(
        self,
        settings: Settings | None = None,
        session_store: InMemorySessionStore | None = None,
        llm_planner: LLMPlanner | None = None,
        data_source: DataSourceAdapter | None = None,
    ) -> None:
        self.settings = settings or Settings.from_env()
        self.data_source = data_source or DuckDBDataSourceAdapter.from_settings(
            self.settings
        )
        self.db_path = self.data_source.db_path
        self.catalog = self.data_source.catalog
        self.sessions = session_store or InMemorySessionStore()
        self.context_router = ContextRouter(self.catalog)
        self.rule_planner = RulePlanner(self.catalog)
        self.pending_resolver = PendingQueryResolver(self.catalog, self.rule_planner)
        self.llm_planner = llm_planner or LLMPlanner(self.settings)
        self.answer_coverage = AnswerCoverageValidator()
        self.plan_validator = PlanValidator(self.catalog)
        self.sql_guard = SQLGuard(self.settings.default_row_limit, self.settings.hard_row_limit)
        self.alignment_validator = AlignmentValidator(self.catalog)
        self.result_validator = ResultValidator()
        self.executor = self.data_source

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
            answer_mode="rule" if has_deterministic_answer(plan, result) else "llm",
            session_id=session_id,
            route=plan.source,
            sql=sql,
            plan=plan.to_dict(),
            columns=result.columns,
            rows=result.rows,
            warnings=warnings,
        )

    def _validate_answer_coverage(
        self,
        question: str,
        plan: QueryPlan,
        trace: list[dict[str, object]],
    ) -> None:
        required, missing = self.answer_coverage.analyze(question, plan)
        trace.append(
            {
                "stage": "answer_coverage",
                "required": required,
                "missing": missing,
                "operation": plan.operation,
            }
        )
        self.answer_coverage.validate(question, plan)

    def _rule_attempt(
        self,
        *,
        planning_question: str,
        response_question: str,
        session_id: str,
        response_state: SessionState,
        planning_state: SessionState,
        use_context: bool,
        trace: list[dict[str, object]],
        stage_prefix: str = "rule",
    ) -> tuple[QueryResponse | None, RuleDecision, Exception | None]:
        decision = self.rule_planner.plan(
            planning_question,
            planning_state,
            use_context=use_context,
        )
        trace.append(
            {
                "stage": f"{stage_prefix}_decision",
                "planning_question": planning_question,
                "use_context": use_context,
                "reason": decision.reason,
                "candidates": decision.candidates,
                "plan": decision.plan.to_dict() if decision.plan else None,
            }
        )
        if decision.plan is None:
            return None, decision, None
        try:
            self._validate_answer_coverage(response_question, decision.plan, trace)
            raw_sql = compile_rule_sql(decision.plan)
            trace.append({"stage": f"{stage_prefix}_sql", "sql": raw_sql})
            plan = replace(decision.plan, sql=raw_sql)
            sql, result = self._validated_execute(plan, raw_sql, trace)
            return (
                self._response(
                    session_id,
                    response_question,
                    response_state,
                    plan,
                    sql,
                    result,
                ),
                decision,
                None,
            )
        except Text2SQLError as exc:
            trace.append(
                {
                    "stage": f"{stage_prefix}_error",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            return None, decision, exc

    def _validate_context_rewrite(
        self,
        original_question: str,
        rewritten_question: str,
    ) -> None:
        original_orgs = set(self.catalog.resolve_organizations(original_question))
        rewritten_orgs = set(self.catalog.resolve_organizations(rewritten_question))
        if not original_orgs.issubset(rewritten_orgs):
            raise PlanningError("上下文改写覆盖了当前问题明确指定的机构")

        original_metrics = self._target_metrics(original_question)
        rewritten_metrics = self._target_metrics(rewritten_question)
        if not original_metrics.issubset(rewritten_metrics):
            raise PlanningError("上下文改写遗漏了当前问题明确指定的指标")

        original_date = self._explicit_current_date(original_question)
        rewritten_date = self._explicit_current_date(rewritten_question)
        if original_date and rewritten_date != original_date:
            raise PlanningError("上下文改写改变了当前问题明确指定的日期")

        original_required = set(answer_obligations(original_question))
        rewritten_required = set(answer_obligations(rewritten_question))
        missing_obligations = original_required - rewritten_required
        if missing_obligations:
            raise PlanningError(
                "上下文改写遗漏回答义务：" + ",".join(sorted(missing_obligations))
            )

        rewritten_context = self.context_router.classify(
            rewritten_question,
            SessionState(),
        )
        if rewritten_context.dependency != "self_contained":
            raise PlanningError("上下文改写后仍包含未消解的历史引用")

    @staticmethod
    def _explicit_current_date(question: str) -> str | None:
        from .date_resolver import resolve_date

        return resolve_date(question)

    def _target_metrics(self, question: str) -> set[str]:
        explicitly_resolved = set(self.catalog.resolve_metrics(question))
        if len(explicitly_resolved) <= 1:
            return explicitly_resolved
        if re.search(r"同时满足|以下条件|(?:且|并且).*?(?:均值|要求|阈值)", question):
            return explicitly_resolved
        analysis = self.rule_planner.plan(
            question,
            SessionState(),
            use_context=False,
        )
        candidates = analysis.candidates.get("metrics")
        if isinstance(candidates, (tuple, list)):
            resolved = {
                item
                for item in candidates
                if isinstance(item, str) and item in self.catalog.metrics
            }
            if resolved:
                return resolved
        return explicitly_resolved

    def _rewrite_context(
        self,
        question: str,
        state: SessionState,
        context: ContextDecision,
        trace: list[dict[str, object]],
    ) -> str:
        feedback: str | None = None
        last_error: Exception | None = None
        for attempt in range(1, self.settings.llm_retries + 2):
            try:
                trace.append(
                    {
                        "stage": "context_rewrite_attempt",
                        "attempt": attempt,
                        "history_limit": context.history_limit,
                        "feedback": feedback,
                    }
                )
                rewrite = self.llm_planner.rewrite(
                    question,
                    state,
                    history_limit=context.history_limit,
                    feedback=feedback,
                    trace=trace,
                )
                if rewrite.unresolved_references or not rewrite.rewritten_question:
                    details = "、".join(rewrite.unresolved_references) or "引用对象"
                    raise ClarificationError(f"上下文引用存在歧义，请明确：{details}")
                self._validate_context_rewrite(question, rewrite.rewritten_question)
                trace.append(
                    {
                        "stage": "context_rewrite_validated",
                        "rewritten_question": rewrite.rewritten_question,
                        "used_turn_indexes": rewrite.used_turn_indexes,
                    }
                )
                return rewrite.rewritten_question
            except ClarificationError:
                raise
            except Text2SQLError as exc:
                last_error = exc
                feedback = retry_feedback(exc)
                trace.append(
                    {
                        "stage": "context_rewrite_error",
                        "attempt": attempt,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
        if last_error:
            raise last_error
        raise PlanningError("LLM未能把上下文问题改写为自包含问题")

    def _llm_attempts(
        self,
        planning_question: str,
        response_question: str,
        session_id: str,
        response_state: SessionState,
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
                    question=planning_question,
                    schema_context=self.catalog.schema_context(planning_question),
                    rule_candidates=decision.candidates,
                    state=SessionState(),
                    feedback=feedback,
                    trace=trace,
                    include_history=False,
                )
                plan = self._augment_llm_plan(planning_question, plan)
                self._validate_answer_coverage(response_question, plan, trace)
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
                warnings = (
                    (f"规则路径降级：{decision.reason}",)
                    if decision.plan or planning_question != response_question
                    else ()
                )
                return self._response(
                    session_id,
                    response_question,
                    response_state,
                    plan,
                    sql,
                    result,
                    warnings,
                )
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
                "pending_query": (
                    {
                        "original_question": state.pending_query.original_question,
                        "missing_slots": state.pending_query.missing_slots,
                        "supplements": state.pending_query.supplements,
                    }
                    if state.pending_query
                    else None
                ),
            }
        )

        if state.pending_query is not None:
            resolution = self.pending_resolver.resolve(question, state.pending_query)
            trace.append(
                {
                    "stage": "pending_query_resolution",
                    "action": resolution.action,
                    "missing_slots": (
                        resolution.pending.missing_slots if resolution.pending else ()
                    ),
                    "resolved_question": resolution.resolved_question,
                }
            )
            if resolution.action == "new_question":
                self.sessions.clear_pending(session_id)
                state = self.sessions.get(session_id)
            elif resolution.action == "cancelled":
                self.sessions.clear_pending(session_id)
                raise ClarificationError(resolution.prompt or "已取消上一条未完成的查询。")
            elif resolution.action == "clarify":
                if resolution.pending is None:
                    raise PlanningError("待补查询解析器未返回可保存的状态")
                self.sessions.set_pending(session_id, resolution.pending)
                raise ClarificationError(
                    resolution.prompt
                    or self.pending_resolver.clarification_prompt(
                        resolution.pending.missing_slots
                    )
                )
            elif resolution.action == "ready":
                if (
                    resolution.pending is None
                    or resolution.planning_state is None
                    or not resolution.resolved_question
                ):
                    raise PlanningError("待补查询解析器未返回完整的执行信息")
                response, pending_decision, pending_rule_error = self._rule_attempt(
                    planning_question=resolution.resolved_question,
                    response_question=resolution.resolved_question,
                    session_id=session_id,
                    response_state=state,
                    planning_state=SessionState(),
                    use_context=False,
                    trace=trace,
                    stage_prefix="pending_rule",
                )
                if response is not None:
                    return response

                # A completed slot set can still describe a complex query. Keep
                # the normal LLM fallback instead of treating rule non-coverage
                # as an error.
                feedback = (
                    retry_feedback(pending_rule_error)
                    if pending_rule_error
                    else pending_decision.reason
                )
                return self._llm_attempts(
                    resolution.resolved_question,
                    resolution.resolved_question,
                    session_id,
                    state,
                    pending_decision,
                    feedback,
                    trace,
                )

        context = self.context_router.classify(question, state)
        trace.append(
            {
                "stage": "context_decision",
                "dependency": context.dependency,
                "allow_context_rule": context.allow_context_rule,
                "history_limit": context.history_limit,
                "reason": context.reason,
            }
        )

        if context.dependency == "self_contained":
            response, decision, rule_error = self._rule_attempt(
                planning_question=question,
                response_question=question,
                session_id=session_id,
                response_state=state,
                planning_state=SessionState(),
                use_context=False,
                trace=trace,
            )
            if response is not None:
                return response
            if rule_error is None and should_start_pending(question, decision):
                pending = self.pending_resolver.start(question, decision)
                self.sessions.set_pending(session_id, pending)
                trace.append(
                    {
                        "stage": "pending_query_created",
                        "missing_slots": pending.missing_slots,
                    }
                )
                raise ClarificationError(
                    self.pending_resolver.clarification_prompt(pending.missing_slots)
                )
            if rule_error is None and is_slot_only_reply(question, self.catalog):
                raise ClarificationError(
                    "当前线程没有可补充的未完成查询。请重新发送包含机构、指标和日期的完整问题。"
                )
            feedback = retry_feedback(rule_error) if rule_error else decision.reason
            return self._llm_attempts(
                question,
                question,
                session_id,
                state,
                decision,
                feedback,
                trace,
            )

        if context.allow_context_rule:
            response, _, context_rule_error = self._rule_attempt(
                planning_question=question,
                response_question=question,
                session_id=session_id,
                response_state=state,
                planning_state=state,
                use_context=True,
                trace=trace,
                stage_prefix="context_rule",
            )
            if response is not None:
                return response
            if context_rule_error is not None:
                trace.append(
                    {
                        "stage": "context_rule_fallback",
                        "reason": retry_feedback(context_rule_error),
                    }
                )

        rewritten_question = self._rewrite_context(question, state, context, trace)
        response, rewritten_decision, rewritten_rule_error = self._rule_attempt(
            planning_question=rewritten_question,
            response_question=question,
            session_id=session_id,
            response_state=state,
            planning_state=SessionState(),
            use_context=False,
            trace=trace,
            stage_prefix="rewritten_rule",
        )
        if response is not None:
            return response
        feedback = (
            retry_feedback(rewritten_rule_error)
            if rewritten_rule_error
            else rewritten_decision.reason
        )
        return self._llm_attempts(
            rewritten_question,
            question,
            session_id,
            state,
            rewritten_decision,
            feedback,
            trace,
        )

    def clear_session(self, session_id: str) -> None:
        self.sessions.clear(session_id)
