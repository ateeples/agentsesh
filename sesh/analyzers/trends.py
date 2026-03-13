"""Cross-session trend analysis.

Computes trends across multiple sessions: grade trajectory,
recurring patterns, tool usage distribution, duration trends.
"""

from collections import Counter
from dataclasses import dataclass, field

from ..parsers.base import Pattern


GRADE_SCORES = {"A+": 97, "A": 92, "B": 80, "C": 65, "D": 50, "F": 30, "N/A": 0}


@dataclass
class SessionSummary:
    """Lightweight summary of a single session for trend analysis."""

    session_id: str
    grade: str
    score: int
    tool_calls: int
    errors: int
    error_rate: float
    bash_overuse_rate: float
    blind_edits: int
    parallel_missed: int
    duration_minutes: float | None
    pattern_types: list[str]
    ingested_at: str


@dataclass
class TrendReport:
    """Cross-session trend analysis results."""

    sessions_analyzed: int
    grade_trajectory: str  # "improving", "stable", "declining"
    grade_change: float  # numeric delta
    avg_score: float
    avg_error_rate: float
    avg_bash_overuse: float
    avg_blind_edits: float
    avg_parallel_missed: float
    recurring_patterns: dict[str, int]  # pattern_type -> count
    pattern_frequency: dict[str, float]  # pattern_type -> % of sessions
    tool_counts: Counter  # tool_name -> total uses
    tool_error_rates: dict[str, float]  # tool_name -> error rate
    grade_distribution: dict[str, int]  # grade -> count
    session_summaries: list[SessionSummary] = field(default_factory=list)


def analyze_trends(summaries: list[SessionSummary]) -> TrendReport:
    """Analyze trends across multiple session summaries.

    Args:
        summaries: List of SessionSummary objects, ordered newest first.

    Returns:
        TrendReport with computed trends.
    """
    n = len(summaries)
    if n == 0:
        return TrendReport(
            sessions_analyzed=0,
            grade_trajectory="stable",
            grade_change=0.0,
            avg_score=0.0,
            avg_error_rate=0.0,
            avg_bash_overuse=0.0,
            avg_blind_edits=0.0,
            avg_parallel_missed=0.0,
            recurring_patterns={},
            pattern_frequency={},
            tool_counts=Counter(),
            tool_error_rates={},
            grade_distribution={},
        )

    # Grade trajectory
    grade_scores = [
        GRADE_SCORES.get(s.grade, 0) for s in summaries if s.grade != "N/A"
    ]
    grade_change = 0.0
    trajectory = "stable"
    if len(grade_scores) >= 4:
        mid = len(grade_scores) // 2
        older_avg = sum(grade_scores[mid:]) / max(len(grade_scores[mid:]), 1)
        newer_avg = sum(grade_scores[:mid]) / max(mid, 1)
        grade_change = newer_avg - older_avg
        if grade_change > 3:
            trajectory = "improving"
        elif grade_change < -3:
            trajectory = "declining"

    # Averages
    avg_score = sum(s.score for s in summaries) / n
    avg_error_rate = sum(s.error_rate for s in summaries) / n
    avg_bash_overuse = sum(s.bash_overuse_rate for s in summaries) / n
    avg_blind_edits = sum(s.blind_edits for s in summaries) / n
    avg_parallel_missed = sum(s.parallel_missed for s in summaries) / n

    # Recurring patterns
    pattern_counts: Counter[str] = Counter()
    for s in summaries:
        for pt in s.pattern_types:
            pattern_counts[pt] += 1

    pattern_frequency = {
        pt: count / n for pt, count in pattern_counts.items()
    }

    # Grade distribution
    grade_dist: Counter[str] = Counter()
    for s in summaries:
        grade_dist[s.grade] += 1

    return TrendReport(
        sessions_analyzed=n,
        grade_trajectory=trajectory,
        grade_change=round(grade_change, 1),
        avg_score=round(avg_score, 1),
        avg_error_rate=round(avg_error_rate, 3),
        avg_bash_overuse=round(avg_bash_overuse, 3),
        avg_blind_edits=round(avg_blind_edits, 1),
        avg_parallel_missed=round(avg_parallel_missed, 1),
        recurring_patterns=dict(pattern_counts.most_common()),
        pattern_frequency={k: round(v, 2) for k, v in pattern_frequency.items()},
        tool_counts=Counter(),  # Populated from DB in full implementation
        tool_error_rates={},
        grade_distribution=dict(grade_dist),
        session_summaries=summaries,
    )
