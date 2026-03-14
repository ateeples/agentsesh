"""Tests for OpenAI Codex CLI transcript parser.

Covers: format detection, session meta extraction, tool call parsing,
error detection from output text, auto-detect priority, and
edge cases (missing fields, empty sessions, interleaved types).
"""

import json
import tempfile
from pathlib import Path

import pytest

from sesh.parsers.openai_codex import OpenAICodexParser, _is_tool_error, _parse_tool_output
from sesh.parsers import auto_detect_parser, parse_transcript


# --- Test helpers ---


def _write_jsonl(lines: list[dict], path: Path):
    """Write a list of dicts as JSONL."""
    with open(path, "w") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")


def _make_session_meta(session_id="test-session-123"):
    return {
        "timestamp": "2026-01-15T10:00:00.000Z",
        "type": "session_meta",
        "payload": {
            "id": session_id,
            "timestamp": "2026-01-15T10:00:00.000Z",
            "cwd": "/tmp/test",
            "originator": "codex_cli",
            "cli_version": "0.100.0",
            "source": "cli",
            "model_provider": "openai",
        },
    }


def _make_turn_context(model="gpt-5.2-codex"):
    return {
        "timestamp": "2026-01-15T10:00:01.000Z",
        "type": "turn_context",
        "payload": {
            "turn_id": "turn-001",
            "cwd": "/tmp/test",
            "model": model,
        },
    }


def _make_function_call(name="exec_command", args='{"cmd":"ls"}', call_id="call_001",
                        timestamp="2026-01-15T10:01:00.000Z"):
    return {
        "timestamp": timestamp,
        "type": "response_item",
        "payload": {
            "type": "function_call",
            "name": name,
            "arguments": args,
            "call_id": call_id,
        },
    }


def _make_function_output(call_id="call_001", output="file1.txt\nfile2.txt\n",
                          timestamp="2026-01-15T10:01:01.000Z"):
    return {
        "timestamp": timestamp,
        "type": "response_item",
        "payload": {
            "type": "function_call_output",
            "call_id": call_id,
            "output": output,
        },
    }


def _make_custom_tool_call(name="apply_patch", input_text="*** Begin Patch\n...",
                           call_id="call_002", timestamp="2026-01-15T10:02:00.000Z"):
    return {
        "timestamp": timestamp,
        "type": "response_item",
        "payload": {
            "type": "custom_tool_call",
            "status": "completed",
            "call_id": call_id,
            "name": name,
            "input": input_text,
        },
    }


def _make_custom_tool_output(call_id="call_002", output=None,
                             timestamp="2026-01-15T10:02:01.000Z"):
    if output is None:
        output = json.dumps({"output": "Success. Updated file.txt\n", "metadata": {"exit_code": 0}})
    return {
        "timestamp": timestamp,
        "type": "response_item",
        "payload": {
            "type": "custom_tool_call_output",
            "call_id": call_id,
            "output": output,
        },
    }


def _make_reasoning(summary="Planning next step", timestamp="2026-01-15T10:00:30.000Z"):
    return {
        "timestamp": timestamp,
        "type": "response_item",
        "payload": {
            "type": "reasoning",
            "summary": [{"type": "summary_text", "text": summary}],
            "content": None,
            "encrypted_content": "...",
        },
    }


def _make_user_message(text="Fix the bug in main.py", timestamp="2026-01-15T10:00:00.000Z"):
    return {
        "timestamp": timestamp,
        "type": "event_msg",
        "payload": {
            "type": "user_message",
            "message": text,
            "images": [],
        },
    }


def _make_agent_message(text="Done. Fixed the bug.", timestamp="2026-01-15T10:05:00.000Z"):
    return {
        "timestamp": timestamp,
        "type": "event_msg",
        "payload": {
            "type": "agent_message",
            "message": text,
        },
    }


# --- Format detection ---


class TestCanParse:
    def test_valid_codex_transcript(self, tmp_path):
        f = tmp_path / "rollout-test.jsonl"
        _write_jsonl([_make_session_meta()], f)
        assert OpenAICodexParser.can_parse(f) is True

    def test_non_jsonl_extension(self, tmp_path):
        f = tmp_path / "session.json"
        _write_jsonl([_make_session_meta()], f)
        assert OpenAICodexParser.can_parse(f) is False

    def test_claude_code_not_claimed(self, tmp_path):
        """Claude Code transcripts should not be claimed by Codex parser."""
        f = tmp_path / "session.jsonl"
        _write_jsonl([
            {"type": "user", "message": {"content": "hello"}, "timestamp": "2026-01-01T00:00:00Z"},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}, "timestamp": "2026-01-01T00:00:01Z"},
        ], f)
        assert OpenAICodexParser.can_parse(f) is False

    def test_response_item_detected(self, tmp_path):
        """response_item with function_call should trigger detection."""
        f = tmp_path / "test.jsonl"
        _write_jsonl([_make_function_call()], f)
        assert OpenAICodexParser.can_parse(f) is True

    def test_empty_file(self, tmp_path):
        f = tmp_path / "empty.jsonl"
        f.write_text("")
        assert OpenAICodexParser.can_parse(f) is False


# --- Full parsing: tool calls, models, timing ---


class TestParse:
    def test_basic_session(self, tmp_path):
        f = tmp_path / "test.jsonl"
        _write_jsonl([
            _make_session_meta("my-session-id"),
            _make_turn_context("gpt-5.2-codex"),
            _make_user_message("Fix the bug"),
            _make_reasoning("Analyzing the code"),
            _make_function_call("exec_command", '{"cmd":"cat main.py"}', "call_001",
                                "2026-01-15T10:01:00.000Z"),
            _make_function_output("call_001", "def main(): pass\n",
                                  "2026-01-15T10:01:01.000Z"),
            _make_custom_tool_call("apply_patch", "*** patch ***", "call_002",
                                   "2026-01-15T10:02:00.000Z"),
            _make_custom_tool_output("call_002", timestamp="2026-01-15T10:02:01.000Z"),
            _make_agent_message("Fixed the bug."),
        ], f)
        session = OpenAICodexParser.parse(f)

        assert session.session_id == "my-session-id"
        assert session.source_format == "openai_codex"
        assert session.model == "gpt-5.2-codex"
        assert len(session.tool_calls) == 2
        assert session.tool_calls[0].name == "Bash"  # exec_command mapped
        assert session.tool_calls[1].name == "Edit"  # apply_patch mapped
        assert session.metadata["reasoning_blocks"] == 1

    def test_error_detection(self, tmp_path):
        f = tmp_path / "test.jsonl"
        _write_jsonl([
            _make_session_meta(),
            _make_function_call("exec_command", '{"cmd":"bad_cmd"}', "call_err"),
            _make_function_output("call_err",
                                  "Process exited with code 1\ncommand not found: bad_cmd"),
        ], f)
        session = OpenAICodexParser.parse(f)
        assert len(session.tool_calls) == 1
        assert session.tool_calls[0].is_error is True

    def test_custom_tool_error(self, tmp_path):
        f = tmp_path / "test.jsonl"
        error_output = json.dumps({"output": "Failed to apply patch", "metadata": {"exit_code": 1}})
        _write_jsonl([
            _make_session_meta(),
            _make_custom_tool_call("apply_patch", "bad patch", "call_err2"),
            _make_custom_tool_output("call_err2", error_output),
        ], f)
        session = OpenAICodexParser.parse(f)
        assert session.tool_calls[0].is_error is True

    def test_events_extracted(self, tmp_path):
        f = tmp_path / "test.jsonl"
        _write_jsonl([
            _make_session_meta(),
            _make_user_message("Do the thing"),
            _make_agent_message("Done."),
        ], f)
        session = OpenAICodexParser.parse(f)
        user_events = [e for e in session.events if e.type == "user_message"]
        assistant_events = [e for e in session.events if e.type == "assistant_text"]
        assert len(user_events) == 1
        assert user_events[0].preview == "Do the thing"
        assert len(assistant_events) == 1

    def test_duration_calculated(self, tmp_path):
        f = tmp_path / "test.jsonl"
        _write_jsonl([
            _make_session_meta(),
            {"timestamp": "2026-01-15T10:00:00.000Z", "type": "turn_context",
             "payload": {"turn_id": "t1", "model": "gpt-5"}},
            _make_function_call(timestamp="2026-01-15T10:30:00.000Z"),
            _make_function_output(timestamp="2026-01-15T11:00:00.000Z"),
        ], f)
        session = OpenAICodexParser.parse(f)
        assert session.duration_minutes == 60.0

    def test_tool_name_mapping(self, tmp_path):
        f = tmp_path / "test.jsonl"
        _write_jsonl([
            _make_session_meta(),
            _make_function_call("exec_command", '{"cmd":"ls"}', "c1"),
            _make_function_output("c1"),
            _make_function_call("request_user_input", '{"prompt":"ok?"}', "c2"),
            _make_function_output("c2", "yes"),
            _make_custom_tool_call("apply_patch", "patch", "c3"),
            _make_custom_tool_output("c3"),
        ], f)
        session = OpenAICodexParser.parse(f)
        assert session.tool_calls[0].name == "Bash"
        assert session.tool_calls[1].name == "AskUserQuestion"
        assert session.tool_calls[2].name == "Edit"

    def test_tool_categories(self, tmp_path):
        f = tmp_path / "test.jsonl"
        _write_jsonl([
            _make_session_meta(),
            _make_function_call("exec_command", '{"cmd":"ls"}', "c1"),
            _make_function_output("c1"),
        ], f)
        session = OpenAICodexParser.parse(f)
        assert "read" in session.tool_calls[0].categories


# --- Auto-detection dispatch with Claude Code priority ---


class TestAutoDetection:
    def test_auto_detects_codex(self, tmp_path):
        f = tmp_path / "rollout.jsonl"
        _write_jsonl([_make_session_meta()], f)
        parser = auto_detect_parser(f)
        assert parser is not None
        assert parser.format_name == "openai_codex"

    def test_parse_transcript_works(self, tmp_path):
        f = tmp_path / "rollout.jsonl"
        _write_jsonl([
            _make_session_meta("auto-test"),
            _make_turn_context(),
            _make_function_call(),
            _make_function_output(),
        ], f)
        session = parse_transcript(f)
        assert session.session_id == "auto-test"
        assert session.source_format == "openai_codex"

    def test_format_hint_works(self, tmp_path):
        f = tmp_path / "rollout.jsonl"
        _write_jsonl([_make_session_meta("hint-test")], f)
        session = parse_transcript(f, format_hint="openai_codex")
        assert session.session_id == "hint-test"


# --- Error detection helpers ---


class TestErrorHelpers:
    def test_exit_code_nonzero(self):
        assert _is_tool_error("Process exited with code 1\nfailed") is True

    def test_exit_code_zero(self):
        assert _is_tool_error("Process exited with code 0\nok") is False

    def test_command_not_found(self):
        assert _is_tool_error("command not found: foo") is True

    def test_clean_output(self):
        assert _is_tool_error("file1.txt\nfile2.txt") is False

    def test_parse_json_output_success(self):
        raw = json.dumps({"output": "Done", "metadata": {"exit_code": 0}})
        text, is_error = _parse_tool_output(raw)
        assert text == "Done"
        assert is_error is False

    def test_parse_json_output_failure(self):
        raw = json.dumps({"output": "Failed", "metadata": {"exit_code": 1}})
        text, is_error = _parse_tool_output(raw)
        assert text == "Failed"
        assert is_error is True

    def test_parse_plain_output(self):
        text, is_error = _parse_tool_output("just plain text")
        assert text == "just plain text"
        assert is_error is False
