"""Parser for OpenAI Codex CLI JSONL session transcripts.

Codex CLI stores transcripts as JSONL files in:
    ~/.codex/sessions/{year}/{month}/{day}/rollout-{timestamp}-{uuid}.jsonl

Each line is a JSON object with:
- type: "session_meta" | "turn_context" | "response_item" | "event_msg"
- timestamp: ISO 8601
- payload: type-specific data

response_item payload types:
- message (roles: developer, user, assistant)
- function_call / function_call_output (exec_command, write_stdin, request_user_input)
- custom_tool_call / custom_tool_call_output (apply_patch)
- reasoning (thinking blocks with optional summary)

event_msg payload types:
- user_message, agent_message, agent_reasoning
- task_started, task_complete, item_completed
- token_count
"""

import json
from datetime import datetime
from pathlib import Path

from .base import BaseParser, Event, NormalizedSession, ToolCall, classify_tool


# Map Codex tool names to AgentSesh-compatible names for classification
_CODEX_TOOL_MAP = {
    "exec_command": "Bash",
    "apply_patch": "Edit",
    "write_stdin": "Bash",
    "request_user_input": "AskUserQuestion",
}


def _is_tool_error(output_text: str) -> bool:
    """Determine if a tool result represents an error."""
    if not output_text:
        return False
    # Check for explicit exit code failures
    if "Process exited with code " in output_text:
        for line in output_text.split("\n"):
            if line.startswith("Process exited with code "):
                code = line.split("code ")[-1].strip()
                if code != "0":
                    return True
    # Check for common error signals
    first_line = output_text.split("\n")[0]
    for signal in ("error", "Error", "command not found", "No such file",
                   "Permission denied", "ENOENT", "EACCES", "fatal:"):
        if signal in first_line:
            return True
    return False


def _parse_tool_output(output_raw: str) -> tuple[str, bool]:
    """Parse tool output, handling both plain text and JSON-wrapped output.

    Returns (output_text, is_error).
    """
    # custom_tool_call_output wraps output in JSON
    try:
        parsed = json.loads(output_raw)
        if isinstance(parsed, dict) and "output" in parsed:
            text = parsed["output"]
            exit_code = parsed.get("metadata", {}).get("exit_code")
            return text, exit_code is not None and exit_code != 0
        return output_raw, _is_tool_error(output_raw)
    except (json.JSONDecodeError, TypeError):
        return output_raw, _is_tool_error(output_raw)


class OpenAICodexParser(BaseParser):
    """Parser for OpenAI Codex CLI JSONL session transcripts."""

    format_name = "openai_codex"

    @staticmethod
    def can_parse(file_path: Path) -> bool:
        """Check if file is a Codex CLI JSONL transcript.

        Looks for session_meta or response_item types in first 20 lines.
        Also checks filename pattern (rollout-*.jsonl).
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
                    if d.get("type") in ("session_meta", "turn_context"):
                        return True
                    if d.get("type") == "response_item":
                        payload = d.get("payload", {})
                        if payload.get("type") in ("message", "function_call",
                                                    "custom_tool_call", "reasoning"):
                            return True
            return False
        except OSError:
            return False

    @staticmethod
    def parse(file_path: Path) -> NormalizedSession:
        """Parse a Codex CLI JSONL transcript into NormalizedSession."""
        # Collect raw data
        function_calls: list[dict] = []
        function_results: dict[str, dict] = {}
        events: list[Event] = []
        raw_parts: list[str] = []
        model_name: str | None = None
        session_id: str | None = None
        reasoning_blocks: list[dict] = []
        first_timestamp: str | None = None
        last_timestamp: str | None = None

        with open(file_path) as f:
            for line in f:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue

                event_type = d.get("type", "?")
                timestamp = d.get("timestamp")

                # Track timestamps for duration
                if timestamp:
                    if not first_timestamp:
                        first_timestamp = timestamp
                    last_timestamp = timestamp

                if event_type == "session_meta":
                    payload = d.get("payload", {})
                    session_id = payload.get("id", file_path.stem)

                elif event_type == "turn_context":
                    payload = d.get("payload", {})
                    if not model_name and payload.get("model"):
                        model_name = payload["model"]

                elif event_type == "response_item":
                    payload = d.get("payload", {})
                    ptype = payload.get("type", "")

                    if ptype == "message":
                        role = payload.get("role", "")
                        content = payload.get("content", [])
                        if isinstance(content, list):
                            text = " ".join(
                                b.get("text", "")
                                for b in content
                                if isinstance(b, dict)
                                and b.get("type") in ("input_text", "output_text", "text")
                            )
                        elif isinstance(content, str):
                            text = content
                        else:
                            text = ""

                        if text.strip():
                            if role == "user":
                                events.append(Event(
                                    type="user_message",
                                    length=len(text),
                                    preview=text[:120],
                                    timestamp=timestamp,
                                ))
                            elif role == "assistant":
                                events.append(Event(
                                    type="assistant_text",
                                    length=len(text),
                                    timestamp=timestamp,
                                ))
                            # developer messages are system prompts — skip for events
                            raw_parts.append(text)

                    elif ptype == "function_call":
                        function_calls.append({
                            "name": payload.get("name", "?"),
                            "call_id": payload.get("call_id", ""),
                            "arguments": payload.get("arguments", ""),
                            "timestamp": timestamp,
                        })

                    elif ptype == "function_call_output":
                        call_id = payload.get("call_id", "")
                        output = payload.get("output", "")
                        text, is_error = output, _is_tool_error(output)
                        function_results[call_id] = {
                            "content_preview": text[:300],
                            "is_error": is_error,
                            "full_length": len(text),
                        }

                    elif ptype == "custom_tool_call":
                        function_calls.append({
                            "name": payload.get("name", "?"),
                            "call_id": payload.get("call_id", ""),
                            "arguments": payload.get("input", ""),
                            "timestamp": timestamp,
                        })

                    elif ptype == "custom_tool_call_output":
                        call_id = payload.get("call_id", "")
                        output_raw = payload.get("output", "")
                        text, is_error = _parse_tool_output(output_raw)
                        function_results[call_id] = {
                            "content_preview": text[:300],
                            "is_error": is_error,
                            "full_length": len(text),
                        }

                    elif ptype == "reasoning":
                        summary = payload.get("summary", [])
                        summary_text = ""
                        if isinstance(summary, list):
                            summary_text = " ".join(
                                b.get("text", "")
                                for b in summary
                                if isinstance(b, dict)
                            )
                        reasoning_blocks.append({
                            "length": len(summary_text),
                            "timestamp": timestamp,
                        })

                elif event_type == "event_msg":
                    payload = d.get("payload", {})
                    emsg_type = payload.get("type", "")

                    if emsg_type == "user_message":
                        text = payload.get("message", "")
                        if text.strip():
                            events.append(Event(
                                type="user_message",
                                length=len(text),
                                preview=text[:120],
                                timestamp=timestamp,
                            ))
                            raw_parts.append(text)

                    elif emsg_type == "agent_message":
                        text = payload.get("message", "")
                        if text.strip():
                            events.append(Event(
                                type="assistant_text",
                                length=len(text),
                                timestamp=timestamp,
                            ))
                            raw_parts.append(text)

        # Build ToolCall objects, mapping Codex tool names to AgentSesh categories
        tool_calls: list[ToolCall] = []
        for seq, fc in enumerate(function_calls):
            result = function_results.get(fc["call_id"], {})
            codex_name = fc["name"]
            mapped_name = _CODEX_TOOL_MAP.get(codex_name, codex_name)

            # Parse arguments
            args = fc["arguments"]
            if isinstance(args, str):
                try:
                    input_data = json.loads(args)
                except (json.JSONDecodeError, TypeError):
                    input_data = {"raw": args}
            elif isinstance(args, dict):
                input_data = args
            else:
                input_data = {"raw": str(args)}

            tc = ToolCall(
                name=mapped_name,
                tool_id=fc["call_id"],
                input_data=input_data,
                output_preview=result.get("content_preview", ""),
                output_length=result.get("full_length", 0),
                is_error=result.get("is_error", False),
                timestamp=fc["timestamp"],
                categories=classify_tool(mapped_name),
                seq=seq,
            )
            tool_calls.append(tc)

        # Compute duration
        duration_minutes = None
        if first_timestamp and last_timestamp:
            try:
                start = datetime.fromisoformat(
                    first_timestamp.replace("Z", "+00:00")
                )
                end = datetime.fromisoformat(
                    last_timestamp.replace("Z", "+00:00")
                )
                duration_minutes = round((end - start).total_seconds() / 60, 1)
            except (ValueError, TypeError):
                pass

        return NormalizedSession(
            session_id=session_id or file_path.stem,
            source_format="openai_codex",
            source_path=str(file_path),
            start_time=first_timestamp,
            end_time=last_timestamp,
            duration_minutes=duration_minutes,
            model=model_name,
            tool_calls=tool_calls,
            events=events,
            raw_text="\n".join(raw_parts),
            metadata={
                "reasoning_blocks": len(reasoning_blocks),
                "total_reasoning_chars": sum(
                    rb["length"] for rb in reasoning_blocks
                ),
            },
        )
