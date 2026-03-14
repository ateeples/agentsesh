"""Repo legibility metric detectors for sesh audit.

Nine metrics that grade a repository's agent-readiness. Each detector
is a standalone function: (repo_path, config) -> MetricResult.
No execution, no LLM calls, filesystem-only.

Detectors self-register at module load via register_metric().
"""

import json as json_mod
import re
from pathlib import Path

from .engine import Finding, MetricResult, register_metric

# File extensions considered "source code" for file_discipline and comment density.
# Intentionally conservative — only well-known language extensions.
_SOURCE_EXTENSIONS = frozenset({
    ".py", ".ts", ".js", ".tsx", ".jsx", ".rs", ".go", ".java",
    ".cpp", ".c", ".h", ".hpp", ".cs", ".rb", ".kt", ".swift",
})

# Directories to always skip when walking the repo tree.
# These are build artifacts, package caches, and generated code.
_SKIP_DIRS = frozenset({
    "node_modules", ".git", "__pycache__", "dist", "build",
    ".tox", ".venv", "venv", ".eggs", "target", ".next",
    "vendor", ".mypy_cache", ".pytest_cache",
})


# ============================================================
# 1. Bootstrap
# ============================================================


def detect_bootstrap(repo_path: Path, config: dict) -> MetricResult:
    """Can an agent set up from scratch?

    Checks for: setup/build script (3pts), dependency file (4pts),
    README with install steps (3pts). Max 10.
    """
    score = 0
    findings = []
    recs = []

    # Setup/build script — how does the agent install this project?
    setup_files = [
        "pyproject.toml", "setup.py", "setup.cfg", "package.json",
        "Cargo.toml", "go.mod", "CMakeLists.txt", "build.gradle",
    ]
    found_setup = [f for f in setup_files if (repo_path / f).exists()]
    if found_setup:
        score += 3
        findings.append(Finding("found", f"Setup script: {', '.join(found_setup)}"))
    else:
        findings.append(Finding("missing", "No setup script (pyproject.toml, package.json, etc.)"))
        recs.append("Add a setup script (pyproject.toml, package.json, or Cargo.toml)")

    # Dependency file
    dep_files = [
        "requirements.txt", "Pipfile", "poetry.lock",
        "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
        "Cargo.lock", "go.sum",
    ]
    # Also check requirements/ directory
    req_dir = repo_path / "requirements"
    has_req_dir = req_dir.is_dir() and any(req_dir.glob("*.txt"))
    found_deps = [f for f in dep_files if (repo_path / f).exists()]
    if found_deps or has_req_dir:
        score += 4
        label = ", ".join(found_deps) if found_deps else "requirements/"
        findings.append(Finding("found", f"Dependency file: {label}"))
    else:
        findings.append(Finding("missing", "No dependency lock/requirements file"))
        recs.append("Add a dependency file (requirements.txt, package-lock.json, etc.)")

    # README with install steps
    readme = repo_path / "README.md"
    if readme.exists():
        try:
            lines = readme.read_text(errors="replace").lower().splitlines()[:100]
            text = " ".join(lines)
            install_keywords = ["install", "pip ", "npm ", "cargo ", "go get", "setup", "getting started"]
            if any(kw in text for kw in install_keywords):
                score += 3
                findings.append(Finding("found", "README has install/setup instructions"))
            else:
                findings.append(Finding("warning", "README exists but no install instructions found"))
                recs.append("Add installation instructions to README (quick start section)")
        except OSError:
            findings.append(Finding("missing", "README.md not readable"))
    else:
        findings.append(Finding("missing", "No README.md"))
        recs.append("Add README.md with project overview and install steps")

    return MetricResult(
        name="bootstrap",
        score=min(10, score),
        findings=findings,
        recommendations=recs,
    )


# ============================================================
# 2. Task Entry Points
# ============================================================


def detect_task_entry_points(repo_path: Path, config: dict) -> MetricResult:
    """Are build/test/lint/run discoverable?

    Checks: package.json scripts (4pts), Makefile targets (3pts),
    pyproject.toml scripts (3pts). Max 10.
    """
    score = 0
    findings = []
    recs = []

    # package.json scripts
    pkg_json = repo_path / "package.json"
    if pkg_json.exists():
        try:
            data = json_mod.loads(pkg_json.read_text(errors="replace"))
            scripts = data.get("scripts", {})
            if len(scripts) >= 2:
                score += 4
                findings.append(Finding("found", f"package.json scripts: {len(scripts)} entries"))
            elif scripts:
                score += 2
                findings.append(Finding("warning", f"package.json scripts: only {len(scripts)} entry"))
            else:
                findings.append(Finding("warning", "package.json exists but no scripts"))
        except (json_mod.JSONDecodeError, OSError):
            findings.append(Finding("warning", "package.json not parseable"))

    # Makefile
    makefile = repo_path / "Makefile"
    if makefile.exists():
        try:
            lines = makefile.read_text(errors="replace").splitlines()
            targets = [l for l in lines if re.match(r'^[a-zA-Z][a-zA-Z0-9_-]*\s*:', l)]
            if len(targets) >= 2:
                score += 3
                findings.append(Finding("found", f"Makefile: {len(targets)} targets"))
            elif targets:
                score += 1
                findings.append(Finding("warning", f"Makefile: only {len(targets)} target"))
        except OSError:
            pass

    # pyproject.toml scripts
    pyproject = repo_path / "pyproject.toml"
    if pyproject.exists():
        try:
            text = pyproject.read_text(errors="replace")
            has_scripts = any(
                section in text
                for section in [
                    "[project.scripts]", "[tool.taskipy", "[tool.hatch.envs",
                    "[tool.poetry.scripts]",
                ]
            )
            if has_scripts:
                score += 3
                findings.append(Finding("found", "pyproject.toml has script/entry point definitions"))
        except OSError:
            pass

    if score == 0:
        recs.append("Add discoverable task entry points (npm scripts, Makefile, or pyproject.toml scripts)")

    return MetricResult(
        name="task_entry_points",
        score=min(10, score),
        findings=findings,
        recommendations=recs,
    )


# ============================================================
# 3. Validation Harness
# ============================================================


def detect_validation_harness(repo_path: Path, config: dict) -> MetricResult:
    """Can the agent verify changes?

    Checks: test files (3pts), test config (3pts), CI config (4pts). Max 10.
    """
    score = 0
    findings = []
    recs = []

    # Test files
    test_dirs = ["tests", "test", "__tests__", "spec"]
    test_patterns = ["test_*.py", "*.test.ts", "*.test.js", "*.spec.ts", "*.spec.js", "*_test.go", "*_test.rs"]
    found_tests = False
    test_count = 0

    for td in test_dirs:
        d = repo_path / td
        if d.is_dir():
            for pattern in test_patterns:
                test_count += len(list(d.rglob(pattern)))
            if test_count > 0:
                found_tests = True
                break

    # Also check repo root if not found in test dirs
    if not found_tests:
        for pattern in test_patterns:
            test_count += len(list(repo_path.rglob(pattern)))
            if test_count > 20:  # cap search early
                break
        found_tests = test_count > 0

    if found_tests:
        score += 3
        findings.append(Finding("found", f"Test files: {min(test_count, 20)}+ found"))
    else:
        findings.append(Finding("missing", "No test files found"))
        recs.append("Add test files (test_*.py, *.test.ts, etc.)")

    # Test config
    test_configs = [
        "pytest.ini", "jest.config.js", "jest.config.ts", "jest.config.mjs",
        "vitest.config.ts", "vitest.config.js", ".mocharc.js", ".mocharc.yml",
        "karma.conf.js", "phpunit.xml",
    ]
    found_config = [f for f in test_configs if (repo_path / f).exists()]

    # Check pyproject.toml for pytest section
    pyproject = repo_path / "pyproject.toml"
    if pyproject.exists():
        try:
            text = pyproject.read_text(errors="replace")
            if "[tool.pytest" in text:
                found_config.append("pyproject.toml [tool.pytest]")
        except OSError:
            pass

    if found_config:
        score += 3
        findings.append(Finding("found", f"Test config: {', '.join(found_config)}"))
    else:
        findings.append(Finding("missing", "No test configuration file"))
        recs.append("Add test config (pytest.ini, jest.config.js, or [tool.pytest] in pyproject.toml)")

    # CI config
    ci_found = False
    gh_workflows = repo_path / ".github" / "workflows"
    if gh_workflows.is_dir() and any(gh_workflows.glob("*.yml")) or any(gh_workflows.glob("*.yaml") if gh_workflows.is_dir() else []):
        ci_found = True
        findings.append(Finding("found", "CI: GitHub Actions workflows"))

    other_ci = [
        (".circleci/config.yml", "CircleCI"),
        (".gitlab-ci.yml", "GitLab CI"),
        ("Jenkinsfile", "Jenkins"),
        (".travis.yml", "Travis CI"),
    ]
    for path, name in other_ci:
        if (repo_path / path).exists():
            ci_found = True
            findings.append(Finding("found", f"CI: {name}"))

    if ci_found:
        score += 4
    else:
        findings.append(Finding("missing", "No CI configuration"))
        recs.append("Add CI config (.github/workflows/, .gitlab-ci.yml, etc.)")

    return MetricResult(
        name="validation_harness",
        score=min(10, score),
        findings=findings,
        recommendations=recs,
    )


# ============================================================
# 4. Linting
# ============================================================


def detect_linting(repo_path: Path, config: dict) -> MetricResult:
    """Can the agent self-check quality?

    Checks linters, formatters, type checkers, and pre-commit hooks.
    Multiple tools stack, capped at 10.

    JS/TS: ESLint (3), Prettier (2), Biome (3).
    Python: Ruff (3), Pylint (2), mypy (3), pyright (3).
    Any: pre-commit (2).
    """
    score = 0
    findings = []
    recs = []

    pyproject = repo_path / "pyproject.toml"
    pyproject_text = ""
    if pyproject.exists():
        try:
            pyproject_text = pyproject.read_text(errors="replace")
        except OSError:
            pass

    # --- JS/TS tools ---

    # ESLint
    eslint_files = [
        ".eslintrc", ".eslintrc.js", ".eslintrc.json", ".eslintrc.yml",
        "eslint.config.js", "eslint.config.mjs",
    ]
    if any((repo_path / f).exists() for f in eslint_files):
        score += 3
        findings.append(Finding("found", "ESLint config"))

    # Prettier
    prettier_files = [
        ".prettierrc", ".prettierrc.js", ".prettierrc.json",
        ".prettierrc.yml", "prettier.config.js", "prettier.config.mjs",
    ]
    if any((repo_path / f).exists() for f in prettier_files):
        score += 2
        findings.append(Finding("found", "Prettier config"))

    # Biome (replaces eslint+prettier for some projects)
    if (repo_path / "biome.json").exists() or (repo_path / "biome.jsonc").exists():
        score += 3
        findings.append(Finding("found", "Biome config"))

    # --- Python tools ---

    # Ruff
    ruff_found = False
    if (repo_path / "ruff.toml").exists() or (repo_path / ".ruff.toml").exists():
        ruff_found = True
    if not ruff_found and _pyproject_has_section(pyproject_text, "[tool.ruff"):
        ruff_found = True
    if ruff_found:
        score += 3
        findings.append(Finding("found", "Ruff config"))

    # Pylint
    pylint_found = (repo_path / ".pylintrc").exists()
    if not pylint_found:
        pylint_found = _pyproject_has_section(pyproject_text, "[tool.pylint")
    if pylint_found:
        score += 2
        findings.append(Finding("found", "Pylint config"))

    # mypy (type checking)
    mypy_found = any(
        (repo_path / f).exists() for f in ["mypy.ini", ".mypy.ini"]
    )
    if not mypy_found:
        mypy_found = _pyproject_has_section(pyproject_text, "[tool.mypy")
    if not mypy_found:
        # setup.cfg [mypy] section
        setup_cfg = repo_path / "setup.cfg"
        if setup_cfg.exists():
            try:
                text = setup_cfg.read_text(errors="replace")
                mypy_found = any(
                    line.strip() == "[mypy]" for line in text.splitlines()
                )
            except OSError:
                pass
    if mypy_found:
        score += 3
        findings.append(Finding("found", "mypy config"))

    # pyright
    pyright_found = (repo_path / "pyrightconfig.json").exists()
    if not pyright_found:
        pyright_found = _pyproject_has_section(pyproject_text, "[tool.pyright")
    if pyright_found:
        score += 3
        findings.append(Finding("found", "pyright config"))

    # --- Cross-language tools ---

    # pre-commit
    if (repo_path / ".pre-commit-config.yaml").exists():
        score += 2
        findings.append(Finding("found", "pre-commit config"))

    if score == 0:
        findings.append(Finding("missing", "No linter/formatter configuration found"))
        recs.append("Add linter config (ruff.toml, .eslintrc, biome.json, or mypy.ini)")

    return MetricResult(
        name="linting",
        score=min(10, score),
        findings=findings,
        recommendations=recs,
    )


def _pyproject_has_section(text: str, prefix: str) -> bool:
    """Check if pyproject.toml text contains a TOML section starting with prefix."""
    if not text:
        return False
    return any(line.strip().startswith(prefix) for line in text.splitlines())


# ============================================================
# 5. Codebase Map
# ============================================================


def detect_codebase_map(repo_path: Path, config: dict) -> MetricResult:
    """Is there a navigation doc?

    Checks: ARCHITECTURE.md (4pts), agent-aware docs like CLAUDE.md (3pts),
    directory-level READMEs (1-3pts). Max 10.
    """
    score = 0
    findings = []
    recs = []

    # Architecture doc
    arch_files = ["ARCHITECTURE.md", "DESIGN.md", "DESIGN_SPEC.md", "OVERVIEW.md"]
    found_arch = [f for f in arch_files if (repo_path / f).exists()]
    if found_arch:
        score += 4
        findings.append(Finding("found", f"Architecture doc: {', '.join(found_arch)}"))
    else:
        findings.append(Finding("missing", "No architecture document (ARCHITECTURE.md, DESIGN.md)"))
        recs.append("Add ARCHITECTURE.md describing high-level structure and key directories")

    # Agent-aware docs
    agent_docs = ["CLAUDE.md", "AGENTS.md", ".cursorrules"]
    found_agent = [f for f in agent_docs if (repo_path / f).exists()]
    if found_agent:
        score += 3
        findings.append(Finding("found", f"Agent-aware docs: {', '.join(found_agent)}"))
    else:
        findings.append(Finding("missing", "No agent-aware docs (CLAUDE.md, AGENTS.md, .cursorrules)"))
        recs.append("Add CLAUDE.md or AGENTS.md with codebase conventions and navigation hints")

    # Directory-level READMEs
    dir_readme_count = 0
    try:
        for child in repo_path.iterdir():
            if child.is_dir() and child.name not in _SKIP_DIRS and not child.name.startswith("."):
                if (child / "README.md").exists():
                    dir_readme_count += 1
    except OSError:
        pass

    if dir_readme_count > 0:
        points = min(3, dir_readme_count)
        score += points
        findings.append(Finding("found", f"Directory READMEs: {dir_readme_count} subdirectories documented"))
    else:
        findings.append(Finding("missing", "No directory-level READMEs"))
        recs.append("Add README.md files to key subdirectories (src/, lib/, etc.)")

    return MetricResult(
        name="codebase_map",
        score=min(10, score),
        findings=findings,
        recommendations=recs,
    )


# ============================================================
# 6. Doc Structure
# ============================================================


def detect_doc_structure(repo_path: Path, config: dict) -> MetricResult:
    """Is documentation organized?

    Checks: substantive README (4pts), docs/ directory (3pts),
    inline comment density >=5% (3pts). Max 10.
    """
    score = 0
    findings = []
    recs = []

    # Substantive README
    readme = repo_path / "README.md"
    if readme.exists():
        try:
            line_count = len(readme.read_text(errors="replace").splitlines())
            if line_count > 50:
                score += 4
                findings.append(Finding("found", f"README.md: {line_count} lines (substantive)"))
            else:
                score += 1
                findings.append(Finding("warning", f"README.md: only {line_count} lines (thin)"))
                recs.append("Expand README.md to >50 lines with usage examples and architecture overview")
        except OSError:
            findings.append(Finding("warning", "README.md not readable"))
    else:
        findings.append(Finding("missing", "No README.md"))
        recs.append("Add README.md with project overview, install steps, and usage")

    # docs/ directory
    docs_dir = repo_path / "docs"
    if docs_dir.is_dir():
        doc_files = list(docs_dir.rglob("*"))
        doc_count = len([f for f in doc_files if f.is_file()])
        if doc_count > 0:
            score += 3
            findings.append(Finding("found", f"docs/ directory: {doc_count} files"))
        else:
            findings.append(Finding("warning", "docs/ directory exists but is empty"))
    else:
        findings.append(Finding("missing", "No docs/ directory"))
        recs.append("Add docs/ directory for detailed documentation")

    # Inline comment density
    threshold = config.get("comment_threshold", 0.05)
    sample_limit = config.get("doc_sample_limit", 20)
    total_lines = 0
    comment_lines = 0
    sampled = 0

    for src in _walk_source_files(repo_path, limit=sample_limit):
        try:
            lines = src.read_text(errors="replace").splitlines()
            total_lines += len(lines)
            for line in lines:
                stripped = line.strip()
                if stripped.startswith("#") or stripped.startswith("//") or stripped.startswith("/*"):
                    comment_lines += 1
            sampled += 1
        except OSError:
            continue

    if total_lines > 0:
        density = comment_lines / total_lines
        if density >= threshold:
            score += 3
            findings.append(Finding("found", f"Comment density: {density:.1%} ({comment_lines}/{total_lines} lines in {sampled} files)"))
        else:
            findings.append(Finding("warning", f"Low comment density: {density:.1%} (threshold: {threshold:.0%})"))
            recs.append("Add inline comments to explain non-obvious logic")
    elif sampled == 0:
        findings.append(Finding("warning", "No source files found for comment analysis"))

    return MetricResult(
        name="doc_structure",
        score=min(10, score),
        findings=findings,
        recommendations=recs,
    )


# ============================================================
# 7. Decision Records
# ============================================================


def detect_decision_records(repo_path: Path, config: dict) -> MetricResult:
    """Are choices documented?

    Checks: ADR directory with .md files (5pts), CHANGELOG with >20 lines (5pts).
    Max 10.
    """
    score = 0
    findings = []
    recs = []

    # ADR directory
    adr_dirs = ["docs/adr", "docs/decisions", "adr", "decisions", "doc/adr"]
    found_adr = False
    for d in adr_dirs:
        adr_path = repo_path / d
        if adr_path.is_dir():
            md_files = list(adr_path.glob("*.md"))
            if md_files:
                found_adr = True
                score += 5
                findings.append(Finding("found", f"ADR directory: {d}/ ({len(md_files)} records)"))
                break

    if not found_adr:
        findings.append(Finding("missing", "No ADR/decision records directory"))
        recs.append("Add docs/adr/ or docs/decisions/ with architectural decision records")

    # CHANGELOG
    changelog_files = ["CHANGELOG.md", "CHANGELOG", "CHANGES.md", "HISTORY.md"]
    found_changelog = False
    for f in changelog_files:
        cl_path = repo_path / f
        if cl_path.exists():
            try:
                line_count = len(cl_path.read_text(errors="replace").splitlines())
                if line_count > 20:
                    score += 5
                    findings.append(Finding("found", f"{f}: {line_count} lines"))
                    found_changelog = True
                else:
                    score += 2
                    findings.append(Finding("warning", f"{f}: only {line_count} lines (thin)"))
                    found_changelog = True
            except OSError:
                pass
            break

    if not found_changelog:
        findings.append(Finding("missing", "No CHANGELOG"))
        recs.append("Add CHANGELOG.md documenting notable changes per version")

    return MetricResult(
        name="decision_records",
        score=min(10, score),
        findings=findings,
        recommendations=recs,
    )


# ============================================================
# 8. Agent Instructions
# ============================================================


def detect_agent_instructions(repo_path: Path, config: dict) -> MetricResult:
    """Is there agent-specific guidance?

    Checks: CLAUDE.md (5pts), AGENTS.md (3pts), .cursorrules (2pts),
    copilot-instructions.md (2pts). Max 10.
    """
    score = 0
    findings = []
    recs = []

    checks = [
        ("CLAUDE.md", 5, "Claude Code instructions"),
        ("AGENTS.md", 3, "Cross-agent guidance"),
        (".cursorrules", 2, "Cursor rules"),
        (".github/copilot-instructions.md", 2, "GitHub Copilot instructions"),
    ]

    for filename, points, label in checks:
        path = repo_path / filename
        if path.exists():
            score += points
            findings.append(Finding("found", f"{label}: {filename}"))

    if score == 0:
        findings.append(Finding("missing", "No agent instruction files found"))
        recs.append("Add CLAUDE.md with agent-specific instructions (conventions, tool rules, project overview)")
        recs.append("Add AGENTS.md for cross-agent/cross-tool guidance")

    return MetricResult(
        name="agent_instructions",
        score=min(10, score),
        findings=findings,
        recommendations=recs,
    )


# ============================================================
# 9. File Discipline
# ============================================================


def detect_file_discipline(repo_path: Path, config: dict) -> MetricResult:
    """Are files kept navigable?

    Starts at 10 and deducts for: largest file >2x threshold (-3) or >1x (-1),
    average >300 lines (-2), >20% files over threshold (-3) or >10% (-1).
    """
    max_loc_threshold = config.get("file_discipline_max_loc", 500)
    sample_limit = config.get("file_discipline_sample_limit", 200)

    findings = []
    recs = []

    file_sizes: list[tuple[str, int]] = []
    for src in _walk_source_files(repo_path, limit=sample_limit):
        try:
            loc = len(src.read_text(errors="replace").splitlines())
            file_sizes.append((str(src.relative_to(repo_path)), loc))
        except OSError:
            continue

    if not file_sizes:
        return MetricResult(
            name="file_discipline",
            score=5,  # neutral — can't assess
            findings=[Finding("warning", "No source files found to analyze")],
            recommendations=[],
        )

    max_loc = max(loc for _, loc in file_sizes)
    avg_loc = sum(loc for _, loc in file_sizes) // len(file_sizes)
    over_threshold = [(f, loc) for f, loc in file_sizes if loc > max_loc_threshold]
    over_pct = len(over_threshold) / len(file_sizes)

    score = 10

    if max_loc > max_loc_threshold * 2:
        score -= 3
        largest = max(file_sizes, key=lambda x: x[1])
        findings.append(Finding("warning", f"Largest file: {largest[0]} ({largest[1]} lines, >{max_loc_threshold * 2} threshold)"))
    elif max_loc > max_loc_threshold:
        score -= 1
        largest = max(file_sizes, key=lambda x: x[1])
        findings.append(Finding("warning", f"Largest file: {largest[0]} ({largest[1]} lines, >{max_loc_threshold} threshold)"))

    if avg_loc > 300:
        score -= 2
        findings.append(Finding("warning", f"Average file size: {avg_loc} lines (high)"))

    if over_pct > 0.2:
        score -= 3
        findings.append(Finding("warning", f"{len(over_threshold)}/{len(file_sizes)} files over {max_loc_threshold} lines ({over_pct:.0%})"))
        recs.append(f"Break up large files — {len(over_threshold)} files exceed {max_loc_threshold} lines")
    elif over_pct > 0.1:
        score -= 1
        findings.append(Finding("warning", f"{len(over_threshold)}/{len(file_sizes)} files over {max_loc_threshold} lines ({over_pct:.0%})"))

    if not findings:
        findings.append(Finding("found", f"File discipline: {len(file_sizes)} files, max {max_loc} lines, avg {avg_loc} lines"))

    score = max(0, min(10, score))

    return MetricResult(
        name="file_discipline",
        score=score,
        findings=findings,
        recommendations=recs,
    )


# ============================================================
# Helpers
# ============================================================


def _walk_source_files(repo_path: Path, limit: int = 200):
    """Walk source files in repo, skipping build/vendor dirs.

    Yields Path objects for files matching _SOURCE_EXTENSIONS.
    Capped at `limit` to prevent slow scans on huge repos.
    """
    count = 0
    for root, dirs, files in repo_path.walk():
        # Prune skip dirs
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]

        for fname in files:
            if count >= limit:
                return
            fpath = root / fname
            if fpath.suffix in _SOURCE_EXTENSIONS:
                yield fpath
                count += 1


# ============================================================
# Self-registration — all detectors register at import time
# so the audit engine discovers them without explicit wiring.
# ============================================================

_ALL_DETECTORS = [
    ("bootstrap", detect_bootstrap),
    ("task_entry_points", detect_task_entry_points),
    ("validation_harness", detect_validation_harness),
    ("linting", detect_linting),
    ("codebase_map", detect_codebase_map),
    ("doc_structure", detect_doc_structure),
    ("decision_records", detect_decision_records),
    ("agent_instructions", detect_agent_instructions),
    ("file_discipline", detect_file_discipline),
]

for _name, _detector in _ALL_DETECTORS:
    register_metric(_name, _detector)
