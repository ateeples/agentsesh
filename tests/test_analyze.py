"""Tests for sesh analyze — one-command session diagnostics.

Tests the full pipeline: JSONL → parse → analyze → report.
No database required.
"""

import json
from pathlib import Path

import pytest

from sesh.analyze import (
    AnalysisResult,
    FailurePoint,
    SessionStats,
    analysis_to_json,
    analyze_session,
    calculate_effective_time,
    estimate_cost,
    extract_stats,
    extract_token_usage,
    format_analysis,
    generate_summary,
    identify_failure_points,
)
from sesh.parsers.base import Pattern, SessionGrade, ToolCall

# --- Fixtures ---


def _write_session(lines: list[dict], tmp_path: Path, name: str = "session.jsonl") -> str:
    """Write JSONL lines to a file in tmp_path and return the path string."""
    path = tmp_path / name
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
    return str(path)


def _make_tool_call(name, seq, input_data=None, is_error=False, timestamp=None):
    """Create a ToolCall object."""
    return ToolCall(
        name=name,
        tool_id=f"t{seq}",
        input_data=input_data or {},
        output_preview="",
        output_length=0,
        is_error=is_error,
        timestamp=timestamp,
        categories=[],
        seq=seq,
    )


def _minimal_session() -> list[dict]:
    """Minimal valid Claude Code session: user prompt, 2 reads, 1 edit."""
    return [
        {"type": "user", "timestamp": "2026-03-13T10:00:00Z",
         "message": {"content": "Fix the auth bug"}},
        {"type": "assistant", "timestamp": "2026-03-13T10:00:30Z",
         "message": {"model": "claude-sonnet-4-20250514",
                      "usage": {"input_tokens": 5000, "output_tokens": 1000},
                      "content": [
                          {"type": "tool_use", "id": "t1", "name": "Read",
                           "input": {"file_path": "/src/auth.py"}},
                      ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1",
             "content": "def login(): pass"}
        ]}},
        {"type": "assistant", "timestamp": "2026-03-13T10:01:00Z",
         "message": {"model": "claude-sonnet-4-20250514",
                      "usage": {"input_tokens": 6000, "output_tokens": 1500},
                      "content": [
                          {"type": "tool_use", "id": "t2", "name": "Read",
                           "input": {"file_path": "/src/utils.py"}},
                      ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t2",
             "content": "def hash_pw(): pass"}
        ]}},
        {"type": "assistant", "timestamp": "2026-03-13T10:02:00Z",
         "message": {"model": "claude-sonnet-4-20250514",
                      "usage": {"input_tokens": 7000, "output_tokens": 2000},
                      "content": [
                          {"type": "tool_use", "id": "t3", "name": "Edit",
                           "input": {"file_path": "/src/auth.py",
                                     "old_string": "pass",
                                     "new_string": "return True"}},
                      ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t3",
             "content": "File updated"}
        ]}},
    ]


def _session_with_errors() -> list[dict]:
    """Session with blind edits, error streaks, and bash overuse."""
    return [
        {"type": "user", "timestamp": "2026-03-13T10:00:00Z",
         "message": {"content": "Refactor auth"}},
        # Read one file
        {"type": "assistant", "timestamp": "2026-03-13T10:00:30Z",
         "message": {"model": "claude-sonnet-4-20250514",
                      "usage": {"input_tokens": 5000, "output_tokens": 500},
                      "content": [
                          {"type": "thinking", "thinking": "Let me read the auth file first"},
                          {"type": "tool_use", "id": "t1", "name": "Read",
                           "input": {"file_path": "/src/auth.py"}},
                      ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1",
             "content": "def login(): pass"}
        ]}},
        # Blind edit — edit a file without reading it first
        {"type": "assistant", "timestamp": "2026-03-13T10:05:00Z",
         "message": {"model": "claude-sonnet-4-20250514",
                      "usage": {"input_tokens": 6000, "output_tokens": 800},
                      "content": [
                          {"type": "thinking", "thinking": "I know the pattern from other files, no need to read"},
                          {"type": "tool_use", "id": "t2", "name": "Edit",
                           "input": {"file_path": "/src/middleware.py",
                                     "old_string": "old", "new_string": "new"}},
                      ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t2", "is_error": True,
             "content": "Error: old_string not found in file"}
        ]}},
        # Error streak — 3 consecutive errors
        {"type": "assistant", "timestamp": "2026-03-13T10:06:00Z",
         "message": {"model": "claude-sonnet-4-20250514",
                      "usage": {"input_tokens": 7000, "output_tokens": 600},
                      "content": [
                          {"type": "thinking", "thinking": "Let me try again with different text"},
                          {"type": "tool_use", "id": "t3", "name": "Bash",
                           "input": {"command": "cat /src/middleware.py"}},
                      ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t3", "is_error": True,
             "content": "Error: No such file"}
        ]}},
        {"type": "assistant", "timestamp": "2026-03-13T10:07:00Z",
         "message": {"model": "claude-sonnet-4-20250514",
                      "usage": {"input_tokens": 8000, "output_tokens": 600},
                      "content": [
                          {"type": "tool_use", "id": "t4", "name": "Bash",
                           "input": {"command": "cat /src/middleware.ts"}},
                      ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t4", "is_error": True,
             "content": "Error: No such file"}
        ]}},
        {"type": "assistant", "timestamp": "2026-03-13T10:08:00Z",
         "message": {"model": "claude-sonnet-4-20250514",
                      "usage": {"input_tokens": 8500, "output_tokens": 600},
                      "content": [
                          {"type": "tool_use", "id": "t5", "name": "Bash",
                           "input": {"command": "grep -r middleware /src/"}},
                      ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t5", "is_error": True,
             "content": "Error: No such file or directory"}
        ]}},
        # Recovery — finally reads
        {"type": "assistant", "timestamp": "2026-03-13T10:10:00Z",
         "message": {"model": "claude-sonnet-4-20250514",
                      "usage": {"input_tokens": 9000, "output_tokens": 500},
                      "content": [
                          {"type": "tool_use", "id": "t6", "name": "Glob",
                           "input": {"pattern": "**/middleware*"}},
                      ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t6",
             "content": "/app/middleware.py"}
        ]}},
        {"type": "assistant", "timestamp": "2026-03-13T10:11:00Z",
         "message": {"model": "claude-sonnet-4-20250514",
                      "usage": {"input_tokens": 9500, "output_tokens": 500},
                      "content": [
                          {"type": "tool_use", "id": "t7", "name": "Read",
                           "input": {"file_path": "/app/middleware.py"}},
                      ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t7",
             "content": "def middleware(): pass"}
        ]}},
        {"type": "assistant", "timestamp": "2026-03-13T10:12:00Z",
         "message": {"model": "claude-sonnet-4-20250514",
                      "usage": {"input_tokens": 10000, "output_tokens": 800},
                      "content": [
                          {"type": "tool_use", "id": "t8", "name": "Edit",
                           "input": {"file_path": "/app/middleware.py",
                                     "old_string": "pass",
                                     "new_string": "return True"}},
                      ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t8",
             "content": "File updated"}
        ]}},
    ]


# ============================================================
# extract_stats
# ============================================================


# --- Stats extraction from tool calls ---


class TestExtractStats:
    def test_basic_stats(self):
        tcs = [
            _make_tool_call("Read", 0, {"file_path": "/a.py"}),
            _make_tool_call("Read", 1, {"file_path": "/b.py"}),
            _make_tool_call("Edit", 2, {"file_path": "/a.py"}),
        ]
        stats = extract_stats(tcs, duration=2.0, model="claude-sonnet-4-20250514")
        assert stats.total_tool_calls == 3
        assert stats.total_errors == 0
        assert stats.error_rate == 0.0
        assert stats.duration_minutes == 2.0
        assert "/a.py" in stats.files_read
        assert "/b.py" in stats.files_read
        assert "/a.py" in stats.files_written
        assert stats.files_touched == 2  # a.py and b.py

    def test_error_rate(self):
        tcs = [
            _make_tool_call("Read", 0, is_error=False),
            _make_tool_call("Bash", 1, is_error=True),
            _make_tool_call("Bash", 2, is_error=True),
        ]
        stats = extract_stats(tcs)
        assert stats.total_errors == 2
        assert abs(stats.error_rate - 2 / 3) < 0.01

    def test_test_detection(self):
        tcs = [
            _make_tool_call("Bash", 0, {"command": "pytest tests/"}),
            _make_tool_call("Bash", 1, {"command": "pytest tests/"}, is_error=True),
            _make_tool_call("Bash", 2, {"command": "npm run build"}),
        ]
        stats = extract_stats(tcs)
        assert stats.test_runs == 2
        assert stats.test_passes == 1
        assert stats.test_failures == 1
        assert stats.build_runs == 1
        assert stats.build_passes == 1

    def test_empty_calls(self):
        stats = extract_stats([])
        assert stats.total_tool_calls == 0
        assert stats.error_rate == 0.0

    def test_files_touched_deduplication(self):
        tcs = [
            _make_tool_call("Read", 0, {"file_path": "/a.py"}),
            _make_tool_call("Read", 1, {"file_path": "/a.py"}),
            _make_tool_call("Edit", 2, {"file_path": "/a.py"}),
        ]
        stats = extract_stats(tcs)
        assert stats.files_touched == 1  # only /a.py


# ============================================================
# extract_token_usage
# ============================================================


class TestExtractTokenUsage:
    def test_extracts_tokens(self, tmp_path):
        path = _write_session([
            {"type": "assistant", "message": {
                "usage": {"input_tokens": 1000, "output_tokens": 500}}},
            {"type": "assistant", "message": {
                "usage": {"input_tokens": 2000, "output_tokens": 800}}},
            {"type": "user", "message": {"content": "hello"}},
        ], tmp_path, "tokens.jsonl")
        inp, out = extract_token_usage(Path(path))
        assert inp == 3000
        assert out == 1300

    def test_no_usage_data(self, tmp_path):
        path = _write_session([
            {"type": "assistant", "message": {"content": []}},
        ], tmp_path, "no-usage.jsonl")
        inp, out = extract_token_usage(Path(path))
        assert inp == 0
        assert out == 0


# ============================================================
# estimate_cost
# ============================================================


class TestEstimateCost:
    def test_sonnet_cost(self):
        # 1M input tokens @ $3, 1M output tokens @ $15
        cost = estimate_cost(1_000_000, 1_000_000, "claude-sonnet-4-20250514")
        assert abs(cost - 18.0) < 0.01

    def test_opus_cost(self):
        cost = estimate_cost(1_000_000, 1_000_000, "claude-opus-4-6")
        assert abs(cost - 90.0) < 0.01

    def test_unknown_model_defaults_sonnet(self):
        cost = estimate_cost(1_000_000, 1_000_000, "some-unknown-model")
        assert abs(cost - 18.0) < 0.01

    def test_zero_tokens(self):
        cost = estimate_cost(0, 0, "claude-sonnet-4-20250514")
        assert cost == 0.0

    def test_none_model(self):
        cost = estimate_cost(100_000, 50_000, None)
        # Should still work with default pricing
        assert cost > 0


# ============================================================
# identify_failure_points
# ============================================================


# --- Failure point identification ---


class TestIdentifyFailurePoints:
    def test_blind_edit_failure_point(self):
        patterns = [
            Pattern(type="write_without_read", severity="concern",
                    detail="1 edit(s) to unread files: middleware.py",
                    tool_indices=[1]),
        ]
        fps = identify_failure_points(patterns, total_steps=5, start_time=None)
        assert len(fps) == 1
        assert fps[0].category == "blind_edit"
        assert "middleware.py" in fps[0].description

    def test_error_streak_failure_point(self):
        patterns = [
            Pattern(type="error_streak", severity="warning",
                    detail="4 consecutive errors (last: Bash) — stuck in a loop?",
                    tool_indices=[2, 3, 4, 5]),
        ]
        fps = identify_failure_points(patterns, total_steps=8, start_time=None)
        assert len(fps) == 1
        assert fps[0].category == "error_loop"

    def test_no_patterns_no_failure_points(self):
        fps = identify_failure_points([], total_steps=5, start_time=None)
        assert fps == []

    def test_multiple_failure_points_sorted_by_seq(self):
        patterns = [
            Pattern(type="error_streak", severity="warning",
                    detail="3 errors", tool_indices=[5, 6, 7]),
            Pattern(type="write_without_read", severity="concern",
                    detail="1 blind edit", tool_indices=[2]),
        ]
        fps = identify_failure_points(patterns, total_steps=10, start_time=None)
        assert len(fps) == 2
        # Should be sorted by first tool index
        assert fps[0].seq < fps[1].seq

    def test_info_severity_excluded(self):
        patterns = [
            Pattern(type="bash_overuse", severity="info",
                    detail="5 bash anti-patterns", tool_indices=[0, 1, 2, 3, 4]),
        ]
        fps = identify_failure_points(patterns, total_steps=10, start_time=None)
        assert fps == []


# ============================================================
# generate_summary
# ============================================================


# --- Summary generation ---


class TestGenerateSummary:
    def test_clean_session_summary(self):
        stats = SessionStats(
            duration_minutes=5.0, total_tool_calls=10, total_errors=0,
            error_rate=0.0, files_touched=3,
        )
        grade = SessionGrade(grade="A", score=92)
        lines = generate_summary(stats, grade, [], [])
        text = "\n".join(lines)
        assert "clean" in text.lower() or "no" in text.lower()

    def test_failure_points_in_summary(self):
        stats = SessionStats(
            duration_minutes=20.0, total_tool_calls=30, total_errors=8,
            error_rate=0.27, files_touched=5,
        )
        grade = SessionGrade(grade="C", score=62)
        fps = [
            FailurePoint(seq=10, category="blind_edit",
                         description="Edited middleware.py without reading"),
            FailurePoint(seq=20, category="error_loop",
                         description="3 consecutive errors on Bash"),
        ]
        lines = generate_summary(stats, grade, [], fps)
        assert len(lines) > 0
        assert any("error" in line.lower() for line in lines)


# ============================================================
# calculate_effective_time
# ============================================================


class TestCalculateEffectiveTime:
    def test_no_failures_full_effective(self):
        stats = SessionStats(duration_minutes=30.0, total_tool_calls=20)
        eff = calculate_effective_time(stats, [])
        assert eff == 30.0

    def test_failure_at_minute_10(self):
        stats = SessionStats(duration_minutes=30.0, total_tool_calls=20)
        fps = [FailurePoint(seq=5, minute=10.0, category="blind_edit")]
        eff = calculate_effective_time(stats, fps)
        assert eff == 10.0

    def test_no_duration_returns_none(self):
        stats = SessionStats(total_tool_calls=20)
        eff = calculate_effective_time(stats, [])
        assert eff is None

    def test_multiple_failures_uses_earliest(self):
        stats = SessionStats(duration_minutes=60.0, total_tool_calls=50)
        fps = [
            FailurePoint(seq=20, minute=25.0, category="error_loop"),
            FailurePoint(seq=10, minute=15.0, category="blind_edit"),
        ]
        eff = calculate_effective_time(stats, fps)
        assert eff == 15.0


# ============================================================
# format_analysis
# ============================================================


# --- Human-readable output formatting ---


class TestFormatAnalysis:
    def _make_result(self, grade="B", score=80, patterns=None, fps=None):
        return AnalysisResult(
            session_id="test-session",
            source_path="/tmp/test.jsonl",
            stats=SessionStats(
                duration_minutes=10.0, total_tool_calls=20,
                total_errors=2, error_rate=0.1, files_touched=5,
                model="claude-sonnet-4-20250514",
                estimated_cost_usd=0.50,
            ),
            grade=SessionGrade(grade=grade, score=score),
            patterns=patterns or [],
            failure_points=fps or [],
            remediations=[],
            summary=["Session completed with minor issues."],
            effective_minutes=8.0,
        )

    def test_header_present(self):
        result = self._make_result()
        output = format_analysis(result)
        assert "Session Analysis" in output
        assert "10" in output  # duration
        assert "20" in output  # tool calls

    def test_grade_shown(self):
        result = self._make_result(grade="B", score=80)
        output = format_analysis(result)
        assert "B" in output
        assert "80" in output

    def test_cost_shown(self):
        result = self._make_result()
        output = format_analysis(result)
        assert "$0.50" in output

    def test_failure_points_in_verbose(self):
        result = self._make_result(fps=[
            FailurePoint(seq=5, category="blind_edit",
                         description="Edited auth.py without reading"),
        ])
        output = format_analysis(result, verbose=True)
        assert "Process Details" in output
        assert "blind" in output.lower() or "auth.py" in output

    def test_effective_time_shown(self):
        result = self._make_result()
        output = format_analysis(result)
        assert "Effective" in output or "effective" in output

    def test_remediations_in_verbose(self):
        from sesh.analyzers.remediation import Remediation
        result = self._make_result()
        result.remediations = [
            Remediation(
                pattern_type="write_without_read",
                title="Read before writing",
                severity="critical",
                description="Always read first.",
                actions=["Add to CLAUDE.md"],
                impact="Eliminates blind edits.",
            ),
        ]
        output = format_analysis(result, verbose=True)
        assert "Remediation" in output or "Process" in output
        assert "Read before writing" in output


# ============================================================
# analysis_to_json
# ============================================================


class TestAnalysisToJson:
    def test_valid_json(self):
        result = AnalysisResult(
            session_id="test",
            source_path="/tmp/test.jsonl",
            stats=SessionStats(total_tool_calls=5, total_errors=1, error_rate=0.2),
            grade=SessionGrade(grade="B", score=82),
            patterns=[],
            failure_points=[],
            remediations=[],
            summary=["Clean session."],
        )
        output = analysis_to_json(result)
        data = json.loads(output)
        assert data["session_id"] == "test"
        assert data["grade"] == "B"
        assert data["score"] == 82
        assert data["stats"]["total_tool_calls"] == 5

    def test_failure_points_in_json(self):
        result = AnalysisResult(
            session_id="test",
            source_path="/tmp/test.jsonl",
            stats=SessionStats(),
            grade=SessionGrade(grade="C", score=60),
            patterns=[Pattern(type="error_streak", severity="warning", detail="3 errors")],
            failure_points=[FailurePoint(seq=5, category="error_loop", description="stuck")],
            remediations=[],
            summary=["Issues found."],
        )
        output = analysis_to_json(result)
        data = json.loads(output)
        assert len(data["failure_points"]) == 1
        assert data["failure_points"][0]["category"] == "error_loop"
        assert len(data["patterns"]) == 1


# ============================================================
# analyze_session (integration)
# ============================================================


# --- Full pipeline integration ---


class TestAnalyzeSession:
    def test_minimal_session(self, tmp_path):
        path = _write_session(_minimal_session(), tmp_path, "minimal.jsonl")
        result = analyze_session(path)
        assert result.session_id  # has a session ID
        assert result.stats.total_tool_calls == 3
        assert result.stats.total_errors == 0
        assert result.grade.grade in ("A+", "A", "B", "N/A")  # clean session
        assert result.stats.model == "claude-sonnet-4-20250514"
        assert result.stats.input_tokens > 0
        assert result.stats.estimated_cost_usd > 0

    def test_session_with_errors(self, tmp_path):
        path = _write_session(_session_with_errors(), tmp_path, "errors.jsonl")
        result = analyze_session(path)
        assert result.stats.total_errors > 0
        assert len(result.patterns) > 0
        assert len(result.failure_points) > 0
        # Should have remediations for detected patterns
        assert len(result.remediations) > 0
        # Grade should be lower
        assert result.grade.score < 90

    def test_format_output(self, tmp_path):
        path = _write_session(_session_with_errors(), tmp_path, "fmt.jsonl")
        result = analyze_session(path)
        output = format_analysis(result)
        assert "Session Analysis" in output
        assert len(output) > 100  # non-trivial output

    def test_json_output(self, tmp_path):
        path = _write_session(_minimal_session(), tmp_path, "json-out.jsonl")
        result = analyze_session(path)
        output = analysis_to_json(result)
        data = json.loads(output)
        assert "session_id" in data
        assert "stats" in data
        assert "grade" in data
        assert "summary" in data

    def test_bad_file_raises(self, tmp_path):
        path = _write_session([{"type": "unknown", "data": "junk"}], tmp_path, "bad.jsonl")
        with pytest.raises(ValueError):
            analyze_session(path)

    def test_decision_points_extracted(self, tmp_path):
        """Session with thinking blocks should have decision points."""
        path = _write_session(_session_with_errors(), tmp_path, "dps.jsonl")
        result = analyze_session(path)
        assert len(result.decision_points) > 0

    def test_files_tracked(self, tmp_path):
        path = _write_session(_minimal_session(), tmp_path, "files.jsonl")
        result = analyze_session(path)
        assert "/src/auth.py" in result.stats.files_read
        assert "/src/utils.py" in result.stats.files_read
        assert "/src/auth.py" in result.stats.files_written
