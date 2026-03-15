"""Tests for cross-session collaboration evolution in profiles.

These test the integration of collaboration analysis into the behavioral
profile system — archetype distribution, score trends, and
collaboration-specific remediations.
"""

import json
import tempfile
from pathlib import Path

from sesh.analyzers.collaboration import CollaborationAnalysis, ConversationArc
from sesh.analyzers.profile import (
    BehavioralProfile,
    build_profile,
    format_profile,
)
from sesh.analyzers.profile_remediation import (
    generate_profile_remediations,
)
from sesh.parsers.base import ToolCall


_jsonl_counter = 0


def _make_jsonl(turns: list[str], tmp_dir: Path) -> Path:
    """Create a minimal JSONL transcript with given human turns."""
    global _jsonl_counter
    _jsonl_counter += 1
    path = tmp_dir / f"session_{_jsonl_counter}.jsonl"
    lines = []
    for i, text in enumerate(turns):
        # User turn
        lines.append(json.dumps({
            "type": "user",
            "message": {"content": [{"type": "text", "text": text}]},
            "timestamp": f"2026-03-15T10:{i:02d}:00Z",
        }))
        # Assistant turn (so tool calls have context)
        lines.append(json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Done."}]},
        }))
    path.write_text("\n".join(lines))
    return path


def _make_tool_calls(n: int) -> list[ToolCall]:
    """Create n dummy tool calls."""
    return [
        ToolCall(
            name="Read",
            tool_id=f"tc_{i}",
            input_data={"file_path": f"/tmp/file{i}.py"},
            output_preview="content",
            output_length=100,
            is_error=False,
        )
        for i in range(n)
    ]


# === BehavioralProfile collaboration fields ===


class TestProfileCollaborationFields:
    """Verify that BehavioralProfile has all collaboration fields."""

    def test_has_avg_collab_score(self):
        p = BehavioralProfile(total_sessions=0, sessions_analyzed=0)
        assert hasattr(p, "avg_collab_score")
        assert p.avg_collab_score == 0.0

    def test_has_collab_grade_distribution(self):
        p = BehavioralProfile(total_sessions=0, sessions_analyzed=0)
        assert hasattr(p, "collab_grade_distribution")
        assert p.collab_grade_distribution == {}

    def test_has_archetype_distribution(self):
        p = BehavioralProfile(total_sessions=0, sessions_analyzed=0)
        assert hasattr(p, "archetype_distribution")
        assert p.archetype_distribution == {}

    def test_has_dominant_archetype(self):
        p = BehavioralProfile(total_sessions=0, sessions_analyzed=0)
        assert hasattr(p, "dominant_archetype")
        assert p.dominant_archetype == ""

    def test_has_collab_trend(self):
        p = BehavioralProfile(total_sessions=0, sessions_analyzed=0)
        assert hasattr(p, "collab_trend")
        assert p.collab_trend == ""

    def test_has_early_and_recent_collab_score(self):
        p = BehavioralProfile(total_sessions=0, sessions_analyzed=0)
        assert hasattr(p, "early_collab_score")
        assert hasattr(p, "recent_collab_score")
        assert p.early_collab_score == 0.0
        assert p.recent_collab_score == 0.0

    def test_has_avg_correction_rate(self):
        p = BehavioralProfile(total_sessions=0, sessions_analyzed=0)
        assert hasattr(p, "avg_correction_rate")
        assert p.avg_correction_rate == 0.0

    def test_has_avg_affirmation_rate(self):
        p = BehavioralProfile(total_sessions=0, sessions_analyzed=0)
        assert hasattr(p, "avg_affirmation_rate")
        assert p.avg_affirmation_rate == 0.0

    def test_has_avg_words_per_turn(self):
        p = BehavioralProfile(total_sessions=0, sessions_analyzed=0)
        assert hasattr(p, "avg_words_per_turn")
        assert p.avg_words_per_turn == 0.0


# === build_profile with paths ===


class TestBuildProfileWithPaths:
    """Test that build_profile accepts paths and computes collaboration."""

    def test_accepts_paths_parameter(self):
        """build_profile should accept an optional paths parameter."""
        profile = build_profile([], paths=[])
        assert profile.total_sessions == 0

    def test_collaboration_computed_when_paths_provided(self):
        """When paths are provided, collaboration metrics should be populated."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            # Create a session with partnership-like turns
            turns = [
                "Build me a parser for JSON files",
                "No, not that way — use streaming instead",
                "Perfect, that's exactly what I wanted",
                "Great work. Now add error handling",
                "Yes, ship it",
                "Awesome job on the tests too",
            ]
            path = _make_jsonl(turns, tmp_path)
            tool_calls = _make_tool_calls(60)  # 10 tc/turn

            profile = build_profile(
                [(tool_calls, "session-1")],
                paths=[path],
            )

            assert profile.avg_collab_score > 0
            assert len(profile.collab_grade_distribution) > 0

    def test_collaboration_skipped_when_no_paths(self):
        """Without paths, collaboration fields stay at defaults."""
        tool_calls = _make_tool_calls(20)
        profile = build_profile([(tool_calls, "session-1")])
        assert profile.avg_collab_score == 0.0
        assert profile.archetype_distribution == {}

    def test_paths_length_must_match_sessions(self):
        """Paths list must match sessions list length."""
        tool_calls = _make_tool_calls(20)
        # This should work — paths[i] corresponds to sessions[i]
        with tempfile.TemporaryDirectory() as tmp:
            path = _make_jsonl(["hello"], Path(tmp))
            profile = build_profile(
                [(tool_calls, "s1")],
                paths=[path],
            )
            # Should not crash, even if the JSONL is minimal
            assert profile.sessions_analyzed >= 0

    def test_paths_length_mismatch_raises(self):
        """Mismatched paths and sessions lengths should raise ValueError."""
        import pytest

        tool_calls = _make_tool_calls(20)
        with tempfile.TemporaryDirectory() as tmp:
            path = _make_jsonl(["hello"], Path(tmp))
            with pytest.raises(ValueError, match="paths length"):
                build_profile(
                    [(tool_calls, "s1"), (tool_calls, "s2")],
                    paths=[path],  # Only 1 path for 2 sessions
                )

    def test_archetype_distribution_populated(self):
        """Archetype distribution should count archetypes across sessions."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            # Session 1: Partnership-like (needs affirmations>=3 AND corrections>=2)
            partnership_turns = [
                "Build the feature",
                "No, wrong approach",
                "Wait, not that either",
                "Good, keep going",
                "Yes perfect",
                "Great, that works",
                "Ship it, nice work",
            ]
            path1 = _make_jsonl(partnership_turns, tmp_path)
            tc1 = _make_tool_calls(60)

            # Session 2: Also partnership (different list object for unique path)
            partnership_turns2 = list(partnership_turns)
            path2 = _make_jsonl(partnership_turns2, tmp_path)
            tc2 = _make_tool_calls(60)

            profile = build_profile(
                [(tc1, "s1"), (tc2, "s2")],
                paths=[path1, path2],
            )

            # Should have archetype data
            total_archetypes = sum(profile.archetype_distribution.values())
            assert total_archetypes > 0

    def test_dominant_archetype_is_most_common(self):
        """dominant_archetype should be the archetype with highest count."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            # Create 3 sessions with partnership turns
            partnership_turns = [
                "Build the feature",
                "No, wrong approach",
                "Good, keep going",
                "Yes perfect",
                "Great, that works",
                "Ship it, nice work",
            ]
            sessions = []
            paths = []
            for i in range(3):
                p = _make_jsonl(partnership_turns, tmp_path)
                paths.append(p)
                sessions.append((_make_tool_calls(60), f"s{i}"))

            profile = build_profile(sessions, paths=paths)

            # dominant_archetype should be set
            if profile.archetype_distribution:
                most_common = max(
                    profile.archetype_distribution,
                    key=profile.archetype_distribution.get,
                )
                assert profile.dominant_archetype == most_common


class TestCollaborationTrend:
    """Test collaboration score trend detection."""

    def test_improving_trend(self):
        """When later sessions have higher collab scores, trend is improving."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            sessions = []
            paths = []

            # Early sessions: low engagement, no corrections, no affirmation
            # (low collab score: base 50, no engagement = -15, long prompts)
            for i in range(6):
                turns = [
                    "Build this entire application from scratch with all the detailed specifications",
                    "Now build the second part of the application with all the remaining features",
                ]
                p = _make_jsonl(turns, tmp_path)
                paths.append(p)
                sessions.append((_make_tool_calls(15), f"early-{i}"))

            # Later sessions: engaged partnership (high scores)
            for i in range(6):
                turns = [
                    "Build me a parser",
                    "No, use streaming instead",
                    "Wait, not that way",
                    "Perfect, keep going",
                    "Great work",
                    "Yes, ship it",
                    "Awesome",
                    "Do the tests too",
                    "Good, that works",
                    "Love it",
                ]
                p = _make_jsonl(turns, tmp_path)
                paths.append(p)
                sessions.append((_make_tool_calls(90), f"late-{i}"))

            profile = build_profile(sessions, paths=paths)

            # 12 sessions with collab scores, trend should be computed
            # Early sessions: ~35 score (no engagement, few turns)
            # Later sessions: ~95+ (corrections, affirmations, good autonomy)
            assert profile.collab_trend == "improving"

    def test_no_trend_with_few_sessions(self):
        """Trend requires minimum sessions to compute."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            turns = ["Hello", "Yes", "Good"]
            p = _make_jsonl(turns, tmp_path)
            profile = build_profile(
                [(_make_tool_calls(30), "s1")],
                paths=[p],
            )
            # With only 1 session, trend should be empty
            assert profile.collab_trend == ""


class TestCollaborationAverages:
    """Test that cross-session averages are computed correctly."""

    def test_avg_rates_computed(self):
        """Average correction/affirmation rates should be computed."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            sessions = []
            paths = []
            for i in range(3):
                turns = [
                    "Build feature X",
                    "No not that",
                    "Good, now continue",
                    "Yes ship it",
                    "Perfect",
                ]
                p = _make_jsonl(turns, tmp_path)
                paths.append(p)
                sessions.append((_make_tool_calls(50), f"s{i}"))

            profile = build_profile(sessions, paths=paths)

            # Should have nonzero rates
            assert profile.avg_correction_rate > 0
            assert profile.avg_affirmation_rate > 0

    def test_avg_words_per_turn_computed(self):
        """Cross-session avg words/turn should be populated."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            turns = ["Build me a thing", "Yes good", "Ship it"]
            p = _make_jsonl(turns, tmp_path)
            profile = build_profile(
                [(_make_tool_calls(30), "s1")],
                paths=[p],
            )

            assert profile.avg_words_per_turn > 0


# === format_profile collaboration section ===


class TestFormatProfileCollaboration:
    """Test that format_profile includes a collaboration section."""

    def test_collaboration_section_present(self):
        """When collab data exists, format_profile should include it."""
        p = BehavioralProfile(
            total_sessions=10,
            sessions_analyzed=10,
            avg_collab_score=72.5,
            collab_grade_distribution={"B": 5, "C": 3, "A": 2},
            archetype_distribution={"The Partnership": 5, "The Autopilot": 3, "The Struggle": 2},
            dominant_archetype="The Partnership",
            avg_correction_rate=0.25,
            avg_affirmation_rate=0.18,
            avg_words_per_turn=45.0,
            collab_trend="improving",
            early_collab_score=60.0,
            recent_collab_score=78.0,
        )
        output = format_profile(p)
        assert "Collaboration" in output
        assert "Partnership" in output

    def test_collaboration_section_absent_when_no_data(self):
        """When no collab data, section should not appear."""
        p = BehavioralProfile(
            total_sessions=10,
            sessions_analyzed=10,
        )
        output = format_profile(p)
        # No collaboration grade distribution = no collaboration section
        assert "Collaboration" not in output

    def test_archetype_distribution_shown(self):
        """Archetype distribution should be displayed."""
        p = BehavioralProfile(
            total_sessions=10,
            sessions_analyzed=10,
            avg_collab_score=65.0,
            collab_grade_distribution={"B": 4, "C": 2},
            archetype_distribution={"The Partnership": 4, "The Spec Dump": 2},
            dominant_archetype="The Partnership",
        )
        output = format_profile(p)
        assert "Partnership" in output

    def test_collab_trend_shown(self):
        """Collaboration trend should be displayed."""
        p = BehavioralProfile(
            total_sessions=20,
            sessions_analyzed=20,
            avg_collab_score=70.0,
            collab_grade_distribution={"B": 10, "A": 5, "C": 5},
            collab_trend="improving",
            early_collab_score=55.0,
            recent_collab_score=80.0,
            archetype_distribution={"The Partnership": 10},
            dominant_archetype="The Partnership",
        )
        output = format_profile(p)
        assert "improving" in output.lower() or "Improving" in output


# === Collaboration-specific remediations ===


def _collab_profile(**kwargs) -> BehavioralProfile:
    """Create a profile with collaboration fields, overriding with kwargs."""
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
        # Collaboration defaults
        "avg_collab_score": 65.0,
        "collab_grade_distribution": {"B": 15, "C": 20, "D": 5},
        "archetype_distribution": {"The Partnership": 15, "The Autopilot": 20, "The Struggle": 5},
        "dominant_archetype": "The Autopilot",
        "collab_trend": "stable",
        "early_collab_score": 63.0,
        "recent_collab_score": 67.0,
        "avg_correction_rate": 0.15,
        "avg_affirmation_rate": 0.10,
        "avg_words_per_turn": 80.0,
    }
    defaults.update(kwargs)
    return BehavioralProfile(**defaults)


class TestCollaborationRemediations:
    """Test collaboration-specific cross-session remediations."""

    def test_spec_dump_dominant_triggers_remediation(self):
        """When Spec Dump is the dominant archetype, recommend shorter prompts."""
        profile = _collab_profile(
            dominant_archetype="The Spec Dump",
            archetype_distribution={"The Spec Dump": 20, "The Partnership": 5},
            avg_words_per_turn=350.0,
        )
        rems = generate_profile_remediations(profile)
        titles = [r.title for r in rems]
        assert any("spec dump" in t.lower() or "prompt" in t.lower() or "collaboration" in t.lower() for t in titles)

    def test_autopilot_dominant_triggers_remediation(self):
        """When Autopilot is dominant, recommend more engagement."""
        profile = _collab_profile(
            dominant_archetype="The Autopilot",
            archetype_distribution={"The Autopilot": 25, "The Partnership": 5},
            avg_affirmation_rate=0.02,
            avg_correction_rate=0.03,
        )
        rems = generate_profile_remediations(profile)
        titles = [r.title for r in rems]
        assert any("autopilot" in t.lower() or "engag" in t.lower() or "collaboration" in t.lower() for t in titles)

    def test_declining_collab_triggers_remediation(self):
        """Declining collaboration trend should trigger a warning."""
        profile = _collab_profile(
            dominant_archetype="The Partnership",
            archetype_distribution={"The Partnership": 20, "The Struggle": 5},
            collab_trend="declining",
            early_collab_score=75.0,
            recent_collab_score=50.0,
        )
        rems = generate_profile_remediations(profile)
        titles = [r.title for r in rems]
        assert any("declin" in t.lower() or "collaboration" in t.lower() for t in titles)

    def test_partnership_dominant_no_remediation(self):
        """When Partnership is dominant and trend is good, no collab remediations."""
        profile = _collab_profile(
            dominant_archetype="The Partnership",
            archetype_distribution={"The Partnership": 30, "The Struggle": 5},
            collab_trend="stable",
            avg_correction_rate=0.25,
            avg_affirmation_rate=0.20,
            avg_collab_score=82.0,
        )
        rems = generate_profile_remediations(profile)
        titles = [r.title for r in rems]
        collab_titles = [t for t in titles if any(
            kw in t.lower() for kw in ["collaboration", "spec dump", "autopilot", "engag"]
        )]
        assert len(collab_titles) == 0

    def test_micromanager_dominant_triggers_remediation(self):
        """When Micromanager is dominant, recommend giving AI more room."""
        profile = _collab_profile(
            dominant_archetype="The Micromanager",
            archetype_distribution={"The Micromanager": 20, "The Struggle": 5},
        )
        rems = generate_profile_remediations(profile)
        titles = [r.title for r in rems]
        assert any("micromanag" in t.lower() or "collaboration" in t.lower() for t in titles)
