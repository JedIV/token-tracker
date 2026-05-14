"""Incremental ingester: walks Claude + Codex log dirs, parses new content,
upserts to SQLite, recomputes session aggregates."""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import parse_claude, parse_codex
from .db import DEFAULT_DB_PATH, connect, init
from .pricing import cost_usd

EST_CHARS_PER_TOKEN = 4


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _get_state(conn: sqlite3.Connection, path: Path) -> tuple[int, float, int]:
    row = conn.execute(
        "SELECT last_offset, last_mtime, last_size FROM ingest_state WHERE source_file=?",
        (str(path),),
    ).fetchone()
    if not row:
        return 0, 0.0, 0
    return row["last_offset"], row["last_mtime"], row["last_size"]


def _put_state(conn: sqlite3.Connection, path: Path, offset: int, mtime: float, size: int) -> None:
    conn.execute(
        """INSERT INTO ingest_state(source_file, last_offset, last_mtime, last_size, updated_at)
           VALUES(?,?,?,?,?)
           ON CONFLICT(source_file) DO UPDATE SET
             last_offset=excluded.last_offset,
             last_mtime=excluded.last_mtime,
             last_size=excluded.last_size,
             updated_at=excluded.updated_at""",
        (str(path), offset, mtime, size, _now_iso()),
    )


def _upsert_session(conn: sqlite3.Connection, meta) -> None:
    conn.execute(
        """INSERT INTO sessions(id, tool, session_uuid, cwd, model, started_at, ended_at)
           VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
             cwd       = COALESCE(excluded.cwd, sessions.cwd),
             model     = COALESCE(excluded.model, sessions.model),
             started_at= CASE WHEN sessions.started_at IS NULL OR excluded.started_at<sessions.started_at
                              THEN excluded.started_at ELSE sessions.started_at END,
             ended_at  = CASE WHEN sessions.ended_at IS NULL OR excluded.ended_at>sessions.ended_at
                              THEN excluded.ended_at ELSE sessions.ended_at END""",
        (meta.session_id, meta.tool, meta.session_uuid, meta.cwd, meta.model,
         meta.started_at, meta.ended_at),
    )


def _insert_messages(conn: sqlite3.Connection, rows) -> int:
    added = 0
    for r in rows:
        # Both vendors: output_tokens already includes reasoning/thinking tokens.
        # (Anthropic: thinking is part of `usage.output_tokens`. OpenAI: `reasoning_output_tokens`
        # is a subset of `output_tokens` — verified: input + output == total_tokens.)
        cost = cost_usd(
            r.tool, r.model,
            input_tokens=r.input_tokens,
            output_tokens=r.output_tokens,
            cache_read=r.cache_read,
            cache_write_5m=r.cache_write_5m,
            cache_write_1h=r.cache_write_1h,
        )
        cur = conn.execute(
            """INSERT OR IGNORE INTO messages
               (session_id, tool, ts, model, input_tokens, output_tokens, cache_read,
                cache_write_5m, cache_write_1h, reasoning_tokens, est_cost_usd,
                source_file, source_line)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (r.session_id, r.tool, r.ts, r.model, r.input_tokens, r.output_tokens,
             r.cache_read, r.cache_write_5m, r.cache_write_1h, r.reasoning_tokens, cost,
             r.source_file, r.source_line),
        )
        added += cur.rowcount
    return added


def _insert_mcp(conn: sqlite3.Connection, rows) -> int:
    added = 0
    for r in rows:
        est_tokens = r.result_chars // EST_CHARS_PER_TOKEN
        cur = conn.execute(
            """INSERT OR IGNORE INTO mcp_calls
               (session_id, tool, ts, server, tool_name, call_id, result_chars,
                est_result_tokens, is_error, source_file, source_line)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (r.session_id, r.tool, r.ts, r.server, r.tool_name, r.call_id,
             r.result_chars, est_tokens, r.is_error, r.source_file, r.source_line),
        )
        added += cur.rowcount
        # If a later run learns the result size, update it (no-op if same row inserted now).
        if cur.rowcount == 0 and r.result_chars > 0:
            conn.execute(
                """UPDATE mcp_calls SET result_chars=?, est_result_tokens=?, is_error=?
                   WHERE source_file=? AND call_id=? AND result_chars=0""",
                (r.result_chars, est_tokens, r.is_error, r.source_file, r.call_id),
            )
    return added


def _recompute_session(conn: sqlite3.Connection, session_id: str) -> None:
    conn.execute(
        """UPDATE sessions SET
             msg_count        = (SELECT COUNT(*) FROM messages WHERE session_id=?),
             input_tokens     = COALESCE((SELECT SUM(input_tokens)     FROM messages WHERE session_id=?), 0),
             output_tokens    = COALESCE((SELECT SUM(output_tokens)    FROM messages WHERE session_id=?), 0),
             cache_read       = COALESCE((SELECT SUM(cache_read)       FROM messages WHERE session_id=?), 0),
             cache_write      = COALESCE((SELECT SUM(cache_write_5m + cache_write_1h) FROM messages WHERE session_id=?), 0),
             reasoning_tokens = COALESCE((SELECT SUM(reasoning_tokens) FROM messages WHERE session_id=?), 0),
             est_cost_usd     = COALESCE((SELECT SUM(est_cost_usd)     FROM messages WHERE session_id=?), 0)
           WHERE id=?""",
        (session_id, session_id, session_id, session_id, session_id, session_id, session_id, session_id),
    )


def _process_file(conn: sqlite3.Connection, path: Path, parser_mod) -> tuple[bool, int, int]:
    """Returns (updated, msgs_added, mcp_added)."""
    try:
        st = path.stat()
    except FileNotFoundError:
        return False, 0, 0

    last_offset, last_mtime, last_size = _get_state(conn, path)
    # If file shrank or was rewritten (mtime decreased significantly), reset.
    if st.st_size < last_size:
        last_offset = 0
    if last_offset >= st.st_size and st.st_mtime == last_mtime:
        return False, 0, 0

    parsed, new_offset = parser_mod.parse_file(path, start_offset=last_offset)

    if not parsed.messages and not parsed.mcp_calls and new_offset == last_offset:
        # File hasn't grown meaningfully; update mtime/size only.
        _put_state(conn, path, new_offset, st.st_mtime, st.st_size)
        return False, 0, 0

    _upsert_session(conn, parsed.session)
    msgs_added = _insert_messages(conn, parsed.messages)
    mcp_added = _insert_mcp(conn, parsed.mcp_calls)
    _recompute_session(conn, parsed.session.session_id)
    _put_state(conn, path, new_offset, st.st_mtime, st.st_size)
    return True, msgs_added, mcp_added


def run(db_path: Path | str = DEFAULT_DB_PATH, *, verbose: bool = False) -> dict:
    init(db_path)
    conn = connect(db_path)
    started = _now_iso()
    started_t = time.time()
    cur = conn.execute(
        "INSERT INTO ingest_runs(started_at, files_scanned) VALUES(?, 0)",
        (started,),
    )
    run_id = cur.lastrowid

    files_scanned = 0
    files_updated = 0
    messages_added = 0
    mcp_added = 0
    error = None

    try:
        for path in parse_claude.discover_files():
            files_scanned += 1
            updated, ma, mc = _process_file(conn, path, parse_claude)
            if updated:
                files_updated += 1
                messages_added += ma
                mcp_added += mc
                if verbose:
                    print(f"[claude] {path.name}: +{ma} msgs +{mc} mcp")

        for path in parse_codex.discover_files():
            files_scanned += 1
            updated, ma, mc = _process_file(conn, path, parse_codex)
            if updated:
                files_updated += 1
                messages_added += ma
                mcp_added += mc
                if verbose:
                    print(f"[codex]  {path.name}: +{ma} msgs +{mc} mcp")

        conn.commit()
    except Exception as e:
        error = repr(e)
        conn.rollback()
        raise
    finally:
        conn.execute(
            """UPDATE ingest_runs SET finished_at=?, files_scanned=?, files_updated=?,
               messages_added=?, mcp_added=?, error=? WHERE id=?""",
            (_now_iso(), files_scanned, files_updated, messages_added, mcp_added, error, run_id),
        )
        conn.commit()
        conn.close()

    return {
        "files_scanned": files_scanned,
        "files_updated": files_updated,
        "messages_added": messages_added,
        "mcp_added": mcp_added,
        "elapsed_sec": round(time.time() - started_t, 2),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Ingest Claude+Codex token usage.")
    ap.add_argument("--db", default=str(DEFAULT_DB_PATH))
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    result = run(args.db, verbose=args.verbose)
    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
