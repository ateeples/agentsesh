"""Parser for Claude Code JSONL session transcripts.

Claude Code stores transcripts as JSONL files in:
    ~/.claude/projects/{encoded-path}/*.jsonl

Each line is a JSON object with:
- type: "user" | "assistant" | "system"
- message.content: array of blocks (text, tool_use, tool_result, thinking)
- timestamp: ISO 8601
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from .base import BaseParser, Event, NormalizedSession, ToolCall, classify_tool


def _is_tool_error(is_error_flag: bool, content_text: str) -> bool:
    """Determine if a tool result represents an actual error.

    The is_error flag from Claude's API isn't always set (some tools return
    error text in content without flagging it), so we also check for
    common error signals in the output text.
    """
    # Explicit error flag from the API
    if is_error_flag:
        return True
    # Exit code prefix from Bash tool results
    if content_text.startswith("Exit code"):
        return True
    # Generic error prefix
    if content_text.startswith("Error:"):
        return True
    # Check first line for OS-level error signals
    first_line = content_text.split("\n")[0] if content_text else ""
    for signal in ("command not found", "No such file", "Permission denied", "ENOENT", "EACCES"):
        if signal in first_line:
            return True
    return False


def _extract_text_from_content(content: list | str) -> str:
    """Extract text from tool result content (can be list of blocks or string)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return str(content)


class ClaudeCodeParser(BaseParser):
    """Parser for Claude Code JSONL session transcripts."""

    format_name = "claude_code"

    @staticmethod
    def can_parse(file_path: Path) -> bool:
        """Check if file is a Claude Code JSONL transcript.

        Checks first 20 lines for any user/assistant/system message type,
        since transcripts can start with queue-operation or other meta lines.
        """
        if file_path.suffix != ".jsonl":
            return False
        try:
            with open(file_path) as f:
                for _ in range(20):
                    line = f.readline().strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if d.get("type") in ("user", "assistant", "system"):
                        return True
            return False
        except OSError:
            return False

    @staticmethod
    def parse(file_path: Path) -> NormalizedSession:
        """Parse a Claude Code JSONL transcript into NormalizedSession."""
        # Two-pass approach: collect tool_use blocks and tool_result blocks
        # separately, then match them by tool_use_id to build ToolCall objects.
        tool_uses: list[dict] = []        # Ordered list of tool invocations
        tool_results: dict[str, dict] = {} # tool_use_id → result data
        events: list[Event] = []           # Non-tool events (user messages, assistant text)
        raw_parts: list[str] = []          # Text content for FTS indexing
        model_name: str | None = None
        thinking_blocks: list[dict] = []   # Metadata for thinking block analysis

        with open(file_path) as f:
            for line_num, line in enumerate(f):
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue

                msg_type = d.get("type", "?")
                timestamp = d.get("timestamp")

                if msg_type == "assistant":
                    msg = d.get("message", {})

                    # Capture model name
                    if not model_name and msg.get("model"):
                        model_name = msg["model"]

                    content = msg.get("content", [])
                    if isinstance(content, list):
                        for block in content:
                            if not isinstance(block, dict):
                                continue

                            if block.get("type") == "tool_use":
                                tool_uses.append({
                                    "name": block.get("name", "?"),
                                    "id": block.get("id", ""),
                                    "input": block.get("input", {}),
                                    "timestamp": timestamp,
                                    "line": line_num,
                                })
                            elif block.get("type") == "text":
                                text = block.get("text", "")
                                if text.strip():
                                    events.append(Event(
                                        type="assistant_text",
                                        length=len(text),
                                        timestamp=timestamp,
                                    ))
                                    raw_parts.append(text)
                            elif block.get("type") == "thinking":
                                thinking_text = block.get("thinking", "")
                                thinking_blocks.append({
                                    "length": len(thinking_text),
                                    "timestamp": timestamp,
                                })

                elif msg_type == "user":
                    msg = d.get("message", {})
                    content = msg.get("content", "")

                    if isinstance(content, list):
                        text_parts = []
                        for block in content:
                            if not isinstance(block, dict):
                                continue

                            if block.get("type") == "tool_result":
                                tool_id = block.get("tool_use_id", "")
                                is_err_flag = block.get("is_error", False)
                                rc = block.get("content", "")
                                rc_text = _extract_text_from_content(rc)
                                tool_results[tool_id] = {
                                    "content_preview": rc_text[:300],
                                    "is_error": _is_tool_error(is_err_flag, rc_text),
                                    "full_length": len(rc_text),
                                }
                            elif block.get("type") == "text":
                                text_parts.append(block.get("text", ""))

                        content = " ".join(text_parts)

                    if isinstance(content, str) and content.strip():
                        events.append(Event(
                            type="user_message",
                            length=len(content),
                            preview=content[:120],
                            timestamp=timestamp,
                        ))
                        raw_parts.append(content)

        # Match tool_use → tool_result by ID and build ToolCall objects
        tool_calls: list[ToolCall] = []
        for seq, tu in enumerate(tool_uses):
            result = tool_results.get(tu["id"], {})
            tc = ToolCall(
                name=tu["name"],
                tool_id=tu["id"],
                input_data=tu["input"],
                output_preview=result.get("content_preview", ""),
                output_length=result.get("full_length", 0),
                is_error=result.get("is_error", False),
                timestamp=tu["timestamp"],
                categories=classify_tool(tu["name"]),
                seq=seq,
            )
            tool_calls.append(tc)

        # Derive session timing from first/last tool call timestamps
        timestamps = [tc.timestamp for tc in tool_calls if tc.timestamp]
        start_time = timestamps[0] if timestamps else None
        end_time = timestamps[-1] if len(timestamps) >= 2 else None
        duration_minutes = None
        if start_time and end_time:
            try:
                start = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
                end = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
                duration_minutes = round((end - start).total_seconds() / 60, 1)
            except (ValueError, TypeError):
                pass

        return NormalizedSession(
            session_id=file_path.stem,
            source_format="claude_code",
            source_path=str(file_path),
            start_time=start_time,
            end_time=end_time,
            duration_minutes=duration_minutes,
            model=model_name,
            tool_calls=tool_calls,
            events=events,
            raw_text="\n".join(raw_parts),
            metadata={
                "thinking_blocks": len(thinking_blocks),
                "total_thinking_chars": sum(tb["length"] for tb in thinking_blocks),
            },
        )
