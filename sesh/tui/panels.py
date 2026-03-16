"""Panel renderers for the sesh TUI dashboard.

Each panel function draws into a specific region of the curses window.
Panels read data from the shared state dict passed by the app loop.
"""

import curses
from collections import Counter

from .widgets import (
    dim_attr,
    draw_box,
    format_date,
    format_duration,
    grade_color,
    header_attr,
    horizontal_bar,
    safe_addstr,
    sparkline,
    truncate,
)


def draw_header(win, y: int, x: int, width: int, stats: dict, sessions: list[dict]) -> int:
    """Draw the top summary bar.

    Shows: total sessions, average grade/score, last grade/score with trend, cost estimate.
    Returns the height consumed.
    """
    height = 3
    draw_box(win, y, x, height, width, "sesh")

    if not stats or not stats.get("total_sessions"):
        safe_addstr(win, y + 1, x + 2, "No sessions yet. Run `sesh watch --once` to ingest.", dim_attr())
        return height

    total = stats.get("total_sessions", 0)
    avg_score = stats.get("avg_score", 0) or 0

    # Compute average grade from score
    avg_grade = _score_to_grade(avg_score)

    # Last session info
    last_grade = "?"
    last_score = 0
    trend = " "
    if sessions:
        last_grade = sessions[0].get("grade", "?") or "?"
        last_score = sessions[0].get("score", 0) or 0
        # Trend: compare last vs average of previous sessions
        if len(sessions) >= 2:
            prev_scores = [s.get("score", 0) or 0 for s in sessions[1:5]]
            if prev_scores:
                prev_avg = sum(prev_scores) / len(prev_scores)
                diff = last_score - prev_avg
                trend = "\u25b2" if diff > 2 else "\u25bc" if diff < -2 else "\u2501"

    # Cost estimate (rough: ~$1.67 per session average based on typical Claude usage)
    est_cost = total * 1.67

    # Build the summary line
    # Total sessions
    total_str = f"{total:,} sessions"
    safe_addstr(win, y + 1, x + 3, total_str, header_attr())

    # Average
    avg_str = f"Avg: {avg_grade} ({int(avg_score)})"
    avg_x = x + 3 + len(total_str) + 3
    safe_addstr(win, y + 1, avg_x, "Avg: ", dim_attr())
    safe_addstr(win, y + 1, avg_x + 5, f"{avg_grade} ({int(avg_score)})", grade_color(avg_grade))

    # Last
    last_str_label = "Last: "
    last_str_val = f"{last_grade} ({last_score})"
    last_x = avg_x + len(avg_str) + 3
    safe_addstr(win, y + 1, last_x, last_str_label, dim_attr())
    safe_addstr(win, y + 1, last_x + len(last_str_label), last_str_val, grade_color(last_grade))
    trend_x = last_x + len(last_str_label) + len(last_str_val) + 1
    if trend == "\u25b2":
        safe_addstr(win, y + 1, trend_x, trend, curses.color_pair(1))
    elif trend == "\u25bc":
        safe_addstr(win, y + 1, trend_x, trend, curses.color_pair(4))
    else:
        safe_addstr(win, y + 1, trend_x, trend, dim_attr())

    # Cost
    cost_str = f"Cost: ${est_cost:,.0f}"
    cost_x = x + width - len(cost_str) - 3
    if cost_x > trend_x + 3:
        safe_addstr(win, y + 1, cost_x, cost_str, dim_attr())

    return height


def draw_trend(win, y: int, x: int, width: int, height: int, sessions: list[dict]) -> None:
    """Draw the grade trend sparkline panel.

    Shows a sparkline of session scores over time (oldest to newest).
    """
    draw_box(win, y, x, height, width, "Grade Trend")

    if not sessions:
        safe_addstr(win, y + 2, x + 3, "No data", dim_attr())
        return

    # Get scores in chronological order, filtering out zero/null (ungraded sessions)
    scores = [
        s.get("score", 0) or 0
        for s in reversed(sessions)
        if (s.get("score") or 0) > 0
    ]

    # Available width for sparkline
    spark_width = width - 6
    if spark_width < 5:
        return

    spark = sparkline(scores, width=spark_width)

    # Center vertically in the panel
    mid_y = y + (height // 2)
    safe_addstr(win, mid_y, x + 3, spark, curses.color_pair(2))

    # Labels
    safe_addstr(win, mid_y + 1, x + 3, "oldest", dim_attr())
    newest_label = "newest"
    safe_addstr(win, mid_y + 1, x + width - len(newest_label) - 3, newest_label, dim_attr())

    # Draw connecting line between labels
    line_start = x + 3 + len("oldest") + 1
    line_end = x + width - len(newest_label) - 4
    if line_end > line_start:
        line = "\u2500" * (line_end - line_start)
        safe_addstr(win, mid_y + 1, line_start, line, dim_attr())


_PATTERN_SHORT = {
    "error_rate": "err rate",
    "error_streak": "err streak",
    "bash_overuse": "bash",
    "write_without_read": "blind edit",
    "write_then_read": "write→read",
    "low_read_ratio": "low reads",
    "repeated_search": "repeat srch",
    "scattered_files": "scattered",
    "missed_parallelism": "no parallel",
}


def draw_patterns(win, y: int, x: int, width: int, height: int, all_patterns: list[dict]) -> None:
    """Draw the top patterns panel.

    Shows pattern types ranked by frequency with bar charts.
    """
    draw_box(win, y, x, height, width, "Top Patterns")

    if not all_patterns:
        safe_addstr(win, y + 2, x + 3, "No patterns detected", dim_attr())
        return

    # Count pattern frequencies
    counter: Counter[str] = Counter()
    for p in all_patterns:
        counter[p.get("type", "unknown")] += 1

    # Available rows for patterns
    available_rows = height - 2
    inner_width = width - 6  # margins inside box

    # Dynamic name width: use abbreviated names
    name_col = 12
    count_col = 4  # " NNN"
    bar_width = inner_width - name_col - count_col - 1
    if bar_width < 3:
        bar_width = 3

    max_count = counter.most_common(1)[0][1] if counter else 1

    for i, (ptype, count) in enumerate(counter.most_common(available_rows)):
        if i >= available_rows:
            break
        row_y = y + 1 + i

        name = _PATTERN_SHORT.get(ptype, truncate(ptype, name_col))
        bar = horizontal_bar(count, max_count, bar_width)
        count_str = f"{count:>3d}"

        safe_addstr(win, row_y, x + 3, f"{name:<{name_col}s}", dim_attr())
        safe_addstr(win, row_y, x + 3 + name_col, bar, curses.color_pair(3))
        safe_addstr(win, row_y, x + 3 + name_col + bar_width + 1, count_str, dim_attr())


def draw_sessions(
    win, y: int, x: int, width: int, height: int,
    sessions: list[dict], selected: int, scroll_offset: int
) -> None:
    """Draw the sessions list panel.

    Shows sessions with date, grade, score, duration, errors.
    The selected row is highlighted with A_REVERSE.
    """
    draw_box(win, y, x, height, width, "Sessions")

    if not sessions:
        safe_addstr(win, y + 2, x + 3, "No sessions", dim_attr())
        return

    # Available rows for session list (leave room for border + help line)
    available_rows = height - 3
    if available_rows < 1:
        return

    for i in range(available_rows):
        session_idx = scroll_offset + i
        if session_idx >= len(sessions):
            break

        s = sessions[session_idx]
        row_y = y + 1 + i
        is_selected = session_idx == selected

        # Build row: marker date grade score duration errors
        marker = "\u25b8" if is_selected else " "
        date = format_date(s.get("start_time") or s.get("ingested_at"))
        grade = s.get("grade") or "?"
        score = s.get("score") or 0
        duration = format_duration(s.get("duration_minutes"))
        errors = s.get("error_count", 0) or 0

        # Handle ungraded sessions cleanly
        if grade in ("N/A", "?", "") or score == 0:
            grade = "\u2014"  # em dash
            score_str = "  \u2014"
        else:
            score_str = f"{score:>3d}"

        # Compose the row
        row_parts = f" {marker} {date}  {grade:<2s} {score_str}  {duration} {errors:>3d}err"
        row_text = truncate(row_parts, width - 2)

        attr = curses.A_REVERSE if is_selected else 0
        safe_addstr(win, row_y, x + 1, row_text.ljust(width - 2), attr)

        # Overlay grade color on the grade portion if not selected
        if not is_selected:
            grade_x = x + 1 + len(f" {marker} {date}  ")
            safe_addstr(win, row_y, grade_x, f"{grade:<2s}", grade_color(grade))

    # Help line at bottom
    help_y = y + height - 2
    help_text = " \u2191\u2193 select  q: quit  r: refresh"
    safe_addstr(win, help_y, x + 2, truncate(help_text, width - 4), dim_attr())


def draw_details(
    win, y: int, x: int, width: int, height: int,
    session: dict | None, tool_calls: list[dict], patterns: list[dict]
) -> None:
    """Draw the session details panel.

    Shows detailed info about the currently selected session.
    """
    draw_box(win, y, x, height, width, "Details")

    if not session:
        safe_addstr(win, y + 2, x + 3, "Select a session", dim_attr())
        return

    available_rows = height - 2
    row = 0

    def add_line(label: str, value: str, value_attr: int = 0) -> None:
        nonlocal row
        if row >= available_rows:
            return
        safe_addstr(win, y + 1 + row, x + 3, f"{label}: ", dim_attr())
        safe_addstr(win, y + 1 + row, x + 3 + len(label) + 2, value, value_attr or dim_attr())
        row += 1

    grade = session.get("grade", "?") or "?"
    score = session.get("score", 0) or 0
    add_line("Grade", f"{grade} ({score}/100)", grade_color(grade))

    duration = session.get("duration_minutes")
    if duration is not None:
        add_line("Duration", f"{int(duration)} min")
    else:
        add_line("Duration", "unknown")

    tc_count = session.get("tool_call_count", 0) or 0
    err_count = session.get("error_count", 0) or 0
    add_line("Tool calls", f"{tc_count} ({err_count} errors)")

    # File count from tool calls
    files_touched: set[str] = set()
    for tc in tool_calls:
        inp = tc.get("input_json", "")
        if isinstance(inp, str) and "file_path" in inp:
            try:
                import json
                data = json.loads(inp)
                fp = data.get("file_path", "")
                if fp:
                    files_touched.add(fp)
            except (ValueError, TypeError):
                pass
    if files_touched:
        add_line("Files", f"{len(files_touched)} touched")

    # Model
    model = session.get("model")
    if model:
        add_line("Model", truncate(model, width - 12))

    # Patterns
    if patterns:
        # Count by type
        pattern_counts: dict[str, int] = {}
        for p in patterns:
            pt = p.get("type", "unknown")
            pattern_counts[pt] = pattern_counts.get(pt, 0) + 1

        if row < available_rows:
            row += 1  # blank line

        for pt, cnt in sorted(pattern_counts.items(), key=lambda x: -x[1]):
            if row >= available_rows:
                break
            safe_addstr(win, y + 1 + row, x + 3, f"  {pt} ({cnt})", curses.color_pair(3))
            row += 1
    elif row < available_rows:
        row += 1
        safe_addstr(win, y + 1 + row, x + 3, "No patterns", curses.color_pair(1))

    # Session ID at the bottom if space permits
    if row + 1 < available_rows:
        sid = session.get("id", "")
        if sid:
            safe_addstr(win, y + 1 + available_rows - 1, x + 3,
                        truncate(f"ID: {sid}", width - 6), dim_attr())


def draw_live(win, y: int, x: int, width: int, height: int, snap) -> int:
    """Draw the live session monitoring panel.

    Shows real-time stats for the currently active session.
    Returns height consumed.
    """
    from ..live import LiveSnapshot

    if snap is None:
        draw_box(win, y, x, 3, width, "Live")
        safe_addstr(win, y + 1, x + 3, "No active session detected", dim_attr())
        return 3

    # Title with active indicator
    indicator = "\u25cf" if snap.active else "\u25cb"  # ● or ○
    title = f"Live {indicator} {snap.project}" if snap.project else f"Live {indicator}"
    draw_box(win, y, x, height, width, title)

    available_rows = height - 2
    row = 0
    inner_x = x + 3
    inner_w = width - 6

    def add_line(text: str, attr: int = 0) -> None:
        nonlocal row
        if row >= available_rows:
            return
        safe_addstr(win, y + 1 + row, inner_x, truncate(text, inner_w), attr or dim_attr())
        row += 1

    # Stats line
    dur_min = int(snap.duration_seconds / 60)
    cost_str = f"${snap.estimated_cost:.2f}" if snap.estimated_cost else "$0.00"
    add_line(
        f"{snap.tool_calls} calls  {snap.errors} err  "
        f"{snap.files_read}R/{snap.files_written}W files  "
        f"{dur_min}m  {cost_str}",
        header_attr(),
    )

    # Tests
    if snap.test_runs > 0:
        test_color = curses.color_pair(1) if snap.test_failures == 0 else curses.color_pair(4)
        add_line(
            f"Tests: {snap.test_passes}/{snap.test_runs} pass"
            f"{'  ' + str(snap.test_failures) + ' FAIL' if snap.test_failures else ''}",
            test_color,
        )

    # Collaboration
    if snap.human_turns >= 2 and snap.archetype:
        arch_color = curses.color_pair(1) if snap.archetype == "Partnership" else (
            curses.color_pair(3) if snap.archetype in ("Spec Dump", "Micromanager") else dim_attr()
        )
        add_line(
            f"Collab: {snap.archetype} ({snap.collab_score})  "
            f"{snap.corrections}c {snap.affirmations}a  "
            f"~{snap.avg_prompt_words:.0f} words/turn",
            arch_color,
        )

    # Nudges
    for nudge in snap.nudges:
        if row >= available_rows:
            break
        if nudge.level == "alert":
            attr = curses.color_pair(4) | curses.A_BOLD
            prefix = "!! "
        elif nudge.level == "warn":
            attr = curses.color_pair(3)
            prefix = " ! "
        else:
            attr = curses.color_pair(2)
            prefix = "   "
        add_line(f"{prefix}{nudge.message}", attr)

    # Fill remaining rows (avoid visual artifacts)
    while row < available_rows:
        safe_addstr(win, y + 1 + row, inner_x, " " * inner_w, dim_attr())
        row += 1

    return height


def _score_to_grade(score: float) -> str:
    """Convert numeric score to letter grade."""
    if score >= 95:
        return "A+"
    elif score >= 85:
        return "A"
    elif score >= 70:
        return "B"
    elif score >= 55:
        return "C"
    elif score >= 40:
        return "D"
    else:
        return "F"
