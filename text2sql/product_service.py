"""Product orchestration for visualization, history, drill-down and permissions."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
import re
import time
from typing import Any, Iterator
import uuid

from .chart_recommender import ChartRecommender
from .final_answer import FinalAnswerGenerator
from .models import PendingQuery, QueryResult
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

    def query(
        self,
        question: str,
        session_id: str | None,
        user: UserContext,
    ) -> ProductQueryResponse:
        public_session_id = session_id or uuid.uuid4().hex
        internal_session_id = self._internal_session_id(public_session_id, user)
        self._authorize_question(question, user)
        self._restore_pending_session(
            internal_session_id,
            public_session_id,
            user,
        )
        started = time.perf_counter()
        query_id = uuid.uuid4().hex
        try:
            response = self.core.ask(
                question,
                internal_session_id,
            )
            self.store.clear_pending_session(user.user_id, public_session_id)
            self._authorize_plan(response.plan, user)
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
            self._audit(
                user,
                "query.completed",
                "low",
                {"question": question, "route": response.route, "row_count": len(response.rows)},
                query_id,
            )
            return self._response_from_record(
                raw_record,
                user,
                alternatives=alternatives,
            )
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
        source = self._owned_query(source_query_id, user)
        plan = source["plan"]
        metrics = tuple(plan.get("metrics") or ())
        organizations = tuple(plan.get("organizations") or ())
        if len(metrics) != 1 or len(organizations) != 1:
            raise ValueError("时间下钻当前要求原查询只包含一个指标和一个机构")
        self._authorize_plan(plan, user)
        start_date, end_date = self._drill_range(
            target,
            plan.get("current_date"),
            period_start,
            period_end,
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
        self.core.executor.preflight(approved_sql)
        result = self.core.executor.execute(approved_sql)
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
        self._audit(
            user,
            "query.time_drill",
            "low",
            {"source_query_id": source_query_id, "target": target},
            query_id,
        )
        return self._response_from_record(record, user)

    def history(
        self,
        user: UserContext,
        *,
        limit: int | None = None,
        keyword: str | None = None,
    ) -> list[dict[str, Any]]:
        owner = None if user.can_view_admin else user.user_id
        titles = self.store.list_session_titles(owner)
        return [
            {
                "query_id": record["query_id"],
                "session_id": record["session_id"],
                "user_id": record["user_id"],
                "question": record["question"],
                "route": record["route"],
                "status": record["status"],
                "answer": (
                    "敏感指标具体数值已按权限隐藏"
                    if user.mask_sensitive_values
                    and self._contains_sensitive_metric(record["plan"])
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
            for record in self.store.list_queries(owner, limit=limit, keyword=keyword)
        ]

    def session_history(
        self,
        session_id: str,
        user: UserContext,
        *,
        owner_user_id: str | None = None,
    ) -> list[ProductQueryResponse]:
        owner = (
            owner_user_id
            if user.can_view_admin and owner_user_id
            else (None if user.can_view_admin else user.user_id)
        )
        records = self.store.list_queries(
            owner,
            limit=None,
            session_id=session_id,
            oldest_first=True,
        )
        return [self._response_from_record(record, user) for record in records]

    def rename_session(
        self,
        session_id: str,
        title: str,
        user: UserContext,
        *,
        owner_user_id: str | None = None,
    ) -> dict[str, str]:
        normalized = " ".join(title.split()).strip()
        if not normalized:
            raise ValueError("会话名称不能为空")
        target_user_id = (
            owner_user_id
            if user.can_view_admin and owner_user_id
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
    ) -> list[dict[str, Any]]:
        if not user.can_export:
            raise PermissionError("当前角色无导出权限")
        if scope not in {"all", "recent", "session"}:
            raise ValueError("不支持的批量导出范围")
        if scope == "session" and not session_id:
            raise ValueError("按会话导出时必须选择 thread_id")
        owner = owner_user_id if user.can_view_admin else user.user_id
        records = self.store.list_queries(
            owner,
            limit=max(1, min(recent_count, 5000)) if scope == "recent" else None,
            session_id=session_id if scope == "session" else None,
        )
        self._audit(
            user,
            "history.batch_exported",
            "medium",
            {
                "scope": scope,
                "record_count": len(records),
                "session_id": session_id,
                "owner_user_id": owner,
            },
        )
        return records

    def get_query(self, query_id: str, user: UserContext) -> ProductQueryResponse:
        return self._response_from_record(self._owned_query(query_id, user), user)

    def stream_final_answer(
        self,
        query_id: str,
        user: UserContext,
    ) -> Iterator[tuple[str, dict[str, Any]]]:
        record = self._owned_query(query_id, user)
        mode = str(record.get("answer_mode") or "rule")
        yield "answer_start", {"query_id": query_id, "mode": mode}

        if user.mask_sensitive_values and self._contains_sensitive_metric(record["plan"]):
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
        if not user.can_share:
            raise PermissionError("当前角色无分享权限")
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
        self._audit(
            user,
            "query.shared",
            "medium",
            {"share_id": share_id, "expires_at": expires_at},
            query_id,
        )
        return {"share_id": share_id, "token": token, "expires_at": expires_at}

    def resolve_share(self, token: str, user: UserContext) -> ProductQueryResponse:
        share = self.store.resolve_share(token)
        if not share:
            raise ValueError("分享链接无效、已撤销或已过期")
        record = self.store.get_query(str(share["query_id"]))
        if not record:
            raise ValueError("分享的查询记录不存在")
        self._authorize_plan(record["plan"], user)
        self._audit(
            user,
            "share.viewed",
            "low",
            {"share_id": share["share_id"]},
            record["query_id"],
        )
        return self._response_from_record(record, user)

    def export_record(self, query_id: str, user: UserContext) -> dict[str, Any]:
        if not user.can_export:
            raise PermissionError("当前角色无导出权限")
        record = self._owned_query(query_id, user)
        self._audit(user, "query.exported", "medium", {}, query_id)
        return record

    def admin_overview(self, user: UserContext) -> dict[str, int]:
        self._require_admin(user)
        return self.store.overview()

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

    def _response_from_record(
        self,
        record: dict[str, Any],
        user: UserContext,
        *,
        alternatives: tuple[ChartSpec, ...] | None = None,
    ) -> ProductQueryResponse:
        plan = record["plan"]
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
        if user.mask_sensitive_values and self._contains_sensitive_metric(plan):
            records = self._mask_records(records)
            insight = Insight(
                headline="敏感指标查询已完成",
                summary="具体数值已按当前角色权限隐藏，仅保留非敏感维度信息。",
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
        if user.role == "admin" or not user.allowed_organizations:
            return
        requested = self.core.catalog.resolve_organizations(question)
        if not requested:
            raise PermissionError("当前数据权限要求在问题中明确指定可访问机构")
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
        if not user.can_view_admin:
            raise PermissionError(
                "权限不足：普通用户不能删除查询记录或清空会话，请联系系统管理员"
            )
        target_user_id = owner_user_id or user.user_id
        self.core.clear_session(f"{target_user_id}:{session_id}")
        self.store.clear_pending_session(target_user_id, session_id)
        if delete_history:
            deleted = self.store.delete_session_queries(target_user_id, session_id)
            self.store.add_audit(
                event_id=uuid.uuid4().hex,
                user_id=user.user_id,
                action="session_clear",
                risk_level="medium",
                details={
                    "session_id": session_id,
                    "owner_user_id": target_user_id,
                    "deleted_queries": deleted,
                },
            )

    @staticmethod
    def _internal_session_id(session_id: str, user: UserContext) -> str:
        return f"{user.user_id}:{session_id}"

    def _authorize_plan(self, plan: dict[str, Any], user: UserContext) -> None:
        if user.role == "admin" or not user.allowed_organizations:
            return
        organizations = set(plan.get("organizations") or ())
        if not organizations or plan.get("organization_scope") == "all":
            raise PermissionError("当前查询范围超出机构数据权限")
        denied = organizations - set(user.allowed_organizations)
        if denied:
            raise PermissionError("查询计划包含无权访问的机构")

    def _owned_query(self, query_id: str, user: UserContext) -> dict[str, Any]:
        record = self.store.get_query(query_id)
        if not record:
            raise ValueError("查询记录不存在")
        if not user.can_view_admin and record["user_id"] != user.user_id:
            raise PermissionError("无权查看该查询记录")
        self._authorize_plan(record["plan"], user)
        return record

    @staticmethod
    def _contains_sensitive_metric(plan: dict[str, Any]) -> bool:
        return bool(set(plan.get("metrics") or ()) & SENSITIVE_METRICS)

    @staticmethod
    def _mask_records(
        records: tuple[dict[str, Any], ...],
    ) -> tuple[dict[str, Any], ...]:
        return tuple(
            {
                key: ("***" if key in VALUE_COLUMNS and value is not None else value)
                for key, value in record.items()
            }
            for record in records
        )

    def _audit(
        self,
        user: UserContext,
        action: str,
        risk_level: str,
        details: dict[str, Any],
        query_id: str | None = None,
    ) -> None:
        self.store.add_audit(
            event_id=uuid.uuid4().hex,
            user_id=user.user_id,
            action=action,
            risk_level=risk_level,
            details=details,
            query_id=query_id,
        )

    @staticmethod
    def _require_admin(user: UserContext) -> None:
        if not user.can_view_admin:
            raise PermissionError("仅管理员可以访问该功能")

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
                "FROM metric_values"
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
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()
