"""SQLite persistence for query history, sharing and audit records."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import secrets
import sqlite3
from typing import Any


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


class ProductStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS query_history (
                    query_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    question TEXT NOT NULL,
                    route TEXT NOT NULL,
                    status TEXT NOT NULL,
                    sql_text TEXT,
                    plan_json TEXT NOT NULL,
                    columns_json TEXT NOT NULL,
                    rows_json TEXT NOT NULL,
                    answer TEXT NOT NULL,
                    answer_mode TEXT NOT NULL DEFAULT 'rule',
                    answer_status TEXT NOT NULL DEFAULT 'completed',
                    visualization_json TEXT NOT NULL,
                    insight_json TEXT NOT NULL,
                    warnings_json TEXT NOT NULL,
                    truncated INTEGER NOT NULL DEFAULT 0,
                    duration_ms INTEGER NOT NULL DEFAULT 0,
                    source_query_id TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS query_history_user_created_idx
                    ON query_history(user_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS query_history_session_created_idx
                    ON query_history(session_id, created_at ASC);

                CREATE TABLE IF NOT EXISTS audit_events (
                    event_id TEXT PRIMARY KEY,
                    query_id TEXT,
                    user_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    risk_level TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS audit_created_idx
                    ON audit_events(created_at DESC);
                CREATE INDEX IF NOT EXISTS audit_risk_created_idx
                    ON audit_events(risk_level, created_at DESC);

                CREATE TABLE IF NOT EXISTS shared_queries (
                    share_id TEXT PRIMARY KEY,
                    token_hash TEXT NOT NULL UNIQUE,
                    query_id TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    revoked INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(query_id) REFERENCES query_history(query_id)
                );
                CREATE TABLE IF NOT EXISTS pending_sessions (
                    user_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    pending_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, session_id)
                );
                CREATE TABLE IF NOT EXISTS session_metadata (
                    user_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, session_id)
                );
                """
            )
            existing_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(query_history)")
            }
            if "answer_mode" not in existing_columns:
                connection.execute(
                    "ALTER TABLE query_history "
                    "ADD COLUMN answer_mode TEXT NOT NULL DEFAULT 'rule'"
                )
            if "answer_status" not in existing_columns:
                connection.execute(
                    "ALTER TABLE query_history "
                    "ADD COLUMN answer_status TEXT NOT NULL DEFAULT 'completed'"
                )
            connection.execute("PRAGMA optimize")

    def save_query(self, record: dict[str, Any]) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO query_history (
                    query_id, user_id, session_id, question, route, status,
                    sql_text, plan_json, columns_json, rows_json, answer,
                    answer_mode, answer_status, visualization_json, insight_json,
                    warnings_json, truncated, duration_ms, source_query_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["query_id"],
                    record["user_id"],
                    record["session_id"],
                    record["question"],
                    record["route"],
                    record["status"],
                    record.get("sql"),
                    _json(record["plan"]),
                    _json(record["columns"]),
                    _json(record["rows"]),
                    record["answer"],
                    record["answer_mode"],
                    record["answer_status"],
                    _json(record["visualization"]),
                    _json(record["insight"]),
                    _json(record.get("warnings", [])),
                    int(bool(record.get("truncated"))),
                    int(record.get("duration_ms", 0)),
                    record.get("source_query_id"),
                    record.get("created_at") or _utc_now(),
                ),
            )

    def save_pending_session(
        self,
        user_id: str,
        session_id: str,
        pending: dict[str, Any],
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO pending_sessions (
                    user_id, session_id, pending_json, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id, session_id) DO UPDATE SET
                    pending_json = excluded.pending_json,
                    updated_at = excluded.updated_at
                """,
                (user_id, session_id, _json(pending), _utc_now()),
            )

    def get_pending_session(
        self,
        user_id: str,
        session_id: str,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT pending_json FROM pending_sessions
                WHERE user_id = ? AND session_id = ?
                """,
                (user_id, session_id),
            ).fetchone()
        return json.loads(row["pending_json"]) if row else None

    def clear_pending_session(self, user_id: str, session_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                DELETE FROM pending_sessions
                WHERE user_id = ? AND session_id = ?
                """,
                (user_id, session_id),
            )

    def update_final_answer(
        self,
        query_id: str,
        *,
        answer: str,
        insight: dict[str, Any] | None,
        status: str,
        warning: str | None = None,
    ) -> None:
        with self._connect() as connection:
            warnings_json: str | None = None
            if warning:
                row = connection.execute(
                    "SELECT warnings_json FROM query_history WHERE query_id = ?",
                    (query_id,),
                ).fetchone()
                if row is None:
                    raise ValueError("查询记录不存在")
                warnings = json.loads(row["warnings_json"])
                if warning not in warnings:
                    warnings.append(warning)
                warnings_json = _json(warnings)
            cursor = connection.execute(
                """
                UPDATE query_history
                SET answer = ?, insight_json = ?, answer_status = ?,
                    warnings_json = COALESCE(?, warnings_json)
                WHERE query_id = ?
                """,
                (answer, _json(insight), status, warnings_json, query_id),
            )
        if cursor.rowcount != 1:
            raise ValueError("查询记录不存在")

    def get_query(self, query_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM query_history WHERE query_id = ?",
                (query_id,),
            ).fetchone()
        return self._decode_query(row) if row else None

    def list_queries(
        self,
        user_id: str | None,
        *,
        limit: int | None = 50,
        keyword: str | None = None,
        session_id: str | None = None,
        oldest_first: bool = False,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[object] = []
        if user_id:
            clauses.append("user_id = ?")
            parameters.append(user_id)
        if keyword:
            clauses.append("question LIKE ?")
            parameters.append(f"%{keyword}%")
        if session_id:
            clauses.append("session_id = ?")
            parameters.append(session_id)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        order = "ASC" if oldest_first else "DESC"
        limit_clause = ""
        if limit is not None:
            parameters.append(max(1, min(limit, 5000)))
            limit_clause = "LIMIT ?"
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM query_history
                {where}
                ORDER BY created_at {order}
                {limit_clause}
                """,
                parameters,
            ).fetchall()
        return [self._decode_query(row) for row in rows]

    def set_session_title(self, user_id: str, session_id: str, title: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO session_metadata (user_id, session_id, title, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id, session_id) DO UPDATE SET
                    title = excluded.title,
                    updated_at = excluded.updated_at
                """,
                (user_id, session_id, title, _utc_now()),
            )

    def list_session_titles(self, user_id: str | None) -> dict[tuple[str, str], str]:
        parameters: tuple[object, ...] = ()
        where = ""
        if user_id:
            where = "WHERE user_id = ?"
            parameters = (user_id,)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT user_id, session_id, title FROM session_metadata {where}",
                parameters,
            ).fetchall()
        return {
            (str(row["user_id"]), str(row["session_id"])): str(row["title"])
            for row in rows
        }

    def delete_session_queries(self, user_id: str, session_id: str) -> int:
        with self._connect() as connection:
            query_ids = [
                str(row["query_id"])
                for row in connection.execute(
                    "SELECT query_id FROM query_history "
                    "WHERE user_id = ? AND session_id = ?",
                    (user_id, session_id),
                ).fetchall()
            ]
            if query_ids:
                placeholders = ",".join("?" for _ in query_ids)
                connection.execute(
                    f"DELETE FROM shared_queries WHERE query_id IN ({placeholders})",
                    query_ids,
                )
            cursor = connection.execute(
                "DELETE FROM query_history WHERE user_id = ? AND session_id = ?",
                (user_id, session_id),
            )
            connection.execute(
                "DELETE FROM session_metadata WHERE user_id = ? AND session_id = ?",
                (user_id, session_id),
            )
        return cursor.rowcount

    def add_audit(
        self,
        *,
        event_id: str,
        user_id: str,
        action: str,
        risk_level: str,
        details: dict[str, Any],
        query_id: str | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO audit_events (
                    event_id, query_id, user_id, action, risk_level,
                    details_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    query_id,
                    user_id,
                    action,
                    risk_level,
                    _json(details),
                    _utc_now(),
                ),
            )

    def list_audit(
        self,
        *,
        sort_by: str = "newest",
        risk_level: str = "all",
        user_id: str | None = None,
    ) -> list[dict[str, Any]]:
        order_by = {
            "newest": "created_at DESC",
            "risk_desc": (
                "CASE risk_level WHEN 'high' THEN 3 WHEN 'medium' THEN 2 "
                "ELSE 1 END DESC, created_at DESC"
            ),
            "risk_asc": (
                "CASE risk_level WHEN 'high' THEN 3 WHEN 'medium' THEN 2 "
                "ELSE 1 END ASC, created_at DESC"
            ),
        }.get(sort_by)
        if order_by is None:
            raise ValueError("不支持的审计排序方式")

        clauses: list[str] = []
        parameters: list[object] = []
        if risk_level == "elevated":
            clauses.append("risk_level IN ('medium', 'high')")
        elif risk_level in {"low", "medium", "high"}:
            clauses.append("risk_level = ?")
            parameters.append(risk_level)
        elif risk_level != "all":
            raise ValueError("不支持的风险筛选条件")
        if user_id:
            clauses.append("user_id = ?")
            parameters.append(user_id)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""

        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM audit_events
                {where}
                ORDER BY {order_by}
                """,
                parameters,
            ).fetchall()
        return [
            {
                **dict(row),
                "details": json.loads(row["details_json"]),
            }
            for row in rows
        ]

    def list_audit_users(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    user_id,
                    COUNT(*) AS operation_count,
                    SUM(
                        CASE WHEN risk_level IN ('medium', 'high') THEN 1 ELSE 0 END
                    ) AS risk_event_count,
                    MAX(created_at) AS last_active_at
                FROM audit_events
                GROUP BY user_id
                ORDER BY risk_event_count DESC, operation_count DESC, user_id ASC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def create_share(
        self,
        *,
        share_id: str,
        query_id: str,
        user_id: str,
        expires_at: str,
    ) -> str:
        token = secrets.token_urlsafe(24)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO shared_queries (
                    share_id, token_hash, query_id, created_by, expires_at,
                    revoked, created_at
                ) VALUES (?, ?, ?, ?, ?, 0, ?)
                """,
                (share_id, token_hash, query_id, user_id, expires_at, _utc_now()),
            )
        return token

    def resolve_share(self, token: str) -> dict[str, Any] | None:
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM shared_queries
                WHERE token_hash = ? AND revoked = 0 AND expires_at > ?
                """,
                (token_hash, _utc_now()),
            ).fetchone()
        return dict(row) if row else None

    def revoke_share(self, share_id: str, user_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE shared_queries SET revoked = 1
                WHERE share_id = ? AND created_by = ?
                """,
                (share_id, user_id),
            )
        return cursor.rowcount > 0

    def overview(self) -> dict[str, int]:
        with self._connect() as connection:
            queries = int(connection.execute("SELECT COUNT(*) FROM query_history").fetchone()[0])
            users = int(
                connection.execute("SELECT COUNT(DISTINCT user_id) FROM query_history").fetchone()[0]
            )
            warnings = int(
                connection.execute(
                    "SELECT COUNT(*) FROM audit_events WHERE risk_level IN ('medium', 'high')"
                ).fetchone()[0]
            )
            risk_users = int(
                connection.execute(
                    "SELECT COUNT(DISTINCT user_id) FROM audit_events "
                    "WHERE risk_level IN ('medium', 'high')"
                ).fetchone()[0]
            )
            shares = int(
                connection.execute(
                    "SELECT COUNT(*) FROM shared_queries WHERE revoked = 0 AND expires_at > ?",
                    (_utc_now(),),
                ).fetchone()[0]
            )
        return {
            "query_count": queries,
            "user_count": users,
            "risk_user_count": risk_users,
            "risk_event_count": warnings,
            "active_share_count": shares,
        }

    @staticmethod
    def _decode_query(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        for source, target in (
            ("plan_json", "plan"),
            ("columns_json", "columns"),
            ("rows_json", "rows"),
            ("visualization_json", "visualization"),
            ("insight_json", "insight"),
            ("warnings_json", "warnings"),
        ):
            result[target] = json.loads(result.pop(source))
        result["sql"] = result.pop("sql_text")
        result["truncated"] = bool(result["truncated"])
        return result
