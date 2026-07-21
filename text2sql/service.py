"""Rule-first orchestration with a bounded LLM fallback and one validation path."""

from __future__ import annotations

from dataclasses import replace
import uuid

from .answerer import format_answer
from .config import Settings
from .data_builder import ensure_database
from .errors import ConfigurationError, Text2SQLError, retry_feedback
from .executor import DuckDBExecutor
from .llm_planner import LLMPlanner
from .models import QueryPlan, QueryResponse, QueryResult, SessionState
from .rule_planner import RuleDecision, RulePlanner
from .semantic_catalog import SemanticCatalog
from .session_store import InMemorySessionStore
from .sql_compiler import compile_rule_sql
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
        self.alignment_validator = AlignmentValidator()
        self.result_validator = ResultValidator()
        self.executor = DuckDBExecutor(self.db_path, self.settings.hard_row_limit)

    def _validated_execute(self, plan: QueryPlan, raw_sql: str) -> tuple[str, QueryResult]:
        self.plan_validator.validate(plan)
        approved_sql, expression = self.sql_guard.validate_and_limit(raw_sql)
        self.alignment_validator.validate(plan, raw_sql, expression)
        self.executor.preflight(approved_sql)
        result = self.executor.execute(approved_sql)
        self.result_validator.validate(plan, result)
        return approved_sql, result

    def _response(
        self,
        session_id: str,
        plan: QueryPlan,
        sql: str,
        result: QueryResult,
        warnings: tuple[str, ...] = (),
    ) -> QueryResponse:
        self.sessions.update(session_id, plan, result)
        return QueryResponse(
            answer=format_answer(plan, result, self.catalog),
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
    ) -> QueryResponse:
        feedback = initial_feedback
        last_error: Exception | None = None
        for _ in range(self.settings.llm_retries + 1):
            try:
                plan = self.llm_planner.plan(
                    question=question,
                    schema_context=self.catalog.schema_context(question),
                    rule_candidates=decision.candidates,
                    state=state,
                    feedback=feedback,
                )
                if not plan.sql:
                    raise ConfigurationError("LLM计划缺少SQL")
                sql, result = self._validated_execute(plan, plan.sql)
                warnings = (f"规则路径降级：{decision.reason}",) if decision.plan else ()
                return self._response(session_id, plan, sql, result, warnings)
            except Text2SQLError as exc:
                last_error = exc
                feedback = retry_feedback(exc)
        if last_error:
            raise last_error
        raise ConfigurationError("LLM兜底未能生成有效查询")

    def ask(self, question: str, session_id: str | None = None) -> QueryResponse:
        if not isinstance(question, str) or not question.strip():
            raise ValueError("问题不能为空")
        session_id = session_id or uuid.uuid4().hex
        state = self.sessions.get(session_id)
        decision = self.rule_planner.plan(question, state)
        rule_error: Exception | None = None
        if decision.plan is not None:
            try:
                raw_sql = compile_rule_sql(decision.plan)
                plan = replace(decision.plan, sql=raw_sql)
                sql, result = self._validated_execute(plan, raw_sql)
                return self._response(session_id, plan, sql, result)
            except Text2SQLError as exc:
                rule_error = exc
        feedback = retry_feedback(rule_error) if rule_error else decision.reason
        return self._llm_attempts(question, session_id, state, decision, feedback)

    def clear_session(self, session_id: str) -> None:
        self.sessions.clear(session_id)

