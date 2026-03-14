"""Session replay — step-by-step timeline reconstruction.

Builds a chronological timeline from either the original JSONL source file
(full fidelity) or the database (tool calls only, truncated output).
The ultimate debugging tool: see exactly what happened, in order,
with pattern annotations showing where things went wrong.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

from .parsers.base import ToolCall, Pattern, classify_tool


@dataclass
class ReplayStep:
    """A single step in the session timeline."""

    seq: int
    type: str  # "user", "assistant", "tool_call", "thinking"
    timestamp: str | None = None
    # Display fields
    summary: str = ""  # One-line summary
    detail: str = ""  # Full content (for --verbose)
    # Tool-specific fields
    tool_name: str | None = None
    tool_input: dict | None = None
    tool_output: str = ""
    is_error: bool = False
    # Annotations added by annotate_timeline
    annotations: list[str] = field(default_factory=list)


def build_timeline_from_source(source_path: str) -> list[ReplayStep]:
    """Build full timeline by re-parsing the original JSONL file.

    Returns steps in chronological order, interleaving user messages,
    assistant text, thinking blocks, and tool calls (paired with results).
    """
    path = Path(source_path)
    if not path.exists():
        return []

    # First pass: collect tool results keyed by tool_use_id
    tool_results: dict[str, dict] = {}
    with open(path) as f:
        for line in f:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("type") != "user":
                continue
            content = d.get("message", {}).get("content", [])
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    tool_id = block.get("tool_use_id", "")
                    is_err = block.get("is_error", False)
                    rc = block.get("content", "")
                    rc_text = _extract_text(rc)
                    tool_results[tool_id] = {
                        "output": rc_text,
                        "is_error": _is_tool_error(is_err, rc_text),
                    }

    # Second pass: build timeline in order
    steps: list[ReplayStep] = []
    seq = 0

    with open(path) as f:
        for line in f:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg_type = d.get("type", "")
            timestamp = d.get("timestamp")

            if msg_type == "assistant":
                content = d.get("message", {}).get("content", [])
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict):
                        continue

                    if block.get("type") == "thinking":
                        text = block.get("thinking", "")
                        if text.strip():
                            steps.append(ReplayStep(
                                seq=seq,
                                type="thinking",
                                timestamp=timestamp,
                                summary=f"[thinking] {len(text)} chars",
                                detail=text,
                            ))
                            seq += 1

                    elif block.get("type") == "text":
                        text = block.get("text", "")
                        if text.strip():
                            steps.append(ReplayStep(
                                seq=seq,
                                type="assistant",
                                timestamp=timestamp,
                                summary=_truncate(text, 120),
                                detail=text,
                            ))
                            seq += 1

                    elif block.get("type") == "tool_use":
                        tool_name = block.get("name", "?")
                        tool_id = block.get("id", "")
                        tool_input = block.get("input", {})
                        result = tool_results.get(tool_id, {})
                        output = result.get("output", "")
                        is_error = result.get("is_error", False)

                        steps.append(ReplayStep(
                            seq=seq,
                            type="tool_call",
                            timestamp=timestamp,
                            summary=_tool_summary(tool_name, tool_input, is_error),
                            detail=output,
                            tool_name=tool_name,
                            tool_input=tool_input,
                            tool_output=output,
                            is_error=is_error,
                        ))
                        seq += 1

            elif msg_type == "user":
                content = d.get("message", {}).get("content", "")
                # Extract user text (skip tool_result blocks)
                if isinstance(content, list):
                    text_parts = []
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text_parts.append(block.get("text", ""))
                    content = " ".join(text_parts)

                if isinstance(content, str) and content.strip():
                    steps.append(ReplayStep(
                        seq=seq,
                        type="user",
                        timestamp=timestamp,
                        summary=_truncate(content, 120),
                        detail=content,
                    ))
                    seq += 1

    return steps


def build_timeline_from_db(tool_calls: list[dict]) -> list[ReplayStep]:
    """Build timeline from DB tool calls (fallback when source file is gone).

    Only has tool calls — no user messages or assistant text.
    Output is truncated to 300 chars (what the DB stores).
    """
    steps: list[ReplayStep] = []
    for tc in tool_calls:
        input_data = tc.get("input_json", "{}")
        if isinstance(input_data, str):
            try:
                input_data = json.loads(input_data)
            except json.JSONDecodeError:
                input_data = {}

        is_error = bool(tc.get("is_error", False))
        tool_name = tc["name"]
        output = tc.get("output_preview", "")

        steps.append(ReplayStep(
            seq=tc["seq"],
            type="tool_call",
            timestamp=tc.get("timestamp"),
            summary=_tool_summary(tool_name, input_data, is_error),
            detail=output,
            tool_name=tool_name,
            tool_input=input_data,
            tool_output=output,
            is_error=is_error,
        ))

    return steps


def build_timeline(
    tool_calls: list[dict],
    source_path: str | None = None,
) -> tuple[list[ReplayStep], str]:
    """Build timeline, preferring source file for full fidelity.

    Returns (steps, source) where source is "file" or "db".
    """
    if source_path:
        steps = build_timeline_from_source(source_path)
        if steps:
            return steps, "file"

    return build_timeline_from_db(tool_calls), "db"


def annotate_timeline(
    steps: list[ReplayStep],
    patterns: list[Pattern | dict],
) -> None:
    """Add pattern annotations to timeline steps in-place.

    Maps pattern tool_indices back to the corresponding tool_call steps.
    """
    # Build index: tool_call seq -> step index
    tool_seq_to_idx: dict[int, int] = {}
    tool_seq = 0
    for i, step in enumerate(steps):
        if step.type == "tool_call":
            tool_seq_to_idx[tool_seq] = i
            tool_seq += 1

    for p in patterns:
        if isinstance(p, dict):
            ptype = p.get("type", "")
            severity = p.get("severity", "info")
            detail = p.get("detail", "")
            # DB patterns don't store tool_indices — annotate nothing
            continue
        else:
            ptype = p.type
            severity = p.severity
            detail = p.detail
            indices = p.tool_indices

        icon = {"warning": "!!", "concern": "!", "info": "~"}.get(severity, "~")
        label = f"[{icon}] {ptype}: {detail}"

        for tool_idx in indices:
            step_idx = tool_seq_to_idx.get(tool_idx)
            if step_idx is not None:
                steps[step_idx].annotations.append(label)


def filter_steps(
    steps: list[ReplayStep],
    errors_only: bool = False,
    tools_only: bool = False,
    step_range: tuple[int, int] | None = None,
    tool_filter: str | None = None,
) -> list[ReplayStep]:
    """Filter timeline steps."""
    result = steps

    if step_range:
        start, end = step_range
        result = [s for s in result if start <= s.seq <= end]

    if errors_only:
        result = [s for s in result if s.is_error]

    if tools_only:
        result = [s for s in result if s.type == "tool_call"]

    if tool_filter:
        names = {n.strip() for n in tool_filter.split(",")}
        result = [s for s in result if s.type != "tool_call" or s.tool_name in names]

    return result


def format_replay(
    steps: list[ReplayStep],
    session: dict,
    source: str = "db",
    compact: bool = False,
    verbose: bool = False,
) -> str:
    """Format timeline as human-readable replay output."""
    lines: list[str] = []

    # Header
    sid = session.get("id", "?")[:16]
    grade = session.get("grade", "?")
    score = session.get("score", 0)
    duration = session.get("duration_minutes")
    model = session.get("model", "?")
    total_tools = session.get("tool_call_count", 0)
    total_errors = session.get("error_count", 0)

    lines.append(f"{'=' * 60}")
    lines.append(f"  Session Replay: {sid}... ({grade}, {score}pts)")
    dur_str = f"{duration:.0f}min" if duration else "?min"
    lines.append(f"  {dur_str} | {total_tools} calls | {total_errors} errors | {model or '?'}")
    source_label = "full transcript" if source == "file" else "database (tool calls only)"
    lines.append(f"  Source: {source_label}")
    lines.append(f"{'=' * 60}")
    lines.append("")

    for step in steps:
        lines.append(_format_step(step, compact=compact, verbose=verbose))

    lines.append("")
    lines.append(f"{'=' * 60}")
    lines.append(f"  {len(steps)} steps shown")

    # Count annotations
    ann_count = sum(len(s.annotations) for s in steps)
    if ann_count:
        lines.append(f"  {ann_count} pattern annotation(s)")

    lines.append(f"{'=' * 60}")

    return "\n".join(lines)


def _format_step(step: ReplayStep, compact: bool = False, verbose: bool = False) -> str:
    """Format a single timeline step."""
    lines: list[str] = []
    ts = _format_ts(step.timestamp)

    if step.type == "user":
        lines.append(f"[{step.seq:3d}] {ts}  USER    {step.summary}")
        if verbose and step.detail != step.summary:
            for line in step.detail.split("\n")[:10]:
                lines.append(f"              {line}")

    elif step.type == "assistant":
        lines.append(f"[{step.seq:3d}] {ts}  AGENT   {step.summary}")
        if verbose and step.detail != step.summary:
            for line in step.detail.split("\n")[:10]:
                lines.append(f"              {line}")

    elif step.type == "thinking":
        if not compact:
            lines.append(f"[{step.seq:3d}] {ts}  THINK   {step.summary}")

    elif step.type == "tool_call":
        status = "x" if step.is_error else "+"
        lines.append(f"[{step.seq:3d}] {ts}  [{status}] {step.summary}")

        if not compact and step.tool_output:
            # Show output preview
            preview = step.tool_output
            if not verbose:
                preview = _truncate(preview, 200)
            if preview.strip():
                for line in preview.split("\n")[:5 if not verbose else 20]:
                    lines.append(f"              {line}")

    # Annotations
    for ann in step.annotations:
        lines.append(f"              >>> {ann}")

    return "\n".join(lines)


def parse_range(range_str: str) -> tuple[int, int]:
    """Parse a range string like '5-15' into (5, 15)."""
    parts = range_str.split("-")
    if len(parts) == 2:
        start, end = int(parts[0]), int(parts[1])
        if start > end:
            raise ValueError(f"Range start must be <= end: {range_str}")
        return start, end
    elif len(parts) == 1:
        n = int(parts[0])
        return n, n
    raise ValueError(f"Invalid range: {range_str}")


# --- Internal helpers ---

def _truncate(text: str, max_len: int) -> str:
    """Truncate text to max_len, collapsing whitespace."""
    text = " ".join(text.split())
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def _format_ts(ts: str | None) -> str:
    """Format timestamp as HH:MM:SS or blank."""
    if not ts:
        return "        "
    # ISO 8601: extract time portion
    try:
        if "T" in ts:
            time_part = ts.split("T")[1]
            return time_part[:8].ljust(8)
    except (IndexError, ValueError):
        pass
    return "        "


def _tool_summary(name: str, input_data: dict, is_error: bool) -> str:
    """One-line summary of a tool call."""
    err = " [ERROR]" if is_error else ""

    if name == "Read":
        path = input_data.get("file_path", "?")
        return f"Read{err} -> {_short_path(path)}"

    elif name in ("Grep", "Glob"):
        pattern = input_data.get("pattern", "?")
        path = input_data.get("path", "")
        where = f" in {_short_path(path)}" if path else ""
        return f'{name}{err} -> "{pattern}"{where}'

    elif name == "Bash":
        cmd = input_data.get("command", "?")
        return f"Bash{err} -> {_truncate(cmd, 80)}"

    elif name in ("Write", "Edit"):
        path = input_data.get("file_path", "?")
        return f"{name}{err} -> {_short_path(path)}"

    elif name == "Agent":
        desc = input_data.get("description", input_data.get("prompt", "?"))
        return f"Agent{err} -> {_truncate(str(desc), 60)}"

    elif name == "Skill":
        skill = input_data.get("skill", "?")
        return f"Skill{err} -> {skill}"

    elif name == "WebFetch":
        url = input_data.get("url", "?")
        return f"WebFetch{err} -> {_truncate(url, 80)}"

    elif name == "WebSearch":
        query = input_data.get("query", "?")
        return f'WebSearch{err} -> "{query}"'

    else:
        s = str(input_data)
        return f"{name}{err} -> {_truncate(s, 60)}"


def _short_path(path: str) -> str:
    """Shorten a file path for display — show last 3 components."""
    parts = Path(path).parts
    if len(parts) <= 3:
        return path
    return ".../" + "/".join(parts[-3:])


def _extract_text(content) -> str:
    """Extract text from tool result content."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return str(content)


def _is_tool_error(is_error_flag: bool, content_text: str) -> bool:
    """Determine if a tool result represents an actual error."""
    if is_error_flag:
        return True
    if content_text.startswith("Exit code"):
        return True
    if content_text.startswith("Error:"):
        return True
    first_line = content_text.split("\n")[0] if content_text else ""
    for signal in ("command not found", "No such file", "Permission denied", "ENOENT", "EACCES"):
        if signal in first_line:
            return True
    return False
