"""Tests for the audit scoring engine — the integration/logic layer.

Tests the scoring engine independently of any metric detectors.
Metrics are injected as test fixtures, not imported from real detectors.

Covers: score-to-grade mapping, metric combination, metric registration,
selective metric execution, and audit result construction.
"""

from pathlib import Path

from sesh.audit.engine import (
    _REGISTRY,
    Finding,
    MetricResult,
    combine_metrics,
    get_registered_metrics,
    register_metric,
    run_audit,
    score_to_grade,
)

# --- Score-to-grade mapping (0-100 → A+ through F) ---


class TestScoreToGrade:
    def test_a_plus(self):
        assert score_to_grade(100) == "A+"
        assert score_to_grade(95) == "A+"

    def test_a(self):
        assert score_to_grade(94) == "A"
        assert score_to_grade(90) == "A"

    def test_b(self):
        assert score_to_grade(89) == "B"
        assert score_to_grade(75) == "B"

    def test_c(self):
        assert score_to_grade(74) == "C"
        assert score_to_grade(60) == "C"

    def test_d(self):
        assert score_to_grade(59) == "D"
        assert score_to_grade(45) == "D"

    def test_f(self):
        assert score_to_grade(44) == "F"
        assert score_to_grade(0) == "F"


class TestCombineMetrics:
    def test_empty_metrics(self):
        result = combine_metrics([])
        assert result.score == 0
        assert result.grade == "F"
        assert len(result.recommendations) == 1

    def test_single_perfect_metric(self):
        metrics = [MetricResult(name="bootstrap", score=10)]
        result = combine_metrics(metrics)
        assert result.score == 100
        assert result.grade == "A+"

    def test_single_zero_metric(self):
        metrics = [MetricResult(name="bootstrap", score=0)]
        result = combine_metrics(metrics)
        assert result.score == 0
        assert result.grade == "F"

    def test_two_metrics_average(self):
        metrics = [
            MetricResult(name="bootstrap", score=10),
            MetricResult(name="linting", score=0),
        ]
        result = combine_metrics(metrics)
        assert result.score == 50  # (10 + 0) / 2 * 10

    def test_three_metrics(self):
        metrics = [
            MetricResult(name="bootstrap", score=8),
            MetricResult(name="linting", score=6),
            MetricResult(name="validation_harness", score=10),
        ]
        result = combine_metrics(metrics)
        # (8 + 6 + 10) / 3 * 10 = 80
        assert result.score == 80
        assert result.grade == "B"

    def test_custom_weights(self):
        metrics = [
            MetricResult(name="bootstrap", score=10),
            MetricResult(name="linting", score=0),
        ]
        # Weight bootstrap 3x more than linting
        weights = {"bootstrap": 3.0, "linting": 1.0}
        result = combine_metrics(metrics, weights=weights)
        # (10*3 + 0*1) / (3+1) * 10 = 75
        assert result.score == 75
        assert result.grade == "B"

    def test_weights_default_to_one(self):
        """Unknown metric names get weight 1.0."""
        metrics = [
            MetricResult(name="custom_metric", score=8),
        ]
        result = combine_metrics(metrics)
        assert result.score == 80

    def test_collects_recommendations(self):
        metrics = [
            MetricResult(name="a", score=5, recommendations=["Add README"]),
            MetricResult(name="b", score=5, recommendations=["Add tests", "Add CI"]),
        ]
        result = combine_metrics(metrics)
        assert len(result.recommendations) == 3
        assert "Add README" in result.recommendations
        assert "Add tests" in result.recommendations

    def test_collects_findings(self):
        findings = [
            Finding(status="found", description="README.md exists"),
            Finding(status="missing", description="No setup script"),
        ]
        metrics = [MetricResult(name="bootstrap", score=5, findings=findings)]
        result = combine_metrics(metrics)
        assert len(result.metrics[0].findings) == 2

    def test_score_clamped_to_100(self):
        """Even with score > 10, result shouldn't exceed 100."""
        metrics = [MetricResult(name="a", score=10)]
        result = combine_metrics(metrics)
        assert result.score <= 100

    def test_score_clamped_to_0(self):
        metrics = [MetricResult(name="a", score=0)]
        result = combine_metrics(metrics)
        assert result.score >= 0

    def test_all_nine_metrics_perfect(self):
        """Simulate all 9 metrics scoring perfectly."""
        names = [
            "bootstrap", "task_entry_points", "validation_harness",
            "linting", "codebase_map", "doc_structure",
            "decision_records", "agent_instructions", "file_discipline",
        ]
        metrics = [MetricResult(name=n, score=10) for n in names]
        result = combine_metrics(metrics)
        assert result.score == 100
        assert result.grade == "A+"

    def test_all_nine_metrics_mediocre(self):
        names = [
            "bootstrap", "task_entry_points", "validation_harness",
            "linting", "codebase_map", "doc_structure",
            "decision_records", "agent_instructions", "file_discipline",
        ]
        metrics = [MetricResult(name=n, score=6) for n in names]
        result = combine_metrics(metrics)
        assert result.score == 60
        assert result.grade == "C"


class TestRegistry:
    def setup_method(self):
        """Save and clear registry before each test."""
        self._saved_registry = list(_REGISTRY)
        _REGISTRY.clear()

    def teardown_method(self):
        """Restore registry after each test."""
        _REGISTRY.clear()
        _REGISTRY.extend(self._saved_registry)

    def test_register_and_list(self):
        def fake_detector(repo_path, config):
            return MetricResult(name="fake", score=5)

        register_metric("fake", fake_detector)
        metrics = get_registered_metrics()
        assert len(metrics) == 1
        assert metrics[0][0] == "fake"

    def test_multiple_registrations(self):
        for name in ["a", "b", "c"]:
            register_metric(name, lambda p, c, n=name: MetricResult(name=n, score=5))
        assert len(get_registered_metrics()) == 3


class TestRunAudit:
    def setup_method(self):
        self._saved_registry = list(_REGISTRY)
        _REGISTRY.clear()

    def teardown_method(self):
        _REGISTRY.clear()
        _REGISTRY.extend(self._saved_registry)

    def test_empty_registry(self):
        result = run_audit(Path("/tmp"))
        assert result.score == 0
        assert result.grade == "F"

    def test_runs_registered_detectors(self):
        register_metric("good", lambda p, c: MetricResult(name="good", score=10))
        register_metric("bad", lambda p, c: MetricResult(name="bad", score=0))

        result = run_audit(Path("/tmp"))
        assert result.score == 50
        assert len(result.metrics) == 2

    def test_enabled_filter(self):
        register_metric("a", lambda p, c: MetricResult(name="a", score=10))
        register_metric("b", lambda p, c: MetricResult(name="b", score=0))

        result = run_audit(Path("/tmp"), enabled=["a"])
        assert len(result.metrics) == 1
        assert result.metrics[0].name == "a"
        assert result.score == 100

    def test_path_set_on_result(self):
        register_metric("x", lambda p, c: MetricResult(name="x", score=5))
        result = run_audit(Path("/some/repo"))
        assert result.path == "/some/repo"

    def test_config_passed_to_detectors(self):
        received_config = {}

        def capture_config(repo_path, config):
            received_config.update(config)
            return MetricResult(name="cap", score=5)

        register_metric("cap", capture_config)
        run_audit(Path("/tmp"), config={"threshold": 800})
        assert received_config.get("threshold") == 800

    def test_custom_weights(self):
        register_metric("heavy", lambda p, c: MetricResult(name="heavy", score=10))
        register_metric("light", lambda p, c: MetricResult(name="light", score=0))

        result = run_audit(
            Path("/tmp"),
            weights={"heavy": 9.0, "light": 1.0},
        )
        # (10*9 + 0*1) / (9+1) * 10 = 90
        assert result.score == 90
        assert result.grade == "A"

    def test_detector_receives_repo_path(self):
        received_path = [None]

        def capture_path(repo_path, config):
            received_path[0] = repo_path
            return MetricResult(name="p", score=5)

        register_metric("p", capture_path)
        run_audit(Path("/my/repo"))
        assert received_path[0] == Path("/my/repo")
