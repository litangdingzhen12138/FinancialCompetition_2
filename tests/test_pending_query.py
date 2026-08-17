from __future__ import annotations

import json

from fastapi.testclient import TestClient
import pytest

from text2sql import api
from text2sql.config import Settings
from text2sql.errors import ClarificationError
from text2sql.product_service import ProductQueryService
from text2sql.product_store import ProductStore
from text2sql.service import Text2SQLService


def test_missing_organization_is_completed_on_the_next_turn(service):
    session_id = "pending-missing-organization"
    question = "2025年三季度末净利润是多少？比二季度末增长了多少？"

    with pytest.raises(ClarificationError, match="查询机构"):
        service.ask(question, session_id)

    pending = service.sessions.get(session_id).pending_query
    assert pending is not None
    assert pending.missing_slots == ("organization",)
    assert service.sessions.get(session_id).recent_turns == ()

    response = service.ask("机构是江苏省J市农商行", session_id)

    assert response.route == "rule"
    assert response.plan["organizations"] == ("ORG010",)
    assert response.plan["metrics"] == ("ZB011",)
    assert response.plan["current_date"] == "2025-09-30"
    assert response.plan["comparison_date"] == "2025-06-30"
    assert response.answer.index("175.6万元") < response.answer.index("25.34万元")
    state = service.sessions.get(session_id)
    assert state.pending_query is None
    assert len(state.recent_turns) == 1
    assert "补充信息" in state.recent_turns[0].question


def test_multiple_missing_slots_can_be_filled_incrementally(service):
    session_id = "pending-multiple-slots"

    with pytest.raises(ClarificationError, match="查询指标、查询日期"):
        service.ask("江苏省J市农商行的数据是多少？", session_id)

    with pytest.raises(ClarificationError, match="查询日期"):
        service.ask("指标是不良贷款率", session_id)

    pending = service.sessions.get(session_id).pending_query
    assert pending is not None
    assert pending.organizations == ("ORG010",)
    assert pending.metrics == ("ZB013",)
    assert pending.missing_slots == ("date",)

    response = service.ask("日期是2026-04-30", session_id)

    assert response.route == "rule"
    assert response.plan["organizations"] == ("ORG010",)
    assert response.plan["metrics"] == ("ZB013",)
    assert response.plan["current_date"] == "2026-04-30"
    state = service.sessions.get(session_id)
    assert state.pending_query is None
    assert len(state.recent_turns) == 1


def test_complete_new_question_replaces_an_old_pending_query(service):
    session_id = "pending-replaced-by-new-question"

    with pytest.raises(ClarificationError, match="查询机构"):
        service.ask(
            "2025年三季度末净利润是多少？比二季度末增长了多少？",
            session_id,
        )

    response = service.ask(
        "江苏省J市农商行2024年末的净利润是多少？",
        session_id,
    )

    assert response.answer == "江苏省J市农商行：130.56万元"
    assert response.plan["organizations"] == ("ORG010",)
    assert response.plan["metrics"] == ("ZB011",)
    assert response.plan["current_date"] == "2024-12-31"
    assert service.sessions.get(session_id).pending_query is None


def test_explicit_slot_correction_overrides_the_pending_value(service):
    session_id = "pending-explicit-correction"

    with pytest.raises(ClarificationError):
        service.ask("江苏省J市农商行的数据是多少？", session_id)

    with pytest.raises(ClarificationError, match="查询日期"):
        service.ask(
            "机构改成江苏省K市农商行，指标是不良贷款率",
            session_id,
        )

    pending = service.sessions.get(session_id).pending_query
    assert pending is not None
    assert pending.organizations == ("ORG011",)
    assert "江苏省K市农商行" in pending.working_question
    assert "江苏省J市农商行" not in pending.working_question

    response = service.ask("日期是2026-04-30", session_id)

    assert response.plan["organizations"] == ("ORG011",)
    assert response.plan["metrics"] == ("ZB013",)


def test_successful_focus_inheritance_remains_independent_from_pending_query(service):
    session_id = "successful-focus-does-not-conflict"
    service.ask(
        "江苏省J市农商行2024年末的净利润是多少？",
        session_id,
    )

    response = service.ask(
        "2025年三季度末净利润是多少？比二季度末增长了多少？",
        session_id,
    )

    assert response.route == "rule"
    assert response.plan["organizations"] == ("ORG010",)
    assert response.plan["comparison_date"] == "2025-06-30"
    state = service.sessions.get(session_id)
    assert state.pending_query is None
    assert len(state.recent_turns) == 2


def test_stream_returns_session_id_before_clarification_for_next_turn(
    tmp_path,
    monkeypatch,
):
    settings = Settings.from_env()
    core = Text2SQLService(settings=settings)
    product = ProductQueryService(
        core,
        ProductStore(tmp_path / "pending-product.sqlite3"),
    )
    monkeypatch.setattr(api, "get_product_service", lambda: product)
    client = TestClient(api.app)

    first = client.post(
        "/api/v1/queries/stream",
        json={
            "question": "2025年三季度末净利润是多少？比二季度末增长了多少？",
            "session_id": None,
        },
    )
    first_events = _sse_events(first.text)
    session_id = first_events[0][1]["session_id"]

    assert first_events[0][0] == "status"
    assert isinstance(session_id, str) and session_id
    assert first_events[-1][0] == "error"
    assert first_events[-1][1]["session_id"] == session_id
    assert "查询机构" in first_events[-1][1]["message"]

    second = client.post(
        "/api/v1/queries/stream",
        json={
            "question": "机构是江苏省J市农商行",
            "session_id": session_id,
        },
    )
    second_events = _sse_events(second.text)
    result = next(payload for event, payload in second_events if event == "result")

    assert result["session_id"] == session_id
    assert "175.6万元" in result["insight"]["summary"]
    assert "25.34万元" in result["insight"]["summary"]


def test_product_pending_query_survives_service_restart(tmp_path):
    settings = Settings.from_env()
    store_path = tmp_path / "restart-pending.sqlite3"
    user = api.user_context(
        x_user_id="restart-user",
        x_user_role="analyst",
        x_org_scope="",
    )
    session_id = "restart-pending-session"

    first_product = ProductQueryService(
        Text2SQLService(settings=settings),
        ProductStore(store_path),
    )
    with pytest.raises(ClarificationError, match="查询机构"):
        first_product.query(
            "2025年三季度末净利润是多少？比二季度末增长了多少？",
            session_id,
            user,
        )

    persisted = first_product.store.get_pending_session(user.user_id, session_id)
    assert persisted is not None
    assert persisted["missing_slots"] == ["organization"]

    restarted_product = ProductQueryService(
        Text2SQLService(settings=settings),
        ProductStore(store_path),
    )
    response = restarted_product.query(
        "江苏省J市农商行",
        session_id,
        user,
    )

    assert response.insight is not None
    assert "175.6万元" in response.insight.summary
    assert "25.34万元" in response.insight.summary
    assert restarted_product.store.get_pending_session(user.user_id, session_id) is None


def test_bare_slot_without_pending_query_has_a_clear_error(service):
    with pytest.raises(ClarificationError, match="没有可补充的未完成查询"):
        service.ask("江苏省J市农商行", "bare-slot-without-pending")


def _sse_events(body: str) -> list[tuple[str, dict[str, object]]]:
    events: list[tuple[str, dict[str, object]]] = []
    for block in body.strip().split("\n\n"):
        lines = block.splitlines()
        event = next(line[7:] for line in lines if line.startswith("event: "))
        data = next(line[6:] for line in lines if line.startswith("data: "))
        events.append((event, json.loads(data)))
    return events
