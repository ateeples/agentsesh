"""Generate actionable recommendations from behavioral profiles.

Unlike single-session remediations (which say "you had 3 bash overuse calls"),
profile remediations are based on patterns ACROSS sessions:

- "You get stuck on read-before-edit in 25% of sessions → add this CLAUDE.md rule"
- "cli.py has been thrashed across 4 sessions → consider splitting it"
- "You only commit in 35% of sessions → add commit checkpoints"
- "You get stuck at 50-75% through sessions → consider shorter sessions"
"""

from dataclasses import dataclass

from .profile import BehavioralProfile


@dataclass
class ProfileRemediation:
    """A cross-session behavioral recommendation."""

    title: str
    severity: str  # "critical", "recommended", "insight"
    evidence: str  # What the data shows
    action: str  # What to do about it
    claude_md_snippet: str | None = None  # Ready-to-paste rule


def generate_profile_remediations(
    profile: BehavioralProfile,
) -> list[ProfileRemediation]:
    """Generate actionable recommendations from a behavioral profile.

    Only generates recommendations backed by data from multiple sessions.
    """
    rems: list[ProfileRemediation] = []

    # 1. Low commit rate
    if profile.sessions_analyzed >= 10:
        commit_rate = (
            profile.sessions_with_commits / profile.sessions_analyzed
        )
        if commit_rate < 0.4:
            rems.append(ProfileRemediation(
                title="Low commit rate",
                severity="critical",
                evidence=(
                    f"Only {profile.sessions_with_commits}/{profile.sessions_analyzed} "
                    f"sessions ({commit_rate:.0%}) produced commits."
                ),
                action=(
                    "Add commit checkpoints to your workflow. After completing "
                    "a logical unit of work, commit before starting the next."
                ),
                claude_md_snippet=(
                    "## Commit Discipline\n"
                    "After completing any logical unit of work (feature, fix, refactor):\n"
                    "1. Run tests\n"
                    "2. Commit with a descriptive message\n"
                    "3. Then continue to the next task\n"
                    "Do not accumulate uncommitted changes across multiple features."
                ),
            ))

    # 2. Low test frequency
    if profile.sessions_analyzed >= 10:
        build_count = sum(
            c for t, c in profile.type_distribution.items()
            if t.startswith("BUILD")
        )
        if build_count > 0:
            test_rate = profile.sessions_with_tests / build_count
            if test_rate < 0.3:
                rems.append(ProfileRemediation(
                    title="Low test frequency",
                    severity="critical",
                    evidence=(
                        f"Only {profile.sessions_with_tests}/{build_count} "
                        f"build sessions ({test_rate:.0%}) ran tests."
                    ),
                    action=(
                        "Run tests before committing. Your resolution rate is "
                        f"{profile.test_resolution_rate:.0%} — when you test, you fix. "
                        "The problem is you don't test often enough."
                    ),
                    claude_md_snippet=(
                        "## Test Before Commit\n"
                        "Before every `git commit`:\n"
                        "1. Run the test suite\n"
                        "2. If tests fail, fix before committing\n"
                        "3. Include test results in commit verification"
                    ),
                ))

    # 3. Read-before-edit stuck pattern
    for sp in profile.stuck_patterns:
        if sp.tool in ("Edit", "Write") and "not been read" in sp.hint:
            rems.append(ProfileRemediation(
                title="Read-before-edit violations",
                severity="critical",
                evidence=(
                    f"Stuck on \"file not read\" errors {sp.count} times across sessions. "
                    f"Average streak: {sp.avg_length} consecutive errors. "
                    f"Tends to happen {sp.position_bias} in sessions."
                ),
                action=(
                    "Always read a file before editing it. This is your most "
                    "common stuck pattern."
                ),
                claude_md_snippet=(
                    "## Read Before Write\n"
                    "ALWAYS use the Read tool on a file before using Edit or Write.\n"
                    "This is non-negotiable — it prevents the most common error pattern."
                ),
            ))
            break

    # 4. Thrashed files
    chronic_thrash = [
        tf for tf in profile.thrashed_files
        if tf.session_count >= 3
    ]
    if chronic_thrash:
        files_str = ", ".join(
            f"{tf.filename} ({tf.total_edits} edits across {tf.session_count} sessions)"
            for tf in chronic_thrash[:3]
        )
        rems.append(ProfileRemediation(
            title="Chronically reworked files",
            severity="recommended",
            evidence=f"Files repeatedly thrashed across sessions: {files_str}",
            action=(
                "These files are being edited heavily in multiple sessions. "
                "Consider: splitting into smaller modules, adding tests to lock "
                "behavior, or refactoring the interfaces they depend on."
            ),
        ))

    # 5. Late-session stuck events
    late_stuck = profile.stuck_position_distribution.get("50-75%", 0)
    very_late = profile.stuck_position_distribution.get("75-100%", 0)
    total_stuck = sum(profile.stuck_position_distribution.values())
    if total_stuck >= 5 and (late_stuck + very_late) / total_stuck > 0.5:
        rems.append(ProfileRemediation(
            title="Late-session fatigue pattern",
            severity="recommended",
            evidence=(
                f"{late_stuck + very_late}/{total_stuck} stuck events occur "
                f"in the second half of sessions."
            ),
            action=(
                "You get stuck more often later in sessions, likely due to "
                "context window pressure or decision fatigue. Consider: "
                "shorter sessions with explicit commit checkpoints, "
                "or a mid-session pause to review what you've done."
            ),
        ))

    # 6. High rework ratio
    if profile.avg_edits_per_commit > 15:
        rems.append(ProfileRemediation(
            title="High rework per commit",
            severity="insight",
            evidence=(
                f"Average {profile.avg_edits_per_commit} edits per commit "
                f"(median: {profile.median_edits_per_commit}). "
                "More editing per commit means more rework before shipping."
            ),
            action=(
                "Commit more frequently with smaller changes. "
                "Aim for under 10 edits per commit."
            ),
        ))

    return rems


def format_profile_remediations(rems: list[ProfileRemediation]) -> str:
    """Format remediations for terminal display."""
    if not rems:
        return "  No recommendations — your profile looks healthy."

    lines = []
    severity_icon = {"critical": "!!!", "recommended": " !!", "insight": "  →"}

    for rem in rems:
        icon = severity_icon.get(rem.severity, "  →")
        lines.append(f"[{icon}] {rem.title} ({rem.severity})")
        lines.append(f"      Evidence: {rem.evidence}")
        lines.append(f"      Action: {rem.action}")
        if rem.claude_md_snippet:
            lines.append(f"      CLAUDE.md rule:")
            for snippet_line in rem.claude_md_snippet.split("\n"):
                lines.append(f"        {snippet_line}")
        lines.append("")

    return "\n".join(lines)
