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


def test_llm_base_url_builds_chat_completions_endpoint(tmp_path):
    settings = Settings(
        xlsx_path=tmp_path / "data.xlsx",
        db_path=tmp_path / "data.duckdb",
        llm_base_url="https://contest.example/v1/",
    )

    assert (
        settings.llm_chat_completions_url
        == "https://contest.example/v1/chat/completions"
    )


def test_existing_full_llm_url_has_priority_over_base_url(tmp_path):
    settings = Settings(
        xlsx_path=tmp_path / "data.xlsx",
        db_path=tmp_path / "data.duckdb",
        llm_url="https://existing.example/chat/completions",
        llm_base_url="https://contest.example/v1",
    )

    assert (
        settings.llm_chat_completions_url
        == "https://existing.example/chat/completions"
    )
