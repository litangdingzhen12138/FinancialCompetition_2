"""Small session-id keyed state store; replaceable by Redis without changing planners."""

from __future__ import annotations

from copy import deepcopy
from threading import RLock

from .models import PendingQuery, QueryPlan, QueryResult, SessionState, TurnMemory


class InMemorySessionStore:
    def __init__(self) -> None:
        self._states: dict[str, SessionState] = {}
        self._lock = RLock()

    def get(self, session_id: str) -> SessionState:
        with self._lock:
            return deepcopy(self._states.get(session_id, SessionState()))

    def update(
        self,
        session_id: str,
        plan: QueryPlan,
        result: QueryResult,
        question: str = "",
        answer: str = "",
    ) -> None:
        with self._lock:
            previous = self._states.get(session_id, SessionState())
            returned_orgs: list[str] = []
            if "org_id" in result.columns:
                index = result.columns.index("org_id")
                for row in result.rows:
                    value = str(row[index])
                    if value not in returned_orgs:
                        returned_orgs.append(value)
            self._states[session_id] = SessionState(
                last_organizations=plan.organizations,
                last_metrics=plan.metrics,
                last_date=plan.current_date,
                last_comparison_date=plan.comparison_date,
                last_operation=plan.operation,
                last_query_type=plan.query_type,
                last_result_organizations=tuple(returned_orgs),
                recent_turns=(
                    *previous.recent_turns,
                    TurnMemory(question=question, plan=plan, result=result, answer=answer),
                )[-20:],
                pending_query=None,
            )

    def set_pending(self, session_id: str, pending_query: PendingQuery) -> None:
        """Store an incomplete query without disturbing the successful focus state."""
        with self._lock:
            state = deepcopy(self._states.get(session_id, SessionState()))
            state.pending_query = pending_query
            self._states[session_id] = state

    def clear_pending(self, session_id: str) -> None:
        with self._lock:
            state = self._states.get(session_id)
            if state is not None:
                state.pending_query = None

    def clear(self, session_id: str) -> None:
        with self._lock:
            self._states.pop(session_id, None)
