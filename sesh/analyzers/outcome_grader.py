"""Outcome-based session grading.

Replaces process-hygiene grading (bash overuse, write-before-read, etc.)
with outcome-oriented signals:

1. Did you ship? (commits)
2. Did tests end green? (test trajectory)
3. Did you get stuck? (error streaks)
4. How much rework? (edits per commit, file thrashing)

Only scores build sessions. Conversations, workspace, research → N/A.

Validated against 98 real sessions. Process grades were INVERSELY
correlated with shipping (A+ = 0.5 avg commits, D = 11 commits).
Outcome grades correlate positively (A = 5.2 avg commits, F = 0).
"""

import re
from dataclasses import dataclass, field
from collections import defaultdict

from ..parsers.base import ToolCall
from .session_type import SessionClassification

# Test result extraction from tool output previews
_PASSED_RE = re.compile(r"(\d+) passed")
_FAILED_RE = re.compile(r"(\d+) failed")


@dataclass
class TestSnapshot:
    """A single test run result at a point in the session."""

    tool_index: int
    passed: int
    failed: int


@dataclass
class StuckEvent:
    """A period where the agent was stuck in an error loop."""

    start_index: int
    length: int
    position_pct: float  # 0.0 - 1.0, where in the session this occurred
    tool_name: str  # Dominant tool causing errors
    hint: str  # First error snippet


@dataclass
class OutcomeNarrative:
    """Rich narrative output — what happened, not just a number."""

    session_type: str
    score: int | None = None  # None for non-build sessions
    grade: str = "N/A"  # "A", "B", "C", "D", "F", or "N/A"

    # What shipped
    commit_count: int = 0
    commit_style: str = ""  # "incremental", "batch", "none"

    # Test story
    test_snapshots: list[TestSnapshot] = field(default_factory=list)
    test_story: str = ""  # Human-readable test narrative
    tests_ended_green: bool | None = None

    # Where stuck
    stuck_events: list[StuckEvent] = field(default_factory=list)

    # Efficiency
    edits_per_commit: float | None = None
    thrashed_files: dict[str, int] = field(default_factory=dict)  # basename → edit count

    # Uncommitted work
    uncommitted_files: list[str] = field(default_factory=list)

    # Breakdown
    strengths: list[str] = field(default_factory=list)
    concerns: list[str] = field(default_factory=list)


def extract_test_snapshots(tool_calls: list[ToolCall]) -> list[TestSnapshot]:
    """Extract test results from tool call outputs.

    Test results (e.g. "355 passed, 2 failed") typically appear at the
    END of pytest/test runner output. We search both output_preview
    (first 300 chars) and output_tail (last 300 chars) to catch them
    regardless of output length.
    """
    snapshots = []
    for tc in tool_calls:
        # Search both head and tail of output
        text_to_search = tc.output_preview
        tail = getattr(tc, "output_tail", "")
        if tail:
            text_to_search = text_to_search + "\n" + tail

        if not text_to_search:
            continue

        # Prefer tail matches (more likely to be the final summary)
        if tail:
            passed_m = _PASSED_RE.search(tail)
            failed_m = _FAILED_RE.search(tail)
        else:
            passed_m = None
            failed_m = None

        # Fall back to preview if tail didn't match
        if not passed_m and not failed_m:
            passed_m = _PASSED_RE.search(tc.output_preview)
            failed_m = _FAILED_RE.search(tc.output_preview)

        if passed_m or failed_m:
            snapshots.append(TestSnapshot(
                tool_index=tc.seq,
                passed=int(passed_m.group(1)) if passed_m else 0,
                failed=int(failed_m.group(1)) if failed_m else 0,
            ))
    return snapshots


def detect_stuck_events(tool_calls: list[ToolCall]) -> list[StuckEvent]:
    """Detect error streaks (3+ consecutive errors).

    Returns structured stuck events with position, length, and context.
    """
    total = len(tool_calls)
    if total == 0:
        return []

    events = []
    streak_start = None
    streak_count = 0

    for i, tc in enumerate(tool_calls):
        if tc.is_error:
            if streak_start is None:
                streak_start = i
            streak_count += 1
        else:
            if streak_count >= 3 and streak_start is not None:
                # Find dominant error tool and first hint
                streak_tools = tool_calls[streak_start:streak_start + streak_count]
                from collections import Counter
                tool_counts = Counter(t.name for t in streak_tools)
                dominant = tool_counts.most_common(1)[0][0]
                hint = streak_tools[0].output_preview[:80]

                events.append(StuckEvent(
                    start_index=streak_start,
                    length=streak_count,
                    position_pct=round(streak_start / total, 3),
                    tool_name=dominant,
                    hint=hint,
                ))
            streak_start = None
            streak_count = 0

    # Handle streak at end of session
    if streak_count >= 3 and streak_start is not None:
        streak_tools = tool_calls[streak_start:streak_start + streak_count]
        from collections import Counter
        tool_counts = Counter(t.name for t in streak_tools)
        dominant = tool_counts.most_common(1)[0][0]
        hint = streak_tools[0].output_preview[:80]
        events.append(StuckEvent(
            start_index=streak_start,
            length=streak_count,
            position_pct=round(streak_start / total, 3),
            tool_name=dominant,
            hint=hint,
        ))

    return events


def _build_test_story(snapshots: list[TestSnapshot]) -> tuple[str, bool | None]:
    """Generate a human-readable test narrative.

    Returns (story_string, ended_green).
    """
    if not snapshots:
        return "", None

    last = snapshots[-1]
    had_failures = any(s.failed > 0 for s in snapshots)
    peak_fails = max(s.failed for s in snapshots)

    if last.failed == 0 and last.passed > 0:
        if had_failures:
            story = (
                f"Started with failures (peak: {peak_fails} failing), "
                f"fixed all → {last.passed} passing"
            )
        else:
            story = f"Tests green throughout ({last.passed} passing)"
        return story, True
    elif last.failed > 0:
        story = f"Tests RED at end: {last.passed} passing, {last.failed} failing"
        return story, False

    return f"{last.passed} passed, {last.failed} failed", last.failed == 0


def grade_outcome(
    tool_calls: list[ToolCall],
    classification: SessionClassification,
) -> OutcomeNarrative:
    """Grade a session based on outcomes, not process hygiene.

    Only scores build sessions. Non-build sessions get grade "N/A".

    Args:
        tool_calls: Ordered tool calls from the session.
        classification: Session type from classify_session().

    Returns:
        OutcomeNarrative with score, grade, and rich narrative data.
    """
    stype = classification.session_type
    narrative = OutcomeNarrative(session_type=stype)

    # Non-build sessions: not scored
    if stype in ("MINIMAL", "CONVERSATION", "WORKSPACE", "RESEARCH"):
        narrative.grade = "N/A"
        return narrative

    total = classification.total_tools
    commit_count = classification.commit_count

    # === Extract signals ===

    # Test snapshots
    test_snapshots = extract_test_snapshots(tool_calls)
    narrative.test_snapshots = test_snapshots
    test_story, ended_green = _build_test_story(test_snapshots)
    narrative.test_story = test_story
    narrative.tests_ended_green = ended_green

    # Stuck events
    stuck = detect_stuck_events(tool_calls)
    narrative.stuck_events = stuck

    # Commit analysis
    narrative.commit_count = commit_count
    commit_positions = [
        tc.seq for tc in tool_calls
        if tc.name == "Bash" and "git commit" in tc.input_data.get("command", "")
    ]
    if len(commit_positions) >= 3:
        spread = (commit_positions[-1] - commit_positions[0]) / max(total, 1)
        narrative.commit_style = "incremental" if spread > 0.4 else "batch"
    elif commit_positions:
        narrative.commit_style = "batch"
    else:
        narrative.commit_style = "none"

    # File edit analysis
    edits_per_file: dict[str, int] = defaultdict(int)
    project_files_edited = []
    from .session_type import is_workspace_file

    for tc in tool_calls:
        if tc.name in ("Edit", "Write"):
            fp = tc.input_data.get("file_path", "")
            if fp and not is_workspace_file(fp):
                edits_per_file[fp] += 1

    total_project_edits = sum(edits_per_file.values())

    # Edits per commit
    if commit_count > 0 and total_project_edits > 0:
        narrative.edits_per_commit = round(total_project_edits / commit_count, 1)

    # Thrashed files (6+ edits)
    for fp, count in edits_per_file.items():
        if count >= 6:
            basename = fp.rsplit("/", 1)[-1] if "/" in fp else fp
            narrative.thrashed_files[basename] = count

    # Uncommitted files
    if commit_count == 0 and edits_per_file:
        narrative.uncommitted_files = [
            fp.rsplit("/", 1)[-1] if "/" in fp else fp
            for fp in sorted(edits_per_file.keys())[:5]
        ]

    # === SCORING ===
    score = 50  # Neutral start

    # 1. SHIPPING (0-30 pts)
    if commit_count == 0:
        if total_project_edits > 0:
            # Scale penalty by how much work was done — editing 2 files
            # is less concerning than editing 15 files without committing
            if total_project_edits >= 15:
                score -= 15
                narrative.concerns.append(
                    f"Edited {len(edits_per_file)} project files ({total_project_edits} edits) but didn't commit"
                )
            else:
                score -= 5
                narrative.concerns.append(
                    f"Edited {len(edits_per_file)} project files but didn't commit"
                )
    elif commit_count <= 2:
        score += 15
        narrative.strengths.append(f"{commit_count} commits")
    elif commit_count <= 5:
        score += 25
        narrative.strengths.append(f"{commit_count} commits — incremental shipping")
    else:
        score += 30
        narrative.strengths.append(f"{commit_count} commits — high output")

    # 2. TESTING (0-25 pts)
    if test_snapshots:
        if ended_green:
            score += 15
            narrative.strengths.append(f"Tests green ({test_snapshots[-1].passed} passing)")
            # Bonus for resolving failures
            if any(s.failed > 0 for s in test_snapshots):
                score += 10
                narrative.strengths.append("Fixed failing tests during session")
        elif ended_green is False:
            score -= 10
            narrative.concerns.append(
                f"Tests RED at end ({test_snapshots[-1].failed} failing)"
            )
    elif commit_count > 0:
        score -= 5
        narrative.concerns.append("Committed without running tests")

    # 3. STUCK PENALTY (-20 max)
    for event in stuck:
        if event.length >= 5:
            score -= 15
            narrative.concerns.append(
                f"Stuck: {event.length} consecutive errors at "
                f"{event.position_pct:.0%} through session"
            )
        elif event.length >= 3:
            score -= 7
            narrative.concerns.append(
                f"Brief error streak ({event.length}) at "
                f"{event.position_pct:.0%} through session"
            )

    # 4. EFFICIENCY (0-10 pts)
    if narrative.edits_per_commit is not None:
        epc = narrative.edits_per_commit
        if epc <= 10:
            score += 10
            narrative.strengths.append(f"Efficient: {epc} edits/commit")
        elif epc <= 20:
            score += 5

    # 5. THRASHING PENALTY (-10 max)
    if narrative.thrashed_files:
        score -= min(10, len(narrative.thrashed_files) * 3)
        worst = max(narrative.thrashed_files.values())
        worst_file = [f for f, c in narrative.thrashed_files.items() if c == worst][0]
        narrative.concerns.append(
            f"{worst_file} edited {worst} times — consider restructuring"
        )

    # Clamp
    score = max(0, min(100, score))
    narrative.score = score

    # Grade
    if score >= 85:
        narrative.grade = "A"
    elif score >= 70:
        narrative.grade = "B"
    elif score >= 55:
        narrative.grade = "C"
    elif score >= 40:
        narrative.grade = "D"
    else:
        narrative.grade = "F"

    return narrative
