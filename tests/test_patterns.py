"""Tests for behavioral pattern detection.

Each pattern detector is tested independently with synthetic tool call
sequences. Tests verify both positive detection (pattern present) and
negative detection (pattern absent / below threshold).
"""

from sesh.analyzers.patterns import (
    detect_all_patterns,
    detect_bash_overuse,
    detect_error_rate,
    detect_error_streak,
    detect_low_read_ratio,
    detect_missed_parallelism,
    detect_repeated_searches,
    detect_scattered_files,
    detect_write_then_read,
    detect_write_without_read,
)
from sesh.parsers.base import ToolCall

# --- Test helper ---


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


# --- Individual pattern detector tests ---


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


# --- Write-without-read: editing a file you haven't Read first ---


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

    def test_write_then_edit_not_blind(self):
        """Write creates a new file — editing it after is not blind."""
        calls = [
            _tc("Write", {"file_path": "/new.py"}, seq=0),
            _tc("Edit", {"file_path": "/new.py"}, seq=1),
        ]
        assert detect_write_without_read(calls) == []

    def test_write_does_not_cover_other_files(self):
        """Writing /a.py does not make editing /b.py safe."""
        calls = [
            _tc("Write", {"file_path": "/a.py"}, seq=0),
            _tc("Edit", {"file_path": "/b.py"}, seq=1),
        ]
        patterns = detect_write_without_read(calls)
        assert len(patterns) == 1


# --- Error rate: overall session error percentage ---


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


# --- Error streak: consecutive failures indicating the agent is stuck ---


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


# --- Low read ratio: writing too much relative to reading ---


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


# --- Bash overuse: using Bash for tasks with dedicated tools ---


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

    def test_test_runner_piped_through_head_not_flagged(self):
        """pytest | head is legitimate — don't flag the head/tail pipe."""
        calls = [
            _tc("Bash", {"command": "cd /proj && python3 -m pytest tests/ | head -50"}, seq=0),
            _tc("Bash", {"command": "cd /proj && python3 -m pytest tests/ | tail -20"}, seq=1),
            _tc("Bash", {"command": "cd /proj && python3 -m pytest tests/ | head -30"}, seq=2),
        ]
        assert detect_bash_overuse(calls) == []

    def test_build_runner_piped_not_flagged(self):
        """npm run build | tail is legitimate."""
        calls = [
            _tc("Bash", {"command": "npm run build 2>&1 | tail -20"}, seq=0),
            _tc("Bash", {"command": "npm run build 2>&1 | head -50"}, seq=1),
            _tc("Bash", {"command": "npm run build 2>&1 | tail -10"}, seq=2),
        ]
        assert detect_bash_overuse(calls) == []

    def test_cat_still_flagged_alongside_runners(self):
        """cat/grep anti-patterns still flagged even if runners are present."""
        calls = [
            _tc("Bash", {"command": "python3 -m pytest tests/ | head -50"}, seq=0),
            _tc("Bash", {"command": "cat /a.py"}, seq=1),
            _tc("Bash", {"command": "grep foo /b.py"}, seq=2),
            _tc("Bash", {"command": "find . -name '*.py'"}, seq=3),
        ]
        patterns = detect_bash_overuse(calls)
        assert len(patterns) == 1
        assert "3/" in patterns[0].detail  # 3 out of 4 bash calls


# --- Write-then-read: acting before understanding ---


class TestWriteThenRead:
    def test_too_few_calls(self):
        calls = [_tc("Write", seq=i) for i in range(5)]
        assert detect_write_then_read(calls) == []

    def test_no_phase_shift(self):
        # 10 reads then 5 writes — normal order
        calls = [_tc("Read", seq=i) for i in range(10)]
        calls += [_tc("Edit", seq=i + 10) for i in range(5)]
        assert detect_write_then_read(calls) == []


# --- Scattered files: editing across too many directories ---


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


# --- Missed parallelism: consecutive reads that could have been batched ---


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


# --- Integration: detect_all_patterns ---


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
