from __future__ import annotations

from fastapi.testclient import TestClient

from text2sql import api
from text2sql.datasource import DataSourceAdapter, DuckDBDataSourceAdapter
from text2sql.product_models import (
    ChartSpec,
    DataColumn,
    Insight,
    ProductQueryResponse,
)


def _query_response(session_id: str) -> ProductQueryResponse:
    return ProductQueryResponse(
        api_version="v1",
        query_id="query-001",
        session_id=session_id,
        status="completed",
        question="查询存款余额",
        route="rule",
        answer_mode="rule",
        answer_status="completed",
        generated_at="2026-08-23T00:00:00+00:00",
        duration_ms=12,
        plan={"operation": "value"},
        sql="SELECT 1",
        columns=(DataColumn("metric_value", "指标值", "number", "亿元"),),
        records=({"metric_value": 42.32},),
        row_count=1,
        truncated=False,
        visualization=ChartSpec("metric", "存款余额"),
        alternatives=(),
        insight=Insight("查询完成", "存款余额为42.32亿元"),
    )


def test_query_contract_returns_request_metadata(monkeypatch) -> None:
    class StubProductService:
        def query(self, question, session_id, user):
            assert question == "查询存款余额"
            assert user.role == "analyst"
            return _query_response(session_id)

    monkeypatch.setattr(api, "get_product_service", lambda: StubProductService())
    client = TestClient(api.app)

    response = client.post(
        "/api/v1/queries",
        headers={"X-Request-ID": "header-request"},
        json={
            "question": "查询存款余额",
            "session_id": "integration-session",
            "request_id": "body-request-001",
            "caller_system": "risk-platform",
        },
    )

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "body-request-001"
    assert response.json()["request_id"] == "body-request-001"
    assert response.json()["caller_system"] == "risk-platform"


def test_validation_error_keeps_detail_and_adds_machine_fields() -> None:
    client = TestClient(api.app)

    response = client.post(
        "/api/v1/queries",
        headers={"X-Request-ID": "validation-request-001"},
        json={"question": ""},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "REQUEST_VALIDATION_ERROR"
    assert response.json()["detail"]
    assert response.json()["request_id"] == "validation-request-001"
    assert response.headers["X-Request-ID"] == "validation-request-001"


def test_query_error_uses_body_request_id(monkeypatch) -> None:
    class FailingProductService:
        def query(self, question, session_id, user):
            raise ValueError("查询条件不完整")

    monkeypatch.setattr(api, "get_product_service", lambda: FailingProductService())
    client = TestClient(api.app)

    response = client.post(
        "/api/v1/queries",
        json={
            "question": "查询数据",
            "request_id": "body-error-request-001",
        },
    )

    assert response.status_code == 400
    assert response.json() == {
        "code": "REQUEST_INVALID",
        "detail": "查询条件不完整",
        "request_id": "body-error-request-001",
    }
    assert response.headers["X-Request-ID"] == "body-error-request-001"


def test_openapi_exposes_query_contract_and_bearer_scheme() -> None:
    api.app.openapi_schema = None
    schema = api.app.openapi()

    assert schema["components"]["securitySchemes"]["BearerAuth"] == {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "opaque-token",
    }
    operation = schema["paths"]["/api/v1/queries"]["post"]
    assert operation["security"] == [{"BearerAuth": []}]
    assert operation["responses"]["200"]["content"]["application/json"][
        "schema"
    ] == {"$ref": "#/components/schemas/ProductQueryResponse"}
    request_fields = schema["components"]["schemas"]["QueryRequest"]["properties"]
    assert {"question", "session_id", "request_id", "caller_system"} <= set(
        request_fields
    )


def test_default_service_uses_declared_data_source_boundary(service) -> None:
    assert isinstance(service.data_source, DuckDBDataSourceAdapter)
    assert isinstance(service.data_source, DataSourceAdapter)
