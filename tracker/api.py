"""FastAPI app exposing token-tracker stats."""
from __future__ import annotations

import json as _json
import sqlite3
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .db import DEFAULT_DB_PATH, connect, init
from .ingest import run as run_ingest
from .parse_claude import _tool_result_chars
from .pricing import _lookup as _price_lookup, reload as _price_reload
from .recompute_costs import run as run_recompute

# Effective max context window (tokens) per model. Used to display the per-turn
# context size as a % of available context in the timeline view. These are the
# advertised maximums; the actual model behaviour may compact earlier.
MODEL_MAX_TOKENS: dict[str, int] = {
    # Claude 4.x — 1M-context variants (per recent Anthropic announcements).
    "claude-opus-4-7":   1_000_000,
    "claude-opus-4-6":   1_000_000,
    "claude-opus-4-5":   1_000_000,
    "claude-sonnet-4-6": 1_000_000,
    "claude-sonnet-4-5": 1_000_000,
    "claude-haiku-4-5":    200_000,
    # Older / smaller-context models.
    "claude-opus-4-1":     200_000,
    "claude-opus-4":       200_000,
    "claude-sonnet-4":     200_000,
    "claude-haiku-3-5":    200_000,
    # Codex (OpenAI) — large context.
    "gpt-5":          400_000,
    "gpt-5-mini":     400_000,
    "gpt-5-codex":    400_000,
    "gpt-5.2":        400_000,
    "gpt-5.2-codex":  400_000,
    "gpt-5.3-codex":  400_000,
    "gpt-5.4":        400_000,
    "gpt-5.4-mini":   400_000,
    "gpt-5.5":        400_000,
}
DEFAULT_MAX_TOKENS = 200_000


def _model_max(model: str | None) -> int:
    if not model:
        return DEFAULT_MAX_TOKENS
    return MODEL_MAX_TOKENS.get(model, DEFAULT_MAX_TOKENS)

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"

app = FastAPI(title="token-tracker", version="0.1.0")


@contextmanager
def db():
    conn = connect(DEFAULT_DB_PATH)
    try:
        yield conn
    finally:
        conn.close()


def _filter_clause(tool, model, project, start, end, agent=None, entrypoint=None, prefix="m"):
    """Returns (sql_fragment, params). All filters optional.

    `agent` semantics: None = no filter; "main" = agent_type IS NULL (top-level session
    turns); any other value = exact match on agent_type.
    """
    clauses = []
    params: list = []
    if tool:
        clauses.append(f"{prefix}.tool = ?")
        params.append(tool)
    if model:
        clauses.append(f"{prefix}.model = ?")
        params.append(model)
    if start:
        clauses.append(f"{prefix}.ts >= ?")
        params.append(start)
    if end:
        clauses.append(f"{prefix}.ts <= ?")
        params.append(end)
    if project:
        clauses.append("s.cwd = ?")
        params.append(project)
    if agent == "main":
        clauses.append(f"{prefix}.agent_type IS NULL")
    elif agent:
        clauses.append(f"{prefix}.agent_type = ?")
        params.append(agent)
    if entrypoint:
        clauses.append("s.entrypoint = ?")
        params.append(entrypoint)
    where = " AND ".join(clauses)
    return (f" WHERE {where}" if where else ""), params


@app.on_event("startup")
def _startup():
    init(DEFAULT_DB_PATH)


@app.get("/api/health")
def health():
    with db() as c:
        n = c.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    return {"ok": True, "messages": n, "db": str(DEFAULT_DB_PATH)}


@app.get("/api/filters")
def filters():
    """Distinct values for filter dropdowns."""
    with db() as c:
        tools = [r[0] for r in c.execute("SELECT DISTINCT tool FROM sessions ORDER BY tool")]
        models = [r[0] for r in c.execute(
            "SELECT DISTINCT model FROM messages WHERE model IS NOT NULL ORDER BY model")]
        projects = [r[0] for r in c.execute(
            "SELECT DISTINCT cwd FROM sessions WHERE cwd IS NOT NULL ORDER BY cwd")]
        agents = [r[0] for r in c.execute(
            "SELECT DISTINCT agent_type FROM messages WHERE agent_type IS NOT NULL ORDER BY agent_type")]
        entrypoints = [r[0] for r in c.execute(
            "SELECT DISTINCT entrypoint FROM sessions WHERE entrypoint IS NOT NULL ORDER BY entrypoint")]
        date_range = c.execute(
            "SELECT MIN(ts), MAX(ts) FROM messages").fetchone()
    return {
        "tools": tools,
        "models": models,
        "projects": projects,
        "agents": agents,
        "entrypoints": entrypoints,
        "date_range": {"min": date_range[0], "max": date_range[1]},
    }


_GRANULARITY = {
    # bucket_expr returns a string usable as GROUP BY / ORDER BY column.
    "minute": "substr(m.ts,1,16)",                                  # 2026-05-14T13:42
    "hour":   "substr(m.ts,1,13)",                                  # 2026-05-14T13
    "day":    "substr(m.ts,1,10)",                                  # 2026-05-14
    "week":   "strftime('%Y-W%W', m.ts)",                           # 2026-W19
    "month":  "substr(m.ts,1,7)",                                   # 2026-05
}


def _pick_granularity(start: str | None, end: str | None) -> str:
    """Auto-pick a granularity based on the visible window."""
    from datetime import datetime, timezone
    if not start:
        return "day"
    try:
        s = datetime.fromisoformat(start.replace("Z", "+00:00"))
        if end:
            e = datetime.fromisoformat(end.replace("Z", "+00:00"))
        else:
            e = datetime.now(timezone.utc)
        hours = max(1, (e - s).total_seconds() / 3600)
    except Exception:
        return "day"
    if hours <=   6: return "minute"
    if hours <=  72: return "hour"
    if hours <= 60*24: return "day"
    if hours <= 365*24: return "week"
    return "month"


@app.get("/api/stats")
def stats(
    tool: str | None = Query(None),
    model: str | None = Query(None),
    project: str | None = Query(None),
    start: str | None = Query(None),
    end: str | None = Query(None),
    agent: str | None = Query(None),
    entrypoint: str | None = Query(None),
    granularity: str = Query("auto"),
):
    """Totals + time-series + breakdowns. granularity: auto|minute|hour|day|week|month."""
    where, params = _filter_clause(tool, model, project, start, end, agent=agent, entrypoint=entrypoint)
    join = "FROM messages m JOIN sessions s ON s.id = m.session_id" + where

    gran = granularity if granularity in _GRANULARITY else _pick_granularity(start, end)
    bucket = _GRANULARITY[gran]
    # bucket duration in hours, for per-bucket rate normalization
    bucket_hours = {"minute": 1/60, "hour": 1.0, "day": 24.0, "week": 24*7.0, "month": 24*30.5}[gran]

    with db() as c:
        totals_row = c.execute(
            f"""SELECT
                  COUNT(*)                AS msgs,
                  COUNT(DISTINCT m.session_id) AS sessions,
                  COALESCE(SUM(m.input_tokens),0)   AS input_tokens,
                  COALESCE(SUM(m.output_tokens),0)  AS output_tokens,
                  COALESCE(SUM(m.cache_read),0)     AS cache_hit,
                  COALESCE(SUM(m.cache_write_5m),0) AS cache_write_5m,
                  COALESCE(SUM(m.cache_write_1h),0) AS cache_write_1h,
                  COALESCE(SUM(m.reasoning_tokens),0) AS reasoning,
                  COALESCE(SUM(m.est_cost_usd),0)   AS cost_usd
                {join}""", params).fetchone()
        totals = dict(totals_row)

        # Cost breakdown by bucket. Group tokens by (tool, model), apply per-bucket rates,
        # then sum. Lets the UI show "where the $ went" (cache reads almost always dominate).
        by_tm = c.execute(
            f"""SELECT m.tool, m.model,
                       SUM(m.input_tokens)   AS in_tok,
                       SUM(m.output_tokens)  AS out_tok,
                       SUM(m.cache_read)     AS cr_tok,
                       SUM(m.cache_write_5m) AS cw5_tok,
                       SUM(m.cache_write_1h) AS cw1_tok
                {join}
                GROUP BY m.tool, m.model""", params).fetchall()
        cb = {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write_5m": 0.0, "cache_write_1h": 0.0}
        for r in by_tm:
            p = _price_lookup(r["tool"], r["model"])
            cb["input"]          += (r["in_tok"]  or 0) * p.get("input", 0) / 1_000_000
            cb["output"]         += (r["out_tok"] or 0) * p.get("output", 0) / 1_000_000
            cb["cache_read"]     += (r["cr_tok"]  or 0) * p.get("cache_read", 0) / 1_000_000
            cb["cache_write_5m"] += (r["cw5_tok"] or 0) * p.get("cache_write_5m", 0) / 1_000_000
            cb["cache_write_1h"] += (r["cw1_tok"] or 0) * p.get("cache_write_1h", 0) / 1_000_000
        totals["cost_breakdown"] = {k: round(v, 4) for k, v in cb.items()}

        # Active hours: sum over sessions of (max(ts) - min(ts)) within the filter window.
        # This excludes pure idle gaps between sessions and gives a more meaningful rate.
        active_row = c.execute(
            f"""SELECT COALESCE(SUM(span_sec), 0) AS active_sec FROM (
                  SELECT m.session_id,
                         (julianday(MAX(m.ts)) - julianday(MIN(m.ts))) * 86400.0 AS span_sec
                  {join}
                  GROUP BY m.session_id
                )""", params).fetchone()
        active_seconds = active_row["active_sec"] or 0.0
        active_hours = active_seconds / 3600.0
        totals["active_hours"] = round(active_hours, 3)
        totals["cost_per_hour"] = round(totals["cost_usd"] / active_hours, 4) if active_hours > 0 else 0.0
        tok_total = (totals["input_tokens"] + totals["output_tokens"]
                     + totals["cache_hit"] + totals["cache_write_5m"] + totals["cache_write_1h"])
        totals["tokens_per_hour"] = int(tok_total / active_hours) if active_hours > 0 else 0

        # time-series at chosen granularity (UTC)
        daily = [dict(r) for r in c.execute(
            f"""SELECT {bucket} AS bucket,
                       SUM(m.input_tokens)  AS input_tokens,
                       SUM(m.output_tokens) AS output_tokens,
                       SUM(m.cache_read)    AS cache_hit,
                       SUM(m.cache_write_5m) AS cache_write_5m,
                       SUM(m.cache_write_1h) AS cache_write_1h,
                       SUM(m.est_cost_usd)  AS cost_usd
                {join}
                GROUP BY bucket
                ORDER BY bucket""", params).fetchall()]

        # by tool
        by_tool = [dict(r) for r in c.execute(
            f"""SELECT m.tool, COUNT(*) msgs,
                       SUM(m.input_tokens) input_tokens,
                       SUM(m.output_tokens) output_tokens,
                       SUM(m.cache_read) cache_hit,
                       SUM(m.cache_write_5m) cache_write_5m,
                       SUM(m.cache_write_1h) cache_write_1h,
                       SUM(m.est_cost_usd) cost_usd
                {join}
                GROUP BY m.tool
                ORDER BY cost_usd DESC""", params).fetchall()]

        # by model
        by_model = [dict(r) for r in c.execute(
            f"""SELECT COALESCE(m.model,'(unknown)') AS model,
                       m.tool,
                       COUNT(*) msgs,
                       SUM(m.input_tokens) input_tokens,
                       SUM(m.output_tokens) output_tokens,
                       SUM(m.cache_read) cache_hit,
                       SUM(m.cache_write_5m) cache_write_5m,
                       SUM(m.cache_write_1h) cache_write_1h,
                       SUM(m.est_cost_usd) cost_usd
                {join}
                GROUP BY m.model, m.tool
                ORDER BY cost_usd DESC""", params).fetchall()]

        # by entrypoint (cli / sdk-cli / codex / …). Tells "interactive REPL" from "spawned SDK runs".
        by_entrypoint = [dict(r) for r in c.execute(
            f"""SELECT COALESCE(s.entrypoint,'(unknown)') AS entrypoint,
                       COUNT(*) msgs,
                       COUNT(DISTINCT m.session_id) sessions,
                       SUM(m.input_tokens) input_tokens,
                       SUM(m.output_tokens) output_tokens,
                       SUM(m.cache_read) cache_hit,
                       SUM(m.cache_write_5m) cache_write_5m,
                       SUM(m.cache_write_1h) cache_write_1h,
                       SUM(m.est_cost_usd) cost_usd
                {join}
                GROUP BY s.entrypoint
                ORDER BY cost_usd DESC""", params).fetchall()]

        # by agent INVOCATION (one row per sub-agent run, identified by agent_id).
        # All main-session turns (agent_type IS NULL) collapse into a single aggregate row.
        by_agent = [dict(r) for r in c.execute(
            f"""SELECT COALESCE(m.agent_type,'main session') AS agent_type,
                       m.agent_id,
                       m.agent_desc,
                       COUNT(*) msgs,
                       COUNT(DISTINCT m.session_id) sessions,
                       SUM(m.input_tokens) input_tokens,
                       SUM(m.output_tokens) output_tokens,
                       SUM(m.cache_read) cache_hit,
                       SUM(m.cache_write_5m) cache_write_5m,
                       SUM(m.cache_write_1h) cache_write_1h,
                       SUM(m.est_cost_usd) cost_usd
                {join}
                GROUP BY m.agent_type, m.agent_id, m.agent_desc
                ORDER BY cost_usd DESC""", params).fetchall()]

        # by project
        by_project = [dict(r) for r in c.execute(
            f"""SELECT COALESCE(s.cwd,'(unknown)') AS project,
                       COUNT(*) msgs,
                       COUNT(DISTINCT m.session_id) sessions,
                       SUM(m.input_tokens) input_tokens,
                       SUM(m.output_tokens) output_tokens,
                       SUM(m.cache_read) cache_hit,
                       SUM(m.cache_write_5m) cache_write_5m,
                       SUM(m.cache_write_1h) cache_write_1h,
                       SUM(m.est_cost_usd) cost_usd
                {join}
                GROUP BY s.cwd
                ORDER BY cost_usd DESC""", params).fetchall()]

    # decorate each bucket with per-hour rates (averaged across the bucket's wall clock)
    for b in daily:
        tok = (b["input_tokens"] + b["output_tokens"] + b["cache_hit"]
               + b["cache_write_5m"] + b["cache_write_1h"])
        b["cost_per_hour"] = round(b["cost_usd"] / bucket_hours, 4) if bucket_hours else 0.0
        b["tokens_per_hour"] = int(tok / bucket_hours) if bucket_hours else 0

    return {
        "totals": totals,
        "granularity": gran,
        "bucket_hours": bucket_hours,
        "daily": daily,
        "by_tool": by_tool,
        "by_model": by_model,
        "by_project": by_project,
        "by_agent": by_agent,
        "by_entrypoint": by_entrypoint,
    }


@app.get("/api/breakdown_series")
def breakdown_series(
    group: str = Query("model", pattern="^(tool|model|project|session|server|mcp_tool|agent|entrypoint)$"),
    tool: str | None = Query(None),
    model: str | None = Query(None),
    project: str | None = Query(None),
    start: str | None = Query(None),
    end: str | None = Query(None),
    agent: str | None = Query(None),
    entrypoint: str | None = Query(None),
    granularity: str = Query("auto"),
    limit: int = Query(5, ge=1, le=10),
):
    """Top-N cost lines for the selected breakdown over the same buckets as /api/stats."""
    gran = granularity if granularity in _GRANULARITY else _pick_granularity(start, end)

    if group in {"server", "mcp_tool"}:
        bucket = _GRANULARITY[gran].replace("m.ts", "mc.ts")
        clauses = []
        params: list = []
        if tool:
            clauses.append("mc.tool = ?"); params.append(tool)
        if model:
            clauses.append("EXISTS (SELECT 1 FROM messages mm WHERE mm.source_file=mc.source_file "
                           "AND mm.source_line=mc.source_line AND mm.model = ?)")
            params.append(model)
        if agent == "main":
            clauses.append("EXISTS (SELECT 1 FROM messages mm WHERE mm.source_file=mc.source_file "
                           "AND mm.source_line=mc.source_line AND mm.agent_type IS NULL)")
        elif agent:
            clauses.append("EXISTS (SELECT 1 FROM messages mm WHERE mm.source_file=mc.source_file "
                           "AND mm.source_line=mc.source_line AND mm.agent_type = ?)")
            params.append(agent)
        if entrypoint:
            clauses.append("s.entrypoint = ?")
            params.append(entrypoint)
        if project:
            clauses.append("s.cwd = ?"); params.append(project)
        if start:
            clauses.append("mc.ts >= ?"); params.append(start)
        if end:
            clauses.append("mc.ts <= ?"); params.append(end)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        join = f"FROM mcp_calls mc JOIN sessions s ON s.id = mc.session_id{where}"

        with db() as c:
            rows = list(c.execute(
                f"""SELECT {bucket} AS bucket,
                           mc.server, mc.tool_name, mc.est_result_tokens AS tokens,
                           s.tool AS tool, s.model AS model,
                           (SELECT COUNT(*) FROM messages mm
                            WHERE mm.session_id = mc.session_id AND mm.ts > mc.ts) AS subseq
                    {join}
                    ORDER BY bucket""", params))

        from collections import defaultdict
        totals = defaultdict(float)
        labels = {}
        by_bucket = defaultdict(lambda: defaultdict(float))
        for r in rows:
            key = r["server"] if group == "server" else f"{r['server']}\u001f{r['tool_name']}"
            label = r["server"] if group == "server" else f"{r['server']} · {r['tool_name']}"
            cost = _per_call_lifecycle_cost(r["tokens"] or 0, r["tool"], r["model"], r["subseq"] or 0)
            totals[key] += cost
            labels[key] = label
            by_bucket[key][r["bucket"]] += cost

        buckets = sorted({r["bucket"] for r in rows})
        top_keys = [k for k, _ in sorted(totals.items(), key=lambda kv: kv[1], reverse=True)[:limit]]
        series = [{
            "key": key,
            "label": labels[key],
            "total_cost_usd": round(totals[key], 6),
            "points": [{"bucket": b, "cost_usd": round(by_bucket[key].get(b, 0.0), 6)} for b in buckets],
        } for key in top_keys]
        return {"granularity": gran, "buckets": buckets, "series": series}

    group_exprs = {
        "tool": ("m.tool", "m.tool"),
        "model": ("COALESCE(m.model,'(unknown)') || '\u001f' || m.tool",
                  "COALESCE(m.model,'(unknown)') || ' · ' || m.tool"),
        "project": ("COALESCE(s.cwd,'(unknown)')", "COALESCE(s.cwd,'(unknown)')"),
        "session": ("s.id", "s.tool || ' · ' || COALESCE(s.cwd, s.id)"),
        "agent": (
            "COALESCE(m.agent_id, COALESCE(m.agent_type,'main session'))",
            "CASE WHEN m.agent_id IS NULL THEN COALESCE(m.agent_type,'main session') "
            "     ELSE COALESCE(m.agent_type,'') || ' · ' || substr(m.agent_id,1,8) || "
            "          ' · ' || COALESCE(m.agent_desc,'(no description)') END",
        ),
        "entrypoint": ("COALESCE(s.entrypoint,'(unknown)')",
                       "COALESCE(s.entrypoint,'(unknown)')"),
    }
    key_expr, label_expr = group_exprs[group]
    bucket = _GRANULARITY[gran]
    where, params = _filter_clause(tool, model, project, start, end, agent=agent, entrypoint=entrypoint)
    join = "FROM messages m JOIN sessions s ON s.id = m.session_id" + where

    with db() as c:
        top = [dict(r) for r in c.execute(
            f"""SELECT {key_expr} AS key,
                       {label_expr} AS label,
                       SUM(m.est_cost_usd) AS cost_usd
                {join}
                GROUP BY key, label
                ORDER BY cost_usd DESC
                LIMIT ?""",
            (*params, limit),
        ).fetchall()]

        if not top:
            return {"granularity": gran, "buckets": [], "series": []}

        keys = [r["key"] for r in top]
        placeholders = ",".join("?" for _ in keys)
        rows = [dict(r) for r in c.execute(
            f"""SELECT {bucket} AS bucket,
                       {key_expr} AS key,
                       SUM(m.est_cost_usd) AS cost_usd
                {join}
                  {"AND" if where else "WHERE"} {key_expr} IN ({placeholders})
                GROUP BY bucket, key
                ORDER BY bucket""",
            (*params, *keys),
        ).fetchall()]

    buckets = sorted({r["bucket"] for r in rows})
    costs_by_key = {k: {b: 0.0 for b in buckets} for k in keys}
    for r in rows:
        costs_by_key[r["key"]][r["bucket"]] = r["cost_usd"] or 0.0

    labels = {r["key"]: r["label"] for r in top}
    totals = {r["key"]: r["cost_usd"] or 0.0 for r in top}
    series = [{
        "key": key,
        "label": labels[key],
        "total_cost_usd": round(totals[key], 6),
        "points": [{"bucket": b, "cost_usd": round(costs_by_key[key].get(b, 0.0), 6)} for b in buckets],
    } for key in keys]
    return {"granularity": gran, "buckets": buckets, "series": series}


@app.get("/api/sessions")
def sessions(
    tool: str | None = Query(None),
    model: str | None = Query(None),
    project: str | None = Query(None),
    start: str | None = Query(None),
    end: str | None = Query(None),
    agent: str | None = Query(None),
    entrypoint: str | None = Query(None),
    limit: int = Query(200, ge=1, le=2000),
    sort: str = Query("cost", pattern="^(cost|recent|messages)$"),
):
    where, params = _filter_clause(tool, model, project, start, end, agent=agent, entrypoint=entrypoint)
    order = {
        "cost": "est_cost_usd DESC",
        "recent": "ended_at DESC",
        "messages": "msg_count DESC",
    }[sort]
    with db() as c:
        rows = [dict(r) for r in c.execute(
            f"""SELECT
                  s.id,
                  s.tool,
                  s.session_uuid,
                  s.cwd,
                  s.model,
                  s.entrypoint,
                  MIN(m.ts) AS started_at,
                  MAX(m.ts) AS ended_at,
                  COUNT(*) AS msg_count,
                  COALESCE(SUM(m.input_tokens),0) AS input_tokens,
                  COALESCE(SUM(m.output_tokens),0) AS output_tokens,
                  COALESCE(SUM(m.cache_read),0) AS cache_read,
                  COALESCE(SUM(m.cache_write_5m + m.cache_write_1h),0) AS cache_write,
                  COALESCE(SUM(m.reasoning_tokens),0) AS reasoning_tokens,
                  COALESCE(SUM(m.est_cost_usd),0) AS est_cost_usd
                FROM messages m JOIN sessions s ON s.id = m.session_id
                {where}
                GROUP BY s.id
                ORDER BY {order}
                LIMIT ?""",
            (*params, limit)).fetchall()]
    return {"sessions": rows}


@app.get("/api/session/{session_id:path}")
def session_detail(
    session_id: str,
    tool: str | None = Query(None),
    model: str | None = Query(None),
    project: str | None = Query(None),
    start: str | None = Query(None),
    end: str | None = Query(None),
    agent: str | None = Query(None),
    entrypoint: str | None = Query(None),
):
    filters, filter_params = _filter_clause(tool, model, project, start, end, agent=agent, entrypoint=entrypoint)
    where = " WHERE m.session_id = ?" + filters.replace(" WHERE", " AND", 1)
    params = [session_id, *filter_params]
    with db() as c:
        base = c.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not base:
            raise HTTPException(404, "session not found")
        row = c.execute(
            f"""SELECT
                  s.id,
                  s.tool,
                  s.session_uuid,
                  s.cwd,
                  s.model,
                  MIN(m.ts) AS started_at,
                  MAX(m.ts) AS ended_at,
                  COUNT(m.id) AS msg_count,
                  COALESCE(SUM(m.input_tokens),0) AS input_tokens,
                  COALESCE(SUM(m.output_tokens),0) AS output_tokens,
                  COALESCE(SUM(m.cache_read),0) AS cache_read,
                  COALESCE(SUM(m.cache_write_5m + m.cache_write_1h),0) AS cache_write,
                  COALESCE(SUM(m.reasoning_tokens),0) AS reasoning_tokens,
                  COALESCE(SUM(m.est_cost_usd),0) AS est_cost_usd
                FROM sessions s LEFT JOIN messages m ON s.id = m.session_id
                {where}
                GROUP BY s.id""",
            params,
        ).fetchone()
        msgs = [dict(r) for r in c.execute(
            f"""SELECT m.ts, m.model, m.input_tokens, m.output_tokens, m.cache_read,
                      m.cache_write_5m, m.cache_write_1h, m.reasoning_tokens, m.est_cost_usd,
                      m.agent_type, m.agent_desc, m.agent_id
                FROM messages m JOIN sessions s ON s.id = m.session_id
                {where}
                ORDER BY m.ts""",
            params).fetchall()]
        mcp = [dict(r) for r in c.execute(
            """SELECT ts, server, tool_name, result_chars, est_result_tokens, is_error
               FROM mcp_calls
               WHERE session_id=?
                 AND (? IS NULL OR ts >= ?)
                 AND (? IS NULL OR ts <= ?)
               ORDER BY ts""",
            (session_id, start, start, end, end)).fetchall()]
    session = dict(base)
    if row:
        session.update(dict(row))
    else:
        session.update({
            "started_at": None,
            "ended_at": None,
            "msg_count": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read": 0,
            "cache_write": 0,
            "reasoning_tokens": 0,
            "est_cost_usd": 0,
        })
    return {"session": session, "messages": msgs, "mcp_calls": mcp}


def _per_call_lifecycle_cost(tokens: int, tool: str, model: str | None, subsequent_msgs: int) -> float:
    """Lifecycle cost of an MCP result that enters context:
    - Claude: cache_write_1h once (most-common TTL in Claude Code) + cache_read × subsequent turns.
    - Codex:  full input rate once (no cache writes) + cache_read × subsequent turns
              (OpenAI's auto-cache only activates after the first send).
    """
    if not tokens:
        return 0.0
    p = _price_lookup(tool, model)
    cache_read = p.get("cache_read", 0)
    if tool == "claude":
        first = p.get("cache_write_1h") or p.get("cache_write_5m") or p.get("input", 0)
    else:
        first = p.get("input", 0)
    cost = tokens * (first + cache_read * subsequent_msgs) / 1_000_000
    return cost


@app.get("/api/mcp")
def mcp(
    tool: str | None = Query(None),
    model: str | None = Query(None),
    project: str | None = Query(None),
    start: str | None = Query(None),
    end: str | None = Query(None),
    agent: str | None = Query(None),
    entrypoint: str | None = Query(None),
):
    """MCP usage breakdown: by server, by server+tool_name. Cost attribution explained
    inline: result tokens × cache_read rate of the session's model (since MCP results
    become cached input on subsequent turns)."""
    clauses = []
    params: list = []
    if tool:
        clauses.append("mc.tool = ?"); params.append(tool)
    if model:
        # An MCP call is issued by the assistant message at the same source_file/source_line.
        clauses.append("EXISTS (SELECT 1 FROM messages mm WHERE mm.source_file=mc.source_file "
                       "AND mm.source_line=mc.source_line AND mm.model = ?)")
        params.append(model)
    if agent == "main":
        clauses.append("EXISTS (SELECT 1 FROM messages mm WHERE mm.source_file=mc.source_file "
                       "AND mm.source_line=mc.source_line AND mm.agent_type IS NULL)")
    elif agent:
        clauses.append("EXISTS (SELECT 1 FROM messages mm WHERE mm.source_file=mc.source_file "
                       "AND mm.source_line=mc.source_line AND mm.agent_type = ?)")
        params.append(agent)
    if entrypoint:
        clauses.append("s.entrypoint = ?")
        params.append(entrypoint)
    if project:
        clauses.append("s.cwd = ?"); params.append(project)
    if start:
        clauses.append("mc.ts >= ?"); params.append(start)
    if end:
        clauses.append("mc.ts <= ?"); params.append(end)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    join = f"FROM mcp_calls mc JOIN sessions s ON s.id = mc.session_id{where}"

    with db() as c:
        # Per-call rows with the session's model + count of subsequent messages in the
        # same session (those are the turns that will re-read the cached MCP result).
        call_rows = list(c.execute(
            f"""SELECT mc.server, mc.tool_name, mc.est_result_tokens AS tokens,
                       mc.result_chars, mc.is_error,
                       s.tool AS tool, s.model AS model,
                       (SELECT COUNT(*) FROM messages mm
                        WHERE mm.session_id = mc.session_id AND mm.ts > mc.ts) AS subseq
                {join}""", params))

        # Distinct-session-cost aggregations remain useful as a secondary signal.
        session_cost_by_server = {r["server"]: r["session_cost"] for r in c.execute(
            f"""SELECT server, SUM(est_cost_usd) AS session_cost FROM (
                  SELECT DISTINCT mc.server, s.id, s.est_cost_usd
                  {join}
                ) GROUP BY server""", params).fetchall()}
        session_cost_by_tool = {(r["server"], r["tool_name"]): r["session_cost"] for r in c.execute(
            f"""SELECT server, tool_name, SUM(est_cost_usd) AS session_cost FROM (
                  SELECT DISTINCT mc.server, mc.tool_name, s.id, s.est_cost_usd
                  {join}
                ) GROUP BY server, tool_name""", params).fetchall()}
        sessions_count_by_server = {r["server"]: r["n"] for r in c.execute(
            f"""SELECT server, COUNT(DISTINCT mc.session_id) AS n {join} GROUP BY server""", params).fetchall()}

    # Roll per-call cost up into server and (server, tool) groupings.
    from collections import defaultdict
    by_server_acc = defaultdict(lambda: {"calls": 0, "tokens": 0, "chars": 0, "errors": 0, "cost": 0.0, "lifetime_reads": 0})
    by_tool_acc   = defaultdict(lambda: {"calls": 0, "tokens": 0, "chars": 0, "errors": 0, "cost": 0.0, "lifetime_reads": 0})
    for r in call_rows:
        tokens = r["tokens"] or 0
        cost = _per_call_lifecycle_cost(tokens, r["tool"], r["model"], r["subseq"] or 0)
        for acc in (by_server_acc[r["server"]], by_tool_acc[(r["server"], r["tool_name"])]):
            acc["calls"] += 1
            acc["tokens"] += tokens
            acc["chars"] += r["result_chars"] or 0
            acc["errors"] += r["is_error"] or 0
            acc["cost"] += cost
            acc["lifetime_reads"] += r["subseq"] or 0

    by_server = [{
        "server": server,
        "calls": v["calls"],
        "sessions": sessions_count_by_server.get(server, 0),
        "result_chars": v["chars"],
        "est_tokens": v["tokens"],
        "avg_lifetime_reads": round(v["lifetime_reads"] / v["calls"], 1) if v["calls"] else 0,
        "errors": v["errors"],
        "est_cost_usd": round(v["cost"], 4),
        "session_cost_usd": round(session_cost_by_server.get(server, 0) or 0, 2),
    } for server, v in by_server_acc.items()]
    by_server.sort(key=lambda r: r["est_cost_usd"], reverse=True)

    by_tool_name = [{
        "server": server,
        "tool_name": tool_name,
        "calls": v["calls"],
        "result_chars": v["chars"],
        "est_tokens": v["tokens"],
        "avg_lifetime_reads": round(v["lifetime_reads"] / v["calls"], 1) if v["calls"] else 0,
        "errors": v["errors"],
        "est_cost_usd": round(v["cost"], 4),
        "session_cost_usd": round(session_cost_by_tool.get((server, tool_name), 0) or 0, 2),
    } for (server, tool_name), v in by_tool_acc.items()]
    by_tool_name.sort(key=lambda r: r["est_cost_usd"], reverse=True)

    return {"by_server": by_server, "by_tool_name": by_tool_name}


@app.get("/api/mcp/server/{server}")
def mcp_server_detail(
    server: str,
    tool_name: str | None = Query(None),
    limit_calls: int = Query(500, ge=1, le=5000),
):
    """All sessions that used this MCP server, plus a sample of recent calls."""
    with db() as c:
        params = [server]
        tool_filter = ""
        if tool_name:
            tool_filter = " AND mc.tool_name = ?"
            params.append(tool_name)
        agg = c.execute(
            f"""SELECT server, COUNT(*) calls, SUM(result_chars) result_chars,
                       SUM(est_result_tokens) est_tokens, SUM(is_error) errors,
                       COUNT(DISTINCT mc.session_id) sessions,
                       MIN(mc.ts) first_at, MAX(mc.ts) last_at
                FROM mcp_calls mc WHERE mc.server = ?{tool_filter}""", params).fetchone()
        by_tool = [dict(r) for r in c.execute(
            f"""SELECT tool_name, COUNT(*) calls, SUM(result_chars) result_chars,
                       SUM(est_result_tokens) est_tokens, SUM(is_error) errors
                FROM mcp_calls mc WHERE mc.server = ?{tool_filter}
                GROUP BY tool_name ORDER BY calls DESC""", params).fetchall()]
        sessions_rows = [dict(r) for r in c.execute(
            f"""SELECT s.id, s.tool, s.model, s.cwd, s.started_at, s.ended_at,
                       s.msg_count, s.est_cost_usd,
                       COUNT(mc.id) AS server_calls,
                       SUM(mc.result_chars) AS server_result_chars,
                       SUM(mc.is_error) AS server_errors
                FROM mcp_calls mc JOIN sessions s ON s.id = mc.session_id
                WHERE mc.server = ?{tool_filter}
                GROUP BY s.id
                ORDER BY server_calls DESC""", params).fetchall()]
        calls = [dict(r) for r in c.execute(
            f"""SELECT mc.ts, mc.session_id, mc.tool_name, mc.result_chars,
                       mc.est_result_tokens, mc.is_error
                FROM mcp_calls mc WHERE mc.server = ?{tool_filter}
                ORDER BY mc.ts DESC LIMIT ?""", (*params, limit_calls)).fetchall()]
    return {
        "server": server,
        "tool_name": tool_name,
        "aggregate": dict(agg) if agg else {},
        "by_tool": by_tool,
        "sessions": sessions_rows,
        "calls": calls,
    }


def _input_preview(name: str, inp) -> str:
    """Short identifier for a tool_use, shown next to the tool name in the timeline."""
    if not isinstance(inp, dict):
        return ""
    for k in ("file_path", "command", "pattern", "path", "url", "query", "description", "prompt"):
        v = inp.get(k)
        if isinstance(v, str):
            return v[:120]
    return ""


def _parse_attributions(source_file: str, wanted_lines: set[int]) -> dict[int, list[dict]]:
    """For one JSONL file, return {assistant_source_line -> [{name, preview, result_chars, is_error}]}.

    Walks the file once, pairs tool_use blocks with their later tool_result blocks via
    tool_use_id. Restricted to assistant lines in `wanted_lines` for memory."""
    out: dict[int, list[dict]] = defaultdict(list)
    pending: dict[str, tuple[int, dict]] = {}  # tool_use_id -> (assistant_line, entry)
    try:
        with open(source_file, "r", errors="replace") as f:
            line_no = 0
            for raw in f:
                line_no += 1
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    d = _json.loads(raw)
                except _json.JSONDecodeError:
                    continue
                t = d.get("type")
                if t == "assistant":
                    msg = d.get("message") or {}
                    for blk in msg.get("content") or []:
                        if isinstance(blk, dict) and blk.get("type") == "tool_use":
                            entry = {
                                "name": blk.get("name", ""),
                                "preview": _input_preview(blk.get("name", ""), blk.get("input")),
                                "result_chars": 0,
                                "is_error": 0,
                            }
                            tu_id = blk.get("id") or f"l{line_no}-{len(pending)}"
                            if line_no in wanted_lines:
                                out[line_no].append(entry)
                                pending[tu_id] = (line_no, entry)
                elif t == "user":
                    msg = d.get("message") or {}
                    content = msg.get("content")
                    if isinstance(content, list):
                        for blk in content:
                            if isinstance(blk, dict) and blk.get("type") == "tool_result":
                                tu_id = blk.get("tool_use_id")
                                if tu_id in pending:
                                    _, entry = pending[tu_id]
                                    chars, _ = _tool_result_chars(blk.get("content"))
                                    entry["result_chars"] = chars
                                    entry["is_error"] = 1 if blk.get("is_error") else 0
                                    del pending[tu_id]
    except (OSError, FileNotFoundError):
        pass
    return out


@app.get("/api/context_timeline")
def context_timeline(
    agent_id: str | None = Query(None),
    session_id: str | None = Query(None),
):
    """Per-turn context size + source attribution for one sub-agent invocation or one
    main-session thread.

    Returns the assistant-turn usage counts (input/output/cache_read/cache_write) so the
    UI can plot context-window utilisation over time, plus the tool_use blocks issued
    on each turn (with the byte size of the matching tool_result, which approximates
    "how many tokens that source brought into the next turn's context").
    """
    if not agent_id and not session_id:
        raise HTTPException(400, "need agent_id or session_id")
    with db() as c:
        if agent_id:
            rows = c.execute(
                """SELECT m.id, m.session_id, m.ts, m.model,
                          m.input_tokens, m.output_tokens,
                          m.cache_read, m.cache_write_5m, m.cache_write_1h,
                          m.reasoning_tokens, m.est_cost_usd,
                          m.source_file, m.source_line,
                          m.agent_type, m.agent_desc, m.agent_id,
                          s.cwd
                   FROM messages m JOIN sessions s ON s.id = m.session_id
                   WHERE m.agent_id=? ORDER BY m.ts, m.source_line""",
                (agent_id,)).fetchall()
        else:
            rows = c.execute(
                """SELECT m.id, m.session_id, m.ts, m.model,
                          m.input_tokens, m.output_tokens,
                          m.cache_read, m.cache_write_5m, m.cache_write_1h,
                          m.reasoning_tokens, m.est_cost_usd,
                          m.source_file, m.source_line,
                          m.agent_type, m.agent_desc, m.agent_id,
                          s.cwd
                   FROM messages m JOIN sessions s ON s.id = m.session_id
                   WHERE m.session_id=? AND m.agent_type IS NULL
                   ORDER BY m.ts, m.source_line""",
                (session_id,)).fetchall()
    msgs = [dict(r) for r in rows]
    if not msgs:
        return {
            "label": agent_id or session_id,
            "turns": [], "model": None, "model_max_tokens": DEFAULT_MAX_TOKENS,
            "cwd": None, "session_id": session_id, "agent_id": agent_id,
        }

    # Parse JSONL attribution per source_file (one for sub-agent; possibly several
    # if the main session was resumed across multiple files).
    by_file: dict[str, set[int]] = defaultdict(set)
    for m in msgs:
        by_file[m["source_file"]].add(m["source_line"])
    attrs_all: dict[tuple[str, int], list[dict]] = {}
    for src, lines in by_file.items():
        per_line = _parse_attributions(src, lines)
        for ln, uses in per_line.items():
            attrs_all[(src, ln)] = uses

    last_model = next((m["model"] for m in reversed(msgs) if m["model"]), None)
    model_max = _model_max(last_model)
    turns = []
    for idx, m in enumerate(msgs):
        ctx_total = (m["input_tokens"] + m["cache_read"]
                     + m["cache_write_5m"] + m["cache_write_1h"])
        uses = attrs_all.get((m["source_file"], m["source_line"]), [])
        turns.append({
            "idx": idx + 1,
            "ts": m["ts"],
            "model": m["model"],
            "input_tokens": m["input_tokens"],
            "output_tokens": m["output_tokens"],
            "cache_read": m["cache_read"],
            "cache_write_5m": m["cache_write_5m"],
            "cache_write_1h": m["cache_write_1h"],
            "reasoning_tokens": m["reasoning_tokens"],
            "context_total": ctx_total,
            "model_max": _model_max(m["model"]),
            "tool_uses": uses,
        })

    label_bits = []
    first = msgs[0]
    if first["agent_type"]:
        label_bits.append(first["agent_type"])
        if first["agent_id"]:
            label_bits.append("#" + first["agent_id"][:8])
        if first["agent_desc"]:
            label_bits.append(first["agent_desc"])
    else:
        label_bits.append("main session")
        if first.get("cwd"):
            label_bits.append(first["cwd"])
    return {
        "label": " · ".join(label_bits),
        "session_id": first["session_id"],
        "agent_id": agent_id,
        "cwd": first.get("cwd"),
        "model": last_model,
        "model_max_tokens": model_max,
        "turns": turns,
    }


@app.post("/api/reingest")
def reingest():
    result = run_ingest(DEFAULT_DB_PATH)
    return result


@app.post("/api/recompute_costs")
def recompute_costs():
    """Re-apply prices.json to all existing message + session rows."""
    return run_recompute(DEFAULT_DB_PATH)


@app.get("/api/ingest_runs")
def ingest_runs(limit: int = Query(20, ge=1, le=200)):
    with db() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM ingest_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]
    return {"runs": rows}


# Static UI
if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


@app.get("/")
def index():
    idx = WEB_DIR / "index.html"
    if not idx.exists():
        return JSONResponse({"hint": "UI not built yet"}, status_code=404)
    return FileResponse(str(idx))
