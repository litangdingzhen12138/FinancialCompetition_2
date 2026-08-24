"""Replaceable data-source boundary with the current DuckDB implementation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from .config import Settings
from .data_builder import ensure_database
from .executor import DuckDBExecutor
from .models import QueryResult
from .semantic_catalog import SemanticCatalog


@runtime_checkable
class DataSourceAdapter(Protocol):
    """Logical-schema contract required by the Text2SQL core."""

    db_path: Path
    catalog: SemanticCatalog

    def preflight(self, sql: str) -> None: ...

    def execute(self, sql: str) -> QueryResult: ...


@dataclass(slots=True)
class DuckDBDataSourceAdapter:
    """Existing competition data path, exposed through the adapter boundary."""

    db_path: Path
    catalog: SemanticCatalog
    executor: DuckDBExecutor

    @classmethod
    def from_settings(cls, settings: Settings) -> "DuckDBDataSourceAdapter":
        db_path = ensure_database(settings.xlsx_path, settings.db_path)
        return cls(
            db_path=db_path,
            catalog=SemanticCatalog(db_path),
            executor=DuckDBExecutor(db_path, settings.hard_row_limit),
        )

    def preflight(self, sql: str) -> None:
        self.executor.preflight(sql)

    def execute(self, sql: str) -> QueryResult:
        return self.executor.execute(sql)
