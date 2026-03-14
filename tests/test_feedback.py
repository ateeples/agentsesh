"""Tests for the feedback module — closed-loop agent context injection."""



from sesh.analyze import AnalysisResult, FailurePoint, SessionStats
from sesh.feedback import MARKER_END, MARKER_START, generate_feedback, write_feedback
from sesh.parsers.base import Pattern, SessionGrade


def _make_result(**overrides) -> AnalysisResult:
    """Build a minimal AnalysisResult for testing."""
    defaults = dict(
        session_id="test-session-abc123",
        source_path="/tmp/test.jsonl",
        stats=SessionStats(total_tool_calls=50, total_errors=3, error_rate=0.06),
        grade=SessionGrade(grade="B", score=82),
        patterns=[
            Pattern(type="bash_overuse", severity="concern", detail="7/44 Bash calls used cat/grep/find"),
            Pattern(type="write_then_read", severity="concern", detail="Write-before-read phase at calls 26-35"),
        ],
        failure_points=[
            FailurePoint(seq=12, minute=2.5, category="blind_edit", description="Edited auth.py without reading it first"),
        ],
        remediations=[],
        summary=["Session had some issues"],
    )
    defaults.update(overrides)
    return AnalysisResult(**defaults)


class TestGenerateFeedback:
    def test_includes_markers(self):
        result = _make_result()
        content = generate_feedback(result)
        assert MARKER_START in content
        assert MARKER_END in content

    def test_includes_grade(self):
        result = _make_result()
        content = generate_feedback(result)
        assert "B (82/100)" in content

    def test_includes_stats(self):
        result = _make_result()
        content = generate_feedback(result)
        assert "50 tool calls" in content
        assert "3 errors" in content

    def test_includes_pattern_directives(self):
        result = _make_result()
        content = generate_feedback(result)
        assert "Read/Grep/Glob" in content  # bash_overuse directive
        assert "7 instances" in content  # count extracted from detail

    def test_includes_failure_points(self):
        result = _make_result()
        content = generate_feedback(result)
        assert "blind_edit" in content
        assert "auth.py" in content

    def test_clean_session_minimal(self):
        """A clean session with no patterns should still produce feedback."""
        result = _make_result(patterns=[], failure_points=[])
        content = generate_feedback(result)
        assert MARKER_START in content
        assert "50 tool calls" in content
        # No directives section
        assert "focus on" not in content

    def test_caps_directives_at_4(self):
        """Should not dump more than 4 directives."""
        patterns = [
            Pattern(type=f"pattern_{i}", severity="concern", detail=f"Detail {i}")
            for i in range(8)
        ]
        result = _make_result(patterns=patterns, failure_points=[])
        content = generate_feedback(result)
        # Count bullet points (no failure points in this result)
        focus_lines = [line for line in content.split("\n") if line.startswith("- ")]
        assert len(focus_lines) <= 4

    def test_info_patterns_excluded_from_directives(self):
        """Info-level patterns should not generate directives."""
        result = _make_result(patterns=[
            Pattern(type="error_rate", severity="info", detail="2% error rate"),
        ])
        content = generate_feedback(result)
        assert "focus on" not in content

    def test_test_stats_included(self):
        stats = SessionStats(total_tool_calls=80, test_runs=5, test_passes=4)
        result = _make_result(stats=stats, patterns=[])
        content = generate_feedback(result)
        assert "4/5 tests passed" in content


class TestWriteFeedback:
    def test_creates_new_file(self, tmp_path):
        target = tmp_path / "CLAUDE.md"
        content = f"{MARKER_START}\n## Test\n{MARKER_END}"
        wrote = write_feedback(content, target)
        assert wrote
        assert target.exists()
        assert MARKER_START in target.read_text()

    def test_appends_to_existing_file(self, tmp_path):
        target = tmp_path / "CLAUDE.md"
        target.write_text("# My Project\n\nExisting content.\n")
        content = f"{MARKER_START}\n## Test\n{MARKER_END}"
        wrote = write_feedback(content, target)
        assert wrote
        text = target.read_text()
        assert "Existing content." in text
        assert MARKER_START in text

    def test_replaces_existing_markers(self, tmp_path):
        target = tmp_path / "CLAUDE.md"
        target.write_text(
            f"# Project\n\n{MARKER_START}\n## Old feedback\n{MARKER_END}\n\n# Other stuff\n"
        )
        new_content = f"{MARKER_START}\n## New feedback\n{MARKER_END}"
        wrote = write_feedback(new_content, target)
        assert wrote
        text = target.read_text()
        assert "New feedback" in text
        assert "Old feedback" not in text
        assert "Other stuff" in text

    def test_no_change_returns_false(self, tmp_path):
        target = tmp_path / "CLAUDE.md"
        content = f"{MARKER_START}\n## Same\n{MARKER_END}"
        target.write_text(f"# Project\n\n{content}\n")
        wrote = write_feedback(content, target)
        assert not wrote

    def test_preserves_surrounding_content(self, tmp_path):
        target = tmp_path / "CLAUDE.md"
        target.write_text(
            f"# Before\n\n{MARKER_START}\nold\n{MARKER_END}\n\n# After\n"
        )
        new_content = f"{MARKER_START}\nnew\n{MARKER_END}"
        write_feedback(new_content, target)
        text = target.read_text()
        assert "# Before" in text
        assert "# After" in text
        assert "new" in text
