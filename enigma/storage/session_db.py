"""跨会话的 SQLite + FTS5 存储层。

职责：
- 保存会话元信息（sessions 表）
- 保存每条消息/工具调用/工具结果/compact 摘要（messages 表）
- 提供按关键字的全历史 FTS5 搜索
- 提供每会话的滚动摘要（session_state 表）

注意：这层是"跨会话可检索的长期档案"，不是当前轮 prompt 的来源。
当前轮 prompt 仍由 ContextManager 从 session.history / memory 组装。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    parent_id TEXT,
    title TEXT,
    model TEXT,
    cwd TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    metadata_json TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    tool_name TEXT,
    tool_call_id TEXT,
    file_path TEXT,
    command TEXT,
    token_estimate INTEGER,
    created_at TEXT NOT NULL,
    metadata_json TEXT,
    FOREIGN KEY(session_id) REFERENCES sessions(id)
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);

CREATE TABLE IF NOT EXISTS session_state (
    session_id TEXT PRIMARY KEY,
    rolling_summary TEXT,
    recent_files_json TEXT,
    open_tasks_json TEXT,
    updated_at TEXT NOT NULL,
    metadata_json TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content,
    file_path,
    command,
    tool_name,
    content='messages',
    content_rowid='id',
    tokenize='unicode61'
);

CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content, file_path, command, tool_name)
    VALUES (new.id, new.content, new.file_path, new.command, new.tool_name);
END;

CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content, file_path, command, tool_name)
    VALUES('delete', old.id, old.content, old.file_path, old.command, old.tool_name);
END;

CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content, file_path, command, tool_name)
    VALUES('delete', old.id, old.content, old.file_path, old.command, old.tool_name);
    INSERT INTO messages_fts(rowid, content, file_path, command, tool_name)
    VALUES (new.id, new.content, new.file_path, new.command, new.tool_name);
END;
"""


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fts_escape(query):
    """FTS5 MATCH 需要转义双引号，并把空查询兜底。"""
    text = str(query or "").strip()
    if not text:
        return ""
    return '"' + text.replace('"', '""') + '"'


class SessionDB:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Windows 文件锁对长连接不友好；每次调用开/关连接，避免测试清理被阻塞。
        conn = self._connect()
        try:
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()

    def _connect(self):
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        return conn

    def close(self):
        """留作兼容：目前没有长连接需要关闭。"""
        return

    @contextmanager
    def _session(self):
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---- 会话生命周期 ----

    def start_session(self, session_id, *, parent_id=None, title=None, model=None, cwd=None, metadata=None):
        now = _now_iso()
        payload = json.dumps(metadata, ensure_ascii=False) if metadata else None
        with self._session() as conn:
            conn.execute(
                """
                INSERT INTO sessions (id, parent_id, title, model, cwd, started_at, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    parent_id = COALESCE(excluded.parent_id, sessions.parent_id),
                    title = COALESCE(excluded.title, sessions.title),
                    model = COALESCE(excluded.model, sessions.model),
                    cwd = COALESCE(excluded.cwd, sessions.cwd),
                    metadata_json = COALESCE(excluded.metadata_json, sessions.metadata_json)
                """,
                (session_id, parent_id, title, model, cwd, now, payload),
            )

    def end_session(self, session_id):
        with self._session() as conn:
            conn.execute(
                "UPDATE sessions SET ended_at = ? WHERE id = ?",
                (_now_iso(), session_id),
            )

    def get_session(self, session_id):
        with self._session() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return dict(row) if row else None

    # ---- 消息流 ----

    def append_message(
        self,
        session_id,
        role,
        kind,
        content,
        *,
        tool_name=None,
        tool_call_id=None,
        file_path=None,
        command=None,
        token_estimate=None,
        metadata=None,
    ):
        """记录一条消息/工具调用/工具结果/compact 摘要到 messages 表。

        - role: user / assistant / tool / system
        - kind: message / tool_call / tool_result / compact_summary / plan / final
        """
        payload = json.dumps(metadata, ensure_ascii=False) if metadata else None
        with self._session() as conn:
            cursor = conn.execute(
                """
                INSERT INTO messages (
                    session_id, role, kind, content,
                    tool_name, tool_call_id, file_path, command,
                    token_estimate, created_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    str(role),
                    str(kind),
                    str(content or ""),
                    tool_name,
                    tool_call_id,
                    file_path,
                    command,
                    int(token_estimate) if token_estimate is not None else None,
                    _now_iso(),
                    payload,
                ),
            )
            return int(cursor.lastrowid)

    def recent_messages(self, session_id, limit=20):
        with self._session() as conn:
            rows = conn.execute(
                """
                SELECT * FROM messages
                WHERE session_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (session_id, int(limit)),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def search_messages(self, query, *, session_id=None, limit=8):
        """FTS5 全文搜索 messages.content / file_path / command / tool_name。"""
        escaped = _fts_escape(query)
        if not escaped:
            return []
        if session_id:
            sql = """
                SELECT m.*, bm25(messages_fts) AS score
                FROM messages_fts
                JOIN messages m ON m.id = messages_fts.rowid
                WHERE messages_fts MATCH ?
                  AND m.session_id = ?
                ORDER BY score
                LIMIT ?
            """
            params = (escaped, session_id, int(limit))
        else:
            sql = """
                SELECT m.*, bm25(messages_fts) AS score
                FROM messages_fts
                JOIN messages m ON m.id = messages_fts.rowid
                WHERE messages_fts MATCH ?
                ORDER BY score
                LIMIT ?
            """
            params = (escaped, int(limit))
        with self._session() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    # ---- 会话状态（滚动摘要） ----

    def update_session_state(
        self,
        session_id,
        *,
        rolling_summary=None,
        recent_files=None,
        open_tasks=None,
        metadata=None,
    ):
        """更新某会话的滚动摘要、最近文件列表、待办任务。未传字段保持不变。"""
        existing = self.get_session_state(session_id) or {}
        new_summary = rolling_summary if rolling_summary is not None else existing.get("rolling_summary")
        new_files = recent_files if recent_files is not None else existing.get("recent_files")
        new_tasks = open_tasks if open_tasks is not None else existing.get("open_tasks")
        new_meta = metadata if metadata is not None else existing.get("metadata")
        with self._session() as conn:
            conn.execute(
                """
                INSERT INTO session_state (
                    session_id, rolling_summary, recent_files_json, open_tasks_json,
                    updated_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    rolling_summary = excluded.rolling_summary,
                    recent_files_json = excluded.recent_files_json,
                    open_tasks_json = excluded.open_tasks_json,
                    updated_at = excluded.updated_at,
                    metadata_json = excluded.metadata_json
                """,
                (
                    session_id,
                    new_summary,
                    json.dumps(new_files, ensure_ascii=False) if new_files is not None else None,
                    json.dumps(new_tasks, ensure_ascii=False) if new_tasks is not None else None,
                    _now_iso(),
                    json.dumps(new_meta, ensure_ascii=False) if new_meta is not None else None,
                ),
            )

    def get_session_state(self, session_id):
        with self._session() as conn:
            row = conn.execute(
                "SELECT * FROM session_state WHERE session_id = ?", (session_id,)
            ).fetchone()
        if not row:
            return None
        data = dict(row)
        data["recent_files"] = json.loads(data.get("recent_files_json") or "null")
        data["open_tasks"] = json.loads(data.get("open_tasks_json") or "null")
        data["metadata"] = json.loads(data.get("metadata_json") or "null")
        return data
