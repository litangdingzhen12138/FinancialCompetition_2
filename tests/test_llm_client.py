from __future__ import annotations

from collections.abc import Iterator

from text2sql.config import Settings
from text2sql.final_answer import FinalAnswerGenerator
from text2sql.llm_client import CompletionResult
from text2sql.llm_planner import LLMPlanner


class FakeChatModelClient:
    available = True

    def complete_json(self, *, system_prompt, payload) -> CompletionResult:
        assert system_prompt
        assert payload == {"question": "测试"}
        return CompletionResult('{"ok": true}', 200)

    def stream_text(self, *, system_prompt, payload) -> Iterator[str]:
        assert system_prompt
        assert payload["question"] == "查询存款余额"
        yield "存款余额"
        yield "为42.32亿元。"


def _settings(tmp_path) -> Settings:
    return Settings(
        xlsx_path=tmp_path / "data.xlsx",
        db_path=tmp_path / "data.duckdb",
    )


def test_llm_planner_accepts_injected_client(tmp_path) -> None:
    planner = LLMPlanner(_settings(tmp_path), client=FakeChatModelClient())

    result = planner._request(
        system_prompt="system",
        payload={"question": "测试"},
        trace=None,
        stage_prefix="test",
    )

    assert result == '{"ok": true}'


def test_openai_compatible_planner_uses_base_url(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self):
            return {
                "choices": [
                    {"message": {"content": '{"ok": true}'}}
                ]
            }

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return FakeResponse()

    monkeypatch.setattr("text2sql.llm_client.requests.post", fake_post)
    settings = Settings(
        xlsx_path=tmp_path / "data.xlsx",
        db_path=tmp_path / "data.duckdb",
        llm_base_url="https://contest.example/v1",
        llm_api_key="contest-key",
        llm_model="contest-model",
    )
    planner = LLMPlanner(settings)

    result = planner._request(
        system_prompt="system",
        payload={"question": "测试"},
        trace=None,
        stage_prefix="test",
    )

    assert result == '{"ok": true}'
    assert captured["url"] == "https://contest.example/v1/chat/completions"
    request_json = captured["json"]
    assert request_json["model"] == "contest-model"
    assert request_json["response_format"] == {"type": "json_object"}


def test_final_answer_accepts_injected_client(tmp_path) -> None:
    generator = FinalAnswerGenerator(
        _settings(tmp_path),
        client=FakeChatModelClient(),
    )

    chunks = list(
        generator.stream(
            question="查询存款余额",
            columns=["metric_value", "unit"],
            rows=[[42.32, "亿元"]],
            truncated=False,
        )
    )

    assert chunks == ["存款余额", "为42.32亿元。"]
