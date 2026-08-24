from __future__ import annotations

from dataclasses import replace

import pytest

from text2sql.chart_recommender import ChartRecommender
from text2sql.models import QueryPlan, QueryResult
from text2sql.product_models import UserContext
from text2sql.product_service import ProductQueryService
from text2sql.product_store import ProductStore


def test_chart_recommender_selects_line_for_time_series(service) -> None:
    plan = QueryPlan(
        source="rule",
        query_type="trend",
        operation="quarterly_trend",
        organizations=("ORG001",),
        organization_scope="selected",
        metrics=("ZB001",),
        current_date="2026-03-31",
        expected_shape="time_series",
    )
    result = QueryResult(
        ("data_date", "org_name", "metric_value", "unit"),
        (
            ("2025-12-31", "江苏省A市农商行", 40.0, "亿元"),
            ("2026-03-31", "江苏省A市农商行", 42.0, "亿元"),
        ),
    )

    primary, alternatives = ChartRecommender(service.catalog).recommend(plan, result)

    assert primary.chart_type == "line"
    assert primary.x_field == "data_date"
    assert primary.y_fields == ("metric_value",)
    assert alternatives[0].chart_type == "table"


def test_difference_result_card_uses_change_value(service) -> None:
    plan = QueryPlan(
        source="rule",
        query_type="comparison",
        operation="difference",
        organizations=("ORG010",),
        organization_scope="selected",
        metrics=("ZB011",),
        current_date="2026-03-31",
        comparison_date="2025-12-31",
    )
    result = QueryResult(
        ("org_name", "current_value", "comparison_value", "change_value", "unit"),
        (("江苏省J市农商行", 163.19, 142.85, 20.34, "万元"),),
    )

    primary, alternatives = ChartRecommender(service.catalog).recommend(plan, result)

    assert primary.chart_type == "metric"
    assert primary.title == "净利润变动"
    assert primary.y_fields == ("change_value",)
    assert primary.unit == "万元"
    assert alternatives[0].chart_type == "table"


def test_product_query_persists_history_and_permissions(service, tmp_path) -> None:
    settings = replace(service.settings, product_db_path=tmp_path / "product.sqlite3")
    service.settings = settings
    product = ProductQueryService(service, ProductStore(settings.product_db_path))
    analyst = UserContext("analyst-1", "analyst")

    response = product.query(
        "江苏省A市农商行在2025年6月15日，各项存款余额是多少？",
        "session-product",
        analyst,
    )
    history = product.history(analyst)

    assert response.query_id
    assert response.sql
    assert response.answer_mode == "rule"
    assert response.answer_status == "completed"
    assert response.insight is not None
    assert response.visualization.chart_type == "metric"
    assert history[0]["query_id"] == response.query_id
    assert product.get_query(response.query_id, analyst).records == response.records


def test_clear_product_session_removes_only_conversation_memory(service, tmp_path) -> None:
    settings = replace(service.settings, product_db_path=tmp_path / "product.sqlite3")
    service.settings = settings
    product = ProductQueryService(service, ProductStore(settings.product_db_path))
    admin = UserContext("admin", "admin")
    owner = UserContext("session-owner", "analyst")
    session_id = "session-to-clear"
    response = product.query(
        "江苏省A市农商行在2026年3月31日，各项存款余额是多少？",
        session_id,
        owner,
    )
    product.store.save_pending_session(
        owner.user_id,
        session_id,
        {"original_question": "待补充问题", "missing_slots": ["date"]},
    )

    product.clear_session(session_id, admin, owner_user_id=owner.user_id)

    internal_session_id = f"{owner.user_id}:{session_id}"
    assert service.sessions.get(internal_session_id).recent_turns == ()
    assert product.store.get_pending_session(owner.user_id, session_id) is None
    assert product.get_query(response.query_id, admin).query_id == response.query_id


def test_analyst_cannot_clear_or_delete_product_session(service, tmp_path) -> None:
    settings = replace(service.settings, product_db_path=tmp_path / "product.sqlite3")
    service.settings = settings
    product = ProductQueryService(service, ProductStore(settings.product_db_path))
    analyst = UserContext("clear-denied", "analyst")
    session_id = "protected-session"
    response = product.query(
        "江苏省A市农商行在2026年3月31日，各项存款余额是多少？",
        session_id,
        analyst,
    )

    with pytest.raises(PermissionError, match="权限不足"):
        product.clear_session(session_id, analyst)
    with pytest.raises(PermissionError, match="权限不足"):
        product.clear_session(session_id, analyst, delete_history=True)

    assert product.get_query(response.query_id, analyst).query_id == response.query_id


def test_delete_product_session_removes_reports_but_keeps_audit(service, tmp_path) -> None:
    settings = replace(service.settings, product_db_path=tmp_path / "product.sqlite3")
    service.settings = settings
    product = ProductQueryService(service, ProductStore(settings.product_db_path))
    admin = UserContext("admin", "admin")
    owner = UserContext("delete-owner", "analyst")
    session_id = "session-to-delete"
    product.query(
        "江苏省A市农商行在2026年3月31日，各项存款余额是多少？",
        session_id,
        owner,
    )

    product.clear_session(
        session_id,
        admin,
        delete_history=True,
        owner_user_id=owner.user_id,
    )

    assert product.history(admin) == []
    audit = product.store.list_audit()
    cleared = next(item for item in audit if item["action"] == "session_clear")
    assert cleared["details"]["deleted_queries"] == 1


def test_session_history_returns_every_turn_in_order(service, tmp_path) -> None:
    settings = replace(service.settings, product_db_path=tmp_path / "product.sqlite3")
    service.settings = settings
    product = ProductQueryService(service, ProductStore(settings.product_db_path))
    analyst = UserContext("history-user", "analyst")
    session_id = "multi-turn-session"
    first = product.query(
        "江苏省A市农商行在2026年3月31日，各项存款余额是多少？",
        session_id,
        analyst,
    )
    second = product.query(
        "比上期增长了多少？",
        session_id,
        analyst,
    )

    history = product.session_history(session_id, analyst)

    assert [item.query_id for item in history] == [first.query_id, second.query_id]


def test_session_title_is_persisted_and_returned_with_history(service, tmp_path) -> None:
    settings = replace(service.settings, product_db_path=tmp_path / "product.sqlite3")
    service.settings = settings
    product = ProductQueryService(service, ProductStore(settings.product_db_path))
    analyst = UserContext("title-user", "analyst")
    session_id = "renamed-session"
    product.query(
        "江苏省A市农商行在2026年3月31日，各项存款余额是多少？",
        session_id,
        analyst,
    )

    renamed = product.rename_session(session_id, "一季度存款分析", analyst)

    assert renamed["title"] == "一季度存款分析"
    assert product.history(analyst)[0]["thread_title"] == "一季度存款分析"


def test_batch_export_records_supports_all_recent_and_session_scope(service, tmp_path) -> None:
    settings = replace(service.settings, product_db_path=tmp_path / "product.sqlite3")
    service.settings = settings
    product = ProductQueryService(service, ProductStore(settings.product_db_path))
    analyst = UserContext("export-user", "analyst")
    first = product.query(
        "江苏省A市农商行在2026年3月31日，各项存款余额是多少？",
        "export-session-a",
        analyst,
    )
    second = product.query(
        "江苏省B市农商行在2026年3月31日，各项存款余额是多少？",
        "export-session-b",
        analyst,
    )
    other_analyst = UserContext("export-user-2", "analyst")
    third = product.query(
        "江苏省C市农商行在2026年3月31日，各项存款余额是多少？",
        "export-session-c",
        other_analyst,
    )

    all_rows = product.batch_export_records(analyst, scope="all")
    recent_rows = product.batch_export_records(analyst, scope="recent", recent_count=1)
    session_rows = product.batch_export_records(
        analyst,
        scope="session",
        session_id="export-session-a",
    )
    with pytest.raises(PermissionError, match="无导出权限"):
        product.batch_export_records(
            UserContext("admin", "admin"),
            scope="all",
        )

    assert {item["query_id"] for item in all_rows} == {first.query_id, second.query_id}
    assert [item["query_id"] for item in recent_rows] == [second.query_id]
    assert [item["query_id"] for item in session_rows] == [first.query_id]


def test_audit_has_no_fixed_limit_and_supports_risk_sorting(tmp_path) -> None:
    store = ProductStore(tmp_path / "audit.sqlite3")
    levels = ("low", "medium", "high")
    for index in range(120):
        store.add_audit(
            event_id=f"event-{index:03d}",
            user_id="audit-user",
            action="query.completed",
            risk_level=levels[index % len(levels)],
            details={},
        )

    all_events = store.list_audit(sort_by="risk_desc")
    elevated = store.list_audit(risk_level="elevated")

    assert len(all_events) == 120
    assert all_events[0]["risk_level"] == "high"
    assert all(item["risk_level"] in {"medium", "high"} for item in elevated)
    assert len(store.list_audit(user_id="audit-user")) == 120
    assert store.list_audit(user_id="missing-user") == []
    assert store.overview()["risk_user_count"] == 1
    assert store.list_audit_users()[0]["user_id"] == "audit-user"


def test_time_drill_uses_period_end_values(service, tmp_path) -> None:
    settings = replace(service.settings, product_db_path=tmp_path / "product.sqlite3")
    service.settings = settings
    product = ProductQueryService(service, ProductStore(settings.product_db_path))
    analyst = UserContext("analyst-2", "analyst")
    source = product.query(
        "江苏省A市农商行在2026年3月31日，各项存款余额是多少？",
        "session-drill",
        analyst,
    )

    drilled = product.time_drill(source.query_id, "month", analyst)

    assert drilled.current_time_level == "month"
    assert drilled.aggregation == "period_end"
    assert drilled.available_time_drill == ("day",)
    assert drilled.drill_path == ("month",)
    assert drilled.visualization.chart_type == "line"
    assert drilled.row_count == 3
    assert drilled.records[0]["period_start"] == "2026-01-01"


def test_difference_query_does_not_offer_time_drill_and_shows_change(service, tmp_path) -> None:
    settings = replace(service.settings, product_db_path=tmp_path / "product.sqlite3")
    service.settings = settings
    product = ProductQueryService(service, ProductStore(settings.product_db_path))
    analyst = UserContext("difference-user", "analyst")
    session_id = "difference-product"
    product.query("江苏省J市农商行2024年末的净利润是多少？", session_id, analyst)

    response = product.query(
        "2026年一季度末的净利润相比2025年四季度末又怎么变了？",
        session_id,
        analyst,
    )

    assert response.visualization.chart_type == "metric"
    assert response.visualization.title == "净利润变动"
    assert response.visualization.y_fields == ("change_value",)
    assert response.records[0]["change_value"] == pytest.approx(20.34)
    assert response.available_time_drill == ()


def test_viewer_masks_sensitive_values(service, tmp_path) -> None:
    settings = replace(service.settings, product_db_path=tmp_path / "product.sqlite3")
    service.settings = settings
    product = ProductQueryService(service, ProductStore(settings.product_db_path))
    analyst = UserContext("shared-user", "analyst")
    response = product.query(
        "江苏省A市农商行在2026年3月31日，不良贷款率是多少？",
        "session-mask",
        analyst,
    )
    viewer = UserContext("shared-user", "viewer")

    masked = product.get_query(response.query_id, viewer)

    assert masked.sql is None
    assert masked.records[0]["metric_value"] == "***"
    assert "隐藏" in masked.insight.summary
