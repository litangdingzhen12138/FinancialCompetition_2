"""FastAPI transport for the Text2SQL service."""

from __future__ import annotations

from functools import lru_cache

from io import BytesIO, StringIO
import csv
import json
import os
import uuid

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from openpyxl import Workbook
from pydantic import BaseModel, Field

from .errors import Text2SQLError
from .product_auth import ProductAuthService
from .product_models import TimeGranularity, UserContext
from .product_service import ProductQueryService
from .product_store import ProductStore
from .service import Text2SQLService


app = FastAPI(title="Bank Text2SQL", version="0.1.0")
cors_origins = [
    value.strip()
    for value in os.getenv(
        "TEXT2SQL_CORS_ORIGINS",
        (
            "http://localhost:3000,http://127.0.0.1:3000,"
            "http://localhost:3101,http://127.0.0.1:3101"
        ),
    ).split(",")
    if value.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class QueryRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    session_id: str | None = Field(default=None, max_length=128)


class DrillRequest(BaseModel):
    target_granularity: TimeGranularity
    period_start: str | None = None
    period_end: str | None = None


class ShareRequest(BaseModel):
    expires_hours: int = Field(default=24, ge=1, le=168)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


class RenameSessionRequest(BaseModel):
    title: str = Field(min_length=1, max_length=60)
    owner_user_id: str | None = Field(default=None, max_length=128)


@lru_cache(maxsize=1)
def get_service() -> Text2SQLService:
    return Text2SQLService()


@lru_cache(maxsize=1)
def get_product_service() -> ProductQueryService:
    service = get_service()
    return ProductQueryService(service, ProductStore(service.settings.product_db_path))


@lru_cache(maxsize=1)
def get_auth_service() -> ProductAuthService:
    return ProductAuthService()


def user_context(
    x_user_id: str = Header(default="demo-analyst", max_length=128),
    x_user_role: str = Header(default="analyst"),
    x_org_scope: str = Header(default="", max_length=2000),
    authorization: str | None = None,
) -> UserContext:
    if authorization:
        try:
            return get_auth_service().resolve_bearer(authorization).to_context()
        except PermissionError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
    allow_header_auth = os.getenv("TEXT2SQL_ALLOW_HEADER_AUTH", "true").strip().lower()
    if allow_header_auth not in {"1", "true", "yes", "on"}:
        raise HTTPException(status_code=401, detail="请先登录")
    if x_user_role not in {"viewer", "analyst", "admin"}:
        raise HTTPException(status_code=400, detail="X-User-Role必须是viewer、analyst或admin")
    organizations = tuple(
        value.strip() for value in x_org_scope.split(",") if value.strip()
    )
    return UserContext(
        user_id=x_user_id,
        role=x_user_role,
        allowed_organizations=organizations,
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/v1/auth/login")
def login(request: LoginRequest) -> dict[str, object]:
    try:
        token, user = get_auth_service().login(request.username, request.password)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    return {"access_token": token, "token_type": "bearer", "user": user.to_dict()}


@app.get("/api/v1/auth/me")
def current_user(
    authorization: str | None = Header(default=None),
) -> dict[str, str]:
    try:
        return get_auth_service().resolve_bearer(authorization).to_dict()
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


@app.post("/api/v1/auth/logout")
def logout(authorization: str | None = Header(default=None)) -> dict[str, bool]:
    try:
        get_auth_service().logout(authorization)
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    return {"logged_out": True}


@app.post("/query")
def query(request: QueryRequest) -> dict[str, object]:
    try:
        return get_service().ask(request.question, request.session_id).to_dict()
    except (Text2SQLError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/sessions/{session_id}")
def clear_session(session_id: str) -> dict[str, bool]:
    get_service().clear_session(session_id)
    return {"cleared": True}


def _product_error(exc: Exception) -> HTTPException:
    if isinstance(exc, PermissionError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, (Text2SQLError, ValueError)):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail="系统处理失败")


@app.get("/api/v1/capabilities")
def capabilities() -> dict[str, object]:
    return {
        "api_version": "v1",
        "features": {
            "nl2sql": True,
            "chart_recommendation": True,
            "business_insight": True,
            "final_answer_stream": True,
            "time_drill": ["year", "quarter", "month", "day"],
            "history": True,
            "export": ["xlsx", "csv"],
            "share": True,
            "audit": True,
        },
        "time_aggregation": "period_end",
    }


@app.delete("/api/v1/sessions/{session_id}")
def clear_product_session(
    session_id: str,
    delete_history: bool = Query(default=True),
    owner_user_id: str | None = Query(default=None, max_length=128),
    x_user_id: str = Header(default="demo-analyst", max_length=128),
    x_user_role: str = Header(default="analyst"),
    x_org_scope: str = Header(default="", max_length=2000),
    authorization: str | None = Header(default=None),
) -> dict[str, object]:
    user = user_context(x_user_id, x_user_role, x_org_scope, authorization)
    try:
        get_product_service().clear_session(
            session_id,
            user,
            delete_history=delete_history,
            owner_user_id=owner_user_id,
        )
        return {"cleared": True}
    except Exception as exc:
        raise _product_error(exc) from exc


@app.patch("/api/v1/sessions/{session_id}")
def rename_product_session(
    session_id: str,
    request: RenameSessionRequest,
    x_user_id: str = Header(default="demo-analyst", max_length=128),
    x_user_role: str = Header(default="analyst"),
    x_org_scope: str = Header(default="", max_length=2000),
    authorization: str | None = Header(default=None),
) -> dict[str, object]:
    user = user_context(x_user_id, x_user_role, x_org_scope, authorization)
    try:
        renamed = get_product_service().rename_session(
            session_id,
            request.title,
            user,
            owner_user_id=request.owner_user_id,
        )
        return {"renamed": True, **renamed}
    except Exception as exc:
        raise _product_error(exc) from exc


@app.post("/api/v1/queries")
def product_query(
    request: QueryRequest,
    x_user_id: str = Header(default="demo-analyst", max_length=128),
    x_user_role: str = Header(default="analyst"),
    x_org_scope: str = Header(default="", max_length=2000),
    authorization: str | None = Header(default=None),
) -> dict[str, object]:
    user = user_context(x_user_id, x_user_role, x_org_scope, authorization)
    try:
        return get_product_service().query(
            request.question,
            request.session_id,
            user,
        ).to_dict()
    except Exception as exc:
        raise _product_error(exc) from exc


@app.post("/api/v1/queries/stream")
def product_query_stream(
    request: QueryRequest,
    x_user_id: str = Header(default="demo-analyst", max_length=128),
    x_user_role: str = Header(default="analyst"),
    x_org_scope: str = Header(default="", max_length=2000),
    authorization: str | None = Header(default=None),
) -> StreamingResponse:
    user = user_context(x_user_id, x_user_role, x_org_scope, authorization)
    public_session_id = request.session_id or uuid.uuid4().hex

    def event_stream():
        yield _sse(
            "status",
            {
                "stage": "understanding",
                "message": "正在理解问题",
                "session_id": public_session_id,
            },
        )
        try:
            result = get_product_service().query(
                request.question,
                public_session_id,
                user,
            ).to_dict()
            yield _sse("status", {"stage": "data_ready", "message": "数据查询完成"})
            yield _sse("result", result)
            yield _sse(
                "complete",
                {
                    "query_id": result["query_id"],
                    "answer_mode": result["answer_mode"],
                    "answer_status": result["answer_status"],
                },
            )
        except Exception as exc:
            status = 403 if isinstance(exc, PermissionError) else 400
            message = (
                str(exc)
                if isinstance(exc, (PermissionError, Text2SQLError, ValueError))
                else "系统处理失败"
            )
            yield _sse(
                "error",
                {
                    "status": status,
                    "message": message,
                    "session_id": public_session_id,
                },
            )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/v1/queries/{query_id}/answer/stream")
def final_answer_stream(
    query_id: str,
    x_user_id: str = Header(default="demo-analyst", max_length=128),
    x_user_role: str = Header(default="analyst"),
    x_org_scope: str = Header(default="", max_length=2000),
    authorization: str | None = Header(default=None),
) -> StreamingResponse:
    user = user_context(x_user_id, x_user_role, x_org_scope, authorization)

    def event_stream():
        try:
            for event, payload in get_product_service().stream_final_answer(
                query_id,
                user,
            ):
                yield _sse(event, payload)
        except Exception as exc:
            status = 403 if isinstance(exc, PermissionError) else 400
            message = (
                str(exc)
                if isinstance(exc, (PermissionError, Text2SQLError, ValueError))
                else "最终回答生成失败"
            )
            yield _sse(
                "answer_error",
                {
                    "query_id": query_id,
                    "status": status,
                    "message": message,
                },
            )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/v1/queries/{query_id}/drill")
def time_drill(
    query_id: str,
    request: DrillRequest,
    x_user_id: str = Header(default="demo-analyst", max_length=128),
    x_user_role: str = Header(default="analyst"),
    x_org_scope: str = Header(default="", max_length=2000),
    authorization: str | None = Header(default=None),
) -> dict[str, object]:
    user = user_context(x_user_id, x_user_role, x_org_scope, authorization)
    try:
        return get_product_service().time_drill(
            query_id,
            request.target_granularity,
            user,
            period_start=request.period_start,
            period_end=request.period_end,
        ).to_dict()
    except Exception as exc:
        raise _product_error(exc) from exc


@app.get("/api/v1/history")
def history(
    limit: int | None = Query(default=None, ge=1, le=5000),
    keyword: str | None = Query(default=None, max_length=200),
    x_user_id: str = Header(default="demo-analyst", max_length=128),
    x_user_role: str = Header(default="analyst"),
    x_org_scope: str = Header(default="", max_length=2000),
    authorization: str | None = Header(default=None),
) -> dict[str, object]:
    user = user_context(x_user_id, x_user_role, x_org_scope, authorization)
    return {
        "items": get_product_service().history(user, limit=limit, keyword=keyword)
    }


@app.get("/api/v1/history/{query_id}")
def history_detail(
    query_id: str,
    x_user_id: str = Header(default="demo-analyst", max_length=128),
    x_user_role: str = Header(default="analyst"),
    x_org_scope: str = Header(default="", max_length=2000),
    authorization: str | None = Header(default=None),
) -> dict[str, object]:
    user = user_context(x_user_id, x_user_role, x_org_scope, authorization)
    try:
        return get_product_service().get_query(query_id, user).to_dict()
    except Exception as exc:
        raise _product_error(exc) from exc


@app.get("/api/v1/sessions/{session_id}/history")
def session_history(
    session_id: str,
    owner_user_id: str | None = Query(default=None, max_length=128),
    x_user_id: str = Header(default="demo-analyst", max_length=128),
    x_user_role: str = Header(default="analyst"),
    x_org_scope: str = Header(default="", max_length=2000),
    authorization: str | None = Header(default=None),
) -> dict[str, object]:
    user = user_context(x_user_id, x_user_role, x_org_scope, authorization)
    try:
        return {
            "items": [
                item.to_dict()
                for item in get_product_service().session_history(
                    session_id,
                    user,
                    owner_user_id=owner_user_id,
                )
            ]
        }
    except Exception as exc:
        raise _product_error(exc) from exc


@app.get("/api/v1/history/export/batch")
def batch_export_history(
    scope: str = Query(pattern="^(all|recent|session)$"),
    recent_count: int = Query(default=20, ge=1, le=5000),
    session_id: str | None = Query(default=None, max_length=128),
    owner_user_id: str | None = Query(default=None, max_length=128),
    x_user_id: str = Header(default="demo-analyst", max_length=128),
    x_user_role: str = Header(default="analyst"),
    x_org_scope: str = Header(default="", max_length=2000),
    authorization: str | None = Header(default=None),
) -> Response:
    user = user_context(x_user_id, x_user_role, x_org_scope, authorization)
    try:
        records = get_product_service().batch_export_records(
            user,
            scope=scope,
            recent_count=recent_count,
            session_id=session_id,
            owner_user_id=owner_user_id,
        )
    except Exception as exc:
        raise _product_error(exc) from exc

    workbook = Workbook()
    summary = workbook.active
    summary.title = "历史汇总"
    summary.append(
        [
            "查询编号",
            "用户",
            "thread_id",
            "生成时间",
            "问题",
            "最终回答",
            "路径",
            "结果行数",
            "耗时(ms)",
        ]
    )
    details = workbook.create_sheet("完整查询数据")
    details.append(
        [
            "查询编号",
            "用户",
            "thread_id",
            "问题",
            "SQL",
            "查询计划JSON",
            "字段JSON",
            "结果JSON",
            "图表配置JSON",
            "洞察JSON",
            "告警JSON",
        ]
    )
    for record in records:
        summary.append(
            [
                record["query_id"],
                record["user_id"],
                record["session_id"],
                record["created_at"],
                record["question"],
                record["answer"],
                record["route"],
                len(record["rows"]),
                record["duration_ms"],
            ]
        )
        details.append(
            [
                record["query_id"],
                record["user_id"],
                record["session_id"],
                record["question"],
                record.get("sql") or "",
                json.dumps(record["plan"], ensure_ascii=False, default=str),
                json.dumps(record["columns"], ensure_ascii=False, default=str),
                json.dumps(record["rows"], ensure_ascii=False, default=str),
                json.dumps(record["visualization"], ensure_ascii=False, default=str),
                json.dumps(record["insight"], ensure_ascii=False, default=str),
                json.dumps(record["warnings"], ensure_ascii=False, default=str),
            ]
        )
    summary.freeze_panes = "A2"
    details.freeze_panes = "A2"
    output = BytesIO()
    workbook.save(output)
    return Response(
        content=output.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": 'attachment; filename="bank-history-batch.xlsx"'
        },
    )


@app.get("/api/v1/queries/{query_id}/export")
def export_query(
    query_id: str,
    format: str = Query(default="xlsx", pattern="^(xlsx|csv)$"),
    x_user_id: str = Header(default="demo-analyst", max_length=128),
    x_user_role: str = Header(default="analyst"),
    x_org_scope: str = Header(default="", max_length=2000),
    authorization: str | None = Header(default=None),
) -> Response:
    user = user_context(x_user_id, x_user_role, x_org_scope, authorization)
    try:
        record = get_product_service().export_record(query_id, user)
    except Exception as exc:
        raise _product_error(exc) from exc
    filename = f"bank-query-{query_id[:8]}"
    if format == "csv":
        stream = StringIO()
        writer = csv.writer(stream)
        writer.writerow(record["columns"])
        writer.writerows(record["rows"])
        return Response(
            content="\ufeff" + stream.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}.csv"'},
        )
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "查询结果"
    sheet.append(["问题", record["question"]])
    sheet.append(["结论", record["answer"]])
    sheet.append(["生成时间", record["created_at"]])
    sheet.append([])
    sheet.append(record["columns"])
    for row in record["rows"]:
        sheet.append(row)
    output = BytesIO()
    workbook.save(output)
    return Response(
        content=output.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}.xlsx"'},
    )


@app.post("/api/v1/queries/{query_id}/share")
def share_query(
    query_id: str,
    request: ShareRequest,
    x_user_id: str = Header(default="demo-analyst", max_length=128),
    x_user_role: str = Header(default="analyst"),
    x_org_scope: str = Header(default="", max_length=2000),
    authorization: str | None = Header(default=None),
) -> dict[str, str]:
    user = user_context(x_user_id, x_user_role, x_org_scope, authorization)
    try:
        return get_product_service().create_share(
            query_id,
            user,
            expires_hours=request.expires_hours,
        )
    except Exception as exc:
        raise _product_error(exc) from exc


@app.get("/api/v1/shares/{token}")
def shared_query(
    token: str,
    x_user_id: str = Header(default="demo-viewer", max_length=128),
    x_user_role: str = Header(default="viewer"),
    x_org_scope: str = Header(default="", max_length=2000),
) -> dict[str, object]:
    user = UserContext(
        user_id="shared-viewer",
        role="viewer",
        allowed_organizations=(),
    )
    try:
        return get_product_service().resolve_share(token, user).to_dict()
    except Exception as exc:
        raise _product_error(exc) from exc


@app.get("/api/v1/admin/overview")
def admin_overview(
    x_user_id: str = Header(default="demo-admin", max_length=128),
    x_user_role: str = Header(default="admin"),
    authorization: str | None = Header(default=None),
) -> dict[str, int]:
    user = user_context(x_user_id, x_user_role, "", authorization)
    try:
        return get_product_service().admin_overview(user)
    except Exception as exc:
        raise _product_error(exc) from exc


@app.get("/api/v1/admin/audit")
def admin_audit(
    sort: str = Query(default="newest", pattern="^(newest|risk_desc|risk_asc)$"),
    risk: str = Query(default="all", pattern="^(all|elevated|low|medium|high)$"),
    audit_user: str | None = Query(default=None, max_length=128, alias="user"),
    x_user_id: str = Header(default="demo-admin", max_length=128),
    x_user_role: str = Header(default="admin"),
    authorization: str | None = Header(default=None),
) -> dict[str, object]:
    current_user = user_context(x_user_id, x_user_role, "", authorization)
    try:
        return {
            "items": get_product_service().admin_audit(
                current_user,
                sort_by=sort,
                risk_level=risk,
                user_id=audit_user,
            )
        }
    except Exception as exc:
        raise _product_error(exc) from exc


@app.get("/api/v1/admin/users")
def admin_users(
    x_user_id: str = Header(default="demo-admin", max_length=128),
    x_user_role: str = Header(default="admin"),
    authorization: str | None = Header(default=None),
) -> dict[str, object]:
    user = user_context(x_user_id, x_user_role, "", authorization)
    try:
        return {"items": get_product_service().admin_users(user)}
    except Exception as exc:
        raise _product_error(exc) from exc


def _sse(event: str, data: object) -> str:
    return (
        f"event: {event}\n"
        f"data: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"
    )
