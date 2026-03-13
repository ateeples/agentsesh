"""Tests for Claude Code JSONL parser."""

import json
import tempfile
from pathlib import Path

import pytest
from sesh.parsers.claude_code import ClaudeCodeParser
from sesh.parsers import parse_transcript


def _write_jsonl(path: Path, lines: list[dict]) -> None:
    with open(path, "w") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")


def _make_assistant_tool_use(name: str, tool_id: str, input_data: dict, ts: str = "2026-03-12T10:00:00Z") -> dict:
    return {
        "type": "assistant",
        "timestamp": ts,
        "message": {
            "model": "claude-opus-4-6",
            "content": [
                {"type": "tool_use", "id": tool_id, "name": name, "input": input_data},
            ],
        },
    }


def _make_tool_result(tool_id: str, content: str, is_error: bool = False) -> dict:
    return {
        "type": "user",
        "message": {
            "content": [
                {"type": "tool_result", "tool_use_id": tool_id, "content": content, "is_error": is_error},
            ],
        },
    }


def _make_user_message(text: str, ts: str = "2026-03-12T10:00:00Z") -> dict:
    return {
        "type": "user",
        "timestamp": ts,
        "message": {"content": text},
    }


class TestCanParse:
    def test_valid_jsonl(self, tmp_path):
        p = tmp_path / "session.jsonl"
        _write_jsonl(p, [{"type": "user", "message": {"content": "hello"}}])
        assert ClaudeCodeParser.can_parse(p) is True

    def test_non_jsonl_extension(self, tmp_path):
        p = tmp_path / "session.json"
        p.write_text('{"type": "user"}')
        assert ClaudeCodeParser.can_parse(p) is False

    def test_no_message_types(self, tmp_path):
        p = tmp_path / "session.jsonl"
        _write_jsonl(p, [{"type": "queue-operation", "data": {}}] * 5)
        assert ClaudeCodeParser.can_parse(p) is False

    def test_message_after_queue_ops(self, tmp_path):
        """Parser should check first 20 lines, not just the first."""
        p = tmp_path / "session.jsonl"
        lines = [{"type": "queue-operation"}] * 10
        lines.append({"type": "assistant", "message": {"content": []}})
        _write_jsonl(p, lines)
        assert ClaudeCodeParser.can_parse(p) is True


class TestParse:
    def test_basic_session(self, tmp_path):
        p = tmp_path / "test-session.jsonl"
        _write_jsonl(p, [
            _make_user_message("Fix the bug"),
            _make_assistant_tool_use("Read", "t1", {"file_path": "/a.py"}, "2026-03-12T10:00:00Z"),
            _make_tool_result("t1", "def foo(): pass"),
            _make_assistant_tool_use("Edit", "t2", {"file_path": "/a.py", "old_string": "pass", "new_string": "return 42"}, "2026-03-12T10:01:00Z"),
            _make_tool_result("t2", "File updated"),
        ])
        session = ClaudeCodeParser.parse(p)
        assert session.session_id == "test-session"
        assert session.source_format == "claude_code"
        assert len(session.tool_calls) == 2
        assert session.tool_calls[0].name == "Read"
        assert session.tool_calls[1].name == "Edit"
        assert session.model == "claude-opus-4-6"
        assert session.duration_minutes is not None
        assert session.duration_minutes == 1.0

    def test_error_detection(self, tmp_path):
        p = tmp_path / "errors.jsonl"
        _write_jsonl(p, [
            _make_assistant_tool_use("Bash", "t1", {"command": "invalid"}, "2026-03-12T10:00:00Z"),
            _make_tool_result("t1", "Exit code 1\ncommand not found"),
            _make_assistant_tool_use("Read", "t2", {"file_path": "/nope"}, "2026-03-12T10:01:00Z"),
            _make_tool_result("t2", "Error: file not found", is_error=True),
        ])
        session = ClaudeCodeParser.parse(p)
        assert session.tool_calls[0].is_error is True
        assert session.tool_calls[1].is_error is True

    def test_events_extracted(self, tmp_path):
        p = tmp_path / "events.jsonl"
        _write_jsonl(p, [
            _make_user_message("Hello world"),
            {
                "type": "assistant",
                "timestamp": "2026-03-12T10:00:00Z",
                "message": {
                    "model": "claude-opus-4-6",
                    "content": [{"type": "text", "text": "I'll help with that."}],
                },
            },
        ])
        session = ClaudeCodeParser.parse(p)
        assert len(session.events) == 2
        assert session.events[0].type == "user_message"
        assert session.events[1].type == "assistant_text"

    def test_thinking_blocks(self, tmp_path):
        p = tmp_path / "thinking.jsonl"
        _write_jsonl(p, [
            {
                "type": "assistant",
                "timestamp": "2026-03-12T10:00:00Z",
                "message": {
                    "model": "claude-opus-4-6",
                    "content": [
                        {"type": "thinking", "thinking": "Let me think about this..."},
                        {"type": "text", "text": "Here's my answer."},
                    ],
                },
            },
        ])
        session = ClaudeCodeParser.parse(p)
        assert session.metadata["thinking_blocks"] == 1
        assert session.metadata["total_thinking_chars"] > 0


class TestAutoDetection:
    def test_auto_detects_claude_code(self, tmp_path):
        p = tmp_path / "session.jsonl"
        _write_jsonl(p, [
            _make_user_message("test"),
            _make_assistant_tool_use("Read", "t1", {"file_path": "/a.py"}),
            _make_tool_result("t1", "content"),
        ])
        session = parse_transcript(p)
        assert session.source_format == "claude_code"

    def test_unknown_format_raises(self, tmp_path):
        p = tmp_path / "weird.txt"
        p.write_text("not a transcript")
        with pytest.raises(ValueError, match="Cannot auto-detect"):
            parse_transcript(p)
