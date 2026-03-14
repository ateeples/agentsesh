"""Closed-loop feedback — write session findings to agent context files.

Generates session-specific feedback from analysis results and writes it
to the agent's context file (CLAUDE.md, .cursorrules, etc.) using
replaceable markers so each run overwrites the previous feedback.
"""

import re
from datetime import datetime
from pathlib import Path

from .analyze import AnalysisResult

# Markers for find-and-replace in target files
MARKER_START = "<!-- sesh:feedback -->"
MARKER_END = "<!-- /sesh:feedback -->"

# Pattern type → session-specific directive template.
# {detail} is replaced with the pattern's detail string.
_PATTERN_DIRECTIVES: dict[str, str] = {
    "bash_overuse": "Use Read/Grep/Glob instead of Bash for file operations",
    "write_then_read": "Read and explore before writing — understand first, then implement",
    "error_retry_loop": "When an approach fails, investigate root cause instead of retrying",
    "missed_parallelism": "Look for independent tool calls that can run in parallel",
    "blind_edit": "Always read a file before editing it",
    "path_guessing": "Search for files (Glob/Grep) before referencing paths",
    "over_reading": "Avoid reading too many files — stay focused on the task",
    "large_output_ignored": "When output is large, extract what you need instead of re-running",
}


def generate_feedback(result: AnalysisResult) -> str:
    """Generate concise, session-specific feedback for agent context injection.

    Returns a markdown block with markers for replacement on subsequent runs.
    Content is actionable — patterns with counts, not generic rules.
    """
    lines = [MARKER_START]

    # Header with grade and date
    date = datetime.now().strftime("%Y-%m-%d")
    lines.append(f"## Last Session: {result.grade.grade} ({result.grade.score}/100) — {date}")
    lines.append("")

    # Stats summary — one line
    stats = result.stats
    parts = [f"{stats.total_tool_calls} tool calls"]
    if stats.total_errors:
        parts.append(f"{stats.total_errors} errors")
    if stats.test_runs:
        parts.append(f"{stats.test_passes}/{stats.test_runs} tests passed")
    pattern_names = [p.type for p in result.patterns if p.severity != "info"]
    if pattern_names:
        parts.append(f"patterns: {', '.join(pattern_names)}")
    lines.append(". ".join(parts) + ".")
    lines.append("")

    # Directives from patterns — specific, with counts from this session
    directives = _build_directives(result)
    if directives:
        lines.append("**This session, focus on:**")
        for d in directives[:4]:  # Cap at 4 — more is noise
            lines.append(f"- {d}")
        lines.append("")

    # Top failure points — where things went wrong
    if result.failure_points:
        top_failures = result.failure_points[:2]
        lines.append("**Failure points from last session:**")
        for fp in top_failures:
            minute = f" at {fp.minute:.0f}m" if fp.minute else ""
            lines.append(f"- [{fp.category}]{minute}: {fp.description}")
        lines.append("")

    lines.append(MARKER_END)
    return "\n".join(lines)


def _build_directives(result: AnalysisResult) -> list[str]:
    """Convert patterns into specific directives with session data."""
    directives = []
    for pattern in result.patterns:
        base = _PATTERN_DIRECTIVES.get(pattern.type)
        if not base:
            # Unknown pattern — use the detail as-is
            if pattern.severity != "info":
                directives.append(pattern.detail)
            continue

        # Extract count from detail if available (e.g. "7/44 Bash calls...")
        count_match = re.match(r"(\d+)[/\s]", pattern.detail)
        if count_match:
            count = count_match.group(1)
            directives.append(f"{base} ({count} instances last session)")
        else:
            directives.append(base)

    return directives


def write_feedback(content: str, target: Path) -> bool:
    """Write feedback to a target file, replacing previous feedback if present.

    If the file contains sesh:feedback markers, replaces that section.
    If not, appends the feedback at the end.
    Returns True if the file was modified.
    """
    if target.exists():
        text = target.read_text()

        # Check for existing markers — replace the section
        pattern = re.compile(
            re.escape(MARKER_START) + r".*?" + re.escape(MARKER_END),
            re.DOTALL,
        )
        if pattern.search(text):
            new_text = pattern.sub(content, text)
            if new_text != text:
                target.write_text(new_text)
                return True
            return False

        # No markers — append with spacing
        separator = "\n\n" if text and not text.endswith("\n\n") else "\n" if text and not text.endswith("\n") else ""
        target.write_text(text + separator + content + "\n")
        return True
    else:
        # File doesn't exist — create with just the feedback
        target.write_text(content + "\n")
        return True
