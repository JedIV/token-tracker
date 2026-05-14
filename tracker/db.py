"""SQLite schema + small helpers."""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id              TEXT PRIMARY KEY,        -- "<tool>:<session_uuid>"
    tool            TEXT NOT NULL,           -- 'claude' | 'codex'
    session_uuid    TEXT NOT NULL,
    cwd             TEXT,
    model           TEXT,                    -- last model observed in session
    started_at      TEXT,
    ended_at        TEXT,
    msg_count       INTEGER NOT NULL DEFAULT 0,
    input_tokens    INTEGER NOT NULL DEFAULT 0,
    output_tokens   INTEGER NOT NULL DEFAULT 0,
    cache_read      INTEGER NOT NULL DEFAULT 0,
    cache_write     INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    est_cost_usd    REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL,
    tool            TEXT NOT NULL,
    ts              TEXT NOT NULL,
    model           TEXT,
    input_tokens    INTEGER NOT NULL DEFAULT 0,
    output_tokens   INTEGER NOT NULL DEFAULT 0,
    cache_read      INTEGER NOT NULL DEFAULT 0,
    cache_write_5m  INTEGER NOT NULL DEFAULT 0,
    cache_write_1h  INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    est_cost_usd    REAL NOT NULL DEFAULT 0,
    source_file     TEXT NOT NULL,
    source_line     INTEGER NOT NULL,
    UNIQUE(source_file, source_line)
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts);
CREATE INDEX IF NOT EXISTS idx_messages_tool ON messages(tool);
CREATE INDEX IF NOT EXISTS idx_messages_model ON messages(model);

CREATE TABLE IF NOT EXISTS mcp_calls (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL,
    tool            TEXT NOT NULL,           -- 'claude' | 'codex'
    ts              TEXT NOT NULL,
    server          TEXT NOT NULL,
    tool_name       TEXT NOT NULL,
    call_id         TEXT NOT NULL,           -- toolu_* or call_*
    result_chars    INTEGER NOT NULL DEFAULT 0,
    est_result_tokens INTEGER NOT NULL DEFAULT 0,
    is_error        INTEGER NOT NULL DEFAULT 0,
    source_file     TEXT NOT NULL,
    source_line     INTEGER NOT NULL,
    UNIQUE(source_file, call_id)
);
CREATE INDEX IF NOT EXISTS idx_mcp_session ON mcp_calls(session_id);
CREATE INDEX IF NOT EXISTS idx_mcp_server ON mcp_calls(server);
CREATE INDEX IF NOT EXISTS idx_mcp_ts ON mcp_calls(ts);

CREATE TABLE IF NOT EXISTS ingest_state (
    source_file     TEXT PRIMARY KEY,
    last_offset     INTEGER NOT NULL DEFAULT 0,
    last_mtime      REAL NOT NULL DEFAULT 0,
    last_size       INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingest_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    files_scanned   INTEGER NOT NULL DEFAULT 0,
    files_updated   INTEGER NOT NULL DEFAULT 0,
    messages_added  INTEGER NOT NULL DEFAULT 0,
    mcp_added       INTEGER NOT NULL DEFAULT 0,
    error           TEXT
);
"""

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "tokens.db"


def connect(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init(db_path: Path | str = DEFAULT_DB_PATH) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    init()
    print(f"Initialized {DEFAULT_DB_PATH}")
