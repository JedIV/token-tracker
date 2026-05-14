"""Parse ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl files.

Relevant record types:
- session_meta: payload.{id, cwd, model_provider, cli_version}
- turn_context: payload.{cwd, model}
- response_item:function_call: name, call_id, arguments  (we track MCP via mcp__ prefix)
- response_item:function_call_output: call_id, output  (string)
- event_msg:token_count: info.last_token_usage / total_token_usage  -> emit one MessageRow per `last_token_usage` (delta)

Token attribution: Codex emits a token_count event after each LLM turn. We use
`last_token_usage` as the per-turn delta to avoid double-counting.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .parse_claude import MessageRow, McpCallRow, SessionMeta, ParsedFile, _mcp_parse_name

CODEX_ROOT = Path.home() / ".codex" / "sessions"


def parse_file(path: Path, *, start_offset: int = 0) -> tuple[ParsedFile, int]:
    session_uuid = ""
    # File name pattern: rollout-2026-03-04T18-51-02-<uuid>.jsonl
    try:
        parts = path.stem.split("-")
        # uuid is last 5 dash-joined chunks
        session_uuid = "-".join(parts[-5:])
    except Exception:
        session_uuid = path.stem

    session_id = f"codex:{session_uuid}"
    meta = SessionMeta(
        session_id=session_id,
        tool="codex",
        session_uuid=session_uuid,
        cwd=None,
        model=None,
        started_at=None,
        ended_at=None,
    )
    parsed = ParsedFile(session=meta)

    pending: dict[str, McpCallRow] = {}

    line_no = 0
    if start_offset > 0:
        with open(path, "rb") as g:
            line_no = g.read(start_offset).count(b"\n")

    with open(path, "rb") as f:
        f.seek(start_offset)
        buf = f.read()

    parts_split = buf.split(b"\n")
    consumed = 0
    for i, raw in enumerate(parts_split):
        is_last = (i == len(parts_split) - 1)
        if is_last and raw:
            break
        line_no += 1
        consumed += len(raw) + (1 if not is_last else 0)
        if not raw.strip():
            continue
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            continue

        ts = d.get("timestamp")
        if ts:
            if meta.started_at is None or ts < meta.started_at:
                meta.started_at = ts
            if meta.ended_at is None or ts > meta.ended_at:
                meta.ended_at = ts

        t = d.get("type")
        payload = d.get("payload") or {}

        if t == "session_meta":
            if not session_uuid and payload.get("id"):
                meta.session_uuid = payload["id"]
                meta.session_id = f"codex:{payload['id']}"
                session_id = meta.session_id
            if payload.get("cwd"):
                meta.cwd = payload["cwd"]

        elif t == "turn_context":
            if payload.get("cwd") and not meta.cwd:
                meta.cwd = payload["cwd"]
            if payload.get("model"):
                meta.model = payload["model"]

        elif t == "event_msg":
            ptype = payload.get("type")
            if ptype == "token_count":
                info = payload.get("info") or {}
                last = info.get("last_token_usage") or {}
                if not last:
                    continue
                input_tokens = int(last.get("input_tokens", 0) or 0)
                cached = int(last.get("cached_input_tokens", 0) or 0)
                output_tokens = int(last.get("output_tokens", 0) or 0)
                reasoning = int(last.get("reasoning_output_tokens", 0) or 0)
                # OpenAI usage convention: `input_tokens` already INCLUDES `cached_input_tokens`.
                # To match Claude's separate accounting, subtract: billed-uncached = input - cached.
                uncached_in = max(0, input_tokens - cached)
                row = MessageRow(
                    session_id=session_id,
                    tool="codex",
                    ts=ts or "",
                    model=meta.model,
                    input_tokens=uncached_in,
                    output_tokens=output_tokens,
                    cache_read=cached,
                    cache_write_5m=0,
                    cache_write_1h=0,
                    reasoning_tokens=reasoning,
                    source_file=str(path),
                    source_line=line_no,
                )
                parsed.messages.append(row)

        elif t == "response_item":
            ptype = payload.get("type")
            if ptype == "function_call":
                name = payload.get("name", "")
                call_id = payload.get("call_id") or f"line{line_no}"
                parsed_name = _mcp_parse_name(name)
                if parsed_name is None:
                    continue
                server, tool_name = parsed_name
                row = McpCallRow(
                    session_id=session_id,
                    tool="codex",
                    ts=ts or "",
                    server=server,
                    tool_name=tool_name,
                    call_id=call_id,
                    source_file=str(path),
                    source_line=line_no,
                )
                parsed.mcp_calls.append(row)
                pending[call_id] = row
            elif ptype == "function_call_output":
                call_id = payload.get("call_id")
                if call_id and call_id in pending:
                    out = payload.get("output")
                    if isinstance(out, str):
                        pending[call_id].result_chars = len(out)
                    elif out is not None:
                        pending[call_id].result_chars = len(json.dumps(out, default=str))

    return parsed, start_offset + consumed


def discover_files() -> list[Path]:
    if not CODEX_ROOT.exists():
        return []
    return sorted(CODEX_ROOT.glob("*/*/*/*.jsonl"))
