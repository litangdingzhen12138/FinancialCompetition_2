"""Read-only DuckDB preflight and bounded execution."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
import re

import duckdb

from .errors import QueryExecutionError
from .models import QueryResult


SAFE_IDENTIFIER = re.compile(r'"([A-Za-z_][A-Za-z0-9_]{0,63})"')


def _json_safe(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)[:20_000]


def _feedback(exc: Exception) -> str | None:
    if type(exc).__name__ not in {"BinderException", "CatalogException", "ConversionException"}:
        return None
    identifiers: list[str] = []
    for identifier in SAFE_IDENTIFIER.findall(str(exc)[:4000]):
        if identifier not in identifiers:
            identifiers.append(identifier)
        if len(identifiers) == 8:
            break
    detail = f"；相关标识符：{', '.join(identifiers)}" if identifiers else ""
    return (
        f"DuckDB {type(exc).__name__}{detail}；请仅使用Schema中的表和列，检查CTE、别名、类型与作用域。"
    )[:500]


class DuckDBExecutor:
    def __init__(self, db_path: Path, hard_limit: int = 1000) -> None:
        self.db_path = db_path
        self.hard_limit = hard_limit

    def preflight(self, sql: str) -> None:
        connection = duckdb.connect(str(self.db_path), read_only=True)
        try:
            connection.execute("EXPLAIN " + sql).fetchall()
        except Exception as exc:
            raise QueryExecutionError(
                f"SQL预检失败：{type(exc).__name__}", retry_feedback=_feedback(exc)
            ) from exc
        finally:
            connection.close()

    def execute(self, sql: str) -> QueryResult:
        connection = duckdb.connect(str(self.db_path), read_only=True)
        try:
            connection.execute("SET memory_limit='1GB'")
            connection.execute("SET threads=4")
            cursor = connection.execute(sql)
            if cursor.description is None:
                raise QueryExecutionError("查询没有返回结果集")
            columns = tuple(str(item[0]) for item in cursor.description)
            raw_rows = cursor.fetchmany(self.hard_limit + 1)
            truncated = len(raw_rows) > self.hard_limit
            rows = tuple(
                tuple(_json_safe(value) for value in row)
                for row in raw_rows[: self.hard_limit]
            )
            return QueryResult(columns, rows, truncated)
        except QueryExecutionError:
            raise
        except Exception as exc:
            raise QueryExecutionError(
                f"查询执行失败：{type(exc).__name__}", retry_feedback=_feedback(exc)
            ) from exc
        finally:
            connection.close()

