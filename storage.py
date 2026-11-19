"""SQLite storage for the plugin's message archive and reports."""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

_SECONDS_PER_DAY = 86400
_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS archive_settings (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        retention_days INTEGER NOT NULL,
        cutoff_at INTEGER NOT NULL,
        provider_id TEXT NOT NULL DEFAULT '',
        prompt TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS archive_groups (
        key TEXT PRIMARY KEY,
        label TEXT NOT NULL,
        started_at INTEGER NOT NULL
    ) WITHOUT ROWID""",
    """CREATE TABLE IF NOT EXISTS collection_windows (
        id INTEGER PRIMARY KEY,
        group_key TEXT NOT NULL REFERENCES archive_groups(key),
        started_at INTEGER NOT NULL,
        heartbeat_at INTEGER NOT NULL,
        ended_at INTEGER
    )""",
    """CREATE INDEX IF NOT EXISTS collection_windows_group_start
        ON collection_windows(group_key, started_at)""",
    """CREATE UNIQUE INDEX IF NOT EXISTS collection_windows_open_group
        ON collection_windows(group_key) WHERE ended_at IS NULL""",
    """CREATE TABLE IF NOT EXISTS archive_messages (
        group_key TEXT NOT NULL REFERENCES archive_groups(key),
        message_id TEXT NOT NULL,
        at INTEGER NOT NULL,
        sender TEXT NOT NULL,
        text TEXT NOT NULL,
        reply_to TEXT,
        PRIMARY KEY (group_key, message_id)
    ) WITHOUT ROWID""",
    """CREATE INDEX IF NOT EXISTS archive_messages_group_time
        ON archive_messages(group_key, at)""",
    """CREATE INDEX IF NOT EXISTS archive_messages_time ON archive_messages(at)""",
    """CREATE TABLE IF NOT EXISTS reports (
        id TEXT PRIMARY KEY NOT NULL,
        status TEXT NOT NULL,
        start_at INTEGER NOT NULL,
        end_at INTEGER NOT NULL,
        created_at INTEGER NOT NULL,
        message_count INTEGER NOT NULL,
        groups_json TEXT NOT NULL,
        provider_id TEXT NOT NULL,
        provider_label TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT 'plugin',
        prompt TEXT NOT NULL,
        body TEXT,
        error TEXT,
        coverage_json TEXT NOT NULL,
        CHECK (
            (status = 'running' AND body IS NULL AND error IS NULL) OR
            (status = 'completed' AND body IS NOT NULL AND length(body) > 0 AND error IS NULL) OR
            (status = 'failed' AND body IS NULL AND error IS NOT NULL)
        )
    )""",
    """CREATE UNIQUE INDEX IF NOT EXISTS reports_one_running
        ON reports(status) WHERE status = 'running'""",
)
_REPORT_COLUMNS = """id, status, start_at, end_at, created_at, message_count,
                     groups_json, provider_id, provider_label, source"""


def _iso(value: int | None) -> str | None:
    return None if value is None else datetime.fromtimestamp(value, UTC).isoformat()


def _report_dict(row: sqlite3.Row, *, detail: bool) -> dict:
    result = {
        "id": row["id"],
        "status": row["status"],
        "start": _iso(row["start_at"]),
        "end": _iso(row["end_at"]),
        "created_at": _iso(row["created_at"]),
        "count": row["message_count"],
        "groups": json.loads(row["groups_json"]),
        "provider_id": row["provider_id"],
        "provider_label": row["provider_label"],
        "source": row["source"],
    }
    if detail:
        result.update(
            prompt=row["prompt"],
            body=row["body"],
            error=row["error"],
            coverage=json.loads(row["coverage_json"]),
        )
    return result


class Store:
    def __init__(self, path: Path) -> None:
        self._path = path

    @contextmanager
    def _transaction(
        self, *, write: bool = False, initialize: bool = False
    ) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self._path, timeout=15, isolation_level=None)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys = ON")
            if initialize:
                db.execute("PRAGMA journal_mode = WAL")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db

    def open(self, groups: dict[str, str], now: int, retention_days: int) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction(write=True, initialize=True) as db:
            for statement in _SCHEMA:
                db.execute(statement)
            if "source" not in {row["name"] for row in db.execute("PRAGMA table_info(reports)")}:
                db.execute("ALTER TABLE reports ADD COLUMN source TEXT NOT NULL DEFAULT 'plugin'")
            db.execute(
                """INSERT INTO archive_settings(id, retention_days, cutoff_at)
                   VALUES (1, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET retention_days = excluded.retention_days""",
                (retention_days, now - retention_days * _SECONDS_PER_DAY),
            )
            db.execute(
                "UPDATE collection_windows SET ended_at = heartbeat_at WHERE ended_at IS NULL"
            )
            self._interrupt(db, "插件已重新启动，报告生成已中断。")
            db.executemany(
                """INSERT INTO archive_groups(key, label, started_at) VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                       label = excluded.label,
                       started_at = MIN(archive_groups.started_at, excluded.started_at)""",
                ((key, label, now) for key, label in groups.items()),
            )
            db.executemany(
                """INSERT INTO collection_windows(group_key, started_at, heartbeat_at)
                   VALUES (?, ?, ?)""",
                ((key, now, now) for key in groups),
            )
            self._expire(db, now)

    def heartbeat(self, now: int) -> None:
        with self._transaction(write=True) as db:
            db.execute(
                """UPDATE collection_windows SET heartbeat_at = ?
                   WHERE ended_at IS NULL AND heartbeat_at < ?""",
                (now, now),
            )
            self._expire(db, now)

    def close(self, now: int) -> None:
        with self._transaction(write=True) as db:
            db.execute(
                """UPDATE collection_windows SET ended_at = MAX(heartbeat_at, ?)
                   WHERE ended_at IS NULL""",
                (now,),
            )
            self._interrupt(db, "插件已停止，报告生成已中断。")

    @staticmethod
    def _interrupt(db: sqlite3.Connection, error: str) -> None:
        db.execute(
            "UPDATE reports SET status = 'failed', error = ? WHERE status = 'running'",
            (error,),
        )

    @staticmethod
    def _effective_cutoff(db: sqlite3.Connection, now: int) -> int:
        return db.execute(
            "SELECT MAX(cutoff_at, ? - retention_days * ?) FROM archive_settings WHERE id = 1",
            (now, _SECONDS_PER_DAY),
        ).fetchone()[0]

    def _expire(self, db: sqlite3.Connection, now: int) -> None:
        cutoff = self._effective_cutoff(db, now)
        # Widening retention cannot bring already-expired history back.
        db.execute(
            "UPDATE archive_settings SET cutoff_at = ? WHERE id = 1 AND cutoff_at < ?",
            (cutoff, cutoff),
        )
        db.execute("DELETE FROM archive_messages WHERE at < ?", (cutoff,))

    def record(
        self, key: str, message_id: str, at: int, sender: str, text: str, reply_to: str | None
    ) -> None:
        with self._transaction(write=True) as db:
            db.execute(
                """INSERT INTO archive_messages(group_key, message_id, at, sender, text, reply_to)
                   SELECT ?, ?, ?, ?, ?, ?
                   WHERE EXISTS (SELECT 1 FROM collection_windows
                                 WHERE group_key = ? AND ended_at IS NULL)
                     AND ? >= (SELECT cutoff_at FROM archive_settings WHERE id = 1)
                   ON CONFLICT(group_key, message_id) DO NOTHING""",
                (key, message_id, at, sender, text, reply_to, key, at),
            )

    def settings(self, default_prompt: str) -> dict:
        with self._transaction() as db:
            row = db.execute("SELECT provider_id, prompt FROM archive_settings WHERE id = 1").fetchone()
            return {
                "provider_id": row["provider_id"],
                "prompt": default_prompt if row["prompt"] is None else row["prompt"],
            }

    def save_settings(self, provider_id: str, prompt: str) -> None:
        with self._transaction(write=True) as db:
            db.execute(
                "UPDATE archive_settings SET provider_id = ?, prompt = ? WHERE id = 1",
                (provider_id, prompt),
            )

    def coverage(self, groups: dict[str, str], start: int, end: int, now: int) -> list[dict]:
        with self._transaction() as db:
            cutoff = self._effective_cutoff(db, now)
            return self._coverage(db, groups, start, end, now, cutoff)

    @staticmethod
    def _coverage(
        db: sqlite3.Connection, groups: dict[str, str], start: int, end: int, now: int, cutoff: int
    ) -> list[dict]:
        end = min(end, now)
        available_start = max(start, cutoff)
        result = []
        for key, label in groups.items():
            count, first_at = db.execute(
                """SELECT COUNT(*), MIN(at) FROM archive_messages
                   WHERE group_key = ? AND at >= ? AND at < ?""",
                (key, available_start, end),
            ).fetchone()
            group = db.execute(
                "SELECT started_at FROM archive_groups WHERE key = ?", (key,)
            ).fetchone()
            gaps = []
            cursor = start
            if available_start < end:
                # Union the plugin's local windows; none measure QQ delivery or connectivity.
                for opened, closed in db.execute(
                    """SELECT started_at, COALESCE(ended_at, MAX(heartbeat_at, ?))
                       FROM collection_windows
                       WHERE group_key = ? AND started_at < ?
                         AND (ended_at IS NULL OR ended_at > ?)
                       ORDER BY started_at""",
                    (now, key, end, available_start),
                ):
                    left, right = max(opened, available_start), min(closed, end)
                    if left >= right or right <= cursor:
                        continue
                    if cursor < left:
                        gaps.append({"start": _iso(cursor), "end": _iso(left)})
                    cursor = right
                    if cursor == end:
                        break
            if cursor < end:
                gaps.append({"start": _iso(cursor), "end": _iso(end)})
            result.append({
                "key": key,
                "label": label,
                "count": count,
                "partial": bool(gaps),
                "first_at": _iso(first_at),
                "started_at": _iso(group["started_at"] if group is not None else None),
                "gaps": gaps,
            })
        return result

    def create(
        self,
        groups: dict[str, str],
        start: int,
        end: int,
        now: int,
        provider_id: str,
        provider_label: str,
        prompt: str,
        *,
        source: str = "plugin",
        snapshot: tuple[list[dict], list[dict]] | None = None,
    ) -> tuple[dict, list[dict]]:
        if source not in ("plugin", "astrbot"):
            raise ValueError("不支持的报告消息来源。")
        if (source == "plugin" and snapshot is not None) or (source == "astrbot" and snapshot is None):
            raise ValueError("报告消息快照与所选来源不匹配。")
        # Reserve the writer while reading coverage and the in-memory message snapshot.
        with self._transaction(write=True) as db:
            if db.execute("SELECT 1 FROM reports WHERE status = 'running'").fetchone():
                raise ValueError("已有报告正在生成，请等待其结束。")
            if source == "plugin":
                cutoff = self._effective_cutoff(db, now)
                coverage = self._coverage(db, groups, start, end, now, cutoff)
                messages = [
                    dict(row)
                    for group in coverage
                    if group["count"]
                    for row in db.execute(
                        """SELECT group_key AS key, message_id, at, sender, text, reply_to
                           FROM archive_messages WHERE group_key = ? AND at >= ? AND at < ?""",
                        (group["key"], max(start, cutoff), min(end, now)),
                    )
                ]
            else:
                assert snapshot is not None
                coverage, messages = snapshot
                counts = dict.fromkeys(groups, 0)
                seen = set()
                for group in coverage:
                    key = group.get("key")
                    if (key not in counts or key in seen
                            or type(group.get("count")) is not int or group["count"] < 0):
                        raise ValueError("报告消息快照的群组或数量不匹配。")
                    seen.add(key)
                if seen != groups.keys():
                    raise ValueError("报告消息快照的群组不匹配。")
                for row in messages:
                    key = row.get("key")
                    at = row.get("at")
                    if (key not in counts or type(at) is not int
                            or not start <= at < min(end, now)
                            or not {"message_id", "sender", "text", "reply_to"} <= row.keys()):
                        raise ValueError("报告消息快照包含所选群组或时间范围以外的消息。")
                    counts[key] += 1
                if any(group["count"] != counts[group["key"]] for group in coverage):
                    raise ValueError("报告消息快照的消息数量不匹配。")
            if not messages:
                raise ValueError("所选时间范围内没有可用于生成报告的留存消息。")
            messages.sort(key=lambda row: (row["at"], row["key"], row["message_id"]))
            report_id = str(uuid4())
            db.execute(
                """INSERT INTO reports(
                       id, status, start_at, end_at, created_at, message_count, groups_json,
                       provider_id, provider_label, prompt, coverage_json, source
                   ) VALUES (?, 'running', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    report_id, start, end, now, len(messages),
                    json.dumps(
                        [{"key": key, "label": label} for key, label in groups.items()],
                        ensure_ascii=False,
                    ),
                    provider_id, provider_label, prompt, json.dumps(coverage, ensure_ascii=False),
                    source,
                ),
            )
            report = self._get_report(db, report_id)
            assert report is not None
            return report, messages

    def list_reports(self) -> list[dict]:
        with self._transaction() as db:
            return [
                _report_dict(row, detail=False)
                for row in db.execute(
                    f"SELECT {_REPORT_COLUMNS} FROM reports ORDER BY created_at DESC, rowid DESC"
                )
            ]

    @staticmethod
    def _get_report(db: sqlite3.Connection, report_id: str) -> dict | None:
        row = db.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
        return None if row is None else _report_dict(row, detail=True)

    def report(self, id: str) -> dict | None:
        with self._transaction() as db:
            return self._get_report(db, id)

    def finish(self, report_id: str, status: str, body: str | None, error: str | None) -> None:
        if status == "completed" and (body is None or not body.strip() or error is not None):
            status = "failed"
            error = error or "报告未生成有效正文。"
        if status == "failed":
            body = None
            error = error or "报告生成失败。"
        elif status != "completed":
            raise ValueError("报告只能结束为已完成或失败。")
        with self._transaction(write=True) as db:
            db.execute(
                """UPDATE reports SET status = ?, body = ?, error = ?
                   WHERE id = ? AND status = 'running'""",
                (status, body, error, report_id),
            )
