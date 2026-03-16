"""Tests for sesh.live — lightweight live session analysis."""

import json
import time
from pathlib import Path

import pytest

from sesh.live import (
    LiveNudge,
    LiveSnapshot,
    _generate_nudges,
    _quick_archetype,
    extract_project_name,
    snapshot,
)


class TestExtractProjectName:
    """Test project name extraction from transcript paths."""

    def test_standard_path(self, tmp_path):
        p = tmp_path / "-Users-andrewteeples-Documents-Projects-agentsesh" / "abc.jsonl"
        p.parent.mkdir(parents=True)
        name = extract_project_name(p)
        assert "agentsesh" in name

    def test_nested_project(self, tmp_path):
        p = tmp_path / "-Users-foo-Projects-tinyclaw-workspace-opus" / "abc.jsonl"
        p.parent.mkdir(parents=True)
        name = extract_project_name(p)
        assert "opus" in name

    def test_projects_dir(self, tmp_path):
        p = tmp_path / "projects" / "abc.jsonl"
        p.parent.mkdir(parents=True)
        assert extract_project_name(p) == ""


class TestQuickArchetype:
    """Test quick archetype classification."""

    def test_partnership(self):
        archetype, score = _quick_archetype(
            human_turns=10, avg_words=150,
            corrections=3, affirmations=4,
            tool_calls=80,
        )
        assert archetype == "Partnership"
        assert score >= 70

    def test_spec_dump(self):
        archetype, score = _quick_archetype(
            human_turns=2, avg_words=600,
            corrections=0, affirmations=0,
            tool_calls=50,
        )
        assert archetype == "Spec Dump"
        assert score < 50

    def test_micromanager(self):
        archetype, score = _quick_archetype(
            human_turns=20, avg_words=30,
            corrections=5, affirmations=0,
            tool_calls=40,
        )
        assert archetype == "Micromanager"

    def test_autopilot(self):
        archetype, score = _quick_archetype(
            human_turns=1, avg_words=200,
            corrections=0, affirmations=0,
            tool_calls=80,
        )
        assert archetype == "Autopilot"

    def test_struggle(self):
        archetype, score = _quick_archetype(
            human_turns=10, avg_words=100,
            corrections=6, affirmations=0,
            tool_calls=50,
        )
        assert archetype == "Struggle"


class TestGenerateNudges:
    """Test nudge generation from live snapshots."""

    def test_no_tests_warning(self):
        snap = LiveSnapshot(tool_calls=30, test_runs=0)
        nudges = _generate_nudges(snap)
        assert any("test" in n.message.lower() for n in nudges)
        assert any(n.level == "warn" for n in nudges)

    def test_error_streak_alert(self):
        snap = LiveSnapshot(tool_calls=20, error_streak=4)
        nudges = _generate_nudges(snap)
        assert any("streak" in n.message.lower() for n in nudges)
        assert any(n.level == "alert" for n in nudges)

    def test_high_error_rate(self):
        snap = LiveSnapshot(tool_calls=20, errors=8, error_rate=0.4)
        nudges = _generate_nudges(snap)
        assert any("error rate" in n.message.lower() for n in nudges)

    def test_file_thrashing(self):
        snap = LiveSnapshot(
            tool_calls=20,
            file_edit_counts={"/some/file.py": 6},
        )
        nudges = _generate_nudges(snap)
        assert any("edited" in n.message.lower() for n in nudges)

    def test_long_session(self):
        snap = LiveSnapshot(tool_calls=20, duration_seconds=8000)
        nudges = _generate_nudges(snap)
        assert any("min" in n.message for n in nudges)

    def test_clean_session_no_warn_nudges(self):
        snap = LiveSnapshot(tool_calls=10, test_runs=0)
        nudges = _generate_nudges(snap)
        # Under 25 tool calls, no "no tests" warning
        assert not any(n.level == "alert" for n in nudges)

    def test_positive_feedback(self):
        snap = LiveSnapshot(
            tool_calls=20,
            test_runs=5, test_passes=4, test_failures=0,
        )
        nudges = _generate_nudges(snap)
        assert any("looking good" in n.message.lower() for n in nudges)

    def test_spec_dump_nudge(self):
        snap = LiveSnapshot(
            tool_calls=20,
            archetype="Spec Dump",
        )
        nudges = _generate_nudges(snap)
        assert any("shorter" in n.message.lower() for n in nudges)


class TestSnapshot:
    """Test the snapshot function with real JSONL data."""

    def _write_session(self, path: Path, messages: list[dict]) -> None:
        """Write a list of message dicts as JSONL."""
        with open(path, "w") as f:
            for msg in messages:
                f.write(json.dumps(msg) + "\n")

    def test_snapshot_with_tool_calls(self, tmp_path):
        session_file = tmp_path / "test.jsonl"
        self._write_session(session_file, [
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Read",
                            "input": {"file_path": "/foo/bar.py"},
                        },
                        {
                            "type": "tool_use",
                            "name": "Edit",
                            "input": {"file_path": "/foo/bar.py"},
                        },
                    ],
                    "usage": {"input_tokens": 1000, "output_tokens": 500},
                },
            },
            {
                "type": "tool_result",
                "is_error": False,
                "content": "ok",
            },
            {
                "type": "tool_result",
                "is_error": False,
                "content": "ok",
            },
        ])

        snap = snapshot(session_file)
        assert snap is not None
        assert snap.tool_calls == 2
        assert snap.errors == 0
        assert snap.files_read == 1
        assert snap.files_written == 1
        assert snap.input_tokens == 1000
        assert snap.output_tokens == 500

    def test_snapshot_detects_user_messages(self, tmp_path):
        session_file = tmp_path / "test.jsonl"
        self._write_session(session_file, [
            {
                "type": "user",
                "message": {
                    "content": [
                        {"type": "text", "text": "yes good work, keep going with that approach"},
                    ],
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {"type": "text", "text": "no not like that, fix the import"},
                    ],
                },
            },
        ])

        snap = snapshot(session_file)
        assert snap is not None
        assert snap.human_turns == 2
        assert snap.corrections >= 1  # "no", "not", "fix"
        assert snap.affirmations >= 1  # "yes", "good"

    def test_snapshot_error_tracking(self, tmp_path):
        session_file = tmp_path / "test.jsonl"
        self._write_session(session_file, [
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "Bash", "input": {"command": "cat foo"}},
                    ],
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            },
            {"type": "tool_result", "is_error": True, "content": "file not found"},
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "Bash", "input": {"command": "cat foo"}},
                    ],
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            },
            {"type": "tool_result", "is_error": True, "content": "file not found"},
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "Bash", "input": {"command": "cat foo"}},
                    ],
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            },
            {"type": "tool_result", "is_error": True, "content": "file not found"},
        ])

        snap = snapshot(session_file)
        assert snap.errors == 3
        assert snap.error_streak == 3

    def test_snapshot_returns_none_for_missing(self):
        result = snapshot(Path("/nonexistent/session.jsonl"))
        assert result is None

    def test_snapshot_test_detection(self, tmp_path):
        session_file = tmp_path / "test.jsonl"
        self._write_session(session_file, [
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "Bash", "input": {"command": "pytest tests/ -x"}},
                    ],
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            },
            {"type": "tool_result", "is_error": False, "content": "5 passed"},
        ])

        snap = snapshot(session_file)
        assert snap.test_runs == 1
