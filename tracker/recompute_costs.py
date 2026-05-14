"""Recompute est_cost_usd on all messages + sessions using current prices.json.
Run this after editing prices.json (otherwise costs are stuck at ingest-time rates).
"""
from __future__ import annotations

import sys
from pathlib import Path

from .db import DEFAULT_DB_PATH, connect
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

        sids = [r[0] for r in conn.execute("SELECT id FROM sessions").fetchall()]
        for sid in sids:
            conn.execute(
                """UPDATE sessions SET est_cost_usd =
                     COALESCE((SELECT SUM(est_cost_usd) FROM messages WHERE session_id=?), 0)
                   WHERE id=?""",
                (sid, sid),
            )
        conn.commit()
        return {"messages_updated": len(msgs), "sessions_updated": len(sids)}
    finally:
        conn.close()


if __name__ == "__main__":
    print(run())
