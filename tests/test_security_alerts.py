from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from text2sql.product_models import UserContext
from text2sql.product_service import (
    ALERT_ACCESS_DENIED,
    ALERT_DAILY_EXPORT,
    ALERT_LARGE_EXPORT,
    ALERT_MULTI_METRIC,
    ALERT_OFF_HOURS_EXPORT,
    ALERT_S3_FREQUENCY,
    ALERT_SHARE_FREQUENCY,
    AccountFrozenError,
    LargeExportConfirmationRequired,
    ProductQueryService,
)
from text2sql.product_store import ProductStore


def _product(service, tmp_path, monkeypatch, now: datetime) -> ProductQueryService:
    product = ProductQueryService(service, ProductStore(tmp_path / "security.sqlite3"))
    monkeypatch.setattr(product, "_now_datetime", lambda: now)
    return product


def _audit_event(
    product: ProductQueryService,
    user: UserContext,
    *,
    event_id: str,
    action: str,
    created_at: datetime,
    details: dict,
) -> None:
    product.store.add_audit(
        event_id=event_id,
        user_id=user.user_id,
        action=action,
        risk_level="low",
        details=details,
        created_at=created_at,
    )


def test_query_frequency_and_multi_metric_alerts(service, tmp_path, monkeypatch) -> None:
    now = datetime(2026, 8, 24, 2, 0, tzinfo=timezone.utc)
    product = _product(service, tmp_path, monkeypatch, now)
    user = UserContext("alert-query-user", "analyst")

    for index in range(10):
        event_id = f"s3-{index}"
        _audit_event(
            product,
            user,
            event_id=event_id,
            action="query.completed",
            created_at=now - timedelta(seconds=10 - index),
            details={"data_level": "S3"},
        )
    product._evaluate_query_alerts(
        user,
        event_id="s3-9",
        query_id="query-s3-at-threshold",
        metrics=["ZB013"],
    )
    assert product.store.list_alerts() == []

    _audit_event(
        product,
        user,
        event_id="s3-10",
        action="query.time_drill",
        created_at=now,
        details={"data_level": "S3"},
    )
    product._evaluate_query_alerts(
        user,
        event_id="s3-10",
        query_id="query-s3",
        metrics=["ZB013"],
    )

    eight_metric_event = product._audit(
        user,
        "query.completed",
        "low",
        {"data_level": "S2"},
        "query-eight-metrics",
    )
    product._evaluate_query_alerts(
        user,
        event_id=eight_metric_event,
        query_id="query-eight-metrics",
        metrics=[f"ZB{index:03d}" for index in range(1, 9)],
    )
    assert ALERT_MULTI_METRIC not in {
        item["rule_code"] for item in product.store.list_alerts()
    }

    multi_event = product._audit(
        user,
        "query.completed",
        "low",
        {"data_level": "S2"},
        "query-multi",
    )
    product._evaluate_query_alerts(
        user,
        event_id=multi_event,
        query_id="query-multi",
        metrics=[f"ZB{index:03d}" for index in range(1, 10)],
    )

    rules = {item["rule_code"] for item in product.store.list_alerts()}
    assert ALERT_S3_FREQUENCY in rules
    assert ALERT_MULTI_METRIC in rules


def test_third_denial_freezes_user_for_fifteen_minutes(
    service,
    tmp_path,
    monkeypatch,
) -> None:
    now = datetime(2026, 8, 24, 2, 0, tzinfo=timezone.utc)
    product = _product(service, tmp_path, monkeypatch, now)
    user = UserContext("denied-user", "analyst")

    for index in range(3):
        product._record_access_denied(
            user,
            question=f"越权问题{index}",
            reason="无权访问机构",
            query_id=f"denied-{index}",
        )

    freeze = product.store.get_active_freeze(user.user_id, now=now)
    assert freeze is not None
    assert datetime.fromisoformat(freeze["frozen_until"]) == now + timedelta(minutes=15)
    assert product.store.list_alerts()[0]["rule_code"] == ALERT_ACCESS_DENIED
    with pytest.raises(AccountFrozenError):
        product._ensure_not_frozen(user)


def test_export_thresholds_and_confirmation(service, tmp_path, monkeypatch) -> None:
    now = datetime(2026, 8, 24, 2, 0, tzinfo=timezone.utc)
    product = _product(service, tmp_path, monkeypatch, now)
    user = UserContext("export-alert-user", "analyst")
    record = {
        "query_id": "large-query",
        "user_id": user.user_id,
        "session_id": "large-session",
        "question": "大批量导出",
        "route": "rule",
        "status": "completed",
        "sql": "SELECT metric_value FROM metric_values",
        "plan": {
            "operation": "value",
            "organizations": ["ORG001"],
            "organization_scope": "selected",
            "metrics": ["ZB001"],
        },
        "columns": ["metric_value"],
        "rows": [[float(index)] for index in range(201)],
        "answer": "共201行",
        "answer_mode": "rule",
        "answer_status": "completed",
        "visualization": {"chart_type": "table", "title": "大批量导出"},
        "insight": None,
        "warnings": [],
        "truncated": False,
        "duration_ms": 1,
        "created_at": now.isoformat(),
    }
    product.store.save_query(record)

    with pytest.raises(LargeExportConfirmationRequired) as exc_info:
        product.export_record("large-query", user)
    assert exc_info.value.row_count == 201
    assert product.store.list_alerts() == []

    exported = product.export_record(
        "large-query",
        user,
        confirm_large_export=True,
    )
    assert len(exported["rows"]) == 201
    assert product.store.list_alerts()[0]["rule_code"] == ALERT_LARGE_EXPORT


def test_daily_off_hours_and_share_alerts(service, tmp_path, monkeypatch) -> None:
    saturday = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
    product = _product(service, tmp_path, monkeypatch, saturday)
    user = UserContext("combined-alert-user", "analyst")

    _audit_event(
        product,
        user,
        event_id="prior-export",
        action="query.exported",
        created_at=saturday - timedelta(minutes=1),
        details={"exported_rows": 1000, "data_level": "S2"},
    )
    product._evaluate_export_alerts(
        user,
        event_id="prior-export",
        query_id="at-daily-threshold",
        exported_rows=1000,
        metrics=["ZB001"],
    )
    assert ALERT_DAILY_EXPORT not in {
        item["rule_code"] for item in product.store.list_alerts()
    }
    export_event = product._audit(
        user,
        "query.exported",
        "low",
        {"exported_rows": 1, "data_level": "S3"},
        "off-hours-query",
    )
    product._evaluate_export_alerts(
        user,
        event_id=export_event,
        query_id="off-hours-query",
        exported_rows=1,
        metrics=["ZB013"],
    )

    for index in range(2):
        share_event = product._audit(
            user,
            "query.shared",
            "low",
            {"share_id": f"share-{index}"},
            "off-hours-query",
        )
    product._evaluate_share_alerts(
        user,
        event_id=share_event,
        query_id="off-hours-query",
    )
    assert ALERT_SHARE_FREQUENCY not in {
        item["rule_code"] for item in product.store.list_alerts()
    }
    share_event = product._audit(
        user,
        "query.shared",
        "low",
        {"share_id": "share-2"},
        "off-hours-query",
    )
    product._evaluate_share_alerts(
        user,
        event_id=share_event,
        query_id="off-hours-query",
    )

    rules = {item["rule_code"] for item in product.store.list_alerts()}
    assert ALERT_DAILY_EXPORT in rules
    assert ALERT_OFF_HOURS_EXPORT in rules
    assert ALERT_SHARE_FREQUENCY in rules


def test_admin_can_list_and_resolve_alerts(service, tmp_path, monkeypatch) -> None:
    now = datetime(2026, 8, 24, 2, 0, tzinfo=timezone.utc)
    product = _product(service, tmp_path, monkeypatch, now)
    user = UserContext("monitored-user", "analyst")
    admin = UserContext("admin", "admin")
    source_event = product._audit(
        user,
        "query.completed",
        "low",
        {"data_level": "S2"},
        "monitored-query",
    )
    alert_id = product._add_alert(
        user,
        source_event_id=source_event,
        rule_code=ALERT_MULTI_METRIC,
        severity="medium",
        details={"metric_count": 9},
        query_id="monitored-query",
    )

    assert product.admin_alerts(admin, status="open")[0]["alert_id"] == alert_id
    updated = product.update_alert_status(alert_id, "resolved", admin)
    assert updated == {"alert_id": alert_id, "status": "resolved"}
    assert product.admin_alerts(admin, status="resolved")[0]["alert_id"] == alert_id


def test_frozen_share_creator_disables_existing_share(
    service,
    tmp_path,
    monkeypatch,
) -> None:
    now = datetime(2026, 8, 24, 2, 0, tzinfo=timezone.utc)
    product = _product(service, tmp_path, monkeypatch, now)
    owner = UserContext("share-owner", "analyst")
    viewer = UserContext("share-viewer", "viewer")
    product.store.save_query(
        {
            "query_id": "share-query",
            "user_id": owner.user_id,
            "session_id": "share-session",
            "question": "查询存款余额",
            "route": "rule",
            "status": "completed",
            "sql": "SELECT metric_value FROM metric_values",
            "plan": {
                "operation": "value",
                "organizations": ["ORG001"],
                "organization_scope": "selected",
                "metrics": ["ZB001"],
            },
            "columns": ["metric_value"],
            "rows": [[123.45]],
            "answer": "123.45亿元",
            "answer_mode": "rule",
            "answer_status": "completed",
            "visualization": {"chart_type": "table", "title": "存款余额"},
            "insight": None,
            "warnings": [],
            "truncated": False,
            "duration_ms": 1,
            "created_at": now.isoformat(),
        }
    )
    token = product.create_share(
        "share-query",
        owner,
        expires_hours=1,
    )["token"]
    product.store.freeze_user(
        user_id=owner.user_id,
        frozen_until=now + timedelta(minutes=15),
        reason="测试冻结",
        updated_at=now,
    )

    with pytest.raises(PermissionError, match="分享创建者账号已冻结"):
        product.resolve_share(token, viewer)


def test_account_freeze_also_blocks_admin_operations(
    service,
    tmp_path,
    monkeypatch,
) -> None:
    now = datetime(2026, 8, 24, 2, 0, tzinfo=timezone.utc)
    product = _product(service, tmp_path, monkeypatch, now)
    admin = UserContext(
        "frozen-admin",
        "admin",
        business_role="system_admin",
        organization_access="none",
    )
    product.store.freeze_user(
        user_id=admin.user_id,
        frozen_until=now + timedelta(minutes=15),
        reason="测试冻结",
        updated_at=now,
    )

    with pytest.raises(AccountFrozenError):
        product.admin_overview(admin)
    with pytest.raises(AccountFrozenError):
        product.clear_session("any-session", admin)
