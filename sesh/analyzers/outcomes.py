"""Outcome-focused session analysis.

Measures what actually matters: did the task work? How much rework?
Did tests pass? Not "was your bash percentage low enough."

This is the diagnostic layer — form metrics tell you your grade,
outcome metrics tell you if you won.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

# Patterns that indicate test execution
TEST_PATTERNS = re.compile(
    r"(?:pytest|python -m pytest|npm test|npx vitest|vitest|jest|"
    r"cargo test|go test|make test|pnpm test|yarn test|bun test|"
    r"python.*unittest|rspec|mocha)",
    re.IGNORECASE,
)

# Patterns that indicate build execution
BUILD_PATTERNS = re.compile(
    r"(?:npm run build|pnpm build|yarn build|cargo build|"
    r"python -m build|tsc|make build|go build|"
    r"next build|vite build|webpack)",
    re.IGNORECASE,
)

# Patterns that indicate lint/typecheck
LINT_PATTERNS = re.compile(
    r"(?:eslint|pylint|flake8|mypy|pyright|tsc --noEmit|"
    r"clippy|golint|prettier --check|biome check)",
    re.IGNORECASE,
)


@dataclass
class OutcomeMetrics:
    """Outcome-focused metrics for a session.

    These measure results, not process aesthetics.
    """

    # Error-retry loops: error → retry similar action → still failing
    error_retry_loops: int = 0
    error_retry_details: list[str] = field(default_factory=list)

    # Rework: files edited more than once (had to go back and fix)
    files_reworked: int = 0
    rework_edits: int = 0  # total re-edit count beyond first edit
    rework_files: list[str] = field(default_factory=list)

    # Terminal state: how did the session end?
    ended_on_error: bool = False
    final_error_streak: int = 0

    # Efficiency
    total_tool_calls: int = 0
    total_errors: int = 0
    success_rate: float = 1.0  # (total - errors) / total

    # Verification outcomes
    test_runs: int = 0
    test_passes: int = 0
    test_failures: int = 0
    build_runs: int = 0
    build_passes: int = 0
    build_failures: int = 0
    lint_runs: int = 0
    lint_passes: int = 0
    lint_failures: int = 0


@dataclass
class OutcomeComparison:
    """Side-by-side comparison of two sessions' outcomes."""

    baseline: OutcomeMetrics
    candidate: OutcomeMetrics
    improvements: list[str] = field(default_factory=list)
    regressions: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    verdict: str = "unchanged"  # "improved", "regressed", "mixed", "unchanged"


def extract_outcomes(tool_calls: list[dict]) -> OutcomeMetrics:
    """Extract outcome metrics from a session's tool calls.

    Args:
        tool_calls: List of tool call dicts (as returned by db.get_tool_calls()).
                    Each has: name, input_json, is_error, output_preview, seq.

    Returns:
        OutcomeMetrics with all outcome signals extracted.
    """
    import contextlib
    import json

    metrics = OutcomeMetrics()
    metrics.total_tool_calls = len(tool_calls)

    if not tool_calls:
        return metrics

    # Parse input_json for each call
    parsed_calls = []
    for tc in tool_calls:
        input_data = {}
        if tc.get("input_json"):
            with contextlib.suppress(json.JSONDecodeError, TypeError):
                input_data = json.loads(tc["input_json"])
        parsed_calls.append({
            "name": tc.get("name", ""),
            "input_data": input_data,
            "is_error": bool(tc.get("is_error", False)),
            "output_preview": tc.get("output_preview", "") or "",
            "seq": tc.get("seq", 0),
        })

    # --- Error count and success rate ---
    metrics.total_errors = sum(1 for tc in parsed_calls if tc["is_error"])
    metrics.success_rate = (
        (metrics.total_tool_calls - metrics.total_errors) / metrics.total_tool_calls
        if metrics.total_tool_calls > 0
        else 1.0
    )

    # --- Terminal state ---
    metrics.final_error_streak = 0
    for tc in reversed(parsed_calls):
        if tc["is_error"]:
            metrics.final_error_streak += 1
        else:
            break
    metrics.ended_on_error = metrics.final_error_streak > 0

    # --- Error-retry loops ---
    # An error-retry loop: error on tool X with input ~Y, followed by
    # another call to tool X with similar input (retry), which also errors.
    metrics.error_retry_loops = 0
    metrics.error_retry_details = []
    i = 0
    while i < len(parsed_calls) - 1:
        if parsed_calls[i]["is_error"]:
            # Look ahead for retry of same tool
            j = i + 1
            while j < len(parsed_calls) and j <= i + 2:
                if parsed_calls[j]["name"] == parsed_calls[i]["name"]:
                    if parsed_calls[j]["is_error"]:
                        metrics.error_retry_loops += 1
                        metrics.error_retry_details.append(
                            f"{parsed_calls[i]['name']} at #{parsed_calls[i]['seq']}"
                            f"→#{parsed_calls[j]['seq']}"
                        )
                        i = j  # skip ahead past the retry
                    break
                j += 1
        i += 1

    # --- Rework detection ---
    # Track files that get Edit calls. Multiple edits = rework.
    edit_counts: Counter[str] = Counter()
    for tc in parsed_calls:
        if tc["name"] in ("Edit", "Write"):
            path = tc["input_data"].get("file_path", "")
            if path:
                edit_counts[path] += 1

    reworked = {path: count for path, count in edit_counts.items() if count > 1}
    metrics.files_reworked = len(reworked)
    metrics.rework_edits = sum(count - 1 for count in reworked.values())
    metrics.rework_files = sorted(reworked.keys())

    # --- Verification outcomes (test/build/lint) ---
    for tc in parsed_calls:
        if tc["name"] != "Bash":
            continue
        cmd = tc["input_data"].get("command", "")
        if not cmd:
            continue

        if TEST_PATTERNS.search(cmd):
            metrics.test_runs += 1
            if tc["is_error"]:
                metrics.test_failures += 1
            else:
                metrics.test_passes += 1

        if BUILD_PATTERNS.search(cmd):
            metrics.build_runs += 1
            if tc["is_error"]:
                metrics.build_failures += 1
            else:
                metrics.build_passes += 1

        if LINT_PATTERNS.search(cmd):
            metrics.lint_runs += 1
            if tc["is_error"]:
                metrics.lint_failures += 1
            else:
                metrics.lint_passes += 1

    return metrics


def compare_outcomes(
    baseline: OutcomeMetrics,
    candidate: OutcomeMetrics,
) -> OutcomeComparison:
    """Compare two sessions' outcome metrics.

    Args:
        baseline: The "before" session (pre-config-change).
        candidate: The "after" session (post-config-change).

    Returns:
        OutcomeComparison with improvements, regressions, and verdict.
    """
    comp = OutcomeComparison(baseline=baseline, candidate=candidate)

    # --- Compare each metric ---
    # Lower is better for these:
    _compare_lower(comp, "error_retry_loops",
                   baseline.error_retry_loops, candidate.error_retry_loops,
                   "error-retry loops")
    _compare_lower(comp, "files_reworked",
                   baseline.files_reworked, candidate.files_reworked,
                   "files reworked")
    _compare_lower(comp, "rework_edits",
                   baseline.rework_edits, candidate.rework_edits,
                   "rework edits")
    _compare_lower(comp, "total_errors",
                   baseline.total_errors, candidate.total_errors,
                   "total errors")
    _compare_lower(comp, "final_error_streak",
                   baseline.final_error_streak, candidate.final_error_streak,
                   "final error streak")

    # Higher is better for these:
    _compare_higher(comp, "success_rate",
                    baseline.success_rate, candidate.success_rate,
                    "success rate", fmt=".1%")

    # Test pass rate (if both ran tests)
    if baseline.test_runs > 0 and candidate.test_runs > 0:
        b_rate = baseline.test_passes / baseline.test_runs
        c_rate = candidate.test_passes / candidate.test_runs
        _compare_higher(comp, "test_pass_rate", b_rate, c_rate,
                        "test pass rate", fmt=".0%")
    elif candidate.test_runs > 0 and baseline.test_runs == 0:
        comp.improvements.append(f"test runs: started running tests (0 → {candidate.test_runs})")
    elif baseline.test_runs > 0 and candidate.test_runs == 0:
        comp.regressions.append(f"test runs: stopped running tests ({baseline.test_runs} → 0)")

    # Build pass rate (if both built)
    if baseline.build_runs > 0 and candidate.build_runs > 0:
        b_rate = baseline.build_passes / baseline.build_runs
        c_rate = candidate.build_passes / candidate.build_runs
        _compare_higher(comp, "build_pass_rate", b_rate, c_rate,
                        "build pass rate", fmt=".0%")

    # Ended on error
    if baseline.ended_on_error and not candidate.ended_on_error:
        comp.improvements.append("terminal state: ended clean (was: ended on error)")
    elif not baseline.ended_on_error and candidate.ended_on_error:
        comp.regressions.append("terminal state: ended on error (was: clean)")
    else:
        state = "on error" if baseline.ended_on_error else "clean"
        comp.unchanged.append(f"terminal state: both ended {state}")

    # --- Verdict ---
    if comp.improvements and not comp.regressions:
        comp.verdict = "improved"
    elif comp.regressions and not comp.improvements:
        comp.verdict = "regressed"
    elif comp.improvements and comp.regressions:
        comp.verdict = "mixed"
    else:
        comp.verdict = "unchanged"

    return comp


def format_outcome_metrics(metrics: OutcomeMetrics) -> str:
    """Format outcome metrics as human-readable text."""
    lines = [
        "## Outcome Metrics",
        "",
        f"  Success rate:      {metrics.success_rate:.1%} "
        f"({metrics.total_tool_calls - metrics.total_errors}/"
        f"{metrics.total_tool_calls} calls succeeded)",
        f"  Error-retry loops: {metrics.error_retry_loops}",
        f"  Files reworked:    {metrics.files_reworked} "
        f"({metrics.rework_edits} extra edits)",
        f"  Terminal state:    {'ERROR' if metrics.ended_on_error else 'clean'}"
        f"{f' (streak of {metrics.final_error_streak})' if metrics.final_error_streak else ''}",
    ]

    if metrics.test_runs > 0:
        lines.append(
            f"  Tests:             {metrics.test_passes}/{metrics.test_runs} passed"
            f"{f', {metrics.test_failures} failed' if metrics.test_failures else ''}"
        )
    if metrics.build_runs > 0:
        lines.append(
            f"  Builds:            {metrics.build_passes}/{metrics.build_runs} passed"
            f"{f', {metrics.build_failures} failed' if metrics.build_failures else ''}"
        )
    if metrics.lint_runs > 0:
        lines.append(
            f"  Lint/typecheck:    {metrics.lint_passes}/{metrics.lint_runs} passed"
        )

    if metrics.rework_files:
        lines.append("")
        lines.append("  Reworked files:")
        for f in metrics.rework_files[:5]:
            lines.append(f"    - {f}")
        if len(metrics.rework_files) > 5:
            lines.append(f"    ... and {len(metrics.rework_files) - 5} more")

    if metrics.error_retry_details:
        lines.append("")
        lines.append("  Error-retry loops:")
        for d in metrics.error_retry_details[:5]:
            lines.append(f"    - {d}")
        if len(metrics.error_retry_details) > 5:
            lines.append(f"    ... and {len(metrics.error_retry_details) - 5} more")

    return "\n".join(lines)


def format_comparison(comp: OutcomeComparison) -> str:
    """Format an outcome comparison as human-readable text."""
    verdict_icons = {
        "improved": "+",
        "regressed": "!",
        "mixed": "~",
        "unchanged": "=",
    }
    icon = verdict_icons.get(comp.verdict, "?")

    lines = [
        f"## Outcome Comparison  [{icon}] {comp.verdict.upper()}",
        "",
    ]

    if comp.improvements:
        lines.append("  Improvements:")
        for item in comp.improvements:
            lines.append(f"    + {item}")
        lines.append("")

    if comp.regressions:
        lines.append("  Regressions:")
        for item in comp.regressions:
            lines.append(f"    ! {item}")
        lines.append("")

    if comp.unchanged:
        lines.append("  Unchanged:")
        for item in comp.unchanged:
            lines.append(f"    = {item}")
        lines.append("")

    return "\n".join(lines)


# --- Internal helpers ---


def _compare_lower(
    comp: OutcomeComparison,
    metric_name: str,
    baseline_val: int | float,
    candidate_val: int | float,
    label: str,
    fmt: str = "d",
) -> None:
    """Compare a metric where lower is better."""
    if candidate_val < baseline_val:
        comp.improvements.append(
            f"{label}: {_fmt(baseline_val, fmt)} → {_fmt(candidate_val, fmt)}"
        )
    elif candidate_val > baseline_val:
        comp.regressions.append(
            f"{label}: {_fmt(baseline_val, fmt)} → {_fmt(candidate_val, fmt)}"
        )
    else:
        comp.unchanged.append(f"{label}: {_fmt(baseline_val, fmt)}")


def _compare_higher(
    comp: OutcomeComparison,
    metric_name: str,
    baseline_val: int | float,
    candidate_val: int | float,
    label: str,
    fmt: str = "d",
) -> None:
    """Compare a metric where higher is better."""
    if candidate_val > baseline_val:
        comp.improvements.append(
            f"{label}: {_fmt(baseline_val, fmt)} → {_fmt(candidate_val, fmt)}"
        )
    elif candidate_val < baseline_val:
        comp.regressions.append(
            f"{label}: {_fmt(baseline_val, fmt)} → {_fmt(candidate_val, fmt)}"
        )
    else:
        comp.unchanged.append(f"{label}: {_fmt(baseline_val, fmt)}")


def _fmt(val: int | float, fmt: str) -> str:
    """Format a value with the given format spec."""
    if fmt.endswith("%"):
        return f"{val:{fmt}}"
    if isinstance(val, float):
        return f"{val:.2f}"
    return str(val)
