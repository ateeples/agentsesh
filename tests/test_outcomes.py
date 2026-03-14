"""Tests for outcome-focused session analysis.

Covers: outcome extraction (test/build/lint counts, error loops, rework),
session comparison (improvements, regressions, verdict), and formatting.
"""

import json
import pytest

from sesh.analyzers.outcomes import (
    extract_outcomes,
    compare_outcomes,
    format_outcome_metrics,
    format_comparison,
    OutcomeMetrics,
)


# --- Test helper ---


def _tc(name, input_data=None, is_error=False, output_preview="", seq=0):
    """Helper to create a tool call dict matching DB format."""
    return {
        "name": name,
        "input_json": json.dumps(input_data or {}),
        "is_error": int(is_error),
        "output_preview": output_preview,
        "seq": seq,
    }


# --- Outcome extraction from tool calls ---


class TestExtractOutcomes:
    def test_empty_session(self):
        m = extract_outcomes([])
        assert m.total_tool_calls == 0
        assert m.success_rate == 1.0
        assert m.error_retry_loops == 0

    def test_basic_counts(self):
        calls = [
            _tc("Read", {"file_path": "/a.py"}, seq=0),
            _tc("Edit", {"file_path": "/a.py"}, seq=1),
            _tc("Bash", {"command": "pytest"}, seq=2),
        ]
        m = extract_outcomes(calls)
        assert m.total_tool_calls == 3
        assert m.total_errors == 0
        assert m.success_rate == 1.0

    def test_error_counting(self):
        calls = [
            _tc("Read", {"file_path": "/a.py"}, seq=0),
            _tc("Edit", {"file_path": "/a.py"}, is_error=True, seq=1),
            _tc("Edit", {"file_path": "/a.py"}, seq=2),
        ]
        m = extract_outcomes(calls)
        assert m.total_errors == 1
        assert abs(m.success_rate - 2 / 3) < 0.01

    def test_rework_detection(self):
        # Editing the same file multiple times indicates rework — the agent
        # had to come back and fix something it already touched
        calls = [
            _tc("Edit", {"file_path": "/a.py"}, seq=0),
            _tc("Edit", {"file_path": "/b.py"}, seq=1),
            _tc("Edit", {"file_path": "/a.py"}, seq=2),  # rework
            _tc("Edit", {"file_path": "/a.py"}, seq=3),  # more rework
        ]
        m = extract_outcomes(calls)
        assert m.files_reworked == 1
        assert m.rework_edits == 2  # 2 extra edits beyond the first
        assert "/a.py" in m.rework_files

    def test_no_rework_single_edits(self):
        calls = [
            _tc("Edit", {"file_path": "/a.py"}, seq=0),
            _tc("Edit", {"file_path": "/b.py"}, seq=1),
            _tc("Edit", {"file_path": "/c.py"}, seq=2),
        ]
        m = extract_outcomes(calls)
        assert m.files_reworked == 0
        assert m.rework_edits == 0

    def test_error_retry_loop(self):
        # Error retry loop: same tool on same file, error → error → success.
        # Counts the number of times the agent retried the same failed operation.
        calls = [
            _tc("Edit", {"file_path": "/a.py"}, is_error=True, seq=0),
            _tc("Edit", {"file_path": "/a.py"}, is_error=True, seq=1),  # retry, still error
            _tc("Edit", {"file_path": "/a.py"}, seq=2),  # finally works
        ]
        m = extract_outcomes(calls)
        assert m.error_retry_loops == 1

    def test_no_error_retry_when_different_tools(self):
        calls = [
            _tc("Edit", {"file_path": "/a.py"}, is_error=True, seq=0),
            _tc("Read", {"file_path": "/a.py"}, seq=1),  # different tool
            _tc("Edit", {"file_path": "/a.py"}, seq=2),
        ]
        m = extract_outcomes(calls)
        assert m.error_retry_loops == 0

    # Terminal state: did the session end cleanly or on an error?
    def test_terminal_state_clean(self):
        calls = [
            _tc("Read", seq=0),
            _tc("Edit", seq=1),
            _tc("Bash", {"command": "pytest"}, seq=2),
        ]
        m = extract_outcomes(calls)
        assert not m.ended_on_error
        assert m.final_error_streak == 0

    def test_terminal_state_error(self):
        calls = [
            _tc("Read", seq=0),
            _tc("Edit", is_error=True, seq=1),
            _tc("Bash", {"command": "pytest"}, is_error=True, seq=2),
        ]
        m = extract_outcomes(calls)
        assert m.ended_on_error
        assert m.final_error_streak == 2

    # Verification detection: test, build, and lint commands in Bash output
    def test_test_detection_pass(self):
        calls = [
            _tc("Bash", {"command": "pytest tests/"}, seq=0),
            _tc("Bash", {"command": "npm test"}, seq=1),
        ]
        m = extract_outcomes(calls)
        assert m.test_runs == 2
        assert m.test_passes == 2
        assert m.test_failures == 0

    def test_test_detection_fail(self):
        calls = [
            _tc("Bash", {"command": "pytest tests/"}, is_error=True, seq=0),
        ]
        m = extract_outcomes(calls)
        assert m.test_runs == 1
        assert m.test_passes == 0
        assert m.test_failures == 1

    def test_build_detection(self):
        calls = [
            _tc("Bash", {"command": "npm run build"}, seq=0),
            _tc("Bash", {"command": "cargo build"}, is_error=True, seq=1),
        ]
        m = extract_outcomes(calls)
        assert m.build_runs == 2
        assert m.build_passes == 1
        assert m.build_failures == 1

    def test_lint_detection(self):
        calls = [
            _tc("Bash", {"command": "eslint src/"}, seq=0),
            _tc("Bash", {"command": "mypy ."}, is_error=True, seq=1),
        ]
        m = extract_outcomes(calls)
        assert m.lint_runs == 2
        assert m.lint_passes == 1
        assert m.lint_failures == 1

    def test_non_bash_ignored_for_verification(self):
        """Only Bash commands count for test/build/lint detection."""
        calls = [
            _tc("Read", {"file_path": "pytest"}, seq=0),
            _tc("Grep", {"pattern": "npm test"}, seq=1),
        ]
        m = extract_outcomes(calls)
        assert m.test_runs == 0
        assert m.build_runs == 0


# --- Session comparison (improvements, regressions) ---


class TestCompareOutcomes:
    def test_identical_sessions(self):
        m = OutcomeMetrics(
            total_tool_calls=10, total_errors=1, success_rate=0.9,
            error_retry_loops=0, files_reworked=0, rework_edits=0,
            ended_on_error=False, final_error_streak=0,
        )
        comp = compare_outcomes(m, m)
        assert comp.verdict == "unchanged"
        assert len(comp.improvements) == 0
        assert len(comp.regressions) == 0

    def test_clear_improvement(self):
        baseline = OutcomeMetrics(
            total_tool_calls=20, total_errors=5, success_rate=0.75,
            error_retry_loops=3, files_reworked=2, rework_edits=4,
            ended_on_error=True, final_error_streak=2,
        )
        candidate = OutcomeMetrics(
            total_tool_calls=15, total_errors=1, success_rate=0.93,
            error_retry_loops=0, files_reworked=0, rework_edits=0,
            ended_on_error=False, final_error_streak=0,
        )
        comp = compare_outcomes(baseline, candidate)
        assert comp.verdict == "improved"
        assert len(comp.improvements) > 0
        assert len(comp.regressions) == 0

    def test_clear_regression(self):
        baseline = OutcomeMetrics(
            total_tool_calls=10, total_errors=0, success_rate=1.0,
            error_retry_loops=0, files_reworked=0, rework_edits=0,
            ended_on_error=False, final_error_streak=0,
        )
        candidate = OutcomeMetrics(
            total_tool_calls=20, total_errors=8, success_rate=0.6,
            error_retry_loops=4, files_reworked=3, rework_edits=5,
            ended_on_error=True, final_error_streak=3,
        )
        comp = compare_outcomes(baseline, candidate)
        assert comp.verdict == "regressed"
        assert len(comp.regressions) > 0
        assert len(comp.improvements) == 0

    def test_mixed_results(self):
        baseline = OutcomeMetrics(
            total_tool_calls=20, total_errors=5, success_rate=0.75,
            error_retry_loops=2, files_reworked=0, rework_edits=0,
            ended_on_error=False, final_error_streak=0,
            test_runs=3, test_passes=3, test_failures=0,
        )
        candidate = OutcomeMetrics(
            total_tool_calls=15, total_errors=1, success_rate=0.93,
            error_retry_loops=0, files_reworked=2, rework_edits=3,
            ended_on_error=False, final_error_streak=0,
            test_runs=3, test_passes=2, test_failures=1,
        )
        comp = compare_outcomes(baseline, candidate)
        assert comp.verdict == "mixed"
        assert len(comp.improvements) > 0
        assert len(comp.regressions) > 0

    def test_test_run_comparison(self):
        baseline = OutcomeMetrics(
            test_runs=5, test_passes=4, test_failures=1,
            total_tool_calls=10, success_rate=0.9,
        )
        candidate = OutcomeMetrics(
            test_runs=5, test_passes=5, test_failures=0,
            total_tool_calls=10, success_rate=0.9,
        )
        comp = compare_outcomes(baseline, candidate)
        assert any("test pass rate" in i for i in comp.improvements)

    def test_started_running_tests(self):
        baseline = OutcomeMetrics(total_tool_calls=10, success_rate=1.0)
        candidate = OutcomeMetrics(
            total_tool_calls=12, success_rate=1.0,
            test_runs=3, test_passes=3,
        )
        comp = compare_outcomes(baseline, candidate)
        assert any("started running tests" in i for i in comp.improvements)

    def test_stopped_running_tests(self):
        baseline = OutcomeMetrics(
            total_tool_calls=10, success_rate=1.0,
            test_runs=3, test_passes=3,
        )
        candidate = OutcomeMetrics(total_tool_calls=8, success_rate=1.0)
        comp = compare_outcomes(baseline, candidate)
        assert any("stopped running tests" in i for i in comp.regressions)


# --- Output formatting ---


class TestFormatting:
    def test_format_outcome_metrics(self):
        m = OutcomeMetrics(
            total_tool_calls=20, total_errors=3, success_rate=0.85,
            error_retry_loops=1, files_reworked=2, rework_edits=3,
            ended_on_error=False, final_error_streak=0,
            test_runs=3, test_passes=2, test_failures=1,
            rework_files=["/a.py", "/b.py"],
            error_retry_details=["Edit at #5→#7"],
        )
        text = format_outcome_metrics(m)
        assert "85.0%" in text
        assert "Error-retry loops: 1" in text
        assert "Files reworked:    2" in text
        assert "clean" in text
        assert "2/3 passed" in text
        assert "/a.py" in text

    def test_format_comparison(self):
        baseline = OutcomeMetrics(
            total_tool_calls=20, total_errors=5, success_rate=0.75,
            error_retry_loops=2, files_reworked=1, rework_edits=2,
            ended_on_error=True, final_error_streak=1,
        )
        candidate = OutcomeMetrics(
            total_tool_calls=15, total_errors=1, success_rate=0.93,
            error_retry_loops=0, files_reworked=0, rework_edits=0,
            ended_on_error=False, final_error_streak=0,
        )
        comp = compare_outcomes(baseline, candidate)
        text = format_comparison(comp)
        assert "IMPROVED" in text
        assert "Improvements:" in text
        assert "+" in text
