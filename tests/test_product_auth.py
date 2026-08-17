from fastapi.testclient import TestClient

from text2sql import api


def test_default_accounts_include_three_analysts(monkeypatch) -> None:
    for name in (
        "TEXT2SQL_ANALYST_USERNAME",
        "TEXT2SQL_ANALYST_PASSWORD",
        "TEXT2SQL_ANALYST_2_USERNAME",
        "TEXT2SQL_ANALYST_2_PASSWORD",
        "TEXT2SQL_ANALYST_3_USERNAME",
        "TEXT2SQL_ANALYST_3_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)
    api.get_auth_service.cache_clear()
    client = TestClient(api.app)

    for username, password in (
        ("analyst", "analyst123"),
        ("analyst2", "analyst2123"),
        ("analyst3", "analyst3123"),
    ):
        response = client.post(
            "/api/v1/auth/login",
            json={"username": username, "password": password},
        )
        assert response.status_code == 200
        assert response.json()["user"]["role"] == "analyst"

    api.get_auth_service.cache_clear()


def test_login_logout_and_relogin(monkeypatch) -> None:
    monkeypatch.setenv("TEXT2SQL_ANALYST_USERNAME", "test-analyst")
    monkeypatch.setenv("TEXT2SQL_ANALYST_PASSWORD", "test-password")
    api.get_auth_service.cache_clear()
    client = TestClient(api.app)

    denied = client.post(
        "/api/v1/auth/login",
        json={"username": "test-analyst", "password": "wrong"},
    )
    assert denied.status_code == 401

    logged_in = client.post(
        "/api/v1/auth/login",
        json={"username": "test-analyst", "password": "test-password"},
    )
    assert logged_in.status_code == 200
    token = logged_in.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/v1/auth/me", headers=headers).json()["role"] == "analyst"
    context = api.user_context("spoofed-admin", "admin", "", headers["Authorization"])
    assert context.user_id == "test-analyst"
    assert context.role == "analyst"

    assert client.post("/api/v1/auth/logout", headers=headers).status_code == 200
    assert client.get("/api/v1/auth/me", headers=headers).status_code == 401

    relogged = client.post(
        "/api/v1/auth/login",
        json={"username": "test-analyst", "password": "test-password"},
    )
    assert relogged.status_code == 200
    assert relogged.json()["access_token"] != token
    api.get_auth_service.cache_clear()


def test_admin_audit_user_query_parameter_filters_without_shadowing_context(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    class StubProductService:
        def admin_audit(self, user, *, sort_by, risk_level, user_id):
            captured.update(
                current_user=user.user_id,
                sort_by=sort_by,
                risk_level=risk_level,
                audit_user=user_id,
            )
            return []

    monkeypatch.setattr(api, "get_product_service", lambda: StubProductService())
    client = TestClient(api.app)

    response = client.get(
        "/api/v1/admin/audit?sort=risk_desc&risk=elevated&user=analyst2",
        headers={"X-User-Id": "admin", "X-User-Role": "admin"},
    )

    assert response.status_code == 200
    assert response.json() == {"items": []}
    assert captured == {
        "current_user": "admin",
        "sort_by": "risk_desc",
        "risk_level": "elevated",
        "audit_user": "analyst2",
    }
