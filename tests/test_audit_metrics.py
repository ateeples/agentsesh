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

    def test_tox_ini(self, tmp_path):
        (tmp_path / "tox.ini").write_text("[tox]\nenvlist = py310,py311\n")
        result = detect_task_entry_points(tmp_path, {})
        assert result.score >= 2
        assert any("tox" in f.description for f in result.findings)

    def test_tox_in_pyproject(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[tool.tox]\nlegacy_tox_ini = '[tox]'\n")
        result = detect_task_entry_points(tmp_path, {})
        assert result.score >= 2

    def test_noxfile(self, tmp_path):
        (tmp_path / "noxfile.py").write_text("import nox\n@nox.session\ndef tests(s): pass\n")
        result = detect_task_entry_points(tmp_path, {})
        assert result.score >= 2
        assert any("noxfile" in f.description for f in result.findings)

    def test_justfile(self, tmp_path):
        (tmp_path / "justfile").write_text("build:\n  cargo build\n\ntest:\n  cargo test\n")
        result = detect_task_entry_points(tmp_path, {})
        assert result.score >= 3
        assert any("justfile" in f.description for f in result.findings)

    def test_taskfile_yml(self, tmp_path):
        (tmp_path / "Taskfile.yml").write_text("version: '3'\ntasks:\n  build:\n    cmds: ['go build']\n")
        result = detect_task_entry_points(tmp_path, {})
        assert result.score >= 3

    def test_python_full_stack(self, tmp_path):
        """Python project with Makefile + pyproject scripts + tox should score 8+."""
        (tmp_path / "pyproject.toml").write_text(
            "[project]\nname='x'\n\n[project.scripts]\napp = 'app.cli:main'\n"
        )
        (tmp_path / "Makefile").write_text("test:\n\tpytest\n\nlint:\n\truff check .\n")
        (tmp_path / "tox.ini").write_text("[tox]\nenvlist = py310\n")
        result = detect_task_entry_points(tmp_path, {})
        assert result.score >= 8  # pyproject(3) + makefile(3) + tox(2)


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

    def test_mypy_ini(self, tmp_path):
        (tmp_path / "mypy.ini").write_text("[mypy]\nstrict = true\n")
        result = detect_linting(tmp_path, {})
        assert result.score >= 3
        assert any("mypy" in f.description for f in result.findings)

    def test_mypy_in_pyproject(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[tool.mypy]\nstrict = true\n")
        result = detect_linting(tmp_path, {})
        assert result.score >= 3

    def test_mypy_in_setup_cfg(self, tmp_path):
        (tmp_path / "setup.cfg").write_text("[mypy]\nstrict = True\n")
        result = detect_linting(tmp_path, {})
        assert result.score >= 3

    def test_pyright(self, tmp_path):
        (tmp_path / "pyrightconfig.json").write_text('{"typeCheckingMode": "strict"}')
        result = detect_linting(tmp_path, {})
        assert result.score >= 3
        assert any("pyright" in f.description for f in result.findings)

    def test_pyright_in_pyproject(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[tool.pyright]\ntypeCheckingMode = 'strict'\n")
        result = detect_linting(tmp_path, {})
        assert result.score >= 3

    def test_pre_commit(self, tmp_path):
        (tmp_path / ".pre-commit-config.yaml").write_text("repos:\n  - repo: https://github.com/astral-sh/ruff-pre-commit\n")
        result = detect_linting(tmp_path, {})
        assert result.score >= 2
        assert any("pre-commit" in f.description for f in result.findings)

    def test_python_full_stack(self, tmp_path):
        """Python project with ruff + mypy + pre-commit should score 8+."""
        (tmp_path / "pyproject.toml").write_text("[tool.ruff]\nline-length = 100\n\n[tool.mypy]\nstrict = true\n")
        (tmp_path / ".pre-commit-config.yaml").write_text("repos: []\n")
        result = detect_linting(tmp_path, {})
        assert result.score >= 8  # ruff(3) + mypy(3) + pre-commit(2)

    def test_python_ruff_plus_mypy(self, tmp_path):
        """ruff + mypy should score 6 — no longer capped at 5 for Python."""
        (tmp_path / "ruff.toml").write_text("line-length = 100\n")
        (tmp_path / "mypy.ini").write_text("[mypy]\nstrict = true\n")
        result = detect_linting(tmp_path, {})
        assert result.score >= 6


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

    def test_python_docstrings_counted(self, tmp_path):
        """Docstrings should count toward documentation density for Python files."""
        (tmp_path / "README.md").write_text("# Project\n" + "Detail\n" * 60)
        # 20 lines total, 6 are docstring lines (30% density — well above 5%)
        (tmp_path / "app.py").write_text(
            '"""Module docstring."""\n'
            "\n"
            "def foo():\n"
            '    """Do the thing.\n'
            "\n"
            '    With details.\n'
            '    """\n'
            "    return 1\n"
            "\n"
            "def bar():\n"
            '    """Another function."""\n'
            "    return 2\n"
            "\n"
            "class Baz:\n"
            '    """A class.\n'
            "\n"
            '    Does stuff.\n'
            '    """\n'
            "    x = 1\n"
            "    y = 2\n"
        )
        result = detect_doc_structure(tmp_path, {})
        # Should get comment density points (docstrings counted)
        density_findings = [f for f in result.findings if "density" in f.description.lower() or "comment" in f.description.lower()]
        assert density_findings
        # Should pass threshold (not "Low comment density")
        assert not any("Low" in f.description for f in density_findings)

    def test_hash_comments_still_counted(self, tmp_path):
        """Regular # comments should still count for Python files."""
        (tmp_path / "README.md").write_text("# Project\n" + "Detail\n" * 60)
        # 10 lines, 5 are # comments (50% — well above threshold)
        (tmp_path / "app.py").write_text(
            "# Main module\n"
            "# Handles core logic\n"
            "import os\n"
            "# Config\n"
            "X = 1\n"
            "# Constants\n"
            "Y = 2\n"
            "# End\n"
            "def run():\n"
            "    pass\n"
        )
        result = detect_doc_structure(tmp_path, {})
        assert not any("Low" in f.description for f in result.findings if "density" in f.description.lower())

    def test_js_comments_unchanged(self, tmp_path):
        """JS/TS files should still use // and /* comment detection."""
        (tmp_path / "README.md").write_text("# Project\n" + "Detail\n" * 60)
        (tmp_path / "app.js").write_text(
            "// Main module\n"
            "/* Config block */\n"
            "const x = 1;\n"
            "const y = 2;\n"
        )
        result = detect_doc_structure(tmp_path, {})
        density_findings = [f for f in result.findings if "density" in f.description.lower() or "comment" in f.description.lower()]
        assert density_findings


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
