from __future__ import annotations

import json

from text2sql.config import Settings
from text2sql.final_answer import FinalAnswerGenerator
from text2sql.models import QueryResponse
from text2sql.product_models import UserContext
from text2sql.product_service import ProductQueryService
from text2sql.product_store import ProductStore


class _FakeStreamResponse:
    encoding = "utf-8"

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def raise_for_status(self) -> None:
        return None

    def iter_lines(self, *, decode_unicode: bool):
        assert decode_unicode is True
        yield 'data: {"choices":[{"delta":{"content":"存款余额"}}]}'
        yield 'data: {"choices":[{"delta":{"content":"为42.32亿元。"}}]}'
        yield 'data: {"choices":[],"usage":{"total_tokens":12}}'
        yield "data: [DONE]"


def test_final_answer_generator_streams_with_restricted_prompt(
    monkeypatch,
    tmp_path,
) -> None:
    captured: dict[str, object] = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return _FakeStreamResponse()

    monkeypatch.setattr("text2sql.final_answer.requests.post", fake_post)
    settings = Settings(
        xlsx_path=tmp_path / "data.xlsx",
        db_path=tmp_path / "data.duckdb",
        llm_url="https://llm.example/chat/completions",
        llm_api_key="test-key",
        llm_model="test-model",
    )
    generator = FinalAnswerGenerator(settings)

    chunks = list(
        generator.stream(
            question="各项存款余额是多少？",
            columns=["metric_value", "unit"],
            rows=[[42.32, "亿元"]],
            truncated=False,
        )
    )

    assert chunks == ["存款余额", "为42.32亿元。"]
    request_json = captured["json"]
    assert isinstance(request_json, dict)
    assert request_json["stream"] is True
    assert request_json["enable_thinking"] is False
    messages = request_json["messages"]
    assert isinstance(messages, list)
    system_prompt = messages[0]["content"]
    assert "不得擅自做任何与问题无关的延申或者结论推导" in system_prompt
    assert "结论控制在200字以内" not in system_prompt
    assert "缺少同比或环比数据时明确说明" not in system_prompt
    user_context = json.loads(messages[1]["content"])
    assert user_context["question"] == "各项存款余额是多少？"
    assert user_context["query_result"]["records"] == [
        {"metric_value": 42.32, "unit": "亿元"}
    ]


def test_product_query_defers_only_unmatched_answer_to_llm(
    service,
    monkeypatch,
    tmp_path,
) -> None:
    class FakeFinalAnswers:
        def stream(self, **context):
            assert context["question"] == "请回答这个自定义问题"
            assert context["rows"] == [["江苏省A市农商行", 42.32, "亿元"]]
            yield "A市农商行各项存款余额"
            yield "为42.32亿元。"

    def fake_ask(
        question: str,
        session_id: str,
        *,
        plan_authorizer=None,
    ) -> QueryResponse:
        return QueryResponse(
            answer="通用格式化结果",
            answer_mode="llm",
            session_id=session_id,
            route="llm",
            sql="SELECT 1",
            plan={
                "source": "llm",
                "query_type": "custom",
                "operation": "multi_condition",
                "organizations": ["ORG001"],
                "organization_scope": "selected",
                "metrics": ["ZB001"],
                "current_date": "2026-03-31",
            },
            columns=("org_name", "result_value", "unit"),
            rows=(("江苏省A市农商行", 42.32, "亿元"),),
        )

    monkeypatch.setattr(service, "ask", fake_ask)
    product = ProductQueryService(
        service,
        ProductStore(tmp_path / "product.sqlite3"),
        final_answers=FakeFinalAnswers(),
    )
    analyst = UserContext("answer-user", "analyst")

    response = product.query(
        "请回答这个自定义问题",
        "answer-session",
        analyst,
    )

    assert response.answer_mode == "llm"
    assert response.answer_status == "pending"
    assert response.insight is None

    events = list(product.stream_final_answer(response.query_id, analyst))

    assert [event for event, _ in events] == [
        "answer_start",
        "answer_delta",
        "answer_delta",
        "answer_done",
    ]
    assert events[1][1]["content"] == "A市农商行各项存款余额"
    completed = product.get_query(response.query_id, analyst)
    assert completed.answer_status == "completed"
    assert completed.insight is not None
    assert completed.insight.summary == "A市农商行各项存款余额为42.32亿元。"


def test_product_query_falls_back_to_core_answer_when_final_llm_fails(
    service,
    monkeypatch,
    tmp_path,
) -> None:
    class FailingFinalAnswers:
        def stream(self, **_context):
            raise RuntimeError("upstream stream failed")
            yield  # pragma: no cover

    def fake_ask(
        question: str,
        session_id: str,
        *,
        plan_authorizer=None,
    ) -> QueryResponse:
        return QueryResponse(
            answer="江苏省C市农商行：净利润回落额为7.9万元；净利润/存款比为241.73%",
            answer_mode="llm",
            session_id=session_id,
            route="llm",
            sql="SELECT 1",
            plan={
                "source": "llm",
                "query_type": "multi_condition",
                "operation": "multi_condition",
                "organizations": ["ORG003"],
                "organization_scope": "selected",
                "metrics": ["ZB011", "ZB001"],
                "current_date": "2026-03-31",
            },
            columns=("metric_name", "unit", "result_value"),
            rows=(("净利润回落额", "万元", 7.9), ("净利润/存款比", "%", 241.73)),
        )

    monkeypatch.setattr(service, "ask", fake_ask)
    store = ProductStore(tmp_path / "product.sqlite3")
    product = ProductQueryService(
        service,
        store,
        final_answers=FailingFinalAnswers(),
    )
    analyst = UserContext("fallback-user", "analyst")
    response = product.query("组合派生指标问题", "fallback-session", analyst)

    assert response.answer_mode == "llm"
    assert response.answer_status == "pending"
    events = list(product.stream_final_answer(response.query_id, analyst))

    assert [event for event, _ in events] == ["answer_start", "answer_done"]
    assert events[-1][1]["fallback"] is True
    assert events[-1][1]["answer"] == (
        "江苏省C市农商行：净利润回落额为7.9万元；净利润/存款比为241.73%"
    )
    completed = product.get_query(response.query_id, analyst)
    assert completed.answer_mode == "llm"
    assert completed.answer_status == "completed"
    assert completed.insight is not None
    assert completed.insight.summary == events[-1][1]["answer"]
    assert completed.warnings == (
        "最终回答模型调用失败，已返回基于查询结果生成的降级答案。",
    )
    audits = store.list_audit()
    assert any(
        audit["action"] == "answer.fallback"
        and audit["query_id"] == response.query_id
        and "RuntimeError" in audit["details"]["error"]
        for audit in audits
    )
