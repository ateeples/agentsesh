"""Tests for outcome-based grading and session type classification.

Tests validate:
1. Session type classification
2. Test snapshot extraction
3. Stuck event detection
4. Outcome scoring correlation (shipping sessions score higher)
"""

import pytest

from sesh.analyzers.outcome_grader import (
    OutcomeNarrative,
    StuckEvent,
    TestSnapshot,
    detect_stuck_events,
    extract_test_snapshots,
    grade_outcome,
)
from sesh.analyzers.session_type import (
    SessionClassification,
    classify_session,
    is_workspace_file,
)
from sesh.parsers.base import ToolCall


def _tc(name: str, seq: int = 0, input_data: dict | None = None,
        is_error: bool = False, output_preview: str = "") -> ToolCall:
    """Helper to create a ToolCall."""
    return ToolCall(
        name=name,
        tool_id=f"tool_{seq}",
        input_data=input_data or {},
        output_preview=output_preview,
        output_length=len(output_preview),
        is_error=is_error,
        seq=seq,
    )


# === Session Type Classification ===


class TestIsWorkspaceFile:
    def test_heartbeat(self):
        assert is_workspace_file("/path/to/opus/heartbeat.md")

    def test_soul(self):
        assert is_workspace_file("/path/to/SOUL.md")

    def test_memory_dir(self):
        assert is_workspace_file("/path/to/memory/project_foo.md")

    def test_claude_md(self):
        assert is_workspace_file("/path/to/CLAUDE.md")

    def test_skills_dir(self):
        assert is_workspace_file("/path/to/skills/relay/send.py")

    def test_python_file_not_workspace(self):
        assert not is_workspace_file("/path/to/sesh/analyze.py")

    def test_typescript_not_workspace(self):
        assert not is_workspace_file("/path/to/src/index.ts")

    def test_thread_summary(self):
        assert is_workspace_file("/path/to/thread-summary-abc123.md")


class TestClassifySession:
    def test_minimal(self):
        """Sessions with < 10 tool calls are MINIMAL."""
        calls = [_tc("Read", i) for i in range(5)]
        result = classify_session(calls)
        assert result.session_type == "MINIMAL"

    def test_build_tested(self):
        """Sessions with commits and test runs are BUILD_TESTED."""
        calls = [
            _tc("Read", 0),
            _tc("Read", 1),
            _tc("Edit", 2, {"file_path": "/src/main.py"}),
            _tc("Edit", 3, {"file_path": "/src/utils.py"}),
            _tc("Bash", 4, {"command": "python3 -m pytest --tb=short"}),
            _tc("Bash", 5, {"command": "git commit -m 'feat: add utils'"}),
            _tc("Read", 6),
            _tc("Edit", 7, {"file_path": "/src/main.py"}),
            _tc("Bash", 8, {"command": "python3 -m pytest"}),
            _tc("Bash", 9, {"command": "git commit -m 'fix: main'"}),
        ]
        result = classify_session(calls)
        assert result.session_type == "BUILD_TESTED"
        assert result.commit_count == 2
        assert result.test_run_count == 2

    def test_build_untested(self):
        """Sessions with commits but no tests are BUILD_UNTESTED."""
        calls = [
            *[_tc("Read", i) for i in range(6)],
            _tc("Edit", 6, {"file_path": "/src/main.py"}),
            _tc("Edit", 7, {"file_path": "/src/utils.py"}),
            _tc("Bash", 8, {"command": "git commit -m 'feat: stuff'"}),
            _tc("Bash", 9, {"command": "git push"}),
        ]
        result = classify_session(calls)
        assert result.session_type == "BUILD_UNTESTED"
        assert result.commit_count == 1
        assert result.test_run_count == 0

    def test_build_uncommitted(self):
        """Sessions with 5+ project edits but no commits."""
        calls = [
            *[_tc("Read", i) for i in range(5)],
            *[_tc("Edit", i + 5, {"file_path": f"/src/file{i}.py"}) for i in range(5)],
        ]
        result = classify_session(calls)
        assert result.session_type == "BUILD_UNCOMMITTED"
        assert result.commit_count == 0

    def test_workspace_only(self):
        """Sessions editing only workspace files."""
        calls = [
            *[_tc("Read", i) for i in range(7)],
            _tc("Edit", 7, {"file_path": "/opus/heartbeat.md"}),
            _tc("Edit", 8, {"file_path": "/opus/SOUL.md"}),
            _tc("Edit", 9, {"file_path": "/opus/memory/project.md"}),
        ]
        result = classify_session(calls)
        assert result.session_type == "WORKSPACE"

    def test_research(self):
        """Sessions dominated by reads with minimal edits."""
        calls = [_tc("Read", i) for i in range(8)] + [
            _tc("Grep", 8),
            _tc("Glob", 9),
        ]
        result = classify_session(calls)
        assert result.session_type == "RESEARCH"


# === Test Snapshot Extraction ===


class TestExtractTestSnapshots:
    def test_pytest_output(self):
        calls = [
            _tc("Bash", 0, {"command": "pytest"}, output_preview="5 passed in 1.2s"),
            _tc("Bash", 1, {"command": "pytest"}, output_preview="5 passed, 2 failed"),
        ]
        snapshots = extract_test_snapshots(calls)
        assert len(snapshots) == 2
        assert snapshots[0].passed == 5
        assert snapshots[0].failed == 0
        assert snapshots[1].passed == 5
        assert snapshots[1].failed == 2

    def test_no_test_output(self):
        calls = [
            _tc("Bash", 0, {"command": "ls"}, output_preview="file1.py\nfile2.py"),
        ]
        assert extract_test_snapshots(calls) == []

    def test_large_test_suite(self):
        calls = [
            _tc("Bash", 0, output_preview="398 passed, 0 failed"),
        ]
        snapshots = extract_test_snapshots(calls)
        assert snapshots[0].passed == 398


# === Stuck Event Detection ===


class TestDetectStuckEvents:
    def test_error_streak_detected(self):
        calls = [
            _tc("Read", 0),
            _tc("Edit", 1, is_error=True, output_preview="File not read"),
            _tc("Edit", 2, is_error=True, output_preview="File not read"),
            _tc("Edit", 3, is_error=True, output_preview="File not read"),
            _tc("Read", 4),
        ]
        events = detect_stuck_events(calls)
        assert len(events) == 1
        assert events[0].length == 3
        assert events[0].start_index == 1

    def test_no_streak_under_threshold(self):
        calls = [
            _tc("Read", 0),
            _tc("Edit", 1, is_error=True),
            _tc("Edit", 2, is_error=True),
            _tc("Read", 3),
        ]
        assert detect_stuck_events(calls) == []

    def test_long_streak(self):
        calls = [_tc("Read", 0)] + [
            _tc("Bash", i + 1, is_error=True, output_preview="Exit code 1") for i in range(7)
        ]
        events = detect_stuck_events(calls)
        assert len(events) == 1
        assert events[0].length == 7

    def test_streak_at_end(self):
        calls = [
            _tc("Read", 0),
            _tc("Edit", 1, is_error=True, output_preview="err"),
            _tc("Edit", 2, is_error=True, output_preview="err"),
            _tc("Edit", 3, is_error=True, output_preview="err"),
        ]
        events = detect_stuck_events(calls)
        assert len(events) == 1


# === Outcome Grading ===


class TestGradeOutcome:
    def _classify(self, calls):
        return classify_session(calls)

    def test_non_build_not_scored(self):
        """Conversations and research sessions get N/A."""
        calls = [_tc("Read", i) for i in range(10)]
        cls = self._classify(calls)
        result = grade_outcome(calls, cls)
        assert result.grade == "N/A"
        assert result.score is None

    def test_shipped_and_tested_scores_high(self):
        """Sessions with commits + green tests should score A."""
        calls = [
            *[_tc("Read", i) for i in range(4)],
            _tc("Edit", 4, {"file_path": "/src/main.py"}),
            _tc("Edit", 5, {"file_path": "/src/utils.py"}),
            _tc("Bash", 6, {"command": "pytest"}, output_preview="10 passed"),
            _tc("Bash", 7, {"command": "git commit -m 'feat: add'"}),
            _tc("Edit", 8, {"file_path": "/src/main.py"}),
            _tc("Bash", 9, {"command": "pytest"}, output_preview="12 passed"),
            _tc("Bash", 10, {"command": "git commit -m 'fix: typo'"}),
        ]
        cls = self._classify(calls)
        result = grade_outcome(calls, cls)
        assert result.grade == "A"
        assert result.score >= 85
        assert result.commit_count == 2
        assert any("commits" in s for s in result.strengths)
        assert any("green" in s.lower() for s in result.strengths)

    def test_shipped_without_tests_scores_lower(self):
        """Commits without tests should score B range."""
        calls = [
            *[_tc("Read", i) for i in range(6)],
            _tc("Edit", 6, {"file_path": "/src/main.py"}),
            _tc("Edit", 7, {"file_path": "/src/utils.py"}),
            _tc("Bash", 8, {"command": "git commit -m 'feat: add'"}),
            _tc("Bash", 9, {"command": "git push"}),
        ]
        cls = self._classify(calls)
        result = grade_outcome(calls, cls)
        assert result.grade in ("B", "C")
        assert any("without" in c.lower() or "test" in c.lower() for c in result.concerns)

    def test_uncommitted_build_scores_low(self):
        """Significant edits without commits should score D/F."""
        calls = [
            *[_tc("Read", i) for i in range(5)],
            *[_tc("Edit", i + 5, {"file_path": f"/src/file{i}.py"}) for i in range(5)],
        ]
        cls = self._classify(calls)
        result = grade_outcome(calls, cls)
        assert result.grade in ("D", "F")
        assert any("didn't commit" in c for c in result.concerns)

    def test_stuck_session_penalized(self):
        """Error streaks should reduce score."""
        calls = [
            *[_tc("Read", i) for i in range(5)],
            _tc("Edit", 5, {"file_path": "/src/main.py"}),
            # Error streak
            _tc("Bash", 6, is_error=True, output_preview="Exit code 1"),
            _tc("Bash", 7, is_error=True, output_preview="Exit code 1"),
            _tc("Bash", 8, is_error=True, output_preview="Exit code 1"),
            _tc("Bash", 9, is_error=True, output_preview="Exit code 1"),
            _tc("Bash", 10, is_error=True, output_preview="Exit code 1"),
            _tc("Bash", 11, {"command": "git commit -m 'fix'"}),
        ]
        cls = self._classify(calls)
        result = grade_outcome(calls, cls)
        assert len(result.stuck_events) >= 1
        assert any("stuck" in c.lower() or "error" in c.lower() for c in result.concerns)

    def test_resolved_failures_bonus(self):
        """Sessions that fix failing tests get a bonus."""
        calls = [
            *[_tc("Read", i) for i in range(4)],
            _tc("Edit", 4, {"file_path": "/src/main.py"}),
            _tc("Bash", 5, {"command": "pytest"}, output_preview="3 passed, 2 failed"),
            _tc("Edit", 6, {"file_path": "/src/main.py"}),
            _tc("Bash", 7, {"command": "pytest"}, output_preview="5 passed"),
            _tc("Bash", 8, {"command": "git commit -m 'fix: all tests'"}),
            _tc("Read", 9),
        ]
        cls = self._classify(calls)
        result = grade_outcome(calls, cls)
        assert result.tests_ended_green is True
        assert any("fixed" in s.lower() or "fix" in s.lower() for s in result.strengths)

    def test_red_tests_at_end_penalized(self):
        """Sessions ending with failing tests should be penalized."""
        calls = [
            *[_tc("Read", i) for i in range(5)],
            _tc("Edit", 5, {"file_path": "/src/main.py"}),
            _tc("Edit", 6, {"file_path": "/src/utils.py"}),
            _tc("Edit", 7, {"file_path": "/src/other.py"}),
            _tc("Edit", 8, {"file_path": "/src/more.py"}),
            _tc("Edit", 9, {"file_path": "/src/extra.py"}),
            _tc("Bash", 10, {"command": "pytest"}, output_preview="3 passed, 4 failed"),
        ]
        cls = self._classify(calls)
        result = grade_outcome(calls, cls)
        assert result.tests_ended_green is False
        assert any("red" in c.lower() for c in result.concerns)

    def test_high_output_session(self):
        """The D→A case: many commits + tests = high score."""
        calls = [_tc("Read", 0)]
        seq = 1
        # Simulate 6 commit cycles with tests
        for i in range(6):
            calls.append(_tc("Edit", seq, {"file_path": f"/src/mod{i}.py"}))
            seq += 1
            calls.append(_tc("Bash", seq, {"command": "pytest"}, output_preview=f"{10 + i * 5} passed"))
            seq += 1
            calls.append(_tc("Bash", seq, {"command": f"git commit -m 'feat: mod{i}'"}))
            seq += 1
        cls = self._classify(calls)
        result = grade_outcome(calls, cls)
        assert result.grade == "A"
        assert result.commit_count == 6
        assert result.tests_ended_green is True

    def test_correlation_direction(self):
        """Verify: more commits + tests = higher score (not inverted)."""
        # Productive session
        productive_calls = [_tc("Read", 0)] + [
            item for i in range(4) for item in [
                _tc("Edit", i * 3 + 1, {"file_path": f"/src/f{i}.py"}),
                _tc("Bash", i * 3 + 2, {"command": "pytest"}, output_preview="20 passed"),
                _tc("Bash", i * 3 + 3, {"command": "git commit -m 'feat'"}),
            ]
        ]
        # Quiet session (lots of reads, no output)
        quiet_calls = [
            *[_tc("Read", i) for i in range(8)],
            _tc("Edit", 8, {"file_path": "/src/a.py"}),
            _tc("Edit", 9, {"file_path": "/src/b.py"}),
            _tc("Edit", 10, {"file_path": "/src/c.py"}),
            _tc("Edit", 11, {"file_path": "/src/d.py"}),
            _tc("Edit", 12, {"file_path": "/src/e.py"}),
        ]

        prod_cls = classify_session(productive_calls)
        quiet_cls = classify_session(quiet_calls)
        prod_result = grade_outcome(productive_calls, prod_cls)
        quiet_result = grade_outcome(quiet_calls, quiet_cls)

        assert prod_result.score > quiet_result.score, (
            f"Productive session ({prod_result.score}) should score higher "
            f"than quiet session ({quiet_result.score})"
        )


class TestOutcomeNarrativeFields:
    def test_thrashed_files_detected(self):
        """Files with 6+ edits should be flagged as thrashed."""
        calls = [_tc("Read", 0)] + [
            _tc("Edit", i + 1, {"file_path": "/src/main.py"}) for i in range(8)
        ] + [
            _tc("Bash", 9, {"command": "git commit -m 'fix'"}),
        ]
        cls = classify_session(calls)
        result = grade_outcome(calls, cls)
        assert "main.py" in result.thrashed_files
        assert result.thrashed_files["main.py"] == 8

    def test_commit_style_incremental(self):
        """3+ commits spread out should be 'incremental'."""
        calls = [_tc("Read", 0)]
        for i in range(4):
            calls.extend([
                _tc("Edit", i * 5 + 1, {"file_path": f"/src/f{i}.py"}),
                _tc("Read", i * 5 + 2),
                _tc("Read", i * 5 + 3),
                _tc("Read", i * 5 + 4),
                _tc("Bash", i * 5 + 5, {"command": "git commit -m 'feat'"}),
            ])
        cls = classify_session(calls)
        result = grade_outcome(calls, cls)
        assert result.commit_style == "incremental"
