"""Tests for cross-session behavioral recommendations."""

from sesh.analyzers.profile import BehavioralProfile, ThrashedFile, StuckPattern
from sesh.analyzers.profile_remediation import (
    generate_profile_remediations,
    format_profile_remediations,
)


def _base_profile(**kwargs) -> BehavioralProfile:
    """Create a profile with sensible defaults, overriding with kwargs."""
    defaults = {
        "total_sessions": 50,
        "sessions_analyzed": 40,
        "type_distribution": {"BUILD_TESTED": 15, "BUILD_UNCOMMITTED": 20, "RESEARCH": 5},
        "sessions_with_commits": 20,
        "total_commits": 45,
        "sessions_with_tests": 15,
        "test_resolution_rate": 0.93,
        "avg_edits_per_commit": 8.0,
        "median_edits_per_commit": 6.0,
        "sessions_with_stuck": 5,
    }
    defaults.update(kwargs)
    return BehavioralProfile(**defaults)


class TestLowCommitRate:
    def test_triggers_below_40_percent(self):
        profile = _base_profile(sessions_with_commits=10, sessions_analyzed=40)
        rems = generate_profile_remediations(profile)
        titles = [r.title for r in rems]
        assert "Low commit rate" in titles

    def test_does_not_trigger_above_40_percent(self):
        profile = _base_profile(sessions_with_commits=20, sessions_analyzed=40)
        rems = generate_profile_remediations(profile)
        titles = [r.title for r in rems]
        assert "Low commit rate" not in titles

    def test_needs_minimum_sessions(self):
        profile = _base_profile(sessions_analyzed=5, sessions_with_commits=1)
        rems = generate_profile_remediations(profile)
        titles = [r.title for r in rems]
        assert "Low commit rate" not in titles

    def test_includes_claude_md_snippet(self):
        profile = _base_profile(sessions_with_commits=10, sessions_analyzed=40)
        rems = generate_profile_remediations(profile)
        commit_rem = [r for r in rems if r.title == "Low commit rate"][0]
        assert commit_rem.claude_md_snippet is not None
        assert "Commit" in commit_rem.claude_md_snippet


class TestReadBeforeEdit:
    def test_triggers_on_edit_stuck_pattern(self):
        profile = _base_profile(
            stuck_patterns=[
                StuckPattern(
                    tool="Edit",
                    hint="<tool_use_error>File has not been read yet",
                    count=5,
                    avg_length=4.0,
                    position_bias="late",
                )
            ]
        )
        rems = generate_profile_remediations(profile)
        titles = [r.title for r in rems]
        assert "Read-before-edit violations" in titles

    def test_triggers_on_write_stuck_pattern(self):
        profile = _base_profile(
            stuck_patterns=[
                StuckPattern(
                    tool="Write",
                    hint="<tool_use_error>File has not been read yet",
                    count=3,
                    avg_length=3.0,
                    position_bias="mid",
                )
            ]
        )
        rems = generate_profile_remediations(profile)
        titles = [r.title for r in rems]
        assert "Read-before-edit violations" in titles

    def test_does_not_trigger_on_bash_stuck(self):
        profile = _base_profile(
            stuck_patterns=[
                StuckPattern(
                    tool="Bash",
                    hint="Exit code 1 Traceback",
                    count=5,
                    avg_length=3.0,
                    position_bias="mid",
                )
            ]
        )
        rems = generate_profile_remediations(profile)
        titles = [r.title for r in rems]
        assert "Read-before-edit violations" not in titles


class TestChronicallyReworkedFiles:
    def test_triggers_on_3_plus_sessions(self):
        profile = _base_profile(
            thrashed_files=[
                ThrashedFile(filename="cli.py", total_edits=58, session_count=4),
            ]
        )
        rems = generate_profile_remediations(profile)
        titles = [r.title for r in rems]
        assert "Chronically reworked files" in titles

    def test_does_not_trigger_under_3_sessions(self):
        profile = _base_profile(
            thrashed_files=[
                ThrashedFile(filename="main.py", total_edits=20, session_count=2),
            ]
        )
        rems = generate_profile_remediations(profile)
        titles = [r.title for r in rems]
        assert "Chronically reworked files" not in titles


class TestLateSessionFatigue:
    def test_triggers_when_majority_late(self):
        profile = _base_profile(
            stuck_position_distribution={
                "0-25%": 1,
                "25-50%": 1,
                "50-75%": 5,
                "75-100%": 3,
            }
        )
        rems = generate_profile_remediations(profile)
        titles = [r.title for r in rems]
        assert "Late-session fatigue pattern" in titles

    def test_does_not_trigger_when_early(self):
        profile = _base_profile(
            stuck_position_distribution={
                "0-25%": 5,
                "25-50%": 3,
                "50-75%": 1,
                "75-100%": 1,
            }
        )
        rems = generate_profile_remediations(profile)
        titles = [r.title for r in rems]
        assert "Late-session fatigue pattern" not in titles

    def test_needs_minimum_stuck_events(self):
        profile = _base_profile(
            stuck_position_distribution={
                "50-75%": 2,
                "75-100%": 1,
            }
        )
        rems = generate_profile_remediations(profile)
        titles = [r.title for r in rems]
        assert "Late-session fatigue pattern" not in titles


class TestHighRework:
    def test_triggers_above_15(self):
        profile = _base_profile(
            avg_edits_per_commit=18.0,
            median_edits_per_commit=15.0,
        )
        rems = generate_profile_remediations(profile)
        titles = [r.title for r in rems]
        assert "High rework per commit" in titles

    def test_does_not_trigger_below_15(self):
        profile = _base_profile(
            avg_edits_per_commit=8.0,
            median_edits_per_commit=6.0,
        )
        rems = generate_profile_remediations(profile)
        titles = [r.title for r in rems]
        assert "High rework per commit" not in titles


class TestFormatting:
    def test_empty_rems(self):
        output = format_profile_remediations([])
        assert "healthy" in output.lower()

    def test_formats_with_snippet(self):
        profile = _base_profile(sessions_with_commits=5, sessions_analyzed=40)
        rems = generate_profile_remediations(profile)
        output = format_profile_remediations(rems)
        assert "CLAUDE.md rule:" in output
        assert "Low commit rate" in output
