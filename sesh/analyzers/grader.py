"""Session grading — process quality assessment.

Grades process quality (A+ to F), not output quality.
Start at 100, deduct for issues, add bonuses.
"""

from collections import Counter
from pathlib import Path

from ..parsers.base import SessionGrade, ToolCall, classify_tool

# Default grading weights — overridable via config
DEFAULT_WEIGHTS = {
    "error_rate_max_deduction": 20,
    "blind_edit_deduction": 5,
    "blind_edit_max": 15,
    "error_streak_deduction": 3,
    "error_streak_max": 15,
    "bash_anti_deduction": 2,
    "bash_anti_max": 10,
    "read_ratio_bonus": 5,
    "read_ratio_threshold": 3.0,
    "parallel_bonus": 5,
    "parallel_min_batches": 3,
}

BASH_ANTI_PATTERNS = ("cat ", "head ", "tail ", "grep ", "rg ", "find ", "sed ", "awk ")

GRADE_SCALE = [
    (95, "A+"),
    (90, "A"),
    (75, "B"),
    (60, "C"),
    (45, "D"),
    (0, "F"),
]


def grade_session(
    tool_calls: list[ToolCall], weights: dict | None = None
) -> SessionGrade:
    """Grade a session's process quality.

    Args:
        tool_calls: Ordered tool calls from the session.
        weights: Override default grading weights.

    Returns:
        SessionGrade with letter grade, numeric score, and breakdown.
    """
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    total = len(tool_calls)

    if total < 3:
        return SessionGrade(grade="N/A", score=0, deductions=["Too few tool calls to grade"])

    score = 100
    deductions: list[str] = []
    bonuses: list[str] = []

    # --- Deductions ---

    # Error rate
    errors = sum(1 for tc in tool_calls if tc.is_error)
    if errors > 0:
        error_rate = errors / total
        d = min(w["error_rate_max_deduction"], int(error_rate * 100))
        score -= d
        deductions.append(f"-{d} error rate ({errors}/{total})")

    # Blind edits (edit without prior read)
    files_read: set[str] = set()
    blind_edits = 0
    for tc in tool_calls:
        if tc.name == "Read":
            files_read.add(tc.input_data.get("file_path", ""))
        elif tc.name == "Edit":
            if tc.input_data.get("file_path", "") not in files_read:
                blind_edits += 1
    if blind_edits:
        d = min(w["blind_edit_max"], blind_edits * w["blind_edit_deduction"])
        score -= d
        deductions.append(f"-{d} blind edits ({blind_edits} files edited without reading)")

    # Error streaks
    max_streak = 0
    current = 0
    for tc in tool_calls:
        if tc.is_error:
            current += 1
            max_streak = max(max_streak, current)
        else:
            current = 0
    if max_streak >= 3:
        d = min(w["error_streak_max"], max_streak * w["error_streak_deduction"])
        score -= d
        deductions.append(f"-{d} error streak of {max_streak} (stuck)")

    # Bash overuse
    bash_anti = 0
    for tc in tool_calls:
        if tc.name == "Bash":
            cmd = tc.input_data.get("command", "")
            for anti in BASH_ANTI_PATTERNS:
                if cmd.startswith(anti) or f" | {anti}" in cmd:
                    bash_anti += 1
                    break
    if bash_anti > 2:
        d = min(w["bash_anti_max"], bash_anti * w["bash_anti_deduction"])
        score -= d
        deductions.append(f"-{d} bash anti-pattern ({bash_anti} calls)")

    # --- Bonuses ---

    # Good read/write ratio
    reads = sum(1 for tc in tool_calls if tc.name in ("Read", "Grep", "Glob"))
    writes = sum(1 for tc in tool_calls if tc.name in ("Edit", "Write"))
    if writes > 0 and reads / writes >= w["read_ratio_threshold"]:
        b = w["read_ratio_bonus"]
        score = min(100, score + b)
        bonuses.append(f"+{b} strong read/write ratio ({reads}:{writes})")

    # Parallelized tool calls (same timestamp = parallel batch)
    timestamps = [tc.timestamp for tc in tool_calls if tc.timestamp]
    ts_counts = Counter(timestamps)
    parallel_batches = sum(1 for v in ts_counts.values() if v > 1)
    if parallel_batches >= w["parallel_min_batches"]:
        b = w["parallel_bonus"]
        score = min(100, score + b)
        bonuses.append(f"+{b} good parallelism ({parallel_batches} batches)")

    # Clamp score
    score = max(0, min(100, score))

    # Determine letter grade
    grade = "F"
    for threshold, letter in GRADE_SCALE:
        if score >= threshold:
            grade = letter
            break

    return SessionGrade(
        grade=grade,
        score=score,
        deductions=deductions,
        bonuses=bonuses,
    )
