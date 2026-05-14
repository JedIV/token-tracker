"""Recompute derived cost/token estimates using current pricing and formulas.
Run this after editing prices.json or changing estimate logic.
"""
from __future__ import annotations

import math
from pathlib import Path

from .db import DEFAULT_DB_PATH, connect
from .ingest import EST_CHARS_PER_TOKEN
from .pricing import cost_usd, reload as reload_prices


def run(db_path: Path | str = DEFAULT_DB_PATH) -> dict:
    reload_prices()
    conn = connect(db_path)
    try:
        msgs = conn.execute(
            """SELECT id, tool, model, input_tokens, output_tokens, cache_read,
                      cache_write_5m, cache_write_1h, reasoning_tokens
               FROM messages""").fetchall()
        for m in msgs:
            # output_tokens already includes reasoning/thinking for both vendors.
            c = cost_usd(
                m["tool"], m["model"],
                input_tokens=m["input_tokens"],
                output_tokens=m["output_tokens"],
                cache_read=m["cache_read"],
                cache_write_5m=m["cache_write_5m"],
                cache_write_1h=m["cache_write_1h"],
            )
            conn.execute("UPDATE messages SET est_cost_usd=? WHERE id=?", (c, m["id"]))

        mcp = conn.execute("SELECT id, result_chars FROM mcp_calls").fetchall()
        for row in mcp:
            est_tokens = math.ceil(row["result_chars"] / EST_CHARS_PER_TOKEN) if row["result_chars"] else 0
            conn.execute(
                "UPDATE mcp_calls SET est_result_tokens=? WHERE id=?",
                (est_tokens, row["id"]),
            )

        sids = [r[0] for r in conn.execute("SELECT id FROM sessions").fetchall()]
        for sid in sids:
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
                (sid, sid, sid, sid, sid, sid, sid, sid),
            )
        conn.commit()
        return {
            "messages_updated": len(msgs),
            "mcp_updated": len(mcp),
            "sessions_updated": len(sids),
        }
    finally:
        conn.close()


if __name__ == "__main__":
    print(run())
