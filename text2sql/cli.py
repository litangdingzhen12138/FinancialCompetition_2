"""Command-line entry point for database building and interactive queries."""

from __future__ import annotations

import argparse
import json

from .config import Settings
from .data_builder import ensure_database
from .service import Text2SQLService


def main() -> None:
    parser = argparse.ArgumentParser(description="Bank Text2SQL CLI")
    parser.add_argument("question", nargs="?", help="自然语言问题")
    parser.add_argument("--session-id", default="cli", help="多轮对话session ID")
    parser.add_argument("--build-db", action="store_true", help="强制从Excel重建DuckDB")
    parser.add_argument("--json", action="store_true", help="输出完整JSON")
    args = parser.parse_args()
    settings = Settings.from_env()
    if args.build_db:
        path = ensure_database(settings.xlsx_path, settings.db_path, force=True)
        print(f"数据库已构建：{path}")
        if not args.question:
            return
    if not args.question:
        parser.error("请提供问题或使用--build-db")
    response = Text2SQLService(settings).ask(args.question, args.session_id)
    print(json.dumps(response.to_dict(), ensure_ascii=False, indent=2) if args.json else response.answer)


if __name__ == "__main__":
    main()
