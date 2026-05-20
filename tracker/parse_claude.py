"""Parse ~/.claude/projects/*/*.jsonl session files.

Each line is a JSON record. We care about:
- `type=="assistant"`: has message.usage with token counts + message.content[] containing tool_use blocks.
- `type=="user"`: message.content may be a list with tool_result blocks (referencing tool_use_id).

We emit:
- message rows (one per assistant record with usage)
- mcp_call rows (one per `mcp__*` tool_use, enriched with the size of its later tool_result)
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

CLAUDE_ROOT = Path.home() / ".claude" / "projects"

EST_CHARS_PER_TOKEN = 4  # rough estimate; only used for MCP result size estimation


@dataclass
class MessageRow:
    session_id: str
    tool: str
    ts: str
    model: str | None
    input_tokens: int
    output_tokens: int
    cache_read: int
    cache_write_5m: int
    cache_write_1h: int
    reasoning_tokens: int
    source_file: str
    source_line: int
    agent_type: str | None = None
    agent_desc: str | None = None


@dataclass
class McpCallRow:
    session_id: str
    tool: str
    ts: str
    server: str
    tool_name: str
    call_id: str = ""
    result_chars: int = 0
    is_error: int = 0
    source_file: str = ""
    source_line: int = 0


@dataclass
class SessionMeta:
    session_id: str          # "claude:<uuid>"
    tool: str
    session_uuid: str
    cwd: str | None
    model: str | None
    started_at: str | None
    ended_at: str | None


@dataclass
class ParsedFile:
    session: SessionMeta
    messages: list[MessageRow] = field(default_factory=list)
    mcp_calls: list[McpCallRow] = field(default_factory=list)


def _mcp_parse_name(name: str) -> tuple[str, str] | None:
    """`mcp__<server>__<tool>` -> (server, tool). Returns None if not MCP."""
    if not name.startswith("mcp__"):
        return None
    rest = name[len("mcp__"):]
    sep = rest.find("__")
    if sep < 0:
        return rest, ""
    return rest[:sep], rest[sep + 2:]


def _tool_result_chars(content) -> tuple[int, int]:
    """Returns (chars, is_error_flag) for a tool_result block's content."""
    if content is None:
        return 0, 0
    if isinstance(content, str):
        return len(content), 0
    if isinstance(content, list):
        total = 0
        for blk in content:
            if isinstance(blk, dict):
                if "text" in blk and isinstance(blk["text"], str):
                    total += len(blk["text"])
                elif blk.get("type") == "image":
                    total += 1000  # approx for image refs
                else:
                    total += len(json.dumps(blk, default=str))
            elif isinstance(blk, str):
                total += len(blk)
        return total, 0
    return len(json.dumps(content, default=str)), 0


def _session_uuid_for(path: Path) -> str:
    """Sub-agent JSONLs live at <project>/<parent-session-uuid>/subagents/agent-*.jsonl
    and must roll up into the parent session. Top-level files use their own stem.
    """
    if path.parent.name == "subagents":
        return path.parent.parent.name
    return path.stem


def _agent_meta(path: Path) -> tuple[str | None, str | None]:
    """For a sub-agent JSONL, read its sibling agent-*.meta.json to get (agentType, description)."""
    if path.parent.name != "subagents":
        return None, None
    meta_path = path.with_suffix(".meta.json")
    if not meta_path.exists():
        return None, None
    try:
        m = json.loads(meta_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None, None
    return m.get("agentType"), m.get("description")


def parse_file(path: Path, *, start_offset: int = 0) -> tuple[ParsedFile, int]:
    """Parse from byte offset; returns (parsed, new_offset).

    new_offset is the byte position AFTER the last fully-parsed line. If a
    trailing partial line exists, we stop before it so the next run picks it up.
    """
    session_uuid = _session_uuid_for(path)
    session_id = f"claude:{session_uuid}"
    agent_type, agent_desc = _agent_meta(path)
    meta = SessionMeta(
        session_id=session_id,
        tool="claude",
        session_uuid=session_uuid,
        cwd=None,
        model=None,
        started_at=None,
        ended_at=None,
    )
    parsed = ParsedFile(session=meta)

    # tool_use_id -> (line_no_recorded, server, tool, message_obj) for later tool_result attribution.
    # We keep server/tool indexed by tool_use_id so when we see a tool_result later in the
    # file we can update the original mcp_call row's result_chars.
    pending: dict[str, McpCallRow] = {}

    line_no = 0
    last_complete_offset = start_offset

    with open(path, "rb") as f:
        f.seek(start_offset)
        # We re-derive 1-based line_no for the WHOLE file because source_line is the unique
        # constraint. So count lines from the beginning of the file when start_offset==0.
        # When resuming, we need to know what line we are on; we approximate by counting
        # lines in [0, start_offset) on demand.
        if start_offset > 0:
            with open(path, "rb") as g:
                head = g.read(start_offset)
                line_no = head.count(b"\n")

        buf = f.read()

    # Process complete lines only (one JSON per line). Split on b"\n"; any trailing
    # bytes without newline are a partial write — leave them for next run.
    parts = buf.split(b"\n")
    # If buf ends with \n, parts[-1] == b"" — that's fine, we consumed all of it.
    # Else parts[-1] is a partial line we must not consume.
    consumed = 0
    for i, raw in enumerate(parts):
        is_last = (i == len(parts) - 1)
        if is_last and raw:
            # partial line — stop
            break
        line_no += 1
        consumed += len(raw) + (1 if not is_last else 0)
        if not raw.strip():
            continue
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            continue

        t = d.get("type")
        ts = d.get("timestamp")
        cwd = d.get("cwd")
        if cwd and not meta.cwd:
            meta.cwd = cwd
        if ts:
            if meta.started_at is None or ts < meta.started_at:
                meta.started_at = ts
            if meta.ended_at is None or ts > meta.ended_at:
                meta.ended_at = ts

        if t == "assistant":
            msg = d.get("message", {}) or {}
            usage = msg.get("usage") or {}
            model = msg.get("model")
            if model == "<synthetic>":
                model = None
            if model:
                meta.model = model
            cache_creation = usage.get("cache_creation") or {}
            row = MessageRow(
                session_id=session_id,
                tool="claude",
                ts=ts or "",
                model=model or meta.model,
                input_tokens=int(usage.get("input_tokens", 0) or 0),
                output_tokens=int(usage.get("output_tokens", 0) or 0),
                cache_read=int(usage.get("cache_read_input_tokens", 0) or 0),
                cache_write_5m=int(cache_creation.get("ephemeral_5m_input_tokens", 0) or 0),
                cache_write_1h=int(cache_creation.get("ephemeral_1h_input_tokens", 0) or 0),
                reasoning_tokens=0,
                source_file=str(path),
                source_line=line_no,
                agent_type=agent_type,
                agent_desc=agent_desc,
            )
            # Some Claude records put cache_write under cache_creation_input_tokens with no split.
            if row.cache_write_5m == 0 and row.cache_write_1h == 0:
                row.cache_write_5m = int(usage.get("cache_creation_input_tokens", 0) or 0)
            parsed.messages.append(row)

            # Scan content for tool_use blocks (record MCP calls).
            for blk in msg.get("content", []) or []:
                if not isinstance(blk, dict):
                    continue
                if blk.get("type") == "tool_use":
                    name = blk.get("name", "")
                    parsed_name = _mcp_parse_name(name)
                    if parsed_name is None:
                        continue
                    server, tool_name = parsed_name
                    tu_id = blk.get("id") or f"line{line_no}-{len(parsed.mcp_calls)}"
                    mcp_row = McpCallRow(
                        session_id=session_id,
                        tool="claude",
                        ts=ts or "",
                        server=server,
                        tool_name=tool_name,
                        call_id=tu_id,
                        source_file=str(path),
                        source_line=line_no,
                    )
                    parsed.mcp_calls.append(mcp_row)
                    pending[tu_id] = mcp_row

        elif t == "user":
            msg = d.get("message", {}) or {}
            content = msg.get("content")
            if isinstance(content, list):
                for blk in content:
                    if isinstance(blk, dict) and blk.get("type") == "tool_result":
                        tu_id = blk.get("tool_use_id")
                        if tu_id and tu_id in pending:
                            row = pending[tu_id]
                            chars, _ = _tool_result_chars(blk.get("content"))
                            row.result_chars = chars
                            row.is_error = 1 if blk.get("is_error") else 0

    last_complete_offset = start_offset + consumed
    return parsed, last_complete_offset


def discover_files() -> list[Path]:
    if not CLAUDE_ROOT.exists():
        return []
    top = CLAUDE_ROOT.glob("*/*.jsonl")
    # Sub-agent (Task tool) sessions are written to <project>/<sessionId>/subagents/agent-*.jsonl.
    sub = CLAUDE_ROOT.glob("*/*/subagents/agent-*.jsonl")
    return sorted({*top, *sub})
