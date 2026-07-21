from __future__ import annotations

import json

import batch_test
from text2sql.errors import PlanningError


def test_batch_error_prints_and_persists_intermediate_diagnostics(tmp_path, monkeypatch, capsys):
    input_path = tmp_path / "questions.jsonl"
    output_path = tmp_path / "results.txt"
    input_path.write_text(json.dumps("测试问题", ensure_ascii=False) + "\n", encoding="utf-8")

    def fail_with_diagnostics(question: str, session_id: str) -> str:
        error = PlanningError("测试失败")
        error.diagnostics = [
            {"stage": "rule_decision", "candidates": {"metrics": ["ZB001"]}},
            {"stage": "llm_response", "content": "raw model output"},
        ]
        raise error

    monkeypatch.setattr(batch_test.run_module, "run", fail_with_diagnostics)

    assert batch_test.main([str(input_path), str(output_path)]) == 0

    console = capsys.readouterr().out
    text_log = output_path.read_text(encoding="utf-8")
    record = json.loads(output_path.with_suffix(".jsonl").read_text(encoding="utf-8"))
    assert "ERR-DIAGNOSTICS" in console
    assert "raw model output" in console
    assert "中间诊断" in text_log
    assert record["diagnostics"][1]["content"] == "raw model output"
