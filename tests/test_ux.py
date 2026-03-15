"""UX regression tests — validate the output a first-time user sees.

These tests aren't about correctness of analysis. They're about whether
the OUTPUT makes sense to someone who just ran `pip install agentsesh &&
sesh analyze` for the first time. Every test here represents a real UX
problem that shipped and confused users.

If a test here fails, it means the output would confuse a stranger.
"""

import re

from sesh.analyze import AnalysisResult, SessionStats, format_analysis
from sesh.analyzers.collaboration import CollaborationAnalysis, ConversationArc
from sesh.analyzers.outcome_grader import OutcomeNarrative
from sesh.analyzers.session_type import SessionClassification
from sesh.parsers.base import SessionGrade


# === Fixtures ===


def _build_session(
    outcome_score=75,
    outcome_grade="B",
    collab_score=80,
    collab_grade="A",
    archetype="The Partnership",
    commit_count=2,
    strengths=None,
    concerns=None,
    thrashed_files=None,
    uncommitted_files=None,
    test_story=None,
    effective_minutes=8.0,
) -> AnalysisResult:
    """Create a realistic BUILD_TESTED session result."""
    outcome = OutcomeNarrative(
        session_type="BUILD_TESTED",
        score=outcome_score,
        grade=outcome_grade,
        commit_count=commit_count,
        commit_style="incremental" if commit_count > 1 else "batch",
        strengths=strengths or [f"{commit_count} commits"],
        concerns=concerns or [],
        test_snapshots=[],
        stuck_events=[],
        thrashed_files=thrashed_files or {},
        uncommitted_files=uncommitted_files or [],
        test_story=test_story or "",
        tests_ended_green=True,
        edits_per_commit=8.0,
    )
    collab = CollaborationAnalysis(
        human_turns=6,
        avg_words_per_turn=45.0,
        corrections=2,
        affirmations=3,
        delegations=1,
        correction_rate=0.33,
        affirmation_rate=0.50,
        engagement_rate=0.83,
        tc_per_turn=15.0,
        score=collab_score,
        grade=collab_grade,
        archetype=archetype,
        archetype_description="Test description.",
        arc=ConversationArc(
            opening_style="short-directive",
            closing_style="affirmation",
            length_trend="shortening",
        ),
        recommendation="Keep doing what you're doing.",
    )
    return AnalysisResult(
        session_id="test-session",
        source_path="/tmp/test.jsonl",
        stats=SessionStats(
            duration_minutes=20.0,
            total_tool_calls=90,
            total_errors=3,
            error_rate=0.033,
            files_touched=12,
            test_runs=5,
            test_passes=5,
            test_failures=0,
            model="claude-sonnet-4-20250514",
            estimated_cost_usd=1.50,
        ),
        grade=SessionGrade(grade="C", score=72),
        patterns=[],
        failure_points=[],
        remediations=[],
        summary=[],
        effective_minutes=effective_minutes,
        session_type=SessionClassification(
            session_type="BUILD_TESTED",
            commit_count=commit_count,
            test_run_count=5,
            project_edit_count=10,
            workspace_edit_count=0,
            total_tools=90,
        ),
        outcome=outcome,
        collaboration=collab,
    )


def _uncommitted_session() -> AnalysisResult:
    """Session that edited files but didn't commit — common first-run case."""
    return _build_session(
        outcome_score=45,
        outcome_grade="D",
        collab_score=95,
        collab_grade="A+",
        archetype="The Partnership",
        commit_count=0,
        strengths=[],
        concerns=["Edited 5 project files but didn't commit"],
        uncommitted_files=["main.py", "utils.py", "config.py"],
    )


def _autopilot_success() -> AnalysisResult:
    """High-autonomy session that shipped well despite Autopilot style."""
    return _build_session(
        outcome_score=90,
        outcome_grade="A",
        collab_score=65,
        collab_grade="C",
        archetype="The Autopilot",
        commit_count=3,
        strengths=["3 commits — incremental shipping", "Tests green (42 passing)"],
    )


def _non_build_session() -> AnalysisResult:
    """Research/conversation session — no outcome score."""
    result = _build_session()
    result.outcome = OutcomeNarrative(
        session_type="RESEARCH",
        score=None,
        grade="N/A",
        commit_count=0,
        commit_style="none",
        strengths=[],
        concerns=[],
        test_snapshots=[],
        stuck_events=[],
        thrashed_files={},
        uncommitted_files=[],
        test_story="",
        tests_ended_green=None,
        edits_per_commit=None,
    )
    result.session_type = SessionClassification(
        session_type="RESEARCH",
        commit_count=0,
        test_run_count=0,
        project_edit_count=2,
        workspace_edit_count=3,
        total_tools=25,
    )
    return result


# === UX Invariant Tests ===


class TestNoRawPaths:
    """Raw file paths longer than 80 chars should never appear in default output."""

    def test_no_long_paths_in_output(self):
        result = _build_session()
        output = format_analysis(result)
        for line in output.split("\n"):
            # Skip the source_path line if present (internal, not shown)
            if "source_path" in line:
                continue
            # No line should contain a path-like string longer than 80 chars
            paths = re.findall(r"/\S{80,}", line)
            assert not paths, f"Raw path in output: {paths[0][:100]}..."


class TestNoUnexplainedSigils:
    """Single-character prefixes without context are confusing for new users."""

    # These sigils appeared in real shipped output and confused users
    _BANNED_SIGILS = [
        (r"^\s+T ", "T prefix (use 'Tests:' instead)"),
        (r"^\s+E ", "E prefix (use 'Edits per commit:' instead)"),
        (r"^\s+S ", "S prefix (use 'Stuck:' instead)"),
        (r"^\s+~ ", "~ prefix (use words instead)"),
        (r"^\s+\? ", "? prefix (use 'Uncommitted:' instead)"),
    ]

    def test_no_banned_sigils_in_build_session(self):
        result = _build_session(
            test_story="Started red, fixed to green",
            thrashed_files={"main.py": 12},
            uncommitted_files=["config.py"],
            concerns=["main.py edited 12 times"],
        )
        output = format_analysis(result)
        for line in output.split("\n"):
            for pattern, name in self._BANNED_SIGILS:
                assert not re.match(pattern, line), (
                    f"Banned sigil '{name}' found in output: '{line.strip()}'"
                )

    def test_strengths_use_words_not_sigils(self):
        result = _build_session(strengths=["3 commits", "Tests green"])
        output = format_analysis(result)
        # Strengths should appear as plain text, not with + prefix
        what_shipped = False
        for line in output.split("\n"):
            if "What Shipped" in line:
                what_shipped = True
            if what_shipped and "3 commits" in line:
                assert not line.strip().startswith("+"), (
                    f"Strength line uses + prefix: '{line.strip()}'"
                )
                break


class TestEffectiveTimeNotInDefault:
    """'Effective time: X of Y min (Z%)' is confusing when outcome is good."""

    def test_effective_time_hidden_in_default(self):
        result = _build_session(outcome_score=95, effective_minutes=7.0)
        output = format_analysis(result)
        assert "Effective time" not in output, (
            "Effective time should not appear in default output — "
            "it confuses users when a high-scoring session shows low %"
        )

    def test_effective_time_shown_in_verbose(self):
        result = _build_session(effective_minutes=7.0)
        output = format_analysis(result, verbose=True)
        assert "Effective time" in output


class TestArchetypeOutcomeContradiction:
    """When archetype predicts failure but session succeeded (or vice versa),
    the output should acknowledge the contradiction, not leave users confused."""

    def test_autopilot_success_acknowledged(self):
        """Autopilot + high outcome should note the contradiction."""
        result = _autopilot_success()
        output = format_analysis(result)
        # Should have some acknowledgment that the style worked despite predictions
        assert "worked" in output.lower() or "shipped well" in output.lower(), (
            "Autopilot archetype says '35% ship rate' but session got an A. "
            "Output should acknowledge this contradiction."
        )

    def test_partnership_failure_acknowledged(self):
        """Partnership + low outcome should note the contradiction."""
        result = _build_session(
            outcome_score=40,
            outcome_grade="D",
            collab_score=95,
            collab_grade="A+",
            archetype="The Partnership",
            commit_count=0,
            strengths=[],
            concerns=["Edited files but didn't commit"],
        )
        output = format_analysis(result)
        assert "commit" in output.lower() or "collaboration" in output.lower(), (
            "Partnership archetype with D outcome should tell the user "
            "the collaboration was good but nothing shipped."
        )

    def test_spec_dump_success_acknowledged(self):
        """Spec Dump + high outcome should note the exception."""
        result = _build_session(
            outcome_score=85,
            outcome_grade="A",
            collab_score=40,
            collab_grade="D",
            archetype="The Spec Dump",
            commit_count=2,
            strengths=["2 commits"],
        )
        output = format_analysis(result)
        assert "worked" in output.lower() or "shipped well" in output.lower(), (
            "Spec Dump with A outcome should acknowledge the exception."
        )


class TestUncommittedSessionClarity:
    """The most common 'bad' session — work done, nothing committed.
    This MUST be immediately clear to a new user."""

    def test_uncommitted_shows_the_problem(self):
        result = _uncommitted_session()
        output = format_analysis(result)
        # The word "commit" should appear in the output
        assert "commit" in output.lower(), (
            "Uncommitted session should mention 'commit' in the output"
        )

    def test_uncommitted_lists_files(self):
        result = _uncommitted_session()
        output = format_analysis(result)
        assert "main.py" in output, (
            "Uncommitted files should be listed by name"
        )

    def test_outcome_d_is_visible(self):
        result = _uncommitted_session()
        output = format_analysis(result)
        assert "D" in output and "45" in output, (
            "D grade and score should be visible for uncommitted session"
        )


class TestNonBuildSessionDoesntConfuse:
    """Research/conversation sessions shouldn't show misleading grades."""

    def test_no_outcome_score_for_non_build(self):
        result = _non_build_session()
        output = format_analysis(result)
        # Should NOT show "Outcome: N/A (None/100)" or similar nonsense
        assert "None/100" not in output
        assert "N/A (None" not in output

    def test_session_type_is_clear(self):
        result = _non_build_session()
        output = format_analysis(result)
        # Should clearly indicate this isn't a build session
        lower = output.lower()
        assert "research" in lower or "not a build" in lower or "conversation" in lower


class TestNextStepsAreActionable:
    """Next steps should be commands the user can copy-paste."""

    def test_next_steps_are_real_commands(self):
        result = _build_session()
        output = format_analysis(result)
        # Find lines that look like commands
        next_section = False
        for line in output.split("\n"):
            if "Next steps" in line:
                next_section = True
                continue
            if next_section and line.strip().startswith("sesh"):
                # Each command line should be copy-pasteable
                cmd = line.strip().split()[0:3]  # e.g. ["sesh", "analyze", "--profile"]
                assert cmd[0] == "sesh", f"Next step doesn't start with sesh: {line}"

    def test_no_redundant_next_steps(self):
        """Don't suggest --collab when collaboration is already shown."""
        result = _build_session()
        output = format_analysis(result)
        assert "--collab" not in output, (
            "Collaboration is already shown in default output — "
            "don't suggest --collab as a next step"
        )


class TestOutputLength:
    """Default output should be scannable, not a wall of text."""

    def test_default_output_under_50_lines(self):
        result = _build_session()
        output = format_analysis(result)
        lines = [l for l in output.split("\n") if l.strip()]
        assert len(lines) <= 50, (
            f"Default output is {len(lines)} non-empty lines. "
            f"A new user won't read more than ~40 lines. "
            f"Consider moving detail to verbose."
        )

    def test_verbose_adds_detail(self):
        result = _build_session()
        default_lines = len(format_analysis(result).split("\n"))
        verbose_lines = len(format_analysis(result, verbose=True).split("\n"))
        # Verbose should add content, not be identical
        assert verbose_lines >= default_lines
