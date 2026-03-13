"""JSON output formatter for sesh --json mode."""

import json


def to_json(data: dict | list, indent: int = 2) -> str:
    """Convert data to formatted JSON string."""
    return json.dumps(data, indent=indent, default=str)


def session_to_json(session: dict, tool_calls: list[dict], patterns: list[dict]) -> str:
    """Format session analysis as JSON."""
    return to_json({
        "session": session,
        "tool_calls": tool_calls,
        "patterns": patterns,
    })


def trend_to_json(report) -> str:
    """Format trend report as JSON."""
    return to_json({
        "sessions_analyzed": report.sessions_analyzed,
        "grade_trajectory": report.grade_trajectory,
        "grade_change": report.grade_change,
        "avg_score": report.avg_score,
        "avg_error_rate": report.avg_error_rate,
        "avg_bash_overuse": report.avg_bash_overuse,
        "avg_blind_edits": report.avg_blind_edits,
        "avg_parallel_missed": report.avg_parallel_missed,
        "recurring_patterns": report.recurring_patterns,
        "pattern_frequency": report.pattern_frequency,
        "grade_distribution": report.grade_distribution,
        "sessions": [
            {
                "id": s.session_id,
                "grade": s.grade,
                "score": s.score,
                "tool_calls": s.tool_calls,
                "errors": s.errors,
                "error_rate": s.error_rate,
                "duration_minutes": s.duration_minutes,
                "pattern_types": s.pattern_types,
            }
            for s in report.session_summaries
        ],
    })
