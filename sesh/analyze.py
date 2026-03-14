"""One-command session analysis — sesh analyze.

Point at a Claude Code transcript, get back: what happened,
what went wrong, why, and what to fix. No database. No setup.

Pipeline:
  JSONL → parse → [stats, patterns, grade, timeline, decisions]
                          ↓
                   identify failure points
                          ↓
                   generate summary + remediations
                          ↓
                   AnalysisResult → format
"""

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .parsers import parse_transcript
from .parsers.base import Pattern, SessionGrade, ToolCall, classify_tool
from .analyzers.patterns import detect_all_patterns
from .analyzers.grader import grade_session
from .analyzers.remediation import (
    Remediation,
    get_all_remediations,
)
from .analyzers.outcomes import TEST_PATTERNS, BUILD_PATTERNS, LINT_PATTERNS
from .replay import build_timeline_from_source, ReplayStep
from .debug import extract_decision_points, DecisionPoint


# Approximate model pricing (USD per million tokens) — input, output
_MODEL_PRICING = {
    "opus": (15.0, 75.0),
    "sonnet": (3.0, 15.0),
    "haiku": (0.25, 1.25),
}

# Pattern types that indicate real failure points (not just style)
_FAILURE_PATTERNS = {
    "write_without_read": "blind_edit",
    "error_streak": "error_loop",
    "repeated_search": "flailing",
    "write_then_read": "premature_action",
}


# --- Data types ---


@dataclass
class SessionStats:
    """Key statistics extracted from a session."""

    duration_minutes: float | None = None
    total_tool_calls: int = 0
    total_errors: int = 0
    error_rate: float = 0.0
    files_touched: int = 0
    files_read: list[str] = field(default_factory=list)
    files_written: list[str] = field(default_factory=list)
    test_runs: int = 0
    test_passes: int = 0
    test_failures: int = 0
    build_runs: int = 0
    build_passes: int = 0
    build_failures: int = 0
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float | None = None


@dataclass
class FailurePoint:
    """A key moment where the session went wrong."""

    seq: int  # Earliest tool index for this failure
    timestamp: str | None = None
    minute: float | None = None  # Minutes from session start
    category: str = ""  # "blind_edit", "error_loop", "flailing", etc.
    description: str = ""
    impact: str = ""
    thinking_context: str | None = None


@dataclass
class AnalysisResult:
    """Complete analysis result for a session."""

    session_id: str
    source_path: str
    stats: SessionStats
    grade: SessionGrade
    patterns: list[Pattern]
    failure_points: list[FailurePoint]
    remediations: list[Remediation]
    summary: list[str]
    effective_minutes: float | None = None
    timeline: list[ReplayStep] = field(default_factory=list)
    decision_points: list[DecisionPoint] = field(default_factory=list)


# --- Core functions ---


def extract_stats(
    tool_calls: list[ToolCall],
    duration: float | None = None,
    model: str | None = None,
) -> SessionStats:
    """Extract statistics from parsed tool calls.

    Args:
        tool_calls: Parsed ToolCall objects from the session.
        duration: Session duration in minutes (from parser).
        model: Model name string.

    Returns:
        SessionStats with all metrics populated.
    """
    stats = SessionStats(
        duration_minutes=duration,
        model=model,
        total_tool_calls=len(tool_calls),
    )

    if not tool_calls:
        return stats

    stats.total_errors = sum(1 for tc in tool_calls if tc.is_error)
    stats.error_rate = stats.total_errors / stats.total_tool_calls

    # File tracking
    read_files: set[str] = set()
    written_files: set[str] = set()

    for tc in tool_calls:
        path = tc.input_data.get("file_path", "") or tc.input_data.get("path", "")

        if tc.name in ("Read",):
            if path:
                read_files.add(path)
        elif tc.name in ("Edit", "Write"):
            if path:
                written_files.add(path)
        elif tc.name == "Bash":
            cmd = tc.input_data.get("command", "")
            if cmd and TEST_PATTERNS.search(cmd):
                stats.test_runs += 1
                if tc.is_error:
                    stats.test_failures += 1
                else:
                    stats.test_passes += 1
            if cmd and BUILD_PATTERNS.search(cmd):
                stats.build_runs += 1
                if tc.is_error:
                    stats.build_failures += 1
                else:
                    stats.build_passes += 1

    stats.files_read = sorted(read_files)
    stats.files_written = sorted(written_files)
    stats.files_touched = len(read_files | written_files)

    return stats


def extract_token_usage(path: Path) -> tuple[int, int]:
    """Extract total input and output tokens from JSONL.

    Sums usage.input_tokens and usage.output_tokens across all
    assistant messages in the transcript.

    Returns:
        (total_input_tokens, total_output_tokens)
    """
    input_tokens = 0
    output_tokens = 0

    with open(path) as f:
        for line in f:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("type") == "assistant":
                usage = d.get("message", {}).get("usage", {})
                input_tokens += usage.get("input_tokens", 0)
                output_tokens += usage.get("output_tokens", 0)

    return input_tokens, output_tokens


def estimate_cost(
    input_tokens: int,
    output_tokens: int,
    model_name: str | None,
) -> float:
    """Estimate cost in USD based on token counts and model.

    Uses approximate pricing. Defaults to sonnet pricing if model is unknown.
    """
    if input_tokens == 0 and output_tokens == 0:
        return 0.0

    key = None
    if model_name:
        model_lower = model_name.lower()
        for k in _MODEL_PRICING:
            if k in model_lower:
                key = k
                break
    if not key:
        key = "sonnet"

    input_price, output_price = _MODEL_PRICING[key]
    return (input_tokens * input_price + output_tokens * output_price) / 1_000_000


def identify_failure_points(
    patterns: list[Pattern],
    total_steps: int,
    start_time: str | None = None,
) -> list[FailurePoint]:
    """Convert detected patterns into failure points.

    Only patterns with severity "warning" or "concern" that have
    tool_indices become failure points. Info-level patterns are excluded.

    Args:
        patterns: Detected behavioral patterns.
        total_steps: Total number of tool calls in the session.
        start_time: ISO 8601 start timestamp for minute calculation.

    Returns:
        List of FailurePoint objects, sorted by sequence number.
    """
    failure_points: list[FailurePoint] = []

    for pattern in patterns:
        if pattern.severity == "info":
            continue

        category = _FAILURE_PATTERNS.get(pattern.type)
        if not category:
            # Map unknown concern/warning patterns generically
            category = pattern.type

        if not pattern.tool_indices:
            continue

        first_idx = min(pattern.tool_indices)

        failure_points.append(FailurePoint(
            seq=first_idx,
            category=category,
            description=pattern.detail,
            impact=_estimate_impact(pattern, total_steps),
        ))

    failure_points.sort(key=lambda fp: fp.seq)
    return failure_points


def generate_summary(
    stats: SessionStats,
    grade: SessionGrade,
    patterns: list[Pattern],
    failure_points: list[FailurePoint],
) -> list[str]:
    """Generate a human-readable summary of what happened.

    Returns a list of summary lines describing the session narrative.
    """
    lines: list[str] = []

    if not failure_points and not patterns and grade.score >= 80:
        lines.append(
            f"Clean session. {stats.total_tool_calls} tool calls, "
            f"no issues detected."
        )
        if stats.test_runs > 0:
            lines.append(
                f"Tests: {stats.test_passes}/{stats.test_runs} passed."
            )
        return lines

    # Describe the session arc
    if stats.total_tool_calls > 0:
        err_pct = f"{stats.error_rate:.0%}" if stats.error_rate > 0 else "0%"
        lines.append(
            f"{stats.total_tool_calls} tool calls, "
            f"{stats.total_errors} errors ({err_pct} error rate)."
        )

    # Describe failure points
    for fp in failure_points:
        if fp.minute is not None:
            lines.append(f"At minute {fp.minute:.0f}: {fp.description}")
        else:
            lines.append(f"At step {fp.seq}: {fp.description}")

    # If patterns but no failure points, mention style issues
    if patterns and not failure_points:
        pattern_types = [p.type.replace("_", " ") for p in patterns]
        lines.append(
            f"No critical failures, but {len(patterns)} process issue(s) detected: "
            f"{', '.join(pattern_types)}."
        )

    # Test/build results
    if stats.test_runs > 0:
        lines.append(
            f"Tests: {stats.test_passes}/{stats.test_runs} passed"
            f"{f', {stats.test_failures} failed' if stats.test_failures else ''}."
        )

    if stats.build_runs > 0:
        lines.append(
            f"Builds: {stats.build_passes}/{stats.build_runs} passed."
        )

    return lines


def calculate_effective_time(
    stats: SessionStats,
    failure_points: list[FailurePoint],
) -> float | None:
    """Calculate effective session time (before first major failure).

    Returns None if duration is unknown.
    """
    if stats.duration_minutes is None:
        return None

    if not failure_points:
        return stats.duration_minutes

    # Find earliest failure with a minute offset
    minutes_with_offset = [
        fp.minute for fp in failure_points if fp.minute is not None
    ]

    if minutes_with_offset:
        return min(minutes_with_offset)

    # No minute data — estimate from step position
    if stats.total_tool_calls > 0:
        earliest_seq = min(fp.seq for fp in failure_points)
        fraction = earliest_seq / stats.total_tool_calls
        return round(fraction * stats.duration_minutes, 1)

    return stats.duration_minutes


def analyze_session(path: str | Path) -> AnalysisResult:
    """Full one-shot analysis of a session transcript.

    No database, no prior setup. Parse → analyze → return.

    Args:
        path: Path to a Claude Code JSONL (or other supported format).

    Returns:
        AnalysisResult with stats, grade, patterns, failure points,
        remediations, and summary.

    Raises:
        ValueError: If the file can't be parsed.
    """
    path = Path(path)

    # 1. Parse transcript
    session = parse_transcript(path)

    # 2. Extract token usage (separate pass over JSONL)
    input_tokens, output_tokens = extract_token_usage(path)

    # 3. Build stats
    stats = extract_stats(
        session.tool_calls,
        duration=session.duration_minutes,
        model=session.model,
    )
    stats.input_tokens = input_tokens
    stats.output_tokens = output_tokens
    if input_tokens or output_tokens:
        stats.estimated_cost_usd = estimate_cost(
            input_tokens, output_tokens, session.model
        )

    # 4. Detect patterns
    patterns = detect_all_patterns(session.tool_calls)

    # 5. Grade
    grade = grade_session(session.tool_calls)

    # 6. Build timeline from source (for thinking blocks)
    timeline = build_timeline_from_source(str(path))

    # 7. Extract decision points
    decision_points = extract_decision_points(timeline) if timeline else []

    # 8. Identify failure points
    failure_points = identify_failure_points(
        patterns,
        total_steps=len(session.tool_calls),
        start_time=session.start_time,
    )

    # Enrich failure points with minute offsets and thinking context
    _enrich_failure_points(
        failure_points,
        session.tool_calls,
        decision_points,
        session.start_time,
        timeline,
    )

    # 9. Get remediations
    pattern_dicts = [
        {"type": p.type, "severity": p.severity, "detail": p.detail}
        for p in patterns
    ]
    remediations = get_all_remediations(pattern_dicts)

    # 10. Generate summary
    summary = generate_summary(stats, grade, patterns, failure_points)

    # 11. Calculate effective time
    effective = calculate_effective_time(stats, failure_points)

    return AnalysisResult(
        session_id=session.session_id,
        source_path=str(path),
        stats=stats,
        grade=grade,
        patterns=patterns,
        failure_points=failure_points,
        remediations=remediations,
        summary=summary,
        effective_minutes=effective,
        timeline=timeline,
        decision_points=decision_points,
    )


# --- Output formatting ---


def format_analysis(
    result: AnalysisResult,
    verbose: bool = False,
) -> str:
    """Format analysis result as human-readable text.

    Matches the design sketch output format.
    """
    lines: list[str] = []

    # Header
    lines.append("")
    lines.append("Session Analysis")
    lines.append("\u2501" * 40)
    lines.append("")

    # Stats line
    dur = f"{result.stats.duration_minutes:.0f} min" if result.stats.duration_minutes else "? min"
    cost = f" | ~${result.stats.estimated_cost_usd:.2f}" if result.stats.estimated_cost_usd else ""
    lines.append(f"Duration: {dur} | {result.stats.total_tool_calls} tool calls{cost}")

    # Files and tests
    parts = [f"Files touched: {result.stats.files_touched}"]
    if result.stats.test_runs > 0:
        parts.append(
            f"Tests: {result.stats.test_runs} run "
            f"({result.stats.test_passes} pass, {result.stats.test_failures} fail)"
        )
    lines.append(" | ".join(parts))

    # Grade
    lines.append(f"Grade: {result.grade.grade} ({result.grade.score}/100)")
    lines.append("")

    # What Happened
    lines.append("What Happened")
    lines.append("\u2500" * 13)
    for line in result.summary:
        lines.append(line)
    lines.append("")

    # Failure Points
    if result.failure_points:
        lines.append("Failure Points")
        lines.append("\u2500" * 14)
        for i, fp in enumerate(result.failure_points, 1):
            if fp.minute is not None:
                loc = f"min {fp.minute:.0f}"
            else:
                loc = f"step {fp.seq}"
            lines.append(f"{i}. [{loc}] {_failure_label(fp.category)}")
            lines.append(f"   {fp.description}")
            if fp.thinking_context and verbose:
                lines.append(f"   Thinking: \"{fp.thinking_context[:150]}\"")
            if fp.impact:
                lines.append(f"   Impact: {fp.impact}")
        lines.append("")

    # What To Fix
    if result.remediations:
        lines.append("What To Fix")
        lines.append("\u2500" * 11)
        severity_icon = {"critical": "!!!", "recommended": " !!", "optional": "  -"}
        for rem in result.remediations:
            icon = severity_icon.get(rem.severity, "  -")
            lines.append(f"[{icon}] {rem.title} ({rem.severity})")
            lines.append(f"      {rem.description}")
            if rem.impact:
                lines.append(f"      Impact: {rem.impact}")
            lines.append("")

    # Effective time
    if result.effective_minutes is not None and result.stats.duration_minutes:
        dur = result.stats.duration_minutes
        eff = result.effective_minutes
        pct = (eff / dur * 100) if dur > 0 else 100
        lines.append(f"Effective time: {eff:.0f} of {dur:.0f} min ({pct:.0f}%)")
    elif result.effective_minutes is not None:
        lines.append(f"Effective time: {result.effective_minutes:.0f} min")

    # Grade breakdown (verbose)
    if verbose and (result.grade.deductions or result.grade.bonuses):
        lines.append("")
        lines.append("Grade Breakdown")
        lines.append("\u2500" * 15)
        for d in result.grade.deductions:
            lines.append(f"  {d}")
        for b in result.grade.bonuses:
            lines.append(f"  {b}")

    return "\n".join(lines)


def analysis_to_json(
    result: AnalysisResult,
    verbose: bool = False,
) -> str:
    """Format analysis result as JSON."""
    data = {
        "session_id": result.session_id,
        "source_path": result.source_path,
        "grade": result.grade.grade,
        "score": result.grade.score,
        "stats": {
            "duration_minutes": result.stats.duration_minutes,
            "total_tool_calls": result.stats.total_tool_calls,
            "total_errors": result.stats.total_errors,
            "error_rate": round(result.stats.error_rate, 4),
            "files_touched": result.stats.files_touched,
            "files_read": result.stats.files_read,
            "files_written": result.stats.files_written,
            "test_runs": result.stats.test_runs,
            "test_passes": result.stats.test_passes,
            "test_failures": result.stats.test_failures,
            "build_runs": result.stats.build_runs,
            "build_passes": result.stats.build_passes,
            "build_failures": result.stats.build_failures,
            "model": result.stats.model,
            "input_tokens": result.stats.input_tokens,
            "output_tokens": result.stats.output_tokens,
            "estimated_cost_usd": result.stats.estimated_cost_usd,
        },
        "patterns": [
            {
                "type": p.type,
                "severity": p.severity,
                "detail": p.detail,
            }
            for p in result.patterns
        ],
        "failure_points": [
            {
                "seq": fp.seq,
                "category": fp.category,
                "description": fp.description,
                "impact": fp.impact,
                "minute": fp.minute,
                "thinking_context": fp.thinking_context if verbose else None,
            }
            for fp in result.failure_points
        ],
        "remediations": [
            {
                "pattern_type": r.pattern_type,
                "title": r.title,
                "severity": r.severity,
                "impact": r.impact,
            }
            for r in result.remediations
        ],
        "summary": result.summary,
        "effective_minutes": result.effective_minutes,
        "grade_breakdown": {
            "deductions": result.grade.deductions,
            "bonuses": result.grade.bonuses,
        },
    }
    return json.dumps(data, indent=2)


# --- Internal helpers ---


def _estimate_impact(pattern: Pattern, total_steps: int) -> str:
    """Estimate the impact of a pattern on the session."""
    affected = len(pattern.tool_indices) if pattern.tool_indices else 0

    if pattern.type == "write_without_read":
        return f"Caused errors from editing unread files."
    elif pattern.type == "error_streak":
        return f"{affected} tool calls wasted in error loop."
    elif pattern.type == "repeated_search":
        return f"{affected} redundant searches wasted context."
    elif pattern.type == "write_then_read":
        return "Acted before understanding — likely caused rework."
    elif pattern.type == "error_rate":
        return f"{affected} errors across the session."
    return ""


def _enrich_failure_points(
    failure_points: list[FailurePoint],
    tool_calls: list[ToolCall],
    decision_points: list[DecisionPoint],
    start_time: str | None,
    timeline: list[ReplayStep],
) -> None:
    """Add minute offsets and thinking context to failure points in-place."""
    start_dt = None
    if start_time:
        try:
            start_dt = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            pass

    # Build tool_index → ToolCall mapping
    for fp in failure_points:
        if fp.seq < len(tool_calls):
            tc = tool_calls[fp.seq]
            fp.timestamp = tc.timestamp

            # Calculate minute offset
            if start_dt and tc.timestamp:
                try:
                    tc_dt = datetime.fromisoformat(
                        tc.timestamp.replace("Z", "+00:00")
                    )
                    fp.minute = round(
                        (tc_dt - start_dt).total_seconds() / 60, 1
                    )
                except (ValueError, TypeError):
                    pass

    # Enrich with thinking context from decision points
    if not decision_points or not timeline:
        return

    # Map tool_call index → timeline step seq
    tool_idx_to_step_seq: dict[int, int] = {}
    tool_idx = 0
    for step in timeline:
        if step.type == "tool_call":
            tool_idx_to_step_seq[tool_idx] = step.seq
            tool_idx += 1

    # Map step seq → decision point
    step_seq_to_dp: dict[int, DecisionPoint] = {}
    for dp in decision_points:
        for action in dp.actions:
            step_seq_to_dp[action.seq] = dp

    for fp in failure_points:
        step_seq = tool_idx_to_step_seq.get(fp.seq)
        if step_seq is not None:
            dp = step_seq_to_dp.get(step_seq)
            if dp is not None:
                fp.thinking_context = dp.thinking.detail[:300]


def _failure_label(category: str) -> str:
    """Human-readable label for a failure category."""
    labels = {
        "blind_edit": "Blind edit",
        "error_loop": "Error loop",
        "flailing": "Flailing (repeated searches)",
        "premature_action": "Premature action",
        "error_rate": "High error rate",
    }
    return labels.get(category, category.replace("_", " ").title())
