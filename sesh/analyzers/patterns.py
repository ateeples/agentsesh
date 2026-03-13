"""Behavioral pattern detection for agent sessions.

Nine patterns, each independently detected from tool call sequences.
Patterns are functions: (tool_calls, config) -> list[Pattern].
"""

import json
from collections import Counter
from pathlib import Path

from ..parsers.base import Pattern, ToolCall, classify_tool

# Default thresholds — overridable via config
DEFAULT_THRESHOLDS = {
    "error_rate_concern": 0.15,
    "bash_overuse_min": 3,
    "scattered_dirs_min": 8,
    "missed_parallel_min": 4,
    "error_streak_min": 3,
    "low_read_ratio": 1.5,
    "min_rw_calls": 5,
    "min_tool_calls_for_scatter": 10,
}

# Bash commands that have dedicated tool equivalents
BASH_ANTI_PATTERNS = ("cat ", "head ", "tail ", "grep ", "rg ", "find ", "sed ", "awk ")


def detect_repeated_searches(
    tool_calls: list[ToolCall], thresholds: dict | None = None
) -> list[Pattern]:
    """Detect identical search calls executed multiple times (flailing)."""
    search_args: list[str] = []
    search_indices: dict[str, list[int]] = {}

    for i, tc in enumerate(tool_calls):
        if tc.name in ("Grep", "Glob"):
            key = f"{tc.name}:{json.dumps(tc.input_data, sort_keys=True)}"
            search_args.append(key)
            search_indices.setdefault(key, []).append(i)

    repeated = {k: v for k, v in search_indices.items() if len(v) > 1}
    if not repeated:
        return []

    details = ", ".join(
        f"{k.split(':')[0]} x{len(v)}" for k, v in repeated.items()
    )
    indices = [idx for idxs in repeated.values() for idx in idxs]
    return [Pattern(
        type="repeated_search",
        severity="warning",
        detail=f"{len(repeated)} search(es) repeated: {details}",
        tool_indices=indices,
    )]


def detect_write_without_read(
    tool_calls: list[ToolCall], thresholds: dict | None = None
) -> list[Pattern]:
    """Detect edits to files that haven't been read in this session."""
    files_read: set[str] = set()
    blind_edits: list[int] = []

    for i, tc in enumerate(tool_calls):
        if tc.name == "Read":
            path = tc.input_data.get("file_path", "")
            if path:
                files_read.add(path)
        elif tc.name == "Edit":
            path = tc.input_data.get("file_path", "")
            if path and path not in files_read:
                blind_edits.append(i)

    if not blind_edits:
        return []

    file_names = [
        Path(tool_calls[i].input_data.get("file_path", "")).name
        for i in blind_edits[:3]
    ]
    return [Pattern(
        type="write_without_read",
        severity="concern",
        detail=f"{len(blind_edits)} edit(s) to unread files: {', '.join(file_names)}",
        tool_indices=blind_edits,
    )]


def detect_error_rate(
    tool_calls: list[ToolCall], thresholds: dict | None = None
) -> list[Pattern]:
    """Detect overall error rate."""
    t = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    total = len(tool_calls)
    if total == 0:
        return []

    errors = sum(1 for tc in tool_calls if tc.is_error)
    if errors == 0:
        return []

    rate = errors / total
    severity = "concern" if rate > t["error_rate_concern"] else "info"
    return [Pattern(
        type="error_rate",
        severity=severity,
        detail=f"{errors}/{total} tool calls had errors ({rate:.0%})",
        tool_indices=[i for i, tc in enumerate(tool_calls) if tc.is_error],
    )]


def detect_error_streak(
    tool_calls: list[ToolCall], thresholds: dict | None = None
) -> list[Pattern]:
    """Detect consecutive tool call errors (stuck in a loop)."""
    t = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    max_streak = 0
    current_streak = 0
    streak_end = 0

    for i, tc in enumerate(tool_calls):
        if tc.is_error:
            current_streak += 1
            if current_streak > max_streak:
                max_streak = current_streak
                streak_end = i
        else:
            current_streak = 0

    if max_streak < t["error_streak_min"]:
        return []

    streak_start = streak_end - max_streak + 1
    last_tool = tool_calls[streak_end].name
    return [Pattern(
        type="error_streak",
        severity="warning",
        detail=f"{max_streak} consecutive errors (last: {last_tool}) — stuck in a loop?",
        tool_indices=list(range(streak_start, streak_end + 1)),
    )]


def detect_low_read_ratio(
    tool_calls: list[ToolCall], thresholds: dict | None = None
) -> list[Pattern]:
    """Detect low read/write ratio (acting without understanding)."""
    t = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    reads = sum(1 for tc in tool_calls if "read" in classify_tool(tc.name))
    writes = sum(1 for tc in tool_calls if "write" in classify_tool(tc.name))

    if reads + writes < t["min_rw_calls"]:
        return []

    ratio = reads / max(writes, 1)
    if ratio >= t["low_read_ratio"]:
        return []

    return [Pattern(
        type="low_read_ratio",
        severity="info",
        detail=f"Read/write ratio: {ratio:.1f} ({reads} reads, {writes} writes). Acting without understanding?",
    )]


def detect_bash_overuse(
    tool_calls: list[ToolCall], thresholds: dict | None = None
) -> list[Pattern]:
    """Detect Bash calls that should use dedicated tools."""
    t = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    bash_total = 0
    anti_indices: list[int] = []

    for i, tc in enumerate(tool_calls):
        if tc.name == "Bash":
            bash_total += 1
            cmd = tc.input_data.get("command", "")
            for anti in BASH_ANTI_PATTERNS:
                if cmd.startswith(anti) or f" | {anti}" in cmd:
                    anti_indices.append(i)
                    break

    if len(anti_indices) < t["bash_overuse_min"]:
        return []

    return [Pattern(
        type="bash_overuse",
        severity="info",
        detail=f"{len(anti_indices)}/{bash_total} Bash calls used cat/grep/find/sed — should use dedicated tools",
        tool_indices=anti_indices,
    )]


def detect_write_then_read(
    tool_calls: list[ToolCall], thresholds: dict | None = None
) -> list[Pattern]:
    """Detect phase shift from writing to reading (acted before understanding)."""
    total = len(tool_calls)
    if total < 10:
        return []

    window = 5
    phases = []
    for i in range(0, total, window):
        chunk = tool_calls[i : i + window]
        cat_counts: Counter[str] = Counter()
        for tc in chunk:
            for cat in classify_tool(tc.name):
                cat_counts[cat] += 1
        dominant = cat_counts.most_common(1)[0][0] if cat_counts else "?"
        phases.append(dominant)

    for i in range(len(phases) - 1):
        if phases[i] == "write" and phases[i + 1] == "read":
            return [Pattern(
                type="write_then_read",
                severity="concern",
                detail=f"Phase shift: writing then reading (around tool calls {i * window + 1}-{(i + 2) * window}). Acted before understanding?",
            )]

    return []


def detect_scattered_files(
    tool_calls: list[ToolCall], thresholds: dict | None = None
) -> list[Pattern]:
    """Detect touching too many different directories (unfocused)."""
    t = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    if len(tool_calls) < t["min_tool_calls_for_scatter"]:
        return []

    file_paths = []
    for tc in tool_calls:
        p = tc.input_data.get("file_path", "") or tc.input_data.get("path", "")
        if p:
            file_paths.append(p)

    if len(file_paths) < 5:
        return []

    unique_dirs = len(set(str(Path(p).parent) for p in file_paths))
    if unique_dirs <= t["scattered_dirs_min"]:
        return []

    return [Pattern(
        type="scattered_files",
        severity="info",
        detail=f"Touched {unique_dirs} different directories — exploring or unfocused?",
    )]


def detect_missed_parallelism(
    tool_calls: list[ToolCall], thresholds: dict | None = None
) -> list[Pattern]:
    """Detect sequential reads that could have been parallel."""
    t = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    missed = 0
    indices: list[int] = []

    for i in range(len(tool_calls) - 1):
        curr = tool_calls[i]
        nxt = tool_calls[i + 1]
        if (
            curr.name == "Read"
            and nxt.name == "Read"
            and curr.input_data.get("file_path") != nxt.input_data.get("file_path")
        ):
            missed += 1
            indices.extend([i, i + 1])

    if missed < t["missed_parallel_min"]:
        return []

    return [Pattern(
        type="missed_parallelism",
        severity="info",
        detail=f"{missed} sequential Read pairs that could have been parallel",
        tool_indices=sorted(set(indices)),
    )]


# Registry of all pattern detectors
ALL_DETECTORS = [
    detect_repeated_searches,
    detect_write_without_read,
    detect_error_rate,
    detect_error_streak,
    detect_low_read_ratio,
    detect_bash_overuse,
    detect_write_then_read,
    detect_scattered_files,
    detect_missed_parallelism,
]


def detect_all_patterns(
    tool_calls: list[ToolCall],
    thresholds: dict | None = None,
    enabled: list[str] | None = None,
    disabled: list[str] | None = None,
) -> list[Pattern]:
    """Run all enabled pattern detectors against a session's tool calls.

    Args:
        tool_calls: Ordered list of tool calls from a session.
        thresholds: Override default thresholds.
        enabled: If set, only run these pattern types. ["all"] means all.
        disabled: Skip these pattern types.

    Returns:
        List of all detected patterns.
    """
    patterns: list[Pattern] = []
    disabled_set = set(disabled or [])

    for detector in ALL_DETECTORS:
        # Derive pattern type from function name (detect_X -> X)
        pattern_type = detector.__name__.replace("detect_", "")

        if enabled and "all" not in enabled and pattern_type not in enabled:
            continue
        if pattern_type in disabled_set:
            continue

        patterns.extend(detector(tool_calls, thresholds))

    return patterns
