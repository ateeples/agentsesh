"""Cross-session behavioral profiling.

Analyzes patterns across multiple sessions to build a developer
behavioral profile. Answers: what are your recurring patterns,
where do you consistently get stuck, and are you improving?

This is the thing that actually changes behavior — not "your last
session was a B" but "across N sessions, here's what keeps happening."
"""

import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from ..parsers.base import ToolCall
from .collaboration import CollaborationAnalysis, analyze_collaboration
from .outcome_grader import (
    OutcomeNarrative,
    StuckEvent,
    detect_stuck_events,
    extract_test_snapshots,
    grade_outcome,
)
from .session_type import SessionClassification, classify_session


@dataclass
class StuckPattern:
    """A recurring stuck pattern across sessions."""

    tool: str  # Which tool causes the stuck event
    hint: str  # Common error message
    count: int  # How many sessions this appeared in
    avg_length: float  # Average streak length
    position_bias: str  # "early", "mid", "late" — when this tends to happen


@dataclass
class ThrashedFile:
    """A file that keeps getting thrashed across sessions."""

    filename: str
    total_edits: int
    session_count: int  # How many sessions touched this file heavily


@dataclass
class BehavioralProfile:
    """Cross-session behavioral analysis."""

    total_sessions: int
    sessions_analyzed: int  # Sessions with enough data to analyze

    # Session type breakdown
    type_distribution: dict[str, int] = field(default_factory=dict)

    # Shipping metrics
    sessions_with_commits: int = 0
    total_commits: int = 0
    avg_commits_per_build: float = 0.0

    # Test behavior
    sessions_with_tests: int = 0
    test_resolution_rate: float = 0.0  # % of tested sessions that end green
    avg_test_runs_per_session: float = 0.0

    # Stuck patterns
    stuck_patterns: list[StuckPattern] = field(default_factory=list)
    sessions_with_stuck: int = 0
    stuck_position_distribution: dict[str, int] = field(default_factory=dict)

    # File thrashing
    thrashed_files: list[ThrashedFile] = field(default_factory=list)

    # Efficiency
    avg_edits_per_commit: float = 0.0
    median_edits_per_commit: float = 0.0

    # Outcome grade distribution
    outcome_grades: dict[str, int] = field(default_factory=dict)
    avg_outcome_score: float = 0.0

    # Trend (is it getting better?)
    early_avg_score: float = 0.0
    recent_avg_score: float = 0.0
    trend: str = ""  # "improving", "stable", "declining"

    # Collaboration (cross-session)
    avg_collab_score: float = 0.0
    collab_grade_distribution: dict[str, int] = field(default_factory=dict)
    archetype_distribution: dict[str, int] = field(default_factory=dict)
    dominant_archetype: str = ""
    collab_trend: str = ""  # "improving", "stable", "declining"
    early_collab_score: float = 0.0
    recent_collab_score: float = 0.0
    avg_correction_rate: float = 0.0
    avg_affirmation_rate: float = 0.0
    avg_words_per_turn: float = 0.0


def build_profile(
    sessions: list[tuple[list[ToolCall], str]],
    paths: list[Path] | None = None,
) -> BehavioralProfile:
    """Build a behavioral profile from multiple sessions.

    Args:
        sessions: List of (tool_calls, session_id) tuples,
                  ordered chronologically (oldest first).
        paths: Optional list of JSONL paths, parallel to sessions.
               When provided, collaboration analysis is included.

    Returns:
        BehavioralProfile with cross-session analysis.

    Raises:
        ValueError: If paths is provided but length doesn't match sessions.
    """
    if paths is not None and len(paths) != len(sessions):
        raise ValueError(
            f"paths length ({len(paths)}) must match sessions length ({len(sessions)})"
        )

    profile = BehavioralProfile(
        total_sessions=len(sessions),
        sessions_analyzed=0,
    )

    if not sessions:
        return profile

    # Collect per-session data
    all_stuck: list[StuckEvent] = []
    all_thrash: dict[str, list[int]] = defaultdict(list)  # filename -> [edit_counts]
    edits_per_commit_list: list[float] = []
    outcome_scores: list[int] = []
    test_ended_green_count = 0
    test_ended_red_count = 0

    # Collaboration per-session data
    collab_scores: list[int] = []
    correction_rates: list[float] = []
    affirmation_rates: list[float] = []
    words_per_turn_all: list[float] = []
    for idx, (tool_calls, session_id) in enumerate(sessions):
        if len(tool_calls) < 10:
            continue

        profile.sessions_analyzed += 1

        # Classify and grade
        classification = classify_session(tool_calls)
        outcome = grade_outcome(tool_calls, classification)

        # Type distribution
        profile.type_distribution[classification.session_type] = (
            profile.type_distribution.get(classification.session_type, 0) + 1
        )

        # Shipping
        if classification.commit_count > 0:
            profile.sessions_with_commits += 1
            profile.total_commits += classification.commit_count

        # Tests
        if outcome.test_snapshots:
            profile.sessions_with_tests += 1
            if outcome.tests_ended_green is True:
                test_ended_green_count += 1
            elif outcome.tests_ended_green is False:
                test_ended_red_count += 1

        # Stuck events
        for se in outcome.stuck_events:
            all_stuck.append(se)
        if outcome.stuck_events:
            profile.sessions_with_stuck += 1

        # Thrashed files
        for filename, count in outcome.thrashed_files.items():
            all_thrash[filename].append(count)

        # Efficiency
        if outcome.edits_per_commit is not None:
            edits_per_commit_list.append(outcome.edits_per_commit)

        # Outcome scores
        if outcome.score is not None:
            outcome_scores.append(outcome.score)
            profile.outcome_grades[outcome.grade] = (
                profile.outcome_grades.get(outcome.grade, 0) + 1
            )

        # Collaboration analysis (if paths provided)
        if paths and idx < len(paths):
            try:
                collab = analyze_collaboration(paths[idx], tool_calls)
                if collab.human_turns >= 2 and collab.score is not None:
                    collab_scores.append(collab.score)
                    profile.collab_grade_distribution[collab.grade] = (
                        profile.collab_grade_distribution.get(collab.grade, 0) + 1
                    )
                    if collab.archetype:
                        profile.archetype_distribution[collab.archetype] = (
                            profile.archetype_distribution.get(collab.archetype, 0) + 1
                        )
                    correction_rates.append(collab.correction_rate)
                    affirmation_rates.append(collab.affirmation_rate)
                    words_per_turn_all.append(collab.avg_words_per_turn)
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                pass  # Skip sessions with file/parse errors

    # === Aggregate metrics ===

    # Shipping
    build_sessions = sum(
        1 for t, c in profile.type_distribution.items()
        if t.startswith("BUILD")
        for _ in range(c)
    )
    if build_sessions > 0:
        profile.avg_commits_per_build = profile.total_commits / build_sessions

    # Test resolution
    tested_total = test_ended_green_count + test_ended_red_count
    if tested_total > 0:
        profile.test_resolution_rate = test_ended_green_count / tested_total

    if profile.sessions_with_tests > 0:
        total_test_runs = sum(
            len(grade_outcome(tc, classify_session(tc)).test_snapshots)
            for tc, _ in sessions
            if len(tc) >= 10
        )
        profile.avg_test_runs_per_session = (
            total_test_runs / profile.sessions_with_tests
        )

    # Stuck patterns
    if all_stuck:
        # Group by tool
        tool_stucks: dict[str, list[StuckEvent]] = defaultdict(list)
        for se in all_stuck:
            tool_stucks[se.tool_name].append(se)

        for tool, events in sorted(
            tool_stucks.items(), key=lambda x: len(x[1]), reverse=True
        ):
            avg_len = sum(e.length for e in events) / len(events)
            avg_pos = sum(e.position_pct for e in events) / len(events)
            if avg_pos < 0.33:
                pos_bias = "early"
            elif avg_pos < 0.66:
                pos_bias = "mid"
            else:
                pos_bias = "late"

            # Most common hint
            hint_counter = Counter(e.hint[:40] for e in events)
            common_hint = hint_counter.most_common(1)[0][0]

            profile.stuck_patterns.append(StuckPattern(
                tool=tool,
                hint=common_hint,
                count=len(events),
                avg_length=round(avg_len, 1),
                position_bias=pos_bias,
            ))

        # Position distribution
        for se in all_stuck:
            if se.position_pct < 0.25:
                bucket = "0-25%"
            elif se.position_pct < 0.50:
                bucket = "25-50%"
            elif se.position_pct < 0.75:
                bucket = "50-75%"
            else:
                bucket = "75-100%"
            profile.stuck_position_distribution[bucket] = (
                profile.stuck_position_distribution.get(bucket, 0) + 1
            )

    # Thrashed files
    for filename, counts in sorted(
        all_thrash.items(), key=lambda x: sum(x[1]), reverse=True
    ):
        profile.thrashed_files.append(ThrashedFile(
            filename=filename,
            total_edits=sum(counts),
            session_count=len(counts),
        ))

    # Efficiency
    if edits_per_commit_list:
        profile.avg_edits_per_commit = round(
            sum(edits_per_commit_list) / len(edits_per_commit_list), 1
        )
        sorted_epc = sorted(edits_per_commit_list)
        profile.median_edits_per_commit = sorted_epc[len(sorted_epc) // 2]

    # Outcome score stats
    if outcome_scores:
        profile.avg_outcome_score = round(
            sum(outcome_scores) / len(outcome_scores), 1
        )

        # Trend: compare first third vs last third
        third = len(outcome_scores) // 3
        if third >= 3:
            profile.early_avg_score = round(
                sum(outcome_scores[:third]) / third, 1
            )
            profile.recent_avg_score = round(
                sum(outcome_scores[-third:]) / third, 1
            )
            diff = profile.recent_avg_score - profile.early_avg_score
            if diff > 5:
                profile.trend = "improving"
            elif diff < -5:
                profile.trend = "declining"
            else:
                profile.trend = "stable"

    # === Collaboration aggregation ===
    if collab_scores:
        profile.avg_collab_score = round(
            sum(collab_scores) / len(collab_scores), 1
        )

        # Dominant archetype
        if profile.archetype_distribution:
            profile.dominant_archetype = max(
                profile.archetype_distribution,
                key=profile.archetype_distribution.get,
            )

        # Collaboration trend: first third vs last third
        collab_third = len(collab_scores) // 3
        if collab_third >= 3:
            profile.early_collab_score = round(
                sum(collab_scores[:collab_third]) / collab_third, 1
            )
            profile.recent_collab_score = round(
                sum(collab_scores[-collab_third:]) / collab_third, 1
            )
            collab_diff = profile.recent_collab_score - profile.early_collab_score
            if collab_diff > 5:
                profile.collab_trend = "improving"
            elif collab_diff < -5:
                profile.collab_trend = "declining"
            else:
                profile.collab_trend = "stable"

        # Average rates
        if correction_rates:
            profile.avg_correction_rate = round(
                sum(correction_rates) / len(correction_rates), 3
            )
        if affirmation_rates:
            profile.avg_affirmation_rate = round(
                sum(affirmation_rates) / len(affirmation_rates), 3
            )
        if words_per_turn_all:
            profile.avg_words_per_turn = round(
                sum(words_per_turn_all) / len(words_per_turn_all), 1
            )

    return profile


def format_profile(profile: BehavioralProfile) -> str:
    """Format a behavioral profile for terminal display."""
    lines: list[str] = []

    lines.append("")
    lines.append("Behavioral Profile")
    lines.append("\u2501" * 40)
    lines.append("")
    lines.append(
        f"Sessions: {profile.sessions_analyzed} analyzed "
        f"(of {profile.total_sessions} total)"
    )

    # Type breakdown
    lines.append("")
    lines.append("Session Types")
    lines.append("\u2500" * 13)
    for stype, count in sorted(
        profile.type_distribution.items(),
        key=lambda x: x[1],
        reverse=True,
    ):
        pct = count / max(profile.sessions_analyzed, 1) * 100
        lines.append(f"  {stype:22s} {count:3d} ({pct:.0f}%)")

    # Shipping
    lines.append("")
    lines.append("Shipping")
    lines.append("\u2500" * 8)
    lines.append(
        f"  Sessions with commits: {profile.sessions_with_commits}"
        f" / {profile.sessions_analyzed}"
        f" ({profile.sessions_with_commits / max(profile.sessions_analyzed, 1) * 100:.0f}%)"
    )
    lines.append(f"  Total commits: {profile.total_commits}")
    lines.append(
        f"  Avg commits per build session: {profile.avg_commits_per_build:.1f}"
    )

    # Testing
    lines.append("")
    lines.append("Testing")
    lines.append("\u2500" * 7)
    lines.append(f"  Sessions with tests: {profile.sessions_with_tests}")
    lines.append(
        f"  Resolution rate: {profile.test_resolution_rate:.0%}"
        f" (sessions ending green / sessions with test results)"
    )

    # Efficiency
    if profile.avg_edits_per_commit > 0:
        lines.append("")
        lines.append("Efficiency")
        lines.append("\u2500" * 10)
        lines.append(f"  Avg edits/commit: {profile.avg_edits_per_commit}")
        lines.append(f"  Median edits/commit: {profile.median_edits_per_commit}")

    # Stuck patterns
    if profile.stuck_patterns:
        lines.append("")
        lines.append("Where You Get Stuck")
        lines.append("\u2500" * 19)
        lines.append(
            f"  {profile.sessions_with_stuck} of {profile.sessions_analyzed}"
            f" sessions had stuck events"
        )
        lines.append("")
        for sp in profile.stuck_patterns[:5]:
            lines.append(
                f"  {sp.tool:15s}  {sp.count}x  "
                f"avg {sp.avg_length} errors  "
                f"tends to happen {sp.position_bias}"
            )
            lines.append(f"    \"{sp.hint}\"")

        if profile.stuck_position_distribution:
            lines.append("")
            lines.append("  When you get stuck:")
            for bucket in ["0-25%", "25-50%", "50-75%", "75-100%"]:
                count = profile.stuck_position_distribution.get(bucket, 0)
                total_stuck = sum(profile.stuck_position_distribution.values())
                pct = count / max(total_stuck, 1) * 100
                bar = "#" * int(pct / 5)
                lines.append(f"    {bucket:8s}  {count:2d} ({pct:3.0f}%)  {bar}")

    # Thrashed files
    if profile.thrashed_files:
        lines.append("")
        lines.append("Most Reworked Files")
        lines.append("\u2500" * 19)
        for tf in profile.thrashed_files[:10]:
            lines.append(
                f"  {tf.filename:35s}  {tf.total_edits:3d} edits"
                f"  across {tf.session_count} session(s)"
            )

    # Outcome grades
    if profile.outcome_grades:
        lines.append("")
        lines.append("Outcome Grade Distribution")
        lines.append("\u2500" * 25)
        for grade in ["A", "B", "C", "D", "F"]:
            count = profile.outcome_grades.get(grade, 0)
            pct = count / max(sum(profile.outcome_grades.values()), 1) * 100
            bar = "\u2588" * int(pct / 3)
            lines.append(f"  {grade}  {count:3d} ({pct:4.1f}%)  {bar}")
        lines.append(f"  Average score: {profile.avg_outcome_score}")

    # Trend
    if profile.trend:
        lines.append("")
        lines.append("Trend")
        lines.append("\u2500" * 5)
        arrow = {"improving": "\u2191", "declining": "\u2193", "stable": "\u2192"}.get(
            profile.trend, ""
        )
        lines.append(
            f"  {arrow} {profile.trend.title()}: "
            f"early avg {profile.early_avg_score} → "
            f"recent avg {profile.recent_avg_score}"
        )

    # Collaboration
    if profile.collab_grade_distribution:
        lines.append("")
        lines.append("Collaboration")
        lines.append("\u2500" * 13)
        lines.append(f"  Average score: {profile.avg_collab_score}")

        # Archetype distribution
        if profile.archetype_distribution:
            lines.append("")
            total_arch = sum(profile.archetype_distribution.values())
            for archetype, count in sorted(
                profile.archetype_distribution.items(),
                key=lambda x: x[1],
                reverse=True,
            ):
                pct = count / max(total_arch, 1) * 100
                marker = " \u2190 dominant" if archetype == profile.dominant_archetype else ""
                lines.append(
                    f"  {archetype:22s} {count:3d} ({pct:.0f}%){marker}"
                )

        # Collaboration metrics
        if profile.avg_correction_rate > 0 or profile.avg_affirmation_rate > 0:
            lines.append("")
            lines.append(
                f"  Avg correction rate: {profile.avg_correction_rate:.0%}"
                f"  |  Avg affirmation rate: {profile.avg_affirmation_rate:.0%}"
            )
        if profile.avg_words_per_turn > 0:
            lines.append(f"  Avg words/turn: {profile.avg_words_per_turn:.0f}")

        # Collaboration trend
        if profile.collab_trend:
            arrow = {"improving": "\u2191", "declining": "\u2193", "stable": "\u2192"}.get(
                profile.collab_trend, ""
            )
            lines.append("")
            lines.append(
                f"  {arrow} {profile.collab_trend.title()}: "
                f"early avg {profile.early_collab_score} → "
                f"recent avg {profile.recent_collab_score}"
            )

    lines.append("")
    return "\n".join(lines)
