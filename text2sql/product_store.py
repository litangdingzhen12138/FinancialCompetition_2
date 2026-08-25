"""SQLite persistence for query history, sharing and audit records."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import secrets
import sqlite3
from typing import Any


AUDIT_GENESIS_HASH = "0" * 64
ALERT_STATUSES = {"open", "acknowledged", "resolved"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _iso(value: str | datetime | None) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return value or _utc_now()


def _audit_event_hash(
    *,
    event_id: str,
    user_id: str,
    action: str,
    target: str | None,
    query_id: str | None,
    risk_level: str,
    ip_address: str | None,
    details_json: str,
    created_at: str,
    previous_hash: str,
) -> str:
    payload = {
        "event_id": event_id,
        "user_id": user_id,
        "action": action,
        "target": target,
        "query_id": query_id,
        "risk_level": risk_level,
        "ip_address": ip_address,
        "details": json.loads(details_json),
        "created_at": created_at,
        "previous_hash": previous_hash,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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
            existing_tables = {
                str(row["name"])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            audit_columns_before = (
                {
                    str(row["name"])
                    for row in connection.execute("PRAGMA table_info(audit_events)")
                }
                if "audit_events" in existing_tables
                else set()
            )
            legacy_audit_without_hashes = bool(audit_columns_before) and not (
                {"previous_hash", "event_hash"} & audit_columns_before
            )
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
                    target TEXT,
                    risk_level TEXT NOT NULL,
                    ip_address TEXT,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS audit_created_idx
                    ON audit_events(created_at DESC);
                CREATE INDEX IF NOT EXISTS audit_risk_created_idx
                    ON audit_events(risk_level, created_at DESC);
                CREATE INDEX IF NOT EXISTS audit_user_action_created_idx
                    ON audit_events(user_id, action, created_at DESC);

                CREATE TABLE IF NOT EXISTS audit_chain_state (
                    singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1),
                    event_count INTEGER NOT NULL,
                    last_event_hash TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS security_alerts (
                    alert_id TEXT PRIMARY KEY,
                    source_event_id TEXT NOT NULL,
                    rule_code TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    query_id TEXT,
                    severity TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(source_event_id, rule_code),
                    FOREIGN KEY(source_event_id) REFERENCES audit_events(event_id)
                );
                CREATE INDEX IF NOT EXISTS security_alert_status_created_idx
                    ON security_alerts(status, created_at DESC);
                CREATE INDEX IF NOT EXISTS security_alert_user_rule_created_idx
                    ON security_alerts(user_id, rule_code, created_at DESC);

                CREATE TABLE IF NOT EXISTS user_freezes (
                    user_id TEXT PRIMARY KEY,
                    frozen_until TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    alert_id TEXT,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(alert_id) REFERENCES security_alerts(alert_id)
                );

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
            audit_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(audit_events)")
            }
            for column in ("target", "ip_address", "previous_hash", "event_hash"):
                if column not in audit_columns:
                    connection.execute(f"ALTER TABLE audit_events ADD COLUMN {column} TEXT")
            if legacy_audit_without_hashes:
                self._backfill_audit_chain(connection)
            chain_state = connection.execute(
                "SELECT 1 FROM audit_chain_state WHERE singleton_id = 1"
            ).fetchone()
            if (
                chain_state is None
                and "audit_events" in existing_tables
                and not legacy_audit_without_hashes
            ):
                raise RuntimeError("审计链锚点缺失，拒绝自动重建")
            if chain_state is None:
                self._sync_audit_chain_state(connection)
            connection.execute("PRAGMA optimize")

    @staticmethod
    def _backfill_audit_chain(connection: sqlite3.Connection) -> None:
        previous_hash = AUDIT_GENESIS_HASH
        rows = connection.execute(
            "SELECT rowid, * FROM audit_events ORDER BY rowid ASC"
        ).fetchall()
        for row in rows:
            event_hash = _audit_event_hash(
                event_id=str(row["event_id"]),
                user_id=str(row["user_id"]),
                action=str(row["action"]),
                target=row["target"],
                query_id=row["query_id"],
                risk_level=str(row["risk_level"]),
                ip_address=row["ip_address"],
                details_json=str(row["details_json"]),
                created_at=str(row["created_at"]),
                previous_hash=previous_hash,
            )
            connection.execute(
                "UPDATE audit_events SET previous_hash = ?, event_hash = ? "
                "WHERE rowid = ?",
                (previous_hash, event_hash, row["rowid"]),
            )
            previous_hash = event_hash

    @staticmethod
    def _sync_audit_chain_state(connection: sqlite3.Connection) -> None:
        summary = connection.execute(
            "SELECT COUNT(*) AS event_count FROM audit_events"
        ).fetchone()
        latest = connection.execute(
            "SELECT event_hash FROM audit_events ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        last_event_hash = (
            str(latest["event_hash"])
            if latest and latest["event_hash"]
            else AUDIT_GENESIS_HASH
        )
        connection.execute(
            """
            INSERT INTO audit_chain_state (
                singleton_id, event_count, last_event_hash, updated_at
            ) VALUES (1, ?, ?, ?)
            ON CONFLICT(singleton_id) DO UPDATE SET
                event_count = excluded.event_count,
                last_event_hash = excluded.last_event_hash,
                updated_at = excluded.updated_at
            """,
            (int(summary["event_count"]), last_event_hash, _utc_now()),
        )

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
        target: str | None = None,
        ip_address: str | None = None,
        created_at: str | datetime | None = None,
    ) -> str:
        details_json = _json(details)
        timestamp = _iso(created_at)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            summary = connection.execute(
                "SELECT COUNT(*) AS event_count FROM audit_events"
            ).fetchone()
            previous = connection.execute(
                "SELECT event_hash FROM audit_events ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            previous_hash = (
                str(previous["event_hash"])
                if previous and previous["event_hash"]
                else AUDIT_GENESIS_HASH
            )
            chain_state = connection.execute(
                "SELECT event_count, last_event_hash FROM audit_chain_state "
                "WHERE singleton_id = 1"
            ).fetchone()
            if (
                chain_state is None
                or int(chain_state["event_count"]) != int(summary["event_count"])
                or str(chain_state["last_event_hash"]) != previous_hash
            ):
                raise RuntimeError("审计链状态异常，拒绝追加审计日志")
            event_hash = _audit_event_hash(
                event_id=event_id,
                user_id=user_id,
                action=action,
                target=target,
                query_id=query_id,
                risk_level=risk_level,
                ip_address=ip_address,
                details_json=details_json,
                created_at=timestamp,
                previous_hash=previous_hash,
            )
            connection.execute(
                """
                INSERT INTO audit_events (
                    event_id, query_id, user_id, action, target, risk_level,
                    ip_address, details_json, created_at, previous_hash, event_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    query_id,
                    user_id,
                    action,
                    target,
                    risk_level,
                    ip_address,
                    details_json,
                    timestamp,
                    previous_hash,
                    event_hash,
                ),
            )
            connection.execute(
                """
                UPDATE audit_chain_state
                SET event_count = ?, last_event_hash = ?, updated_at = ?
                WHERE singleton_id = 1
                """,
                (int(summary["event_count"]) + 1, event_hash, timestamp),
            )
        return event_id

    def verify_audit_chain(self) -> bool:
        previous_hash = AUDIT_GENESIS_HASH
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT rowid, * FROM audit_events ORDER BY rowid ASC"
            ).fetchall()
            chain_state = connection.execute(
                "SELECT event_count, last_event_hash FROM audit_chain_state "
                "WHERE singleton_id = 1"
            ).fetchone()
        if chain_state is None or int(chain_state["event_count"]) != len(rows):
            return False
        for row in rows:
            if row["previous_hash"] != previous_hash:
                return False
            try:
                expected_hash = _audit_event_hash(
                    event_id=str(row["event_id"]),
                    user_id=str(row["user_id"]),
                    action=str(row["action"]),
                    target=row["target"],
                    query_id=row["query_id"],
                    risk_level=str(row["risk_level"]),
                    ip_address=row["ip_address"],
                    details_json=str(row["details_json"]),
                    created_at=str(row["created_at"]),
                    previous_hash=previous_hash,
                )
            except (TypeError, ValueError):
                return False
            if row["event_hash"] != expected_hash:
                return False
            previous_hash = expected_hash
        return str(chain_state["last_event_hash"]) == previous_hash

    def count_audit_events(
        self,
        *,
        user_id: str,
        action: str,
        since: str | datetime,
        data_level: str | None = None,
    ) -> int:
        clauses = ["user_id = ?", "action = ?", "created_at >= ?"]
        parameters: list[object] = [user_id, action, _iso(since)]
        if data_level is not None:
            clauses.append("json_extract(details_json, '$.data_level') = ?")
            parameters.append(data_level)
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT COUNT(*) FROM audit_events WHERE {' AND '.join(clauses)}",
                parameters,
            ).fetchone()
        return int(row[0])

    def sum_exported_rows(
        self,
        *,
        user_id: str,
        since: str | datetime,
    ) -> int:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT COALESCE(
                    SUM(CAST(json_extract(details_json, '$.exported_rows') AS INTEGER)),
                    0
                )
                FROM audit_events
                WHERE user_id = ?
                  AND action IN ('query.exported', 'history.batch_exported')
                  AND created_at >= ?
                """,
                (user_id, _iso(since)),
            ).fetchone()
        return int(row[0])

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

    def add_alert(
        self,
        *,
        alert_id: str,
        source_event_id: str,
        rule_code: str,
        user_id: str,
        severity: str,
        details: dict[str, Any],
        query_id: str | None = None,
        status: str = "open",
        created_at: str | datetime | None = None,
        frozen_until: str | datetime | None = None,
        freeze_reason: str | None = None,
    ) -> str:
        normalized_status = status.lower()
        if normalized_status not in ALERT_STATUSES:
            raise ValueError("不支持的告警状态")
        normalized_severity = severity.lower()
        if normalized_severity not in {"low", "medium", "high"}:
            raise ValueError("不支持的告警级别")
        if (frozen_until is None) != (freeze_reason is None):
            raise ValueError("冻结截止时间和冻结原因必须同时提供")
        timestamp = _iso(created_at)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO security_alerts (
                    alert_id, source_event_id, rule_code, user_id, query_id,
                    severity, status, details_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    alert_id,
                    source_event_id,
                    rule_code,
                    user_id,
                    query_id,
                    normalized_severity,
                    normalized_status,
                    _json(details),
                    timestamp,
                    timestamp,
                ),
            )
            if frozen_until is not None and freeze_reason is not None:
                self._upsert_freeze(
                    connection,
                    user_id=user_id,
                    frozen_until=_iso(frozen_until),
                    reason=freeze_reason,
                    alert_id=alert_id,
                    updated_at=timestamp,
                )
        return alert_id

    def list_alerts(
        self,
        *,
        status: str = "all",
        severity: str = "all",
        user_id: str | None = None,
    ) -> list[dict[str, Any]]:
        normalized_status = status.lower()
        if normalized_status != "all" and normalized_status not in ALERT_STATUSES:
            raise ValueError("不支持的告警状态")
        normalized_severity = severity.lower()
        if normalized_severity not in {"all", "low", "medium", "high"}:
            raise ValueError("不支持的告警级别")

        clauses: list[str] = []
        parameters: list[object] = []
        if normalized_status != "all":
            clauses.append("status = ?")
            parameters.append(normalized_status)
        if normalized_severity != "all":
            clauses.append("severity = ?")
            parameters.append(normalized_severity)
        if user_id:
            clauses.append("user_id = ?")
            parameters.append(user_id)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""

        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM security_alerts
                {where}
                ORDER BY created_at DESC, alert_id DESC
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

    def update_alert_status(
        self,
        alert_id: str,
        status: str,
        *,
        updated_at: str | datetime | None = None,
    ) -> bool:
        normalized_status = status.lower()
        if normalized_status not in ALERT_STATUSES:
            raise ValueError("不支持的告警状态")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE security_alerts
                SET status = ?, updated_at = ?
                WHERE alert_id = ?
                """,
                (normalized_status, _iso(updated_at), alert_id),
            )
        return cursor.rowcount == 1

    def has_recent_alert(
        self,
        *,
        user_id: str,
        rule_code: str,
        since: str | datetime,
    ) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM security_alerts
                WHERE user_id = ? AND rule_code = ? AND created_at >= ?
                LIMIT 1
                """,
                (user_id, rule_code, _iso(since)),
            ).fetchone()
        return row is not None

    def freeze_user(
        self,
        *,
        user_id: str,
        frozen_until: str | datetime,
        reason: str,
        alert_id: str | None = None,
        updated_at: str | datetime | None = None,
    ) -> None:
        until = _iso(frozen_until)
        timestamp = _iso(updated_at)
        with self._connect() as connection:
            self._upsert_freeze(
                connection,
                user_id=user_id,
                frozen_until=until,
                reason=reason,
                alert_id=alert_id,
                updated_at=timestamp,
            )

    @staticmethod
    def _upsert_freeze(
        connection: sqlite3.Connection,
        *,
        user_id: str,
        frozen_until: str,
        reason: str,
        alert_id: str | None,
        updated_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO user_freezes (
                user_id, frozen_until, reason, alert_id, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                frozen_until = CASE
                    WHEN excluded.frozen_until > user_freezes.frozen_until
                    THEN excluded.frozen_until ELSE user_freezes.frozen_until
                END,
                reason = CASE
                    WHEN excluded.frozen_until > user_freezes.frozen_until
                    THEN excluded.reason ELSE user_freezes.reason
                END,
                alert_id = CASE
                    WHEN excluded.frozen_until > user_freezes.frozen_until
                    THEN excluded.alert_id ELSE user_freezes.alert_id
                END,
                updated_at = CASE
                    WHEN excluded.frozen_until > user_freezes.frozen_until
                    THEN excluded.updated_at ELSE user_freezes.updated_at
                END
            """,
            (user_id, frozen_until, reason, alert_id, updated_at),
        )

    def get_active_freeze(
        self,
        user_id: str,
        *,
        now: str | datetime | None = None,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM user_freezes
                WHERE user_id = ? AND frozen_until > ?
                """,
                (user_id, _iso(now)),
            ).fetchone()
        return dict(row) if row else None

    def list_active_freezes(
        self,
        *,
        now: str | datetime | None = None,
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM user_freezes
                WHERE frozen_until > ?
                ORDER BY frozen_until DESC, user_id ASC
                """,
                (_iso(now),),
            ).fetchall()
        return [dict(row) for row in rows]

    def unfreeze_user(
        self,
        user_id: str,
        *,
        now: str | datetime | None = None,
    ) -> dict[str, Any] | None:
        timestamp = _iso(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM user_freezes
                WHERE user_id = ? AND frozen_until > ?
                """,
                (user_id, timestamp),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "DELETE FROM user_freezes WHERE user_id = ?",
                (user_id,),
            )
        return dict(row)

    def list_audit_users(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                WITH audit_summary AS (
                    SELECT
                        user_id,
                        COUNT(*) AS operation_count,
                        SUM(
                            CASE WHEN risk_level IN ('medium', 'high')
                                 THEN 1 ELSE 0 END
                        ) AS audit_risk_count,
                        MAX(created_at) AS last_active_at
                    FROM audit_events
                    GROUP BY user_id
                ),
                alert_summary AS (
                    SELECT user_id, COUNT(*) AS alert_count
                    FROM security_alerts
                    GROUP BY user_id
                )
                SELECT
                    a.user_id,
                    a.operation_count,
                    a.audit_risk_count + COALESCE(s.alert_count, 0)
                        AS risk_event_count,
                    a.last_active_at
                FROM audit_summary AS a
                LEFT JOIN alert_summary AS s ON s.user_id = a.user_id
                ORDER BY risk_event_count DESC, operation_count DESC, a.user_id ASC
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
                    "SELECT "
                    "(SELECT COUNT(*) FROM audit_events "
                    " WHERE risk_level IN ('medium', 'high')) + "
                    "(SELECT COUNT(*) FROM security_alerts)"
                ).fetchone()[0]
            )
            risk_users = int(
                connection.execute(
                    "SELECT COUNT(*) FROM ("
                    " SELECT user_id FROM audit_events "
                    " WHERE risk_level IN ('medium', 'high') "
                    " UNION SELECT user_id FROM security_alerts"
                    ")"
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
