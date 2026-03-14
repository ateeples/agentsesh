"""Remediation engine — actionable fixes for detected anti-patterns.

Maps each pattern type to concrete infrastructure changes that make
the bad pattern harder or impossible. Procedural gates > advice.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Remediation:
    """A concrete fix for a detected anti-pattern."""

    pattern_type: str
    title: str
    severity: str  # "critical", "recommended", "optional"
    description: str
    actions: list[str]  # Specific things to do
    claude_md_snippet: str | None = None  # If fix involves CLAUDE.md changes
    config_snippet: str | None = None  # If fix involves config changes
    impact: str = ""  # Expected improvement


# --- Remediation definitions ---
# Each function takes the pattern detail string and returns a Remediation.
# The detail string contains context about the specific instance.


def _remediate_repeated_search(detail: str) -> Remediation:
    return Remediation(
        pattern_type="repeated_search",
        title="Eliminate repeated searches",
        severity="recommended",
        description=(
            "Identical search calls waste context window and indicate the agent "
            "forgot previous results or is flailing without a plan."
        ),
        actions=[
            "Add to CLAUDE.md: 'Before searching, check if you already have the answer from a previous search in this conversation.'",
            "Use the Agent tool to delegate broad searches — it preserves results in its own context.",
            "If searching for the same thing twice, stop and re-read previous results instead.",
        ],
        claude_md_snippet=(
            "## Search discipline\n"
            "- Never repeat an identical search. Re-read previous results.\n"
            "- Use Agent tool for broad exploration that may need multiple queries.\n"
            "- Grep for content, Glob for files. Pick the right tool first time."
        ),
        impact="Reduces wasted tool calls and context window consumption.",
    )


def _remediate_write_without_read(detail: str) -> Remediation:
    return Remediation(
        pattern_type="write_without_read",
        title="Read before writing",
        severity="critical",
        description=(
            "Editing files without reading them first causes blind edits — "
            "wrong indentation, duplicate code, broken context. "
            "This is the single highest-impact fix for session quality."
        ),
        actions=[
            "Add to CLAUDE.md: 'ALWAYS read a file before editing it. No exceptions.'",
            "If using Claude Code, the Edit tool already enforces this — but Write does not.",
            "Review blind edits in the timeline to see which files were affected.",
        ],
        claude_md_snippet=(
            "## Read-before-write rule\n"
            "- ALWAYS Read a file before using Edit or Write on it.\n"
            "- No exceptions. Even for files you 'know' the contents of."
        ),
        impact="Eliminates blind edits. Typical score improvement: +5 to +15 points.",
    )


def _remediate_error_rate(detail: str) -> Remediation:
    # Parse error rate from detail if possible
    severity = "recommended"
    if "concern" in detail or any(c.isdigit() and int(c) > 2 for c in detail.split("/")[0][-2:] if c.isdigit()):
        severity = "critical"

    return Remediation(
        pattern_type="error_rate",
        title="Reduce error rate",
        severity=severity,
        description=(
            "High error rates indicate the agent is guessing instead of verifying. "
            "Most errors come from wrong file paths, bad assumptions about code structure, "
            "or syntax errors in edits."
        ),
        actions=[
            "Search for files before referencing them — never guess a path.",
            "Read surrounding code before editing to understand context.",
            "After writing code, run the build/tests immediately to catch errors early.",
            "Check error types in the timeline — path errors need Glob, syntax errors need Read.",
        ],
        claude_md_snippet=(
            "## Error prevention\n"
            "- Search for files (Glob/Grep) before referencing paths.\n"
            "- Read surrounding context before editing.\n"
            "- Run tests after every significant change."
        ),
        impact="Reducing error rate from 15% to 5% typically improves grade by one letter.",
    )


def _remediate_error_streak(detail: str) -> Remediation:
    return Remediation(
        pattern_type="error_streak",
        title="Break error loops",
        severity="critical",
        description=(
            "Consecutive errors mean the agent is stuck in a loop — retrying the same "
            "failing approach instead of stepping back. This is the most expensive "
            "anti-pattern in terms of wasted tokens and time."
        ),
        actions=[
            "Add to CLAUDE.md: 'After 2 consecutive errors, STOP. Re-read the error messages. Change your approach.'",
            "Add to CLAUDE.md: 'If a command fails, do not retry it unchanged. Diagnose first.'",
            "Consider adding disabled_tools for tools the agent misuses under pressure.",
        ],
        claude_md_snippet=(
            "## Error recovery\n"
            "- After 2 consecutive errors, STOP and reassess.\n"
            "- Never retry a failed command without changing something.\n"
            "- Read error output carefully before trying again.\n"
            "- If stuck for 3+ attempts, try a completely different approach."
        ),
        impact="Breaks the retry loop. Saves 5-20 wasted tool calls per incident.",
    )


def _remediate_low_read_ratio(detail: str) -> Remediation:
    return Remediation(
        pattern_type="low_read_ratio",
        title="Read more, write less",
        severity="recommended",
        description=(
            "A low read/write ratio means the agent is outputting code faster than "
            "it's understanding the codebase. This leads to edits that don't fit "
            "the existing patterns, duplicate implementations, and rework."
        ),
        actions=[
            "Add research-first gates: 'Before implementing, read all related files.'",
            "Use Grep to understand existing patterns before writing new code.",
            "Target a 3:1 read:write ratio for unfamiliar codebases.",
        ],
        claude_md_snippet=(
            "## Research-first development\n"
            "- Before implementing anything, read all related files first.\n"
            "- Use Grep to find existing patterns and conventions.\n"
            "- Understand the codebase before changing it."
        ),
        impact="Higher read ratio correlates with fewer rework cycles and cleaner PRs.",
    )


def _remediate_bash_overuse(detail: str) -> Remediation:
    return Remediation(
        pattern_type="bash_overuse",
        title="Use dedicated tools instead of Bash",
        severity="recommended",
        description=(
            "Using `cat`, `grep`, `find`, `sed` via Bash when dedicated tools exist "
            "(Read, Grep, Glob, Edit) bypasses the agent framework's tracking, makes "
            "output harder to parse, and loses structured data."
        ),
        actions=[
            "Add to CLAUDE.md: 'Use Read instead of cat/head/tail. Use Grep instead of grep/rg. Use Glob instead of find. Use Edit instead of sed/awk.'",
            "If the agent runtime supports disabled_tools, disable Bash for file operations.",
            "Reserve Bash for: git commands, npm/pip, docker, and other system operations.",
        ],
        claude_md_snippet=(
            "## Tool usage rules\n"
            "- Read instead of cat/head/tail\n"
            "- Grep instead of grep/rg\n"
            "- Glob instead of find/ls\n"
            "- Edit instead of sed/awk\n"
            "- Bash is for system commands only: git, npm, docker, etc."
        ),
        impact="Improves output parsing, reduces errors, typically +4-10 score points.",
    )


def _remediate_write_then_read(detail: str) -> Remediation:
    return Remediation(
        pattern_type="write_then_read",
        title="Research before implementation",
        severity="recommended",
        description=(
            "The session shows a phase where writing happened first, then reading — "
            "the agent started implementing before fully understanding the problem. "
            "This leads to rework when the read phase reveals the implementation was wrong."
        ),
        actions=[
            "Structure sessions in phases: explore -> plan -> implement -> verify.",
            "Add to CLAUDE.md: 'Read all relevant files before writing any code.'",
            "Use the Plan tool or TodoWrite to outline approach before coding.",
        ],
        claude_md_snippet=(
            "## Session structure\n"
            "- Phase 1: Read and explore (understand the problem)\n"
            "- Phase 2: Plan (outline the approach)\n"
            "- Phase 3: Implement (write code)\n"
            "- Phase 4: Verify (run tests, review)"
        ),
        impact="Eliminates rework from premature implementation.",
    )


def _remediate_scattered_files(detail: str) -> Remediation:
    return Remediation(
        pattern_type="scattered_files",
        title="Focus file access",
        severity="optional",
        description=(
            "Touching many directories in a single session can indicate unfocused "
            "exploration or scope creep. Sometimes it's legitimate (refactoring across "
            "a codebase), but often it means the agent is wandering."
        ),
        actions=[
            "Break large tasks into focused sub-tasks, each touching fewer directories.",
            "Use Agent tool to delegate exploration to a subagent — keeps main context clean.",
            "If refactoring, plan which files need changes upfront instead of discovering them ad-hoc.",
        ],
        impact="More focused sessions have fewer context switches and lower error rates.",
    )


def _remediate_missed_parallelism(detail: str) -> Remediation:
    return Remediation(
        pattern_type="missed_parallelism",
        title="Parallelize independent operations",
        severity="optional",
        description=(
            "Sequential reads of independent files waste time. When files don't depend "
            "on each other, reading them in parallel is faster and uses the same context."
        ),
        actions=[
            "Add to CLAUDE.md: 'When reading multiple independent files, batch them in a single response.'",
            "Same for Grep/Glob — if searches are independent, run them in parallel.",
            "Look for patterns like: Read A, Read B, Read C (independent) -> batch all three.",
        ],
        claude_md_snippet=(
            "## Parallelism\n"
            "- Batch independent Read/Grep/Glob calls in a single response.\n"
            "- If files don't depend on each other, read them in parallel."
        ),
        impact="Reduces session duration. Modest score bonus (+5) at 3+ parallel batches.",
    )


# Registry mapping pattern types to remediation functions
_REMEDIATIONS: dict[str, callable] = {
    "repeated_search": _remediate_repeated_search,
    "write_without_read": _remediate_write_without_read,
    "error_rate": _remediate_error_rate,
    "error_streak": _remediate_error_streak,
    "low_read_ratio": _remediate_low_read_ratio,
    "bash_overuse": _remediate_bash_overuse,
    "write_then_read": _remediate_write_then_read,
    "scattered_files": _remediate_scattered_files,
    "missed_parallelism": _remediate_missed_parallelism,
}


def get_remediation(pattern_type: str, detail: str = "") -> Remediation | None:
    """Get the remediation for a specific pattern type.

    Args:
        pattern_type: The anti-pattern type (e.g., "bash_overuse").
        detail: The pattern detail string for context-specific advice.

    Returns:
        Remediation object, or None if no remediation exists for this pattern.
    """
    func = _REMEDIATIONS.get(pattern_type)
    if func is None:
        return None
    return func(detail)


def get_all_remediations(
    patterns: list[dict],
) -> list[Remediation]:
    """Get remediations for all detected patterns in a session.

    Args:
        patterns: List of pattern dicts (as returned by db.get_patterns()).

    Returns:
        List of Remediation objects, ordered by severity (critical first).
    """
    severity_order = {"critical": 0, "recommended": 1, "optional": 2}
    remediations = []

    seen_types: set[str] = set()
    for p in patterns:
        ptype = p.get("type", p.get("pattern_type", ""))
        if ptype in seen_types:
            continue
        seen_types.add(ptype)

        detail = p.get("detail", "")
        rem = get_remediation(ptype, detail)
        if rem:
            remediations.append(rem)

    remediations.sort(key=lambda r: severity_order.get(r.severity, 9))
    return remediations


def format_remediations(remediations: list[Remediation], include_snippets: bool = True) -> str:
    """Format remediations as human-readable text.

    Args:
        remediations: List of Remediation objects.
        include_snippets: Whether to include CLAUDE.md snippets.

    Returns:
        Formatted string.
    """
    if not remediations:
        return "No remediations needed. Clean session."

    lines = ["## Remediations", ""]

    severity_icons = {"critical": "!!!", "recommended": " !!", "optional": "  -"}

    for rem in remediations:
        icon = severity_icons.get(rem.severity, "  -")
        lines.append(f"  [{icon}] {rem.title} ({rem.severity})")
        lines.append(f"        {rem.description}")
        lines.append("")
        lines.append("        Actions:")
        for action in rem.actions:
            lines.append(f"          - {action}")
        if rem.impact:
            lines.append(f"        Impact: {rem.impact}")

        if include_snippets and rem.claude_md_snippet:
            lines.append("")
            lines.append("        Add to CLAUDE.md:")
            for snippet_line in rem.claude_md_snippet.split("\n"):
                lines.append(f"          {snippet_line}")

        lines.append("")

    return "\n".join(lines)


def generate_claude_md_patch(remediations: list[Remediation]) -> str:
    """Generate a combined CLAUDE.md snippet from all remediations.

    This is the "just give me the fix" output — a single block of text
    that can be appended to an agent's CLAUDE.md to address all detected issues.

    Args:
        remediations: List of Remediation objects.

    Returns:
        Combined CLAUDE.md snippet ready to paste.
    """
    if not remediations:
        return ""

    snippets = []
    for rem in remediations:
        if rem.claude_md_snippet:
            snippets.append(rem.claude_md_snippet)

    if not snippets:
        return ""

    lines = [
        "# Process Rules (auto-generated by sesh)",
        "",
        "These rules address behavioral anti-patterns detected in session analysis.",
        "",
    ]
    lines.append("\n\n".join(snippets))
    lines.append("")

    return "\n".join(lines)
