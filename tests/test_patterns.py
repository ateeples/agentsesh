"""Tests for behavioral pattern detection."""

import pytest
from sesh.parsers.base import ToolCall
from sesh.analyzers.patterns import (
    detect_repeated_searches,
    detect_write_without_read,
    detect_error_rate,
    detect_error_streak,
    detect_low_read_ratio,
    detect_bash_overuse,
    detect_write_then_read,
    detect_scattered_files,
    detect_missed_parallelism,
    detect_all_patterns,
)


def _tc(name: str, input_data: dict | None = None, is_error: bool = False, seq: int = 0) -> ToolCall:
    """Helper to create a ToolCall with minimal boilerplate."""
    return ToolCall(
        name=name,
        tool_id=f"tc_{seq}",
        input_data=input_data or {},
        output_preview="",
        output_length=0,
        is_error=is_error,
        seq=seq,
    )


class TestRepeatedSearches:
    def test_no_repeats(self):
        calls = [
            _tc("Grep", {"pattern": "foo"}, seq=0),
            _tc("Grep", {"pattern": "bar"}, seq=1),
        ]
        assert detect_repeated_searches(calls) == []

    def test_detects_repeat(self):
        calls = [
            _tc("Grep", {"pattern": "foo"}, seq=0),
            _tc("Read", {"file_path": "/a.py"}, seq=1),
            _tc("Grep", {"pattern": "foo"}, seq=2),
        ]
        patterns = detect_repeated_searches(calls)
        assert len(patterns) == 1
        assert patterns[0].type == "repeated_search"
        assert patterns[0].severity == "warning"

    def test_ignores_non_search_tools(self):
        calls = [
            _tc("Read", {"file_path": "/a.py"}, seq=0),
            _tc("Read", {"file_path": "/a.py"}, seq=1),
        ]
        assert detect_repeated_searches(calls) == []


class TestWriteWithoutRead:
    def test_clean_edit(self):
        calls = [
            _tc("Read", {"file_path": "/a.py"}, seq=0),
            _tc("Edit", {"file_path": "/a.py"}, seq=1),
        ]
        assert detect_write_without_read(calls) == []

    def test_blind_edit(self):
        calls = [
            _tc("Edit", {"file_path": "/a.py"}, seq=0),
        ]
        patterns = detect_write_without_read(calls)
        assert len(patterns) == 1
        assert patterns[0].type == "write_without_read"
        assert patterns[0].severity == "concern"

    def test_different_file_blind(self):
        calls = [
            _tc("Read", {"file_path": "/a.py"}, seq=0),
            _tc("Edit", {"file_path": "/b.py"}, seq=1),
        ]
        patterns = detect_write_without_read(calls)
        assert len(patterns) == 1


class TestErrorRate:
    def test_no_errors(self):
        calls = [_tc("Read", seq=i) for i in range(5)]
        assert detect_error_rate(calls) == []

    def test_detects_errors(self):
        calls = [
            _tc("Read", is_error=False, seq=0),
            _tc("Bash", is_error=True, seq=1),
            _tc("Read", is_error=False, seq=2),
        ]
        patterns = detect_error_rate(calls)
        assert len(patterns) == 1
        assert patterns[0].type == "error_rate"
        assert "1/3" in patterns[0].detail

    def test_high_error_rate_is_concern(self):
        calls = [_tc("Bash", is_error=True, seq=i) for i in range(5)]
        calls.append(_tc("Read", is_error=False, seq=5))
        patterns = detect_error_rate(calls)
        assert patterns[0].severity == "concern"

    def test_empty_list(self):
        assert detect_error_rate([]) == []


class TestErrorStreak:
    def test_no_streak(self):
        calls = [
            _tc("Bash", is_error=True, seq=0),
            _tc("Read", is_error=False, seq=1),
            _tc("Bash", is_error=True, seq=2),
        ]
        assert detect_error_streak(calls) == []

    def test_detects_streak(self):
        calls = [
            _tc("Bash", is_error=True, seq=0),
            _tc("Bash", is_error=True, seq=1),
            _tc("Bash", is_error=True, seq=2),
        ]
        patterns = detect_error_streak(calls)
        assert len(patterns) == 1
        assert patterns[0].type == "error_streak"
        assert "3 consecutive" in patterns[0].detail


class TestLowReadRatio:
    def test_good_ratio(self):
        calls = [_tc("Read", seq=i) for i in range(6)]
        calls.append(_tc("Edit", seq=6))
        assert detect_low_read_ratio(calls) == []

    def test_low_ratio(self):
        calls = [
            _tc("Read", seq=0),
            _tc("Edit", seq=1),
            _tc("Edit", seq=2),
            _tc("Edit", seq=3),
            _tc("Edit", seq=4),
            _tc("Edit", seq=5),
        ]
        patterns = detect_low_read_ratio(calls)
        assert len(patterns) == 1
        assert patterns[0].type == "low_read_ratio"

    def test_too_few_calls(self):
        calls = [_tc("Read", seq=0), _tc("Edit", seq=1)]
        assert detect_low_read_ratio(calls) == []


class TestBashOveruse:
    def test_legitimate_bash(self):
        calls = [
            _tc("Bash", {"command": "python3 test.py"}, seq=0),
            _tc("Bash", {"command": "npm install"}, seq=1),
        ]
        assert detect_bash_overuse(calls) == []

    def test_detects_anti_patterns(self):
        calls = [
            _tc("Bash", {"command": "cat /a.py"}, seq=0),
            _tc("Bash", {"command": "grep foo /b.py"}, seq=1),
            _tc("Bash", {"command": "find . -name '*.py'"}, seq=2),
            _tc("Bash", {"command": "sed -i 's/foo/bar/' /c.py"}, seq=3),
        ]
        patterns = detect_bash_overuse(calls)
        assert len(patterns) == 1
        assert patterns[0].type == "bash_overuse"
        assert "4/" in patterns[0].detail

    def test_below_threshold(self):
        calls = [_tc("Bash", {"command": "cat /a.py"}, seq=0)]
        assert detect_bash_overuse(calls) == []


class TestWriteThenRead:
    def test_too_few_calls(self):
        calls = [_tc("Write", seq=i) for i in range(5)]
        assert detect_write_then_read(calls) == []

    def test_no_phase_shift(self):
        # 10 reads then 5 writes — normal order
        calls = [_tc("Read", seq=i) for i in range(10)]
        calls += [_tc("Edit", seq=i + 10) for i in range(5)]
        assert detect_write_then_read(calls) == []


class TestScatteredFiles:
    def test_focused_work(self):
        calls = [
            _tc("Read", {"file_path": f"/project/src/file{i}.py"}, seq=i)
            for i in range(12)
        ]
        assert detect_scattered_files(calls) == []

    def test_scattered(self):
        dirs = [f"/dir{i}/file.py" for i in range(15)]
        calls = [_tc("Read", {"file_path": d}, seq=i) for i, d in enumerate(dirs)]
        patterns = detect_scattered_files(calls)
        assert len(patterns) == 1
        assert patterns[0].type == "scattered_files"

    def test_too_few_calls(self):
        calls = [_tc("Read", {"file_path": f"/dir{i}/f.py"}, seq=i) for i in range(5)]
        assert detect_scattered_files(calls) == []


class TestMissedParallelism:
    def test_no_missed(self):
        calls = [
            _tc("Read", {"file_path": "/a.py"}, seq=0),
            _tc("Edit", {"file_path": "/a.py"}, seq=1),
            _tc("Read", {"file_path": "/b.py"}, seq=2),
        ]
        assert detect_missed_parallelism(calls) == []

    def test_detects_missed(self):
        calls = [
            _tc("Read", {"file_path": f"/file{i}.py"}, seq=i)
            for i in range(10)
        ]
        patterns = detect_missed_parallelism(calls)
        assert len(patterns) == 1
        assert patterns[0].type == "missed_parallelism"


class TestDetectAll:
    def test_runs_all_detectors(self):
        # A session with multiple issues
        calls = [
            _tc("Edit", {"file_path": "/blind.py"}, seq=0),
            _tc("Bash", {"command": "cat /a.py"}, is_error=True, seq=1),
            _tc("Bash", {"command": "grep foo /b.py"}, is_error=True, seq=2),
            _tc("Bash", {"command": "find . -name '*.py'"}, is_error=True, seq=3),
            _tc("Bash", {"command": "sed 's/a/b/' /c.py"}, seq=4),
            _tc("Read", {"file_path": "/a.py"}, seq=5),
            _tc("Read", {"file_path": "/b.py"}, seq=6),
            _tc("Read", {"file_path": "/c.py"}, seq=7),
            _tc("Read", {"file_path": "/d.py"}, seq=8),
            _tc("Read", {"file_path": "/e.py"}, seq=9),
        ]
        patterns = detect_all_patterns(calls)
        types = {p.type for p in patterns}
        assert "write_without_read" in types
        assert "error_rate" in types
        assert "error_streak" in types
        assert "bash_overuse" in types

    def test_disabled_patterns(self):
        calls = [_tc("Bash", {"command": "cat /a.py"}, is_error=True, seq=i) for i in range(5)]
        patterns = detect_all_patterns(calls, disabled=["error_rate"])
        types = {p.type for p in patterns}
        assert "error_rate" not in types
