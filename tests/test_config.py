from __future__ import annotations

from text2sql.config import Settings


def test_dotenv_has_priority_over_process_environment(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "TEXT2SQL_LLM_URL=https://from-dotenv.example/chat/completions\n"
        "TEXT2SQL_LLM_MODEL=dotenv-model\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TEXT2SQL_LLM_URL", "https://from-process.example/chat/completions")
    monkeypatch.setenv("TEXT2SQL_LLM_MODEL", "process-model")

    settings = Settings.from_env(env_file)

    assert settings.llm_url == "https://from-dotenv.example/chat/completions"
    assert settings.llm_model == "dotenv-model"


def test_process_environment_fills_missing_dotenv_values(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("TEXT2SQL_LLM_MODEL=dotenv-model\n", encoding="utf-8")
    monkeypatch.setenv("TEXT2SQL_LLM_URL", "https://process-fallback.example/chat/completions")

    settings = Settings.from_env(env_file)

    assert settings.llm_url == "https://process-fallback.example/chat/completions"
