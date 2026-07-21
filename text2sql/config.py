"""Environment-backed settings with workspace-local defaults."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_XLSX = (
    PROJECT_ROOT
    / "比赛数据"
    / "22-多模态技术与数据治理赛道-江苏农商联合银行-基于大模型与NL2SQL的银行业智能问数系统构建与应用"
    / "基于大模型与NL2SQL的银行业智能问数系统构建与应用_数据集.xlsx"
)


@dataclass(frozen=True, slots=True)
class Settings:
    xlsx_path: Path
    db_path: Path
    default_row_limit: int = 200
    hard_row_limit: int = 1000
    llm_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str = "qwen-plus"
    llm_timeout_seconds: float = 20.0
    llm_retries: int = 1

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> "Settings":
        load_dotenv(env_file or PROJECT_ROOT / ".env", override=True)
        xlsx_path = Path(os.getenv("TEXT2SQL_XLSX_PATH", str(DEFAULT_XLSX)))
        db_path = Path(
            os.getenv("TEXT2SQL_DB_PATH", str(PROJECT_ROOT / "data" / "bank_metrics.duckdb"))
        )
        return cls(
            xlsx_path=xlsx_path,
            db_path=db_path,
            default_row_limit=int(os.getenv("TEXT2SQL_DEFAULT_ROW_LIMIT", "200")),
            hard_row_limit=int(os.getenv("TEXT2SQL_HARD_ROW_LIMIT", "1000")),
            llm_url=os.getenv("TEXT2SQL_LLM_URL"),
            llm_api_key=os.getenv("TEXT2SQL_LLM_API_KEY"),
            llm_model=os.getenv("TEXT2SQL_LLM_MODEL", "qwen-plus"),
            llm_timeout_seconds=float(os.getenv("TEXT2SQL_LLM_TIMEOUT", "20")),
            llm_retries=int(os.getenv("TEXT2SQL_LLM_RETRIES", "1")),
        )
