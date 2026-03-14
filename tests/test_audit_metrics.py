"""Tests for sesh audit metric detectors.

Each detector gets: empty repo, fully populated, partial.
Plus integration test against the agentsesh repo itself.
"""

import json
from pathlib import Path

import pytest

from sesh.audit.metrics import (
    detect_bootstrap,
    detect_task_entry_points,
    detect_validation_harness,
    detect_linting,
    detect_codebase_map,
    detect_doc_structure,
    detect_decision_records,
    detect_agent_instructions,
    detect_file_discipline,
)
from sesh.audit.engine import run_audit, _REGISTRY
from sesh.audit.formatter import format_audit_report, audit_to_json


# ============================================================
# Bootstrap
# ============================================================


class TestBootstrap:
    def test_empty_repo(self, tmp_path):
        result = detect_bootstrap(tmp_path, {})
        assert result.score == 0
        assert result.name == "bootstrap"
        assert any(f.status == "missing" for f in result.findings)

    def test_full_python_project(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[build-system]\nrequires = ['setuptools']\n")
        (tmp_path / "requirements.txt").write_text("pytest\nrequests\n")
        (tmp_path / "README.md").write_text("# Project\n\n## Install\n\npip install -e .\n" * 5)
        result = detect_bootstrap(tmp_path, {})
        assert result.score == 10
        assert all(f.status == "found" for f in result.findings)

    def test_partial_no_deps(self, tmp_path):
        (tmp_path / "package.json").write_text('{"name": "x"}')
        result = detect_bootstrap(tmp_path, {})
        assert 1 <= result.score <= 5
        assert any(f.status == "missing" for f in result.findings)

    def test_readme_without_install(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        (tmp_path / "requirements.txt").write_text("flask\n")
        (tmp_path / "README.md").write_text("# Project\n\nThis is a project.\n")
        result = detect_bootstrap(tmp_path, {})
        # Has setup + deps but README has no install keywords
        assert result.score == 7
        assert any(f.status == "warning" for f in result.findings)


# ============================================================
# Task Entry Points
# ============================================================


class TestTaskEntryPoints:
    def test_empty_repo(self, tmp_path):
        result = detect_task_entry_points(tmp_path, {})
        assert result.score == 0

    def test_package_json_with_scripts(self, tmp_path):
        (tmp_path / "package.json").write_text(json.dumps({
            "name": "x",
            "scripts": {"build": "tsc", "test": "jest", "lint": "eslint ."}
        }))
        result = detect_task_entry_points(tmp_path, {})
        assert result.score >= 4

    def test_makefile_with_targets(self, tmp_path):
        (tmp_path / "Makefile").write_text("build:\n\tgo build\n\ntest:\n\tgo test ./...\n")
        result = detect_task_entry_points(tmp_path, {})
        assert result.score >= 3

    def test_pyproject_with_scripts(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            "[project]\nname='x'\n\n[project.scripts]\nsesh = 'sesh.cli:main'\n"
        )
        result = detect_task_entry_points(tmp_path, {})
        assert result.score >= 3


# ============================================================
# Validation Harness
# ============================================================


class TestValidationHarness:
    def test_empty_repo(self, tmp_path):
        result = detect_validation_harness(tmp_path, {})
        assert result.score == 0

    def test_full_harness(self, tmp_path):
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_main.py").write_text("def test_x(): pass")
        (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\ntestpaths=['tests']\n")
        gh = tmp_path / ".github" / "workflows"
        gh.mkdir(parents=True)
        (gh / "ci.yml").write_text("name: CI\non: push\njobs: {}")
        result = detect_validation_harness(tmp_path, {})
        assert result.score == 10

    def test_tests_only(self, tmp_path):
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_app.py").write_text("def test_y(): pass")
        result = detect_validation_harness(tmp_path, {})
        assert result.score == 3  # tests only, no config or CI


# ============================================================
# Linting
# ============================================================


class TestLinting:
    def test_empty_repo(self, tmp_path):
        result = detect_linting(tmp_path, {})
        assert result.score == 0

    def test_ruff_in_pyproject(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[tool.ruff]\nline-length = 100\n")
        result = detect_linting(tmp_path, {})
        assert result.score >= 3

    def test_eslint_and_prettier(self, tmp_path):
        (tmp_path / ".eslintrc.json").write_text('{"extends": "eslint:recommended"}')
        (tmp_path / ".prettierrc").write_text('{"semi": true}')
        result = detect_linting(tmp_path, {})
        assert result.score >= 5  # 3 + 2

    def test_biome(self, tmp_path):
        (tmp_path / "biome.json").write_text('{}')
        result = detect_linting(tmp_path, {})
        assert result.score >= 3


# ============================================================
# Codebase Map
# ============================================================


class TestCodebaseMap:
    def test_empty_repo(self, tmp_path):
        result = detect_codebase_map(tmp_path, {})
        assert result.score == 0

    def test_full_map(self, tmp_path):
        (tmp_path / "ARCHITECTURE.md").write_text("# Architecture\n\nOverview here.\n")
        (tmp_path / "CLAUDE.md").write_text("# Claude Instructions\n")
        src = tmp_path / "src"
        src.mkdir()
        (src / "README.md").write_text("# Source\n")
        result = detect_codebase_map(tmp_path, {})
        assert result.score >= 8

    def test_claude_md_only(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("# Instructions\n")
        result = detect_codebase_map(tmp_path, {})
        assert result.score == 3


# ============================================================
# Doc Structure
# ============================================================


class TestDocStructure:
    def test_empty_repo(self, tmp_path):
        result = detect_doc_structure(tmp_path, {})
        assert result.score == 0

    def test_substantive_readme(self, tmp_path):
        (tmp_path / "README.md").write_text("# Project\n" + "Line of docs\n" * 60)
        result = detect_doc_structure(tmp_path, {})
        assert result.score >= 4

    def test_thin_readme(self, tmp_path):
        (tmp_path / "README.md").write_text("# Project\n\nA thing.\n")
        result = detect_doc_structure(tmp_path, {})
        assert result.score <= 2

    def test_docs_directory(self, tmp_path):
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "guide.md").write_text("# Guide\n")
        (tmp_path / "README.md").write_text("# Project\n" + "Detail\n" * 60)
        result = detect_doc_structure(tmp_path, {})
        assert result.score >= 7


# ============================================================
# Decision Records
# ============================================================


class TestDecisionRecords:
    def test_empty_repo(self, tmp_path):
        result = detect_decision_records(tmp_path, {})
        assert result.score == 0

    def test_adr_directory(self, tmp_path):
        adr = tmp_path / "docs" / "adr"
        adr.mkdir(parents=True)
        (adr / "001-use-postgres.md").write_text("# Use PostgreSQL\n\nDecided because...\n")
        result = detect_decision_records(tmp_path, {})
        assert result.score >= 5

    def test_changelog(self, tmp_path):
        (tmp_path / "CHANGELOG.md").write_text("# Changelog\n\n" + "- Fix something\n" * 25)
        result = detect_decision_records(tmp_path, {})
        assert result.score >= 5

    def test_both(self, tmp_path):
        adr = tmp_path / "docs" / "decisions"
        adr.mkdir(parents=True)
        (adr / "001.md").write_text("# Decision\n")
        (tmp_path / "CHANGELOG.md").write_text("# Changelog\n" + "- entry\n" * 25)
        result = detect_decision_records(tmp_path, {})
        assert result.score == 10


# ============================================================
# Agent Instructions
# ============================================================


class TestAgentInstructions:
    def test_empty_repo(self, tmp_path):
        result = detect_agent_instructions(tmp_path, {})
        assert result.score == 0
        assert len(result.recommendations) > 0

    def test_claude_md_only(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("# Instructions\nRead before writing.\n")
        result = detect_agent_instructions(tmp_path, {})
        assert result.score == 5

    def test_full_agent_setup(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("# Claude\n")
        (tmp_path / "AGENTS.md").write_text("# Agents\n")
        (tmp_path / ".cursorrules").write_text("rules here\n")
        gh = tmp_path / ".github"
        gh.mkdir()
        (gh / "copilot-instructions.md").write_text("# Copilot\n")
        result = detect_agent_instructions(tmp_path, {})
        assert result.score == 10  # 5+3+2+2 = 12, capped at 10


# ============================================================
# File Discipline
# ============================================================


class TestFileDiscipline:
    def test_empty_repo(self, tmp_path):
        result = detect_file_discipline(tmp_path, {})
        assert result.score == 5  # neutral
        assert result.name == "file_discipline"

    def test_small_files(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        for i in range(5):
            (src / f"mod_{i}.py").write_text("# module\n" * 50)
        result = detect_file_discipline(tmp_path, {})
        assert result.score >= 8

    def test_large_file_penalty(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "huge.py").write_text("x = 1\n" * 1200)  # >2x threshold
        for i in range(3):
            (src / f"small_{i}.py").write_text("x = 1\n" * 50)
        result = detect_file_discipline(tmp_path, {})
        assert result.score <= 7  # penalized for huge file

    def test_many_large_files(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        for i in range(5):
            (src / f"big_{i}.py").write_text("x = 1\n" * 600)
        result = detect_file_discipline(tmp_path, {})
        assert result.score <= 5  # heavy penalty

    def test_skips_node_modules(self, tmp_path):
        nm = tmp_path / "node_modules" / "pkg"
        nm.mkdir(parents=True)
        (nm / "huge.js").write_text("x = 1;\n" * 5000)
        src = tmp_path / "src"
        src.mkdir()
        (src / "app.ts").write_text("const x = 1;\n" * 50)
        result = detect_file_discipline(tmp_path, {})
        assert result.score >= 8  # node_modules ignored


# ============================================================
# Integration
# ============================================================


class TestRunAuditIntegration:
    def test_registry_has_9_metrics(self):
        """All 9 detectors should be registered."""
        names = [name for name, _ in _REGISTRY]
        assert len(names) >= 9
        assert "bootstrap" in names
        assert "file_discipline" in names

    def test_audit_agentsesh_repo(self):
        """Run audit on the agentsesh repo itself."""
        repo = Path(__file__).parent.parent
        result = run_audit(repo)
        assert result.score > 0
        assert result.grade in ("A+", "A", "B", "C", "D", "F")
        assert len(result.metrics) == 9

    def test_audit_empty_repo(self, tmp_path):
        result = run_audit(tmp_path)
        assert result.grade in ("D", "F")
        assert result.score < 50

    def test_audit_single_metric(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("# Instructions\n")
        result = run_audit(tmp_path, enabled=["agent_instructions"])
        assert len(result.metrics) == 1
        assert result.metrics[0].name == "agent_instructions"
        assert result.metrics[0].score == 5

    def test_format_audit_report(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        result = run_audit(tmp_path)
        output = format_audit_report(result)
        assert "Repo Audit" in output
        assert "Grade" in output

    def test_audit_to_json(self, tmp_path):
        result = run_audit(tmp_path)
        output = audit_to_json(result)
        data = json.loads(output)
        assert "score" in data
        assert "grade" in data
        assert "metrics" in data
        assert len(data["metrics"]) == 9
