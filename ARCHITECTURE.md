# Architecture

## Overview

AgentSesh is a pipeline: transcript in → structured data → analysis → actionable output.

```
JSONL transcript
      ↓
  [Parsers]           sesh/parsers/     — format detection, tool call extraction
      ↓
  [Database]          sesh/db.py        — SQLite + FTS5 storage (optional)
      ↓
  [Analyzers]         sesh/analyzers/   — patterns, grading, trends, remediation
      ↓
  [Formatters]        sesh/formatters/  — human-readable, JSON, handoff
      ↓
  CLI / MCP / Web
```

## Key Directories

### `sesh/parsers/`
Format-specific transcript parsers. Each parser converts raw JSONL into a normalized `Session` with `ToolCall` objects. Currently supports Claude Code and OpenAI Codex formats.

### `sesh/analyzers/`
Stateless analysis functions that operate on parsed data:
- **patterns.py** — behavioral anti-pattern detection (write-without-read, error streaks, bash overuse, etc.)
- **grader.py** — session grading (A+ to F) based on weighted pattern severity
- **trends.py** — cross-session trend analysis
- **remediation.py** — generates actionable fixes and CLAUDE.md patches
- **outcomes.py** — test/build/lint outcome extraction and comparison

### `sesh/audit/`
Repository legibility scoring. Nine filesystem-only detectors grade how agent-ready a repo is (0-100). No execution, no LLM calls.

### `sesh/formatters/`
Output formatting separated from analysis logic. Report, handoff, and JSON formatters.

## Data Flow

### `sesh analyze` (no DB)
```
file → parse_transcript() → detect_all_patterns() → grade_session()
                           → build_timeline() → extract_decision_points()
                           → get_all_remediations()
                           → AnalysisResult → format_analysis()
```

### `sesh audit` (no DB)
```
repo_path → run_audit() → [9 metric detectors] → AuditResult → format_audit_report()
```

### DB-backed commands (`reflect`, `replay`, `fix`, etc.)
```
file → parse_transcript() → db.ingest_session() → SQLite
                                                      ↓
                              db.get_session() ← CLI query
                                    ↓
                              analyzers/formatters → output
```

## Design Decisions

- **Zero dependencies** — the stdlib handles everything. See `docs/adr/001-zero-dependencies.md`.
- **Filesystem-only audit** — detectors read files, never execute code. Safe to run on any repo.
- **Self-registering detectors** — pattern and audit metric detectors register at import time via decorator/function call.
- **Dual interface** — every command works both as CLI (`--json`) and as MCP tool, sharing the same analysis engine.
