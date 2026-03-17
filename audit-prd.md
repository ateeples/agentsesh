# sesh audit — Repo Legibility Scoring

## What this achieves

sesh grades agent **behavior** (sessions). `sesh audit` grades the **environment** (repos). Together: "did the agent fail because it's bad, or because the repo was hostile?"

## Feature list (metrics)

Each metric scores 0-10 and produces findings (what exists, what's missing).

| # | Metric | What it checks |
|---|--------|---------------|
| 1 | **Bootstrap** | Can an agent set up from scratch? Setup script, dependency file, README with install steps |
| 2 | **Task entry points** | Are build/test/lint/run discoverable? package.json scripts, Makefile, pyproject.toml |
| 3 | **Validation harness** | Can the agent verify changes? Test files exist, test config, CI config |
| 4 | **Linting** | Can the agent self-check quality? Linter/formatter configs (.eslintrc, ruff, prettier) |
| 5 | **Codebase map** | Is there a navigation doc? ARCHITECTURE.md, AGENTS.md, CLAUDE.md, directory-level READMEs |
| 6 | **Doc structure** | Is documentation organized? README substantive (>50 lines), docs/ directory, inline comments |
| 7 | **Decision records** | Are choices documented? ADRs, CHANGELOG, meaningful commit messages |
| 8 | **Agent instructions** | Is there agent-specific guidance? CLAUDE.md, .cursorrules, AGENTS.md, .github/copilot |
| 9 | **File discipline** | Are files kept navigable? Max LOC, avg LOC, files over threshold |

## Integration logic

```
repo path
  → run each metric detector against the filesystem
  → each returns: MetricResult(name, score 0-10, findings[], recommendations[])
  → scoring engine weights + combines → AuditResult(score 0-100, grade, metrics[], recommendations[])
  → formatter renders report
```

**Scoring:** weighted average of metric scores, scaled to 0-100. Default weights equal. Configurable in `.sesh/config.json` under `audit.weights`.

**Grade mapping:** same scale as sessions — A+ (95+), A (90+), B (75+), C (60+), D (45+), F (<45).

## How metrics work together

Metrics are **independent detectors** — same pattern as `sesh/analyzers/patterns.py`. Each is a function: `(repo_path, config) -> MetricResult`. A registry runs all enabled metrics. New metrics plug in without changing the engine.

The findings from each metric feed into recommendations — same pattern as `sesh/analyzers/remediation.py`. "Missing test config" → "Add pytest.ini or [tool.pytest] to pyproject.toml".

## MiniMax harness connection

The eval loop becomes:

```
1. sesh audit <repo>                    ← NEW: score the environment
2. Call MiniMax with bad CLAUDE.md      (existing)
3. Grade with sesh                      (existing)
4. sesh fix --patch                     (existing)
5. Re-run with patched CLAUDE.md        (existing)
6. Compare grades                       (existing)
```

Audit score is included in eval results. Enables:
- Correlation: "agent scored C, but repo scored D on validation — environment is the bottleneck"
- The harness could generate AGENTS.md / CLAUDE.md from audit findings as a pre-step

## CLI

```
sesh audit [path]           # Audit repo at path (default: cwd)
sesh audit --json           # JSON output
sesh audit --metric bootstrap  # Run only one metric
```

## What this does NOT do

- Does not read file contents for quality assessment (that's subjective)
- Does not execute anything (no running tests, no building)
- Does not require a .sesh/ database (filesystem-only analysis)
- Does not replace session grading — complements it
