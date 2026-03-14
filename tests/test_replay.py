"""Tests for session replay timeline builder and formatter."""

import json
import tempfile
from pathlib import Path

import pytest

from sesh.replay import (
    ReplayStep,
    build_timeline_from_source,
    build_timeline_from_db,
    build_timeline,
    annotate_timeline,
    filter_steps,
    format_replay,
    parse_range,
)
from sesh.parsers.base import Pattern


def _tc(name, input_data=None, is_error=False, output_preview="", seq=0, timestamp=None):
    """Helper to create a tool call dict matching DB format."""
    return {
        "name": name,
        "input_json": json.dumps(input_data or {}),
        "is_error": int(is_error),
        "output_preview": output_preview,
        "seq": seq,
        "timestamp": timestamp,
        "tool_id": f"tool_{seq}",
        "output_length": len(output_preview),
        "categories": "read",
    }


def _jsonl_line(msg_type, content, timestamp="2026-03-13T14:00:00Z", model=None):
    """Build a Claude Code JSONL line."""
    d = {"type": msg_type, "timestamp": timestamp}
    if msg_type == "assistant":
        msg = {"content": content}
        if model:
            msg["model"] = model
        d["message"] = msg
    elif msg_type == "user":
        d["message"] = {"content": content}
    return json.dumps(d)


def _write_jsonl(lines):
    """Write JSONL lines to a temp file and return the path."""
    f = tempfile.NamedTemporaryFile(suffix=".jsonl", mode="w", delete=False)
    for line in lines:
        f.write(line + "\n")
    f.close()
    return f.name


class TestBuildTimelineFromDB:
    def test_empty(self):
        steps = build_timeline_from_db([])
        assert steps == []

    def test_single_tool_call(self):
        calls = [_tc("Read", {"file_path": "/a.py"}, seq=0)]
        steps = build_timeline_from_db(calls)
        assert len(steps) == 1
        assert steps[0].type == "tool_call"
        assert steps[0].tool_name == "Read"
        assert "/a.py" in steps[0].summary

    def test_preserves_order(self):
        calls = [
            _tc("Read", {"file_path": "/a.py"}, seq=0),
            _tc("Edit", {"file_path": "/a.py"}, seq=1),
            _tc("Bash", {"command": "pytest"}, seq=2),
        ]
        steps = build_timeline_from_db(calls)
        assert len(steps) == 3
        assert [s.tool_name for s in steps] == ["Read", "Edit", "Bash"]
        assert [s.seq for s in steps] == [0, 1, 2]

    def test_error_flagging(self):
        calls = [
            _tc("Edit", {"file_path": "/a.py"}, is_error=True, seq=0),
        ]
        steps = build_timeline_from_db(calls)
        assert steps[0].is_error is True
        assert "[ERROR]" in steps[0].summary

    def test_tool_summary_formats(self):
        calls = [
            _tc("Grep", {"pattern": "TODO", "path": "/src"}, seq=0),
            _tc("Glob", {"pattern": "*.py"}, seq=1),
            _tc("Agent", {"description": "explore codebase"}, seq=2),
            _tc("Skill", {"skill": "commit"}, seq=3),
        ]
        steps = build_timeline_from_db(calls)
        assert '"TODO"' in steps[0].summary
        assert '"*.py"' in steps[1].summary
        assert "explore codebase" in steps[2].summary
        assert "commit" in steps[3].summary


class TestBuildTimelineFromSource:
    def test_empty_file(self):
        path = _write_jsonl([])
        steps = build_timeline_from_source(path)
        assert steps == []
        Path(path).unlink()

    def test_nonexistent_file(self):
        steps = build_timeline_from_source("/nonexistent/path.jsonl")
        assert steps == []

    def test_user_message(self):
        lines = [
            _jsonl_line("user", "Fix the login bug", timestamp="2026-03-13T14:00:00Z"),
        ]
        path = _write_jsonl(lines)
        steps = build_timeline_from_source(path)
        assert len(steps) == 1
        assert steps[0].type == "user"
        assert "Fix the login bug" in steps[0].summary
        Path(path).unlink()

    def test_assistant_text(self):
        lines = [
            _jsonl_line("assistant", [
                {"type": "text", "text": "I'll fix the bug."},
            ]),
        ]
        path = _write_jsonl(lines)
        steps = build_timeline_from_source(path)
        assert len(steps) == 1
        assert steps[0].type == "assistant"
        assert "fix the bug" in steps[0].summary
        Path(path).unlink()

    def test_tool_call_with_result(self):
        tool_id = "tool_123"
        lines = [
            _jsonl_line("assistant", [
                {"type": "tool_use", "name": "Read", "id": tool_id, "input": {"file_path": "/a.py"}},
            ], timestamp="2026-03-13T14:01:00Z"),
            _jsonl_line("user", [
                {"type": "tool_result", "tool_use_id": tool_id, "content": "file contents here"},
            ], timestamp="2026-03-13T14:01:01Z"),
        ]
        path = _write_jsonl(lines)
        steps = build_timeline_from_source(path)
        # Should have 1 step: the paired tool call
        tool_steps = [s for s in steps if s.type == "tool_call"]
        assert len(tool_steps) == 1
        assert tool_steps[0].tool_name == "Read"
        assert tool_steps[0].tool_output == "file contents here"
        assert not tool_steps[0].is_error
        Path(path).unlink()

    def test_tool_error_detection(self):
        tool_id = "tool_err"
        lines = [
            _jsonl_line("assistant", [
                {"type": "tool_use", "name": "Bash", "id": tool_id, "input": {"command": "cat /nope"}},
            ]),
            _jsonl_line("user", [
                {"type": "tool_result", "tool_use_id": tool_id, "is_error": True, "content": "No such file"},
            ]),
        ]
        path = _write_jsonl(lines)
        steps = build_timeline_from_source(path)
        tool_steps = [s for s in steps if s.type == "tool_call"]
        assert tool_steps[0].is_error is True
        Path(path).unlink()

    def test_thinking_block(self):
        lines = [
            _jsonl_line("assistant", [
                {"type": "thinking", "thinking": "Let me analyze this..."},
                {"type": "text", "text": "Here's what I found."},
            ]),
        ]
        path = _write_jsonl(lines)
        steps = build_timeline_from_source(path)
        types = [s.type for s in steps]
        assert "thinking" in types
        assert "assistant" in types
        Path(path).unlink()

    def test_full_conversation(self):
        """Interleaved user/assistant/tool flow."""
        tool_id_1 = "t1"
        tool_id_2 = "t2"
        lines = [
            _jsonl_line("user", "Fix auth.py", timestamp="2026-03-13T14:00:00Z"),
            _jsonl_line("assistant", [
                {"type": "text", "text": "I'll look at the file."},
                {"type": "tool_use", "name": "Read", "id": tool_id_1, "input": {"file_path": "/auth.py"}},
            ], timestamp="2026-03-13T14:00:01Z"),
            _jsonl_line("user", [
                {"type": "tool_result", "tool_use_id": tool_id_1, "content": "def login(): pass"},
            ], timestamp="2026-03-13T14:00:02Z"),
            _jsonl_line("assistant", [
                {"type": "text", "text": "Found the issue."},
                {"type": "tool_use", "name": "Edit", "id": tool_id_2,
                 "input": {"file_path": "/auth.py", "old_string": "pass", "new_string": "return True"}},
            ], timestamp="2026-03-13T14:00:03Z"),
            _jsonl_line("user", [
                {"type": "tool_result", "tool_use_id": tool_id_2, "content": "Applied edit."},
            ], timestamp="2026-03-13T14:00:04Z"),
        ]
        path = _write_jsonl(lines)
        steps = build_timeline_from_source(path)
        types = [s.type for s in steps]
        assert types == ["user", "assistant", "tool_call", "assistant", "tool_call"]
        assert steps[0].summary == "Fix auth.py"
        assert steps[2].tool_name == "Read"
        assert steps[4].tool_name == "Edit"
        Path(path).unlink()

    def test_tool_result_as_list(self):
        """Tool result content can be a list of blocks."""
        tool_id = "t_list"
        lines = [
            _jsonl_line("assistant", [
                {"type": "tool_use", "name": "Read", "id": tool_id, "input": {"file_path": "/x.py"}},
            ]),
            _jsonl_line("user", [
                {"type": "tool_result", "tool_use_id": tool_id,
                 "content": [{"type": "text", "text": "line 1"}, {"type": "text", "text": "line 2"}]},
            ]),
        ]
        path = _write_jsonl(lines)
        steps = build_timeline_from_source(path)
        tool_steps = [s for s in steps if s.type == "tool_call"]
        assert "line 1" in tool_steps[0].tool_output
        assert "line 2" in tool_steps[0].tool_output
        Path(path).unlink()


class TestBuildTimeline:
    def test_prefers_source_file(self):
        tool_id = "t1"
        lines = [
            _jsonl_line("user", "Hello"),
            _jsonl_line("assistant", [
                {"type": "tool_use", "name": "Read", "id": tool_id, "input": {"file_path": "/a.py"}},
            ]),
            _jsonl_line("user", [
                {"type": "tool_result", "tool_use_id": tool_id, "content": "contents"},
            ]),
        ]
        path = _write_jsonl(lines)
        db_calls = [_tc("Read", {"file_path": "/a.py"}, seq=0)]

        steps, source = build_timeline(db_calls, source_path=path)
        assert source == "file"
        # Source has user + tool_call, DB only has tool_call
        assert len(steps) > len(db_calls)
        Path(path).unlink()

    def test_falls_back_to_db(self):
        db_calls = [_tc("Read", {"file_path": "/a.py"}, seq=0)]
        steps, source = build_timeline(db_calls, source_path="/nonexistent.jsonl")
        assert source == "db"
        assert len(steps) == 1

    def test_no_source_path(self):
        db_calls = [_tc("Read", {"file_path": "/a.py"}, seq=0)]
        steps, source = build_timeline(db_calls, source_path=None)
        assert source == "db"


class TestAnnotateTimeline:
    def test_annotates_correct_steps(self):
        steps = [
            ReplayStep(seq=0, type="user", summary="Fix it"),
            ReplayStep(seq=1, type="tool_call", tool_name="Edit", summary="Edit -> /a.py"),
            ReplayStep(seq=2, type="tool_call", tool_name="Read", summary="Read -> /a.py"),
            ReplayStep(seq=3, type="tool_call", tool_name="Edit", summary="Edit -> /a.py"),
        ]
        patterns = [
            Pattern(
                type="write_without_read",
                severity="concern",
                detail="1 edit(s) to unread files: a.py",
                tool_indices=[0],  # First tool call (Edit at index 0)
            ),
        ]
        annotate_timeline(steps, patterns)
        # Step 1 is the first tool_call (tool seq 0)
        assert len(steps[1].annotations) == 1
        assert "write_without_read" in steps[1].annotations[0]
        # Other steps should have no annotations
        assert len(steps[0].annotations) == 0
        assert len(steps[2].annotations) == 0

    def test_multiple_annotations(self):
        steps = [
            ReplayStep(seq=0, type="tool_call", tool_name="Edit", is_error=True),
            ReplayStep(seq=1, type="tool_call", tool_name="Edit", is_error=True),
            ReplayStep(seq=2, type="tool_call", tool_name="Edit", is_error=True),
        ]
        patterns = [
            Pattern(type="error_streak", severity="warning",
                    detail="3 consecutive errors", tool_indices=[0, 1, 2]),
            Pattern(type="error_rate", severity="concern",
                    detail="3/3 errors", tool_indices=[0, 1, 2]),
        ]
        annotate_timeline(steps, patterns)
        # Each step should have 2 annotations
        assert len(steps[0].annotations) == 2
        assert len(steps[1].annotations) == 2

    def test_empty_patterns(self):
        steps = [ReplayStep(seq=0, type="tool_call", tool_name="Read")]
        annotate_timeline(steps, [])
        assert steps[0].annotations == []

    def test_db_dict_patterns_skipped(self):
        """DB patterns (dicts without tool_indices) are skipped gracefully."""
        steps = [ReplayStep(seq=0, type="tool_call", tool_name="Read")]
        patterns = [{"type": "error_rate", "severity": "info", "detail": "1/10 errors"}]
        annotate_timeline(steps, patterns)
        assert steps[0].annotations == []


class TestFilterSteps:
    def _sample_steps(self):
        return [
            ReplayStep(seq=0, type="user", summary="Fix it"),
            ReplayStep(seq=1, type="tool_call", tool_name="Read", is_error=False),
            ReplayStep(seq=2, type="tool_call", tool_name="Edit", is_error=True),
            ReplayStep(seq=3, type="assistant", summary="Done"),
            ReplayStep(seq=4, type="tool_call", tool_name="Bash", is_error=False),
        ]

    def test_no_filters(self):
        steps = self._sample_steps()
        result = filter_steps(steps)
        assert len(result) == 5

    def test_errors_only(self):
        result = filter_steps(self._sample_steps(), errors_only=True)
        assert len(result) == 1
        assert result[0].tool_name == "Edit"

    def test_tools_only(self):
        result = filter_steps(self._sample_steps(), tools_only=True)
        assert len(result) == 3
        assert all(s.type == "tool_call" for s in result)

    def test_range_filter(self):
        result = filter_steps(self._sample_steps(), step_range=(1, 3))
        assert [s.seq for s in result] == [1, 2, 3]

    def test_tool_name_filter(self):
        result = filter_steps(self._sample_steps(), tool_filter="Edit,Bash")
        # user and assistant steps pass through (type != "tool_call")
        # Read is filtered out
        names = [s.tool_name for s in result if s.type == "tool_call"]
        assert "Read" not in names
        assert "Edit" in names
        assert "Bash" in names

    def test_combined_filters(self):
        result = filter_steps(
            self._sample_steps(),
            tools_only=True,
            step_range=(0, 2),
        )
        assert len(result) == 2
        assert all(s.type == "tool_call" for s in result)


class TestFormatReplay:
    def test_basic_format(self):
        steps = [
            ReplayStep(seq=0, type="user", summary="Fix the bug"),
            ReplayStep(seq=1, type="tool_call", tool_name="Read",
                       summary="Read -> /a.py", tool_output="contents"),
        ]
        session = {
            "id": "abc123def456",
            "grade": "B",
            "score": 78,
            "duration_minutes": 12.3,
            "model": "claude-opus-4-6",
            "tool_call_count": 15,
            "error_count": 2,
        }
        output = format_replay(steps, session)
        assert "abc123def456" in output
        assert "B" in output
        assert "78" in output
        assert "USER" in output
        assert "Fix the bug" in output
        assert "Read" in output

    def test_compact_hides_output(self):
        steps = [
            ReplayStep(seq=0, type="tool_call", tool_name="Read",
                       summary="Read -> /a.py", tool_output="long output here"),
            ReplayStep(seq=1, type="thinking", summary="[thinking] 500 chars"),
        ]
        session = {"id": "x", "grade": "A", "score": 90, "tool_call_count": 1, "error_count": 0}
        output = format_replay(steps, session, compact=True)
        assert "long output here" not in output
        # Thinking blocks hidden in compact mode
        assert "THINK" not in output

    def test_annotations_shown(self):
        steps = [
            ReplayStep(seq=0, type="tool_call", tool_name="Edit",
                       summary="Edit -> /a.py",
                       annotations=["[!] write_without_read: blind edit"]),
        ]
        session = {"id": "x", "grade": "C", "score": 60, "tool_call_count": 1, "error_count": 0}
        output = format_replay(steps, session)
        assert ">>>" in output
        assert "write_without_read" in output

    def test_error_status_marker(self):
        steps = [
            ReplayStep(seq=0, type="tool_call", tool_name="Edit",
                       summary="Edit [ERROR] -> /a.py", is_error=True),
            ReplayStep(seq=1, type="tool_call", tool_name="Read",
                       summary="Read -> /a.py", is_error=False),
        ]
        session = {"id": "x", "grade": "B", "score": 75, "tool_call_count": 2, "error_count": 1}
        output = format_replay(steps, session)
        assert "[x]" in output
        assert "[+]" in output

    def test_source_label(self):
        steps = [ReplayStep(seq=0, type="tool_call", tool_name="Read", summary="Read -> /a.py")]
        session = {"id": "x", "grade": "A", "score": 90, "tool_call_count": 1, "error_count": 0}

        file_output = format_replay(steps, session, source="file")
        assert "full transcript" in file_output

        db_output = format_replay(steps, session, source="db")
        assert "database" in db_output


class TestParseRange:
    def test_range(self):
        assert parse_range("5-15") == (5, 15)

    def test_single(self):
        assert parse_range("7") == (7, 7)

    def test_invalid(self):
        with pytest.raises(ValueError):
            parse_range("a-b")

    def test_too_many_parts(self):
        with pytest.raises(ValueError):
            parse_range("1-2-3")
