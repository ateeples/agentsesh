"""Audit scoring engine — runs metric detectors and combines results.

The engine is generic. Individual metrics plug in as detector functions:
    (repo_path: Path, config: dict) -> MetricResult

Same pattern as sesh/analyzers/patterns.py — registry of independent detectors,
each producing structured results that the engine combines.
"""

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Finding:
    """A single observation from a metric detector."""

    status: str  # "found", "missing", "warning"
    description: str


@dataclass
class MetricResult:
    """Result from a single metric detector."""

    name: str
    score: int  # 0-10
    findings: list[Finding] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)


@dataclass
class AuditResult:
    """Combined result from all metric detectors."""

    path: str
    score: int  # 0-100
    grade: str  # A+ through F
    metrics: list[MetricResult] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)


# Grade thresholds — same scale as session grading
GRADE_THRESHOLDS = [
    (95, "A+"),
    (90, "A"),
    (75, "B"),
    (60, "C"),
    (45, "D"),
    (0, "F"),
]

# Default weights — equal for all metrics
DEFAULT_WEIGHTS: dict[str, float] = {
    "bootstrap": 1.0,
    "task_entry_points": 1.0,
    "validation_harness": 1.0,
    "linting": 1.0,
    "codebase_map": 1.0,
    "doc_structure": 1.0,
    "decision_records": 1.0,
    "agent_instructions": 1.0,
    "file_discipline": 1.0,
}


def score_to_grade(score: int) -> str:
    """Convert a 0-100 score to a letter grade."""
    for threshold, grade in GRADE_THRESHOLDS:
        if score >= threshold:
            return grade
    return "F"


def combine_metrics(
    metrics: list[MetricResult],
    weights: dict[str, float] | None = None,
) -> AuditResult:
    """Combine individual metric results into an overall audit score.

    Weighted average of metric scores (each 0-10), scaled to 0-100.
    Missing weights default to 1.0.
    """
    if not metrics:
        return AuditResult(
            path="",
            score=0,
            grade="F",
            metrics=[],
            recommendations=["No metrics were evaluated."],
        )

    w = {**DEFAULT_WEIGHTS, **(weights or {})}

    total_weight = 0.0
    weighted_sum = 0.0

    for m in metrics:
        metric_weight = w.get(m.name, 1.0)
        weighted_sum += m.score * metric_weight
        total_weight += metric_weight

    # Weighted average of 0-10 scores, scaled to 0-100
    raw = (weighted_sum / total_weight) * 10 if total_weight > 0 else 0
    score = min(100, max(0, round(raw)))

    # Collect all recommendations
    all_recs: list[str] = []
    for m in metrics:
        all_recs.extend(m.recommendations)

    return AuditResult(
        path="",
        score=score,
        grade=score_to_grade(score),
        metrics=metrics,
        recommendations=all_recs,
    )


# Type for metric detector functions
MetricDetector = type(lambda repo_path, config: MetricResult(name="", score=0))

# Registry of all metric detectors — populated by register_metric()
_REGISTRY: list[tuple[str, callable]] = []


def register_metric(name: str, detector: callable) -> None:
    """Register a metric detector function."""
    _REGISTRY.append((name, detector))


def get_registered_metrics() -> list[tuple[str, callable]]:
    """Return all registered metric detectors."""
    return list(_REGISTRY)


def run_audit(
    repo_path: Path,
    config: dict | None = None,
    weights: dict[str, float] | None = None,
    enabled: list[str] | None = None,
) -> AuditResult:
    """Run all registered metric detectors against a repo and combine results.

    Args:
        repo_path: Path to the repository root.
        config: Configuration dict passed to each detector.
        weights: Override default metric weights.
        enabled: If set, only run these metrics. None means all.
    """
    cfg = config or {}
    results: list[MetricResult] = []

    for name, detector in _REGISTRY:
        if enabled and name not in enabled:
            continue
        result = detector(repo_path, cfg)
        results.append(result)

    audit = combine_metrics(results, weights=weights)
    audit.path = str(repo_path)
    return audit
