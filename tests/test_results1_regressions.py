from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest

from text2sql.config import Settings
from text2sql.llm_planner import parse_llm_plan
from text2sql.service import Text2SQLService


RESULTS_PATH = Path(__file__).resolve().parents[1] / "results1.jsonl"


class RecordedLLMPlanner:
    def __init__(self, responses: dict[str, str]) -> None:
        self.responses = responses

    def plan(self, question: str, **kwargs):
        return parse_llm_plan(self.responses[question])


class ForbiddenLLMPlanner:
    def plan(self, **kwargs):
        raise AssertionError("historical network failure should now use the rule path")


def test_replay_results1_non_network_failures_without_api():
    if not RESULTS_PATH.is_file():
        pytest.skip("results1.jsonl is not available")

    records = [json.loads(line) for line in RESULTS_PATH.read_text(encoding="utf-8").splitlines()]
    replay_records: list[dict[str, object]] = []
    responses: dict[str, str] = {}
    for record in records:
        error = record.get("error")
        if not error or "LLM网络调用失败" in str(error):
            continue
        llm_responses = [
            event["content"]
            for event in record.get("diagnostics", [])
            if event.get("stage") == "llm_response" and isinstance(event.get("content"), str)
        ]
        if llm_responses:
            question = str(record["question"])
            responses[question] = llm_responses[-1]
            replay_records.append(record)

    settings = replace(Settings.from_env(), llm_retries=0)
    service = Text2SQLService(settings=settings, llm_planner=RecordedLLMPlanner(responses))
    failures: list[str] = []
    for record in replay_records:
        try:
            response = service.ask(str(record["question"]), f"replay-{record['index']}")
            assert response.answer
        except Exception as exc:  # collect every historical regression in one test report
            failures.append(f"#{record['index']} {type(exc).__name__}: {exc}")

    assert not failures, "\n".join(failures)


def test_results1_network_failures_now_use_rules_without_api():
    if not RESULTS_PATH.is_file():
        pytest.skip("results1.jsonl is not available")

    records = [json.loads(line) for line in RESULTS_PATH.read_text(encoding="utf-8").splitlines()]
    network_failures = [
        record for record in records if "LLM网络调用失败" in str(record.get("error"))
    ]
    settings = replace(Settings.from_env(), llm_retries=0)
    service = Text2SQLService(settings=settings, llm_planner=ForbiddenLLMPlanner())
    failures: list[str] = []
    for record in network_failures:
        try:
            response = service.ask(str(record["question"]), f"network-replay-{record['index']}")
            assert response.route == "rule"
            assert response.answer
        except Exception as exc:
            failures.append(f"#{record['index']} {type(exc).__name__}: {exc}")

    assert not failures, "\n".join(failures)
