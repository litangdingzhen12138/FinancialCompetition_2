from __future__ import annotations

import sqlite3

import pytest

from text2sql.product_store import AUDIT_GENESIS_HASH, ProductStore


def _add_audit(
    store: ProductStore,
    event_id: str,
    *,
    action: str = "query.completed",
    user_id: str = "security-user",
    created_at: str = "2026-08-24T10:00:00+00:00",
    details: dict[str, object] | None = None,
) -> str:
    return store.add_audit(
        event_id=event_id,
        user_id=user_id,
        action=action,
        risk_level="low",
        details=details or {},
        query_id="query-1",
        target="metric_values",
        ip_address="127.0.0.1",
        created_at=created_at,
    )


def test_audit_hash_chain_detects_tampering(tmp_path) -> None:
    path = tmp_path / "product.sqlite3"
    store = ProductStore(path)

    assert _add_audit(store, "event-1", details={"data_level": "S3"}) == "event-1"
    _add_audit(
        store,
        "event-2",
        action="query.exported",
        created_at="2026-08-24T10:01:00+00:00",
        details={"exported_rows": 20},
    )

    events = {item["event_id"]: item for item in store.list_audit()}
    assert events["event-1"]["previous_hash"] == AUDIT_GENESIS_HASH
    assert events["event-2"]["previous_hash"] == events["event-1"]["event_hash"]
    assert store.verify_audit_chain() is True

    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE audit_events SET details_json = ? WHERE event_id = ?",
            ('{"exported_rows": 9999}', "event-2"),
        )

    assert ProductStore(path).verify_audit_chain() is False


def test_existing_audit_rows_are_migrated_into_hash_chain(tmp_path) -> None:
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE audit_events (
                event_id TEXT PRIMARY KEY,
                query_id TEXT,
                user_id TEXT NOT NULL,
                action TEXT NOT NULL,
                risk_level TEXT NOT NULL,
                details_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            INSERT INTO audit_events VALUES (
                'legacy-1', NULL, 'legacy-user', 'query.completed',
                'low', '{"data_level":"S2"}', '2026-08-24T09:00:00+00:00'
            );
            INSERT INTO audit_events VALUES (
                'legacy-2', NULL, 'legacy-user', 'query.completed',
                'low', '{"data_level":"S3"}', '2026-08-24T09:01:00+00:00'
            );
            """
        )

    store = ProductStore(path)

    with sqlite3.connect(path) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(audit_events)")
        }
    assert {"target", "ip_address", "previous_hash", "event_hash"} <= columns
    assert store.verify_audit_chain() is True

    _add_audit(store, "new-event")
    assert store.verify_audit_chain() is True

    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE audit_events SET details_json = ?, event_hash = NULL "
            "WHERE event_id = ?",
            ('{"data_level":"S1"}', "legacy-2"),
        )

    reopened = ProductStore(path)
    assert reopened.verify_audit_chain() is False


def test_audit_chain_anchor_detects_tail_deletion(tmp_path) -> None:
    path = tmp_path / "tail-deletion.sqlite3"
    store = ProductStore(path)
    _add_audit(store, "event-1")
    _add_audit(store, "event-2")

    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM audit_events WHERE event_id = 'event-2'")

    reopened = ProductStore(path)
    assert reopened.verify_audit_chain() is False
    with pytest.raises(RuntimeError, match="审计链状态异常"):
        _add_audit(reopened, "event-3")

    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE audit_chain_state")

    with pytest.raises(RuntimeError, match="审计链锚点缺失"):
        ProductStore(path)


def test_audit_chain_anchor_detects_full_event_deletion(tmp_path) -> None:
    path = tmp_path / "all-deleted.sqlite3"
    store = ProductStore(path)
    _add_audit(store, "event-1")

    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM audit_events")
        connection.execute("DROP TABLE audit_chain_state")

    with pytest.raises(RuntimeError, match="审计链锚点缺失"):
        ProductStore(path)


def test_audit_window_counts_and_exported_row_sum(tmp_path) -> None:
    store = ProductStore(tmp_path / "events.sqlite3")
    _add_audit(
        store,
        "s3-recent",
        created_at="2026-08-24T10:00:00+00:00",
        details={"data_level": "S3"},
    )
    _add_audit(
        store,
        "s2-recent",
        created_at="2026-08-24T10:01:00+00:00",
        details={"data_level": "S2"},
    )
    _add_audit(
        store,
        "s3-old",
        created_at="2026-08-24T09:50:00+00:00",
        details={"data_level": "S3"},
    )
    _add_audit(
        store,
        "s3-other-user",
        user_id="other-user",
        created_at="2026-08-24T10:02:00+00:00",
        details={"data_level": "S3"},
    )
    _add_audit(
        store,
        "single-export",
        action="query.exported",
        created_at="2026-08-24T10:03:00+00:00",
        details={"exported_rows": 201},
    )
    _add_audit(
        store,
        "batch-export",
        action="history.batch_exported",
        created_at="2026-08-24T10:04:00+00:00",
        details={"exported_rows": 800},
    )
    _add_audit(
        store,
        "not-exported",
        action="export.confirmation_required",
        created_at="2026-08-24T10:05:00+00:00",
        details={"exported_rows": 5000},
    )

    since = "2026-08-24T09:55:00+00:00"
    assert (
        store.count_audit_events(
            user_id="security-user",
            action="query.completed",
            since=since,
        )
        == 2
    )
    assert (
        store.count_audit_events(
            user_id="security-user",
            action="query.completed",
            since=since,
            data_level="S3",
        )
        == 1
    )
    assert store.sum_exported_rows(user_id="security-user", since=since) == 1001


def test_alert_lifecycle_and_recent_deduplication(tmp_path) -> None:
    store = ProductStore(tmp_path / "alerts.sqlite3")
    _add_audit(store, "source-event")

    assert (
        store.add_alert(
            alert_id="alert-1",
            source_event_id="source-event",
            rule_code="S3_QUERY_BURST",
            user_id="security-user",
            query_id="query-1",
            severity="MEDIUM",
            details={"window_count": 11},
            created_at="2026-08-24T10:05:00+00:00",
        )
        == "alert-1"
    )

    alerts = store.list_alerts(status="open", severity="medium")
    assert len(alerts) == 1
    assert alerts[0]["details"] == {"window_count": 11}
    assert store.overview()["risk_user_count"] == 1
    assert store.overview()["risk_event_count"] == 1
    assert store.list_audit_users()[0]["risk_event_count"] == 1
    assert store.has_recent_alert(
        user_id="security-user",
        rule_code="S3_QUERY_BURST",
        since="2026-08-24T10:00:00+00:00",
    )
    assert not store.has_recent_alert(
        user_id="security-user",
        rule_code="S3_QUERY_BURST",
        since="2026-08-24T10:06:00+00:00",
    )

    assert store.update_alert_status(
        "alert-1",
        "resolved",
        updated_at="2026-08-24T10:07:00+00:00",
    )
    assert store.list_alerts(status="resolved")[0]["status"] == "resolved"
    assert store.update_alert_status("missing", "resolved") is False
    with pytest.raises(ValueError, match="告警状态"):
        store.update_alert_status("alert-1", "deleted")


def test_user_freeze_keeps_later_expiry_and_expires(tmp_path) -> None:
    store = ProductStore(tmp_path / "freezes.sqlite3")
    _add_audit(store, "denied-event", action="access.denied")
    store.add_alert(
        alert_id="freeze-alert",
        source_event_id="denied-event",
        rule_code="REPEATED_ACCESS_DENIED",
        user_id="security-user",
        severity="high",
        details={"attempts": 3},
        frozen_until="2026-08-24T10:15:00+00:00",
        freeze_reason="10分钟内连续3次越权",
        created_at="2026-08-24T10:00:00+00:00",
    )
    store.freeze_user(
        user_id="security-user",
        frozen_until="2026-08-24T10:10:00+00:00",
        reason="较短冻结不应覆盖",
        updated_at="2026-08-24T10:01:00+00:00",
    )

    active = store.get_active_freeze(
        "security-user", now="2026-08-24T10:14:59+00:00"
    )
    assert active is not None
    assert active["frozen_until"] == "2026-08-24T10:15:00+00:00"
    assert active["reason"] == "10分钟内连续3次越权"
    assert (
        store.get_active_freeze(
            "security-user", now="2026-08-24T10:15:00+00:00"
        )
        is None
    )


def test_list_and_unfreeze_only_active_accounts(tmp_path) -> None:
    store = ProductStore(tmp_path / "admin-freezes.sqlite3")
    store.freeze_user(
        user_id="active-user",
        frozen_until="2026-08-24T10:15:00+00:00",
        reason="测试有效冻结",
        updated_at="2026-08-24T10:00:00+00:00",
    )
    store.freeze_user(
        user_id="expired-user",
        frozen_until="2026-08-24T09:59:00+00:00",
        reason="测试过期冻结",
        updated_at="2026-08-24T09:45:00+00:00",
    )

    freezes = store.list_active_freezes(now="2026-08-24T10:00:00+00:00")
    assert [item["user_id"] for item in freezes] == ["active-user"]

    removed = store.unfreeze_user(
        "active-user",
        now="2026-08-24T10:00:00+00:00",
    )
    assert removed is not None
    assert removed["reason"] == "测试有效冻结"
    assert store.get_active_freeze(
        "active-user",
        now="2026-08-24T10:00:00+00:00",
    ) is None
    assert store.unfreeze_user(
        "expired-user",
        now="2026-08-24T10:00:00+00:00",
    ) is None
