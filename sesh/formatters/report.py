"""Human-readable report formatters for sesh."""

from collections import Counter
from pathlib import Path

from ..analyzers.remediation import get_all_remediations, format_remediations
from ..analyzers.trends import TrendReport
from ..parsers.base import classify_tool


def format_session_report(session: dict, tool_calls: list[dict], patterns: list[dict]) -> str:
    """Format a single session analysis as human-readable text."""
    lines = []

    # Header
    lines.append(f"# Session: {session['id']}")
    lines.append("")

    # Metadata
    if session.get("start_time"):
        lines.append(f"  Started:  {session['start_time']}")
    if session.get("duration_minutes"):
        lines.append(f"  Duration: ~{session['duration_minutes']:.0f} min")
    if session.get("model"):
        lines.append(f"  Model:    {session['model']}")
    lines.append(f"  Grade:    {session['grade']} (score: {session['score']})")
    lines.append(f"  Tools:    {session['tool_call_count']} calls ({session['error_count']} errors)")
    lines.append("")

    # Grade breakdown
    if session.get("grade_notes") and session["grade_notes"] != "Clean session":
        lines.append("## Grade Breakdown")
        for note in session["grade_notes"].split(" | "):
            lines.append(f"  {note}")
        lines.append("")

    # Tool usage
    lines.append("## Tool Usage")
    tool_counts: Counter[str] = Counter()
    tool_errors: Counter[str] = Counter()
    for tc in tool_calls:
        tool_counts[tc["name"]] += 1
        if tc["is_error"]:
            tool_errors[tc["name"]] += 1
    for name, count in tool_counts.most_common():
        err = tool_errors.get(name, 0)
        err_str = f" ({err} errors)" if err else ""
        lines.append(f"  {name}: {count}{err_str}")
    lines.append("")

    # Behavioral breakdown
    lines.append("## Behavioral Breakdown")
    cat_counts: Counter[str] = Counter()
    for tc in tool_calls:
        for cat in classify_tool(tc["name"]):
            cat_counts[cat] += 1
    total = len(tool_calls)
    for cat, count in cat_counts.most_common():
        pct = count / max(total, 1) * 100
        lines.append(f"  {cat}: {count} ({pct:.0f}%)")
    lines.append("")

    # Patterns
    if patterns:
        lines.append("## Patterns Detected")
        for p in patterns:
            icon = {"warning": "!!", "concern": "!", "info": "-"}.get(p["severity"], "-")
            lines.append(f"  [{icon}] {p['detail']}")
        lines.append("")

    # Remediations
    if patterns:
        remediations = get_all_remediations(patterns)
        if remediations:
            lines.append(format_remediations(remediations, include_snippets=False))

    # Timeline (first 50)
    lines.append("## Timeline")
    for tc in tool_calls[:50]:
        err = " [ERROR]" if tc["is_error"] else ""
        inp = tc.get("input_json", "{}")
        if isinstance(inp, str):
            try:
                inp_data = __import__("json").loads(inp)
            except Exception:
                inp_data = {}
        else:
            inp_data = inp

        summary = ""
        if tc["name"] == "Read":
            summary = inp_data.get("file_path", "")
        elif tc["name"] in ("Grep", "Glob"):
            summary = f'pattern="{inp_data.get("pattern", "")}"'
        elif tc["name"] == "Bash":
            cmd = inp_data.get("command", "")
            summary = cmd[:80] + ("..." if len(cmd) > 80 else "")
        elif tc["name"] in ("Write", "Edit"):
            summary = inp_data.get("file_path", "")
        else:
            s = str(inp_data)
            summary = s[:80] + ("..." if len(s) > 80 else "")

        lines.append(f"  {tc['seq'] + 1:3d}. {tc['name']}{err} — {summary}")

    if len(tool_calls) > 50:
        lines.append(f"  ... and {len(tool_calls) - 50} more")
    lines.append("")

    return "\n".join(lines)


def format_trend_report(report: TrendReport) -> str:
    """Format a cross-session trend analysis as human-readable text."""
    lines = []
    lines.append(f"# Trend Report ({report.sessions_analyzed} sessions)")
    lines.append("")

    # Trajectory
    arrow = {"improving": "↑", "stable": "→", "declining": "↓"}.get(
        report.grade_trajectory, "→"
    )
    lines.append(f"  Trajectory:     {arrow} {report.grade_trajectory} ({report.grade_change:+.1f} pts)")
    lines.append(f"  Average score:  {report.avg_score:.0f}")
    lines.append(f"  Avg error rate: {report.avg_error_rate:.1%}")
    lines.append(f"  Avg bash abuse: {report.avg_bash_overuse:.1%}")
    lines.append(f"  Avg blind edits: {report.avg_blind_edits:.1f}/session")
    lines.append(f"  Avg missed ||:  {report.avg_parallel_missed:.1f}/session")
    lines.append("")

    # Grade distribution
    lines.append("## Grade Distribution")
    for grade in ["A+", "A", "B", "C", "D", "F"]:
        count = report.grade_distribution.get(grade, 0)
        if count:
            bar = "█" * count
            lines.append(f"  {grade:>2}: {bar} ({count})")
    lines.append("")

    # Recurring patterns
    if report.recurring_patterns:
        lines.append("## Recurring Patterns")
        for ptype, count in sorted(
            report.recurring_patterns.items(), key=lambda x: -x[1]
        ):
            freq = report.pattern_frequency.get(ptype, 0)
            lines.append(f"  {ptype}: {count}/{report.sessions_analyzed} sessions ({freq:.0%})")
        lines.append("")

    # Session list
    lines.append("## Sessions (newest first)")
    for s in report.session_summaries:
        dur = f"{s.duration_minutes:.0f}m" if s.duration_minutes else "?m"
        lines.append(
            f"  [{s.grade:>2}] {s.session_id[:12]}... "
            f"{s.tool_calls} calls, {s.errors} err, {dur}"
        )
    lines.append("")

    return "\n".join(lines)


def format_session_list(sessions: list[dict]) -> str:
    """Format a list of sessions for `sesh list`."""
    if not sessions:
        return "No sessions logged yet."

    lines = ["# Sessions (newest first)", ""]
    for s in sessions:
        grade = s.get("grade", "?")
        score = s.get("score", 0)
        tools = s.get("tool_call_count", 0)
        errs = s.get("error_count", 0)
        dur = s.get("duration_minutes")
        dur_str = f"{dur:.0f}m" if dur else "?m"
        date = (s.get("start_time") or s.get("ingested_at") or "")[:10]
        sid = s["id"][:16]

        lines.append(f"  [{grade:>2}|{score:>3}] {date} {sid}... {tools} calls, {errs} err, {dur_str}")

    return "\n".join(lines)


def format_stats(stats: dict, tool_stats: list[dict]) -> str:
    """Format aggregate statistics."""
    lines = ["# sesh stats", ""]

    total = stats.get("total_sessions", 0)
    lines.append(f"  Sessions:       {total}")
    lines.append(f"  Average score:  {stats.get('avg_score', 0):.0f}")
    lines.append(f"  Avg error rate: {(stats.get('avg_error_rate') or 0):.1%}")
    lines.append(f"  Avg bash abuse: {(stats.get('avg_bash_overuse') or 0):.1%}")
    lines.append(f"  Avg blind edits: {(stats.get('avg_blind_edits') or 0):.1f}/session")
    lines.append(f"  Total tool calls: {stats.get('total_tool_calls', 0)}")
    lines.append(f"  Avg duration:   {(stats.get('avg_duration') or 0):.0f} min")
    lines.append("")

    if tool_stats:
        lines.append("## Tool Usage (all sessions)")
        for ts in tool_stats:
            err_str = f" ({ts['errors']} errors, {ts['error_rate']:.0%})" if ts["errors"] else ""
            lines.append(f"  {ts['name']}: {ts['uses']}{err_str}")
        lines.append("")

    return "\n".join(lines)


def format_search_results(results: list[dict]) -> str:
    """Format FTS5 search results."""
    if not results:
        return "No results found."

    lines = [f"# Search Results ({len(results)} matches)", ""]
    for r in results:
        grade = r.get("grade", "?")
        sid = r["session_id"][:16]
        date = (r.get("ingested_at") or "")[:10]
        snippet = r.get("snippet", "")
        lines.append(f"  [{grade}] {date} {sid}...")
        lines.append(f"      {snippet}")
        lines.append("")

    return "\n".join(lines)
