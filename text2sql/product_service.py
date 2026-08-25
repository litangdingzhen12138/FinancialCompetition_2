"""Product orchestration for visualization, history, drill-down and permissions."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, time as clock_time, timedelta, timezone
import re
import time
from typing import Any, Iterator
import uuid
from threading import Lock

from .chart_recommender import ChartRecommender
from .final_answer import FinalAnswerGenerator
from .models import DataAccessScope, PendingQuery, QueryResult
from .product_models import (
    ChartSpec,
    DataColumn,
    Insight,
    ProductQueryResponse,
    TimeGranularity,
    UserContext,
)
from .product_store import ProductStore
from .service import Text2SQLService


API_VERSION = "v1"
SENSITIVE_METRICS = {"ZB013", "ZB014", "ZB015", "ZB016", "ZB017"}
CROSS_ORGANIZATION_OPERATIONS = {
    "rank",
    "province_average_compare",
    "multi_condition",
    "count_vs_average",
    "profile",
    "period_rank_extremes",
    "period_change_rank",
    "multi_rank",
    "multi_rank_change",
    "three_dimension_profile",
    "multi_metric_province_compare",
}
ALERT_S3_FREQUENCY = "S3_QUERY_FREQUENCY"
ALERT_ACCESS_DENIED = "REPEATED_ACCESS_DENIED"
ALERT_LARGE_EXPORT = "LARGE_EXPORT"
ALERT_DAILY_EXPORT = "DAILY_EXPORT_VOLUME"
ALERT_OFF_HOURS_EXPORT = "OFF_HOURS_S3_EXPORT"
ALERT_SHARE_FREQUENCY = "SHARE_LINK_FREQUENCY"
ALERT_MULTI_METRIC = "MULTI_METRIC_QUERY"
SHANGHAI_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
FINAL_ANSWER_FALLBACK_WARNING = (
    "最终回答模型调用失败，已返回基于查询结果生成的降级答案。"
)
VALUE_COLUMN_ORDER = (
    "metric_value",
    "current_value",
    "comparison_value",
    "change_value",
    "growth_rate",
    "derived_value",
    "result_value",
    "province_average",
)
VALUE_COLUMNS = set(VALUE_COLUMN_ORDER)
COLUMN_LABELS = {
    "period_start": "统计周期",
    "data_date": "数据日期",
    "org_id": "机构编码",
    "org_name": "机构名称",
    "metric_id": "指标编码",
    "metric_name": "指标名称",
    "metric_value": "指标值",
    "current_value": "当前值",
    "comparison_value": "比较期值",
    "change_value": "变化值",
    "growth_rate": "增幅",
    "metric_rank": "排名",
    "province_average": "全省均值",
    "unit": "单位",
}
NEXT_TIME_LEVEL: dict[TimeGranularity, TimeGranularity | None] = {
    "year": "quarter",
    "quarter": "month",
    "month": "day",
    "day": None,
}


class AccountFrozenError(PermissionError):
    def __init__(self, frozen_until: str, reason: str) -> None:
        super().__init__(f"账号因异常访问已临时冻结至 {frozen_until}：{reason}")
        self.frozen_until = frozen_until
        self.reason = reason


class LargeExportConfirmationRequired(ValueError):
    def __init__(self, row_count: int) -> None:
        super().__init__(f"本次导出共{row_count}行，超过200行，需要确认")
        self.row_count = row_count


class ProductQueryService:
    def __init__(
        self,
        core: Text2SQLService,
        store: ProductStore | None = None,
        final_answers: FinalAnswerGenerator | None = None,
    ) -> None:
        self.core = core
        self.store = store or ProductStore(core.settings.product_db_path)
        self.charts = ChartRecommender(core.catalog)
        self.final_answers = final_answers or FinalAnswerGenerator(core.settings)
        self._security_lock = Lock()

    def query(
        self,
        question: str,
        session_id: str | None,
        user: UserContext,
    ) -> ProductQueryResponse:
        public_session_id = session_id or uuid.uuid4().hex
        internal_session_id = self._internal_session_id(public_session_id, user)
        started = time.perf_counter()
        query_id = uuid.uuid4().hex
        self._audit(
            user,
            "query.requested",
            "low",
            {"question": question, "session_id": public_session_id},
            query_id,
        )
        try:
            self._ensure_not_frozen(user)
            if not user.can_query_data:
                raise PermissionError("当前业务角色无数据查询权限")
            self._authorize_question(question, user)
            self._restore_pending_session(
                internal_session_id,
                public_session_id,
                user,
            )
            response = self.core.ask(
                question,
                internal_session_id,
                plan_authorizer=lambda plan: self._authorize_plan(
                    plan.to_dict(),
                    user,
                ),
            )
            self.store.clear_pending_session(user.user_id, public_session_id)
            result = QueryResult(response.columns, response.rows)
            primary, alternatives = self.charts.recommend(response.plan, result)
            answer_mode = response.answer_mode
            answer_status = "completed" if answer_mode == "rule" else "pending"
            # Keep the core formatter output as a persisted fallback while the
            # optional final-answer model is pending.
            answer = response.answer
            insight = (
                self._build_insight(
                    question=question,
                    answer=response.answer,
                    route=response.route,
                    plan=response.plan,
                    records=result.dictionaries(),
                )
                if answer_mode == "rule"
                else None
            )
            duration_ms = round((time.perf_counter() - started) * 1000)
            generated_at = self._now()
            raw_record = {
                "query_id": query_id,
                "user_id": user.user_id,
                "session_id": public_session_id,
                "question": question,
                "route": response.route,
                "status": "completed",
                "sql": response.sql,
                "plan": response.plan,
                "columns": list(response.columns),
                "rows": [list(row) for row in response.rows],
                "answer": answer,
                "answer_mode": answer_mode,
                "answer_status": answer_status,
                "visualization": asdict(primary),
                "insight": asdict(insight) if insight else None,
                "warnings": list(response.warnings),
                "truncated": False,
                "duration_ms": duration_ms,
                "created_at": generated_at,
            }
            self.store.save_query(raw_record)
            metrics = sorted(set(response.plan.get("metrics") or ()))
            event_id = self._audit(
                user,
                "query.completed",
                "low",
                {
                    "question": question,
                    "route": response.route,
                    "row_count": len(response.rows),
                    "metrics": metrics,
                    "metric_count": len(metrics),
                    "organizations": list(response.plan.get("organizations") or ()),
                    "data_level": self._data_level(metrics),
                    "plan": response.plan,
                    "sql": response.sql,
                },
                query_id,
            )
            self._evaluate_query_alerts(
                user,
                event_id=event_id,
                query_id=query_id,
                metrics=metrics,
            )
            return self._response_from_record(
                raw_record,
                user,
                alternatives=alternatives,
            )
        except AccountFrozenError:
            raise
        except PermissionError as exc:
            self._persist_pending_session(
                internal_session_id,
                public_session_id,
                user,
            )
            self._record_access_denied(
                user,
                question=question,
                reason=str(exc),
                query_id=query_id,
            )
            raise
        except Exception as exc:
            self._persist_pending_session(
                internal_session_id,
                public_session_id,
                user,
            )
            self._audit(
                user,
                "query.failed",
                "high" if "SQL" in str(exc).upper() else "medium",
                {"question": question, "error": f"{type(exc).__name__}: {exc}"},
                query_id,
            )
            raise

    def _restore_pending_session(
        self,
        internal_session_id: str,
        public_session_id: str,
        user: UserContext,
    ) -> None:
        if self.core.sessions.get(internal_session_id).pending_query is not None:
            return
        payload = self.store.get_pending_session(user.user_id, public_session_id)
        if payload is None:
            return
        for field in ("organizations", "metrics", "missing_slots", "supplements"):
            payload[field] = tuple(payload.get(field) or ())
        self.core.sessions.set_pending(internal_session_id, PendingQuery(**payload))

    def _persist_pending_session(
        self,
        internal_session_id: str,
        public_session_id: str,
        user: UserContext,
    ) -> None:
        pending = self.core.sessions.get(internal_session_id).pending_query
        if pending is None:
            self.store.clear_pending_session(user.user_id, public_session_id)
            return
        self.store.save_pending_session(
            user.user_id,
            public_session_id,
            asdict(pending),
        )

    def time_drill(
        self,
        source_query_id: str,
        target: TimeGranularity,
        user: UserContext,
        *,
        period_start: str | None = None,
        period_end: str | None = None,
    ) -> ProductQueryResponse:
        self._ensure_not_frozen(user)
        if not user.can_query_data:
            reason = "当前业务角色无数据查询权限"
            self._record_access_denied(
                user,
                question="时间下钻",
                reason=reason,
                query_id=source_query_id,
            )
            raise PermissionError(reason)
        source = self._owned_query(source_query_id, user)
        plan = source["plan"]
        metrics = tuple(plan.get("metrics") or ())
        organizations = tuple(plan.get("organizations") or ())
        if len(metrics) != 1 or len(organizations) != 1:
            raise ValueError("时间下钻当前要求原查询只包含一个指标和一个机构")
        access_scope = self._authorize_plan(plan, user)
        start_date, end_date = self._drill_range(
            target,
            plan.get("current_date"),
            period_start,
            period_end,
            access_scope,
        )
        metric_id = self._safe_identifier(metrics[0], "指标")
        org_id = self._safe_identifier(organizations[0], "机构")
        sql = self._time_drill_sql(
            metric_id,
            org_id,
            target,
            start_date,
            end_date,
        )
        approved_sql, _ = self.core.sql_guard.validate_and_limit(sql)
        self.core.executor.preflight(approved_sql, access_scope)
        result = self.core.executor.execute(approved_sql, access_scope)
        query_id = uuid.uuid4().hex
        metric_name = self.core.catalog.metrics[metric_id].name
        organization_name = self.core.catalog.organizations[org_id]
        latest = result.dictionaries()[-1] if result.rows else None
        answer = (
            f"{organization_name}{metric_name}已按{self._level_label(target)}下钻，"
            f"共返回{result.row_count}个周期"
        )
        if latest:
            answer += (
                f"；最新周期{latest['period_start']}为"
                f"{latest['metric_value']}{latest['unit']}"
            )
        drill_plan = {
            "source": "rule",
            "query_type": "time_drill",
            "operation": "time_drill",
            "organizations": [org_id],
            "organization_scope": "selected",
            "metrics": [metric_id],
            "current_date": end_date.isoformat(),
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "dimensions": ["time"],
            "expected_shape": "time_series",
            "aggregation": "period_end",
            "time_granularity": target,
            "drill_path": [*(plan.get("drill_path") or []), target],
            "sql": approved_sql,
        }
        chart = ChartSpec(
            chart_type="line",
            title=f"{organization_name}{metric_name}{self._level_label(target)}趋势",
            x_field="period_start",
            y_fields=("metric_value",),
            unit=self.core.catalog.metrics[metric_id].unit,
            reason=f"按{self._level_label(target)}展示期末值变化",
            available_time_drill=(
                (NEXT_TIME_LEVEL[target],) if NEXT_TIME_LEVEL[target] else ()
            ),
        )
        insight = self._build_insight(
            question=f"时间下钻：{source['question']}",
            answer=answer,
            route="rule",
            plan=drill_plan,
            records=result.dictionaries(),
        )
        generated_at = self._now()
        record = {
            "query_id": query_id,
            "user_id": user.user_id,
            "session_id": source["session_id"],
            "question": f"时间下钻至{self._level_label(target)}：{source['question']}",
            "route": "rule",
            "status": "completed",
            "sql": approved_sql,
            "plan": drill_plan,
            "columns": list(result.columns),
            "rows": [list(row) for row in result.rows],
            "answer": answer,
            "answer_mode": "rule",
            "answer_status": "completed",
            "visualization": asdict(chart),
            "insight": asdict(insight),
            "warnings": ["时间聚合口径为周期最后一个有数据日期的期末值"],
            "truncated": result.truncated,
            "duration_ms": 0,
            "source_query_id": source_query_id,
            "created_at": generated_at,
        }
        self.store.save_query(record)
        event_id = self._audit(
            user,
            "query.time_drill",
            "low",
            {
                "source_query_id": source_query_id,
                "target": target,
                "metrics": [metric_id],
                "organizations": [org_id],
                "row_count": result.row_count,
                "data_level": self._data_level([metric_id]),
                "sql": approved_sql,
            },
            query_id,
        )
        self._evaluate_query_alerts(
            user,
            event_id=event_id,
            query_id=query_id,
            metrics=[metric_id],
        )
        return self._response_from_record(record, user)

    def history(
        self,
        user: UserContext,
        *,
        limit: int | None = None,
        keyword: str | None = None,
    ) -> list[dict[str, Any]]:
        self._ensure_not_frozen(user)
        owner = None if user.can_view_all_queries else user.user_id
        titles = self.store.list_session_titles(owner)
        records = self.store.list_queries(owner, limit=limit, keyword=keyword)
        visible_records = self._visible_history_records(records, user)
        items = [
            {
                "query_id": record["query_id"],
                "session_id": record["session_id"],
                "user_id": record["user_id"],
                "question": record["question"],
                "route": record["route"],
                "status": record["status"],
                "answer": (
                    "查询结果数值已按权限隐藏"
                    if self._should_mask_values(user, record["plan"])
                    else (
                        "最终回答生成中"
                        if record.get("answer_status") in {"pending", "streaming"}
                        else record["answer"] or "最终回答生成失败"
                    )
                ),
                "answer_mode": record.get("answer_mode", "rule"),
                "answer_status": record.get("answer_status", "completed"),
                "row_count": len(record["rows"]),
                "created_at": record["created_at"],
                "duration_ms": record["duration_ms"],
                "thread_title": titles.get(
                    (str(record["user_id"]), str(record["session_id"]))
                ),
            }
            for record in visible_records
        ]
        self._audit(
            user,
            "history.viewed",
            "low",
            {
                "record_count": len(items),
                "hidden_by_current_permissions": len(records) - len(visible_records),
                "keyword": keyword,
            },
        )
        return items

    def session_history(
        self,
        session_id: str,
        user: UserContext,
        *,
        owner_user_id: str | None = None,
    ) -> list[ProductQueryResponse]:
        self._ensure_not_frozen(user)
        owner = (
            owner_user_id
            if user.can_view_all_queries and owner_user_id
            else (None if user.can_view_all_queries else user.user_id)
        )
        records = self.store.list_queries(
            owner,
            limit=None,
            session_id=session_id,
            oldest_first=True,
        )
        visible_records = self._visible_history_records(records, user)
        return [
            self._response_from_record(record, user) for record in visible_records
        ]

    def rename_session(
        self,
        session_id: str,
        title: str,
        user: UserContext,
        *,
        owner_user_id: str | None = None,
    ) -> dict[str, str]:
        self._ensure_not_frozen(user)
        normalized = " ".join(title.split()).strip()
        if not normalized:
            raise ValueError("会话名称不能为空")
        target_user_id = (
            owner_user_id
            if user.can_view_all_queries and owner_user_id
            else user.user_id
        )
        self.store.set_session_title(target_user_id, session_id, normalized[:60])
        return {
            "session_id": session_id,
            "owner_user_id": target_user_id,
            "title": normalized[:60],
        }

    def batch_export_records(
        self,
        user: UserContext,
        *,
        scope: str,
        recent_count: int = 20,
        session_id: str | None = None,
        owner_user_id: str | None = None,
        confirm_large_export: bool = False,
    ) -> list[dict[str, Any]]:
        self._ensure_not_frozen(user)
        if not user.can_export:
            reason = "当前角色无导出权限"
            self._record_access_denied(
                user,
                question="批量导出历史记录",
                reason=reason,
                query_id=None,
            )
            raise PermissionError(reason)
        if scope not in {"all", "recent", "session"}:
            raise ValueError("不支持的批量导出范围")
        if scope == "session" and not session_id:
            raise ValueError("按会话导出时必须选择 thread_id")
        owner = owner_user_id if user.can_view_all_queries else user.user_id
        records = self.store.list_queries(
            owner,
            limit=max(1, min(recent_count, 5000)) if scope == "recent" else None,
            session_id=session_id if scope == "session" else None,
        )
        try:
            for record in records:
                self._authorize_plan(record["plan"], user)
        except PermissionError as exc:
            self._record_access_denied(
                user,
                question="批量导出历史记录",
                reason=str(exc),
                query_id=None,
            )
            raise
        exported_rows = sum(len(record["rows"]) for record in records)
        if exported_rows > 200 and not confirm_large_export:
            raise LargeExportConfirmationRequired(exported_rows)
        metrics = sorted(
            {
                str(metric)
                for record in records
                for metric in record["plan"].get("metrics") or ()
            }
        )
        event_id = self._audit(
            user,
            "history.batch_exported",
            "low",
            {
                "scope": scope,
                "record_count": len(records),
                "exported_rows": exported_rows,
                "session_id": session_id,
                "owner_user_id": owner,
                "metrics": metrics,
                "data_level": self._data_level(metrics),
            },
        )
        self._evaluate_export_alerts(
            user,
            event_id=event_id,
            query_id=None,
            exported_rows=exported_rows,
            metrics=metrics,
        )
        return [self._sanitize_record_for_user(record, user) for record in records]

    def get_query(self, query_id: str, user: UserContext) -> ProductQueryResponse:
        self._ensure_not_frozen(user)
        return self._response_from_record(self._owned_query(query_id, user), user)

    def stream_final_answer(
        self,
        query_id: str,
        user: UserContext,
    ) -> Iterator[tuple[str, dict[str, Any]]]:
        self._ensure_not_frozen(user)
        record = self._owned_query(query_id, user)
        mode = str(record.get("answer_mode") or "rule")
        yield "answer_start", {"query_id": query_id, "mode": mode}

        if self._should_mask_values(user, record["plan"]):
            insight = Insight(
                headline="最终回答",
                summary="敏感指标具体数值已按当前角色权限隐藏。",
                caveats=("如需查看具体数值，请申请相应数据权限。",),
                confidence="high",
            )
            yield "answer_done", {
                "query_id": query_id,
                "mode": "rule",
                "answer": insight.summary,
                "insight": asdict(insight),
            }
            return

        if mode == "rule" or record.get("answer_status") == "completed":
            insight_data = record.get("insight")
            insight = (
                Insight(**insight_data)
                if isinstance(insight_data, dict)
                else Insight(
                    headline="最终回答",
                    summary=str(record.get("answer") or ""),
                    confidence="high" if mode == "rule" else "medium",
                )
            )
            yield "answer_done", {
                "query_id": query_id,
                "mode": mode,
                "answer": insight.summary,
                "insight": asdict(insight),
            }
            return

        fallback_answer = str(record.get("answer") or "").strip()
        self.store.update_final_answer(
            query_id,
            answer=fallback_answer,
            insight=None,
            status="streaming",
        )
        chunks: list[str] = []
        try:
            for chunk in self.final_answers.stream(
                question=record["question"],
                columns=list(record["columns"]),
                rows=[list(row) for row in record["rows"]],
                truncated=bool(record.get("truncated")),
            ):
                chunks.append(chunk)
                yield "answer_delta", {
                    "query_id": query_id,
                    "content": chunk,
                }
            answer = "".join(chunks).strip()
            if not answer:
                raise ValueError("最终回答LLM未返回有效内容")
            insight = Insight(
                headline="最终回答",
                summary=answer,
                confidence="medium",
            )
            self.store.update_final_answer(
                query_id,
                answer=answer,
                insight=asdict(insight),
                status="completed",
            )
            self._audit(
                user,
                "answer.completed",
                "low",
                {"mode": "llm"},
                query_id,
            )
            yield "answer_done", {
                "query_id": query_id,
                "mode": "llm",
                "answer": answer,
                "insight": asdict(insight),
            }
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            if fallback_answer:
                insight = Insight(
                    headline="最终回答",
                    summary=fallback_answer,
                    caveats=(FINAL_ANSWER_FALLBACK_WARNING,),
                    confidence="medium",
                )
                self.store.update_final_answer(
                    query_id,
                    answer=fallback_answer,
                    insight=asdict(insight),
                    status="completed",
                    warning=FINAL_ANSWER_FALLBACK_WARNING,
                )
                self._audit(
                    user,
                    "answer.fallback",
                    "medium",
                    {"mode": "llm", "error": error},
                    query_id,
                )
                yield "answer_done", {
                    "query_id": query_id,
                    "mode": "llm",
                    "answer": fallback_answer,
                    "insight": asdict(insight),
                    "fallback": True,
                    "warning": FINAL_ANSWER_FALLBACK_WARNING,
                }
                return
            self.store.update_final_answer(
                query_id,
                answer="",
                insight=None,
                status="failed",
            )
            self._audit(
                user,
                "answer.failed",
                "medium",
                {"mode": "llm", "error": error},
                query_id,
            )
            raise

    def create_share(
        self,
        query_id: str,
        user: UserContext,
        *,
        expires_hours: int,
    ) -> dict[str, str]:
        self._ensure_not_frozen(user)
        if not user.can_share:
            reason = "当前角色无分享权限"
            self._record_access_denied(
                user,
                question="创建分享链接",
                reason=reason,
                query_id=query_id,
            )
            raise PermissionError(reason)
        self._owned_query(query_id, user)
        share_id = uuid.uuid4().hex
        expires_at = (
            datetime.now(timezone.utc) + timedelta(hours=max(1, min(expires_hours, 168)))
        ).isoformat()
        token = self.store.create_share(
            share_id=share_id,
            query_id=query_id,
            user_id=user.user_id,
            expires_at=expires_at,
        )
        event_id = self._audit(
            user,
            "query.shared",
            "low",
            {"share_id": share_id, "expires_at": expires_at},
            query_id,
        )
        self._evaluate_share_alerts(
            user,
            event_id=event_id,
            query_id=query_id,
        )
        return {"share_id": share_id, "token": token, "expires_at": expires_at}

    def resolve_share(self, token: str, user: UserContext) -> ProductQueryResponse:
        self._ensure_not_frozen(user)
        share = self.store.resolve_share(token)
        if not share:
            raise ValueError("分享链接无效、已撤销或已过期")
        creator_freeze = self.store.get_active_freeze(
            str(share["created_by"]),
            now=self._now_datetime(),
        )
        if creator_freeze:
            self._audit(
                user,
                "share.access_denied",
                "medium",
                {
                    "share_id": share["share_id"],
                    "reason": "creator_frozen",
                },
                str(share["query_id"]),
            )
            raise PermissionError("分享创建者账号已冻结，当前分享暂不可访问")
        record = self.store.get_query(str(share["query_id"]))
        if not record:
            raise ValueError("分享的查询记录不存在")
        try:
            self._authorize_plan(record["plan"], user)
        except PermissionError as exc:
            self._record_access_denied(
                user,
                question="访问分享查询结果",
                reason=str(exc),
                query_id=str(record["query_id"]),
            )
            raise
        self._audit(
            user,
            "share.viewed",
            "low",
            {"share_id": share["share_id"]},
            record["query_id"],
        )
        return self._response_from_record(record, user)

    def export_record(
        self,
        query_id: str,
        user: UserContext,
        *,
        confirm_large_export: bool = False,
    ) -> dict[str, Any]:
        self._ensure_not_frozen(user)
        if not user.can_export:
            reason = "当前角色无导出权限"
            self._record_access_denied(
                user,
                question="导出查询结果",
                reason=reason,
                query_id=query_id,
            )
            raise PermissionError(reason)
        record = self._owned_query(query_id, user)
        exported_rows = len(record["rows"])
        if exported_rows > 200 and not confirm_large_export:
            raise LargeExportConfirmationRequired(exported_rows)
        metrics = sorted(set(record["plan"].get("metrics") or ()))
        event_id = self._audit(
            user,
            "query.exported",
            "low",
            {
                "exported_rows": exported_rows,
                "metrics": metrics,
                "data_level": self._data_level(metrics),
            },
            query_id,
        )
        self._evaluate_export_alerts(
            user,
            event_id=event_id,
            query_id=query_id,
            exported_rows=exported_rows,
            metrics=metrics,
        )
        return self._sanitize_record_for_user(record, user)

    def admin_overview(self, user: UserContext) -> dict[str, Any]:
        self._require_admin(user)
        return {
            **self.store.overview(),
            "audit_chain_valid": self.store.verify_audit_chain(),
            "open_alert_count": len(self.store.list_alerts(status="open")),
        }

    def admin_users(self, user: UserContext) -> list[dict[str, Any]]:
        self._require_admin(user)
        return self.store.list_audit_users()

    def admin_audit(
        self,
        user: UserContext,
        *,
        sort_by: str = "newest",
        risk_level: str = "all",
        user_id: str | None = None,
    ) -> list[dict[str, Any]]:
        self._require_admin(user)
        return self.store.list_audit(
            sort_by=sort_by,
            risk_level=risk_level,
            user_id=user_id,
        )

    def admin_alerts(
        self,
        user: UserContext,
        *,
        status: str = "all",
        severity: str = "all",
        user_id: str | None = None,
    ) -> list[dict[str, Any]]:
        self._require_admin(user)
        return self.store.list_alerts(
            status=status,
            severity=severity,
            user_id=user_id,
        )

    def admin_freezes(self, user: UserContext) -> list[dict[str, Any]]:
        self._require_admin(user)
        return self.store.list_active_freezes(now=self._now_datetime())

    def unfreeze_user(
        self,
        target_user_id: str,
        user: UserContext,
    ) -> dict[str, object]:
        self._require_admin(user)
        freeze = self.store.unfreeze_user(
            target_user_id,
            now=self._now_datetime(),
        )
        if freeze is None:
            raise ValueError(f"用户 {target_user_id} 当前未被冻结")
        self._audit(
            user,
            "user.unfrozen",
            "low",
            {
                "target_user_id": target_user_id,
                "previous_frozen_until": freeze["frozen_until"],
                "previous_reason": freeze["reason"],
            },
        )
        return {"user_id": target_user_id, "unfrozen": True}

    def update_alert_status(
        self,
        alert_id: str,
        status: str,
        user: UserContext,
    ) -> dict[str, str]:
        self._require_admin(user)
        if not self.store.update_alert_status(alert_id, status):
            raise ValueError("告警不存在")
        self._audit(
            user,
            "alert.status_updated",
            "low",
            {"alert_id": alert_id, "status": status},
        )
        return {"alert_id": alert_id, "status": status}

    def _response_from_record(
        self,
        record: dict[str, Any],
        user: UserContext,
        *,
        alternatives: tuple[ChartSpec, ...] | None = None,
    ) -> ProductQueryResponse:
        plan = dict(record["plan"])
        if not user.can_view_sql:
            plan.pop("sql", None)
        result = QueryResult(
            tuple(record["columns"]),
            tuple(tuple(row) for row in record["rows"]),
            bool(record.get("truncated")),
        )
        visualization = ChartSpec(**record["visualization"])
        if alternatives is None:
            _, alternatives = self.charts.recommend(plan, result)
        records = tuple(result.dictionaries())
        insight_data = record.get("insight")
        insight = Insight(**insight_data) if isinstance(insight_data, dict) else None
        if self._should_mask_values(user, plan):
            records = self._mask_records(
                records,
            )
            insight = Insight(
                headline="查询已完成",
                summary="查询结果字段已按当前角色权限统一隐藏。",
                caveats=("如需查看具体数值，请申请相应数据权限。",),
                confidence="high",
            )
        current_level = plan.get("time_granularity")
        time_targets: tuple[TimeGranularity, ...] = ()
        if "metric_value" in result.columns and len(
            tuple(plan.get("metrics") or ())
        ) == 1 and len(
            tuple(plan.get("organizations") or ())
        ) == 1:
            if current_level in NEXT_TIME_LEVEL and NEXT_TIME_LEVEL[current_level]:
                time_targets = (NEXT_TIME_LEVEL[current_level],)
            elif current_level is None:
                time_targets = ("year",)
        return ProductQueryResponse(
            api_version=API_VERSION,
            query_id=record["query_id"],
            session_id=record["session_id"],
            status="completed",
            question=record["question"],
            route=record["route"],
            answer_mode=record.get("answer_mode", "rule"),
            answer_status=record.get("answer_status", "completed"),
            generated_at=record["created_at"],
            duration_ms=int(record.get("duration_ms", 0)),
            plan=plan,
            sql=record.get("sql") if user.can_view_sql else None,
            columns=self._columns(result, plan),
            records=records,
            row_count=result.row_count,
            truncated=result.truncated,
            visualization=visualization,
            alternatives=alternatives or (),
            insight=insight,
            current_time_level=current_level,
            available_time_drill=time_targets,
            drill_path=tuple(plan.get("drill_path") or ()),
            aggregation=plan.get("aggregation"),
            permissions={
                "can_view_sql": user.can_view_sql,
                "can_export": user.can_export,
                "can_share": user.can_share,
                "can_view_admin": user.can_view_admin,
                "can_query_data": user.can_query_data,
            },
            warnings=tuple(record.get("warnings") or ()),
        )

    def _columns(self, result: QueryResult, plan: dict[str, Any]) -> tuple[DataColumn, ...]:
        sensitive = self._contains_sensitive_metric(plan)
        output: list[DataColumn] = []
        dictionaries = result.dictionaries()
        unit = ""
        metrics = tuple(plan.get("metrics") or ())
        if len(metrics) == 1 and metrics[0] in self.core.catalog.metrics:
            unit = self.core.catalog.metrics[metrics[0]].unit
        for key in result.columns:
            values = [row.get(key) for row in dictionaries if row.get(key) is not None]
            data_type = "string"
            if values and all(isinstance(value, bool) for value in values):
                data_type = "boolean"
            elif values and all(isinstance(value, (int, float)) for value in values):
                data_type = "number"
            elif values and all(
                isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value)
                for value in values
            ):
                data_type = "date"
            output.append(
                DataColumn(
                    key=key,
                    label=COLUMN_LABELS.get(key, key),
                    data_type=data_type,
                    unit=unit if key in VALUE_COLUMNS else "",
                    sensitive=sensitive and key in VALUE_COLUMNS,
                )
            )
        return tuple(output)

    def _build_insight(
        self,
        *,
        question: str,
        answer: str,
        route: str,
        plan: dict[str, Any],
        records: list[dict[str, Any]],
    ) -> Insight:
        operation = str(plan.get("operation") or "")
        metric_names = [
            self.core.catalog.metrics[metric].name
            for metric in tuple(plan.get("metrics") or ())
            if metric in self.core.catalog.metrics
        ]
        headline = "、".join(metric_names) + "分析" if metric_names else "查询结论"
        evidence: list[str] = []
        for record in records[:3]:
            label = str(
                record.get("org_name")
                or record.get("metric_name")
                or record.get("period_start")
                or record.get("data_date")
                or "结果"
            )
            value = next(
                (
                    record.get(key)
                    for key in VALUE_COLUMN_ORDER
                    if record.get(key) is not None
                ),
                None,
            )
            if value is not None:
                evidence.append(f"{label}：{value}{record.get('unit') or ''}")
        inference = operation in {"growth", "difference", "quarterly_trend", "time_drill"}
        caveats = (
            ("当前结论基于指标变化；业务原因需结合营销活动、产品和渠道明细验证。",)
            if inference
            else ()
        )
        return Insight(
            headline=headline,
            summary=answer,
            evidence=tuple(evidence),
            caveats=caveats,
            confidence="high" if route == "rule" else "medium",
            inference=False,
        )

    def _authorize_question(self, question: str, user: UserContext) -> None:
        if user.effective_organization_access != "restricted":
            return
        if not user.allowed_organizations:
            raise PermissionError("当前账号尚未配置可访问机构")
        requested = self.core.catalog.resolve_organizations(question)
        if not requested:
            return
        denied = sorted(set(requested) - set(user.allowed_organizations))
        if denied:
            names = "、".join(self.core.catalog.organizations.get(item, item) for item in denied)
            raise PermissionError(f"无权访问机构：{names}")

    def clear_session(
        self,
        session_id: str,
        user: UserContext,
        *,
        delete_history: bool = False,
        owner_user_id: str | None = None,
    ) -> None:
        """Clear one user's conversational context and optionally its reports."""
        self._ensure_not_frozen(user)
        if not user.can_clear_sessions:
            reason = "权限不足：普通用户不能删除查询记录或清空会话，请联系系统管理员"
            self._record_access_denied(
                user,
                question="清除会话",
                reason=reason,
                query_id=None,
            )
            raise PermissionError(reason)
        target_user_id = owner_user_id or user.user_id
        self.core.clear_session(f"{target_user_id}:{session_id}")
        self.store.clear_pending_session(target_user_id, session_id)
        if delete_history:
            deleted = self.store.delete_session_queries(target_user_id, session_id)
            self._audit(
                user,
                "session_clear",
                "medium",
                {
                    "session_id": session_id,
                    "owner_user_id": target_user_id,
                    "deleted_queries": deleted,
                },
            )

    @staticmethod
    def _internal_session_id(session_id: str, user: UserContext) -> str:
        return f"{user.user_id}:{session_id}"

    def _authorize_plan(
        self,
        plan: dict[str, Any],
        user: UserContext,
    ) -> DataAccessScope:
        if user.role != "viewer" and not user.can_query_data:
            raise PermissionError("当前业务角色无数据查询权限")

        metrics = set(plan.get("metrics") or ())
        if not metrics:
            raise PermissionError("查询计划未声明指标，拒绝访问历史结果")
        denied_metrics = metrics - set(user.allowed_metric_ids)
        if denied_metrics:
            raise PermissionError(
                "当前岗位无权访问指标：" + "、".join(sorted(denied_metrics))
            )

        organization_access = user.effective_organization_access
        if organization_access == "none":
            raise PermissionError("当前业务角色无机构数据权限")
        scope_kind = plan.get("organization_scope")
        organizations = set(plan.get("organizations") or ())
        if scope_kind not in {"selected", "all"}:
            raise PermissionError("查询计划缺少有效机构范围")
        if scope_kind == "selected" and not organizations:
            raise PermissionError("查询计划未声明机构，拒绝访问历史结果")
        unknown_organizations = organizations - set(self.core.catalog.organizations)
        if unknown_organizations:
            raise PermissionError(
                "查询计划包含未知机构：" + "、".join(sorted(unknown_organizations))
            )
        organization_scope: tuple[str, ...] | None = None
        if organization_access == "restricted":
            allowed = set(user.allowed_organizations)
            if not allowed:
                raise PermissionError("当前账号尚未配置可访问机构")
            if scope_kind == "all":
                raise PermissionError("当前查询范围超出机构数据权限")
            denied = organizations - allowed
            if denied:
                raise PermissionError(
                    "查询计划包含无权访问的机构：" + "、".join(sorted(denied))
                )
            if plan.get("operation") in CROSS_ORGANIZATION_OPERATIONS:
                raise PermissionError("当前版本暂不支持局部机构用户执行跨机构排名或省均值计算")
            if "rank_population_all" in set(plan.get("assumptions") or ()):
                raise PermissionError("当前版本暂不支持局部机构用户执行全省排名计算")
            organization_scope = tuple(sorted(organizations))
        elif (
            plan.get("organization_scope") == "selected"
            and plan.get("organizations")
            and plan.get("operation") not in CROSS_ORGANIZATION_OPERATIONS
            and "rank_population_all" not in set(plan.get("assumptions") or ())
        ):
            organization_scope = tuple(
                sorted(set(plan.get("organizations") or ()))
            )

        return DataAccessScope(
            metric_ids=tuple(sorted(metrics)),
            organization_ids=organization_scope,
        )

    def _visible_history_records(
        self,
        records: list[dict[str, Any]],
        user: UserContext,
    ) -> list[dict[str, Any]]:
        if user.can_view_admin:
            return records
        visible: list[dict[str, Any]] = []
        for record in records:
            try:
                self._authorize_plan(record["plan"], user)
            except PermissionError:
                continue
            visible.append(record)
        return visible

    def _owned_query(self, query_id: str, user: UserContext) -> dict[str, Any]:
        record = self.store.get_query(query_id)
        if not record:
            raise ValueError("查询记录不存在")
        if not user.can_view_all_queries and record["user_id"] != user.user_id:
            reason = "无权查看该查询记录"
            self._record_access_denied(
                user,
                question="访问查询记录",
                reason=reason,
                query_id=query_id,
            )
            raise PermissionError(reason)
        if not user.can_view_admin:
            try:
                self._authorize_plan(record["plan"], user)
            except PermissionError as exc:
                self._record_access_denied(
                    user,
                    question="访问查询记录",
                    reason=str(exc),
                    query_id=query_id,
                )
                raise
        return record

    @staticmethod
    def _contains_sensitive_metric(plan: dict[str, Any]) -> bool:
        return bool(set(plan.get("metrics") or ()) & SENSITIVE_METRICS)

    def _should_mask_values(
        self,
        user: UserContext,
        plan: dict[str, Any],
    ) -> bool:
        return user.mask_all_values or (
            user.mask_sensitive_values and self._contains_sensitive_metric(plan)
        )

    def _mask_records(
        self,
        records: tuple[dict[str, Any], ...],
    ) -> tuple[dict[str, Any], ...]:
        return tuple(
            {
                key: value if value is None else "***"
                for key, value in record.items()
            }
            for record in records
        )

    def _sanitize_record_for_user(
        self,
        record: dict[str, Any],
        user: UserContext,
    ) -> dict[str, Any]:
        sanitized = dict(record)
        plan = dict(record["plan"])
        if not user.can_view_sql:
            sanitized["sql"] = None
            plan.pop("sql", None)
        sanitized["plan"] = plan
        if not self._should_mask_values(user, plan):
            return sanitized

        dictionaries = tuple(
            dict(zip(record["columns"], row, strict=True)) for row in record["rows"]
        )
        masked = self._mask_records(
            dictionaries,
        )
        sanitized["rows"] = [
            [item.get(column) for column in record["columns"]] for item in masked
        ]
        sanitized["answer"] = "查询结果字段已按当前角色权限统一隐藏"
        sanitized["insight"] = {
            "headline": "查询已完成",
            "summary": "查询结果字段已按当前角色权限统一隐藏。",
            "evidence": [],
            "caveats": ["如需查看具体数值，请申请相应数据权限。"],
            "confidence": "high",
            "inference": False,
        }
        return sanitized

    def _audit(
        self,
        user: UserContext,
        action: str,
        risk_level: str,
        details: dict[str, Any],
        query_id: str | None = None,
    ) -> str:
        event_id = uuid.uuid4().hex
        return self.store.add_audit(
            event_id=event_id,
            user_id=user.user_id,
            action=action,
            risk_level=risk_level,
            details=details,
            query_id=query_id,
            target=query_id,
            created_at=self._now_datetime(),
        )

    @staticmethod
    def _data_level(metrics: list[str] | tuple[str, ...]) -> str:
        return "S3" if set(metrics) & SENSITIVE_METRICS else "S2"

    def _ensure_not_frozen(self, user: UserContext) -> None:
        freeze = self.store.get_active_freeze(
            user.user_id,
            now=self._now_datetime(),
        )
        if freeze:
            raise AccountFrozenError(
                str(freeze["frozen_until"]),
                str(freeze["reason"]),
            )

    def _add_alert(
        self,
        user: UserContext,
        *,
        source_event_id: str,
        rule_code: str,
        severity: str,
        details: dict[str, Any],
        query_id: str | None,
    ) -> str:
        return self.store.add_alert(
            alert_id=uuid.uuid4().hex,
            source_event_id=source_event_id,
            rule_code=rule_code,
            user_id=user.user_id,
            query_id=query_id,
            severity=severity,
            details=details,
            created_at=self._now_datetime(),
        )

    def _record_access_denied(
        self,
        user: UserContext,
        *,
        question: str,
        reason: str,
        query_id: str | None,
    ) -> None:
        event_id = self._audit(
            user,
            "access.denied",
            "medium",
            {"question": question, "reason": reason},
            query_id,
        )
        now = self._now_datetime()
        since = now - timedelta(minutes=10)
        with self._security_lock:
            denied_count = self.store.count_audit_events(
                user_id=user.user_id,
                action="access.denied",
                since=since,
            )
            if denied_count < 3 or self.store.has_recent_alert(
                user_id=user.user_id,
                rule_code=ALERT_ACCESS_DENIED,
                since=since,
            ):
                return
            frozen_until = now + timedelta(minutes=15)
            self.store.add_alert(
                alert_id=uuid.uuid4().hex,
                source_event_id=event_id,
                rule_code=ALERT_ACCESS_DENIED,
                user_id=user.user_id,
                severity="high",
                details={
                    "window_minutes": 10,
                    "denied_count": denied_count,
                    "frozen_minutes": 15,
                    "frozen_until": frozen_until.isoformat(),
                },
                query_id=query_id,
                created_at=now,
                frozen_until=frozen_until,
                freeze_reason="10分钟内连续3次越权访问",
            )

    def _evaluate_query_alerts(
        self,
        user: UserContext,
        *,
        event_id: str,
        query_id: str,
        metrics: list[str],
    ) -> None:
        with self._security_lock:
            metric_count = len(set(metrics))
            if metric_count > 8:
                self._add_alert(
                    user,
                    source_event_id=event_id,
                    rule_code=ALERT_MULTI_METRIC,
                    severity="medium",
                    details={"metric_count": metric_count, "threshold": 8},
                    query_id=query_id,
                )
            if self._data_level(metrics) != "S3":
                return
            since = self._now_datetime() - timedelta(minutes=5)
            query_count = sum(
                self.store.count_audit_events(
                    user_id=user.user_id,
                    action=action,
                    since=since,
                    data_level="S3",
                )
                for action in ("query.completed", "query.time_drill")
            )
            if query_count > 10 and not self.store.has_recent_alert(
                user_id=user.user_id,
                rule_code=ALERT_S3_FREQUENCY,
                since=since,
            ):
                self._add_alert(
                    user,
                    source_event_id=event_id,
                    rule_code=ALERT_S3_FREQUENCY,
                    severity="medium",
                    details={
                        "window_minutes": 5,
                        "query_count": query_count,
                        "threshold": 10,
                    },
                    query_id=query_id,
                )

    def _evaluate_export_alerts(
        self,
        user: UserContext,
        *,
        event_id: str,
        query_id: str | None,
        exported_rows: int,
        metrics: list[str],
    ) -> None:
        with self._security_lock:
            now = self._now_datetime()
            if exported_rows > 200:
                self._add_alert(
                    user,
                    source_event_id=event_id,
                    rule_code=ALERT_LARGE_EXPORT,
                    severity="medium",
                    details={"exported_rows": exported_rows, "threshold": 200},
                    query_id=query_id,
                )

            local_now = now.astimezone(SHANGHAI_TZ)
            local_day_start = datetime.combine(
                local_now.date(),
                clock_time.min,
                tzinfo=SHANGHAI_TZ,
            )
            day_start = local_day_start.astimezone(timezone.utc)
            daily_rows = self.store.sum_exported_rows(
                user_id=user.user_id,
                since=day_start,
            )
            if daily_rows > 1000 and not self.store.has_recent_alert(
                user_id=user.user_id,
                rule_code=ALERT_DAILY_EXPORT,
                since=day_start,
            ):
                self._add_alert(
                    user,
                    source_event_id=event_id,
                    rule_code=ALERT_DAILY_EXPORT,
                    severity="high",
                    details={"daily_exported_rows": daily_rows, "threshold": 1000},
                    query_id=query_id,
                )

            outside_work_hours = local_now.weekday() >= 5 or not (
                clock_time(8, 0) <= local_now.time() < clock_time(19, 0)
            )
            if self._data_level(metrics) == "S3" and outside_work_hours:
                self._add_alert(
                    user,
                    source_event_id=event_id,
                    rule_code=ALERT_OFF_HOURS_EXPORT,
                    severity="high",
                    details={
                        "exported_rows": exported_rows,
                        "local_time": local_now.isoformat(),
                        "work_hours": "工作日08:00-19:00",
                    },
                    query_id=query_id,
                )

    def _evaluate_share_alerts(
        self,
        user: UserContext,
        *,
        event_id: str,
        query_id: str,
    ) -> None:
        with self._security_lock:
            since = self._now_datetime() - timedelta(minutes=10)
            share_count = self.store.count_audit_events(
                user_id=user.user_id,
                action="query.shared",
                since=since,
            )
            if share_count >= 3 and not self.store.has_recent_alert(
                user_id=user.user_id,
                rule_code=ALERT_SHARE_FREQUENCY,
                since=since,
            ):
                self._add_alert(
                    user,
                    source_event_id=event_id,
                    rule_code=ALERT_SHARE_FREQUENCY,
                    severity="medium",
                    details={
                        "window_minutes": 10,
                        "share_count": share_count,
                        "threshold": 3,
                    },
                    query_id=query_id,
                )

    def _require_admin(self, user: UserContext) -> None:
        self._ensure_not_frozen(user)
        if not user.can_view_admin:
            reason = "仅管理员可以访问该功能"
            self._record_access_denied(
                user,
                question="访问管理员功能",
                reason=reason,
                query_id=None,
            )
            raise PermissionError(reason)

    @staticmethod
    def _safe_identifier(value: str, label: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_]{1,64}", value):
            raise ValueError(f"{label}编码不合法")
        return value

    def _drill_range(
        self,
        target: TimeGranularity,
        current_date: str | None,
        period_start: str | None,
        period_end: str | None,
        access_scope: DataAccessScope | None = None,
    ) -> tuple[date, date]:
        if period_start or period_end:
            if not period_start or not period_end:
                raise ValueError("自定义下钻范围必须同时提供开始和结束日期")
            start, end = date.fromisoformat(period_start), date.fromisoformat(period_end)
            if start > end:
                raise ValueError("下钻开始日期不能晚于结束日期")
            return start, end
        if target == "year":
            row = self.core.executor.execute(
                "SELECT CAST(MIN(data_date) AS VARCHAR), CAST(MAX(data_date) AS VARCHAR) "
                "FROM metric_values",
                access_scope,
            ).rows[0]
            return date.fromisoformat(str(row[0])), date.fromisoformat(str(row[1]))
        focus = date.fromisoformat(current_date) if current_date else date.today()
        if target == "quarter":
            return date(focus.year, 1, 1), date(focus.year, 12, 31)
        if target == "month":
            quarter_start_month = ((focus.month - 1) // 3) * 3 + 1
            start = date(focus.year, quarter_start_month, 1)
            next_quarter = (
                date(focus.year + 1, 1, 1)
                if quarter_start_month == 10
                else date(focus.year, quarter_start_month + 3, 1)
            )
            return start, next_quarter - timedelta(days=1)
        start = date(focus.year, focus.month, 1)
        next_month = (
            date(focus.year + 1, 1, 1)
            if focus.month == 12
            else date(focus.year, focus.month + 1, 1)
        )
        return start, next_month - timedelta(days=1)

    def _time_drill_sql(
        self,
        metric_id: str,
        org_id: str,
        target: TimeGranularity,
        start_date: date,
        end_date: date,
    ) -> str:
        if target == "day":
            return f"""
                SELECT CAST(v.data_date AS VARCHAR) AS period_start,
                       v.org_id, o.org_name, v.metric_id, m.metric_name,
                       v.metric_value, m.unit,
                       CAST(v.data_date AS VARCHAR) AS data_date
                FROM metric_values AS v
                JOIN organizations AS o USING (org_id)
                JOIN metrics AS m USING (metric_id)
                WHERE v.metric_id = '{metric_id}'
                  AND v.org_id = '{org_id}'
                  AND v.data_date BETWEEN DATE '{start_date}' AND DATE '{end_date}'
                ORDER BY v.data_date
            """
        return f"""
            WITH period_values AS (
                SELECT DATE_TRUNC('{target}', v.data_date) AS period_start,
                       ARG_MAX(v.metric_value, v.data_date) AS metric_value,
                       MAX(v.data_date) AS data_date
                FROM metric_values AS v
                WHERE v.metric_id = '{metric_id}'
                  AND v.org_id = '{org_id}'
                  AND v.data_date BETWEEN DATE '{start_date}' AND DATE '{end_date}'
                GROUP BY DATE_TRUNC('{target}', v.data_date)
            )
            SELECT CAST(CAST(p.period_start AS DATE) AS VARCHAR) AS period_start,
                   o.org_id, o.org_name, m.metric_id, m.metric_name,
                   p.metric_value, m.unit,
                   CAST(p.data_date AS VARCHAR) AS data_date
            FROM period_values AS p
            CROSS JOIN organizations AS o
            CROSS JOIN metrics AS m
            WHERE o.org_id = '{org_id}' AND m.metric_id = '{metric_id}'
            ORDER BY p.period_start
        """

    @staticmethod
    def _level_label(level: TimeGranularity) -> str:
        return {"year": "年度", "quarter": "季度", "month": "月度", "day": "日"}[level]

    @staticmethod
    def _now_datetime() -> datetime:
        return datetime.now(timezone.utc)

    def _now(self) -> str:
        return self._now_datetime().isoformat()
