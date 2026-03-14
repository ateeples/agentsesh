# AgentSesh

Agent session intelligence CLI. Parses AI agent transcripts, detects behavioral anti-patterns, grades sessions, and generates remediations.

## Quick Start

```bash
pip install -e .          # install from source
pytest tests/ -v          # run tests (~400)
ruff check sesh/ tests/   # lint
```

## Architecture

- `sesh/` — main package
  - `cli.py` — CLI entry point, argparse subcommands
  - `analyze.py` — one-command analysis pipeline (no DB)
  - `db.py` — SQLite + FTS5 session storage
  - `config.py` — configuration loading from `.sesh/config.json`
  - `watch.py` — filesystem polling for auto-ingest
  - `debug.py` — thinking block extraction and search
  - `replay.py` — step-by-step timeline reconstruction
  - `mcp_server.py` — MCP server for agent-native access
  - `parsers/` — transcript parsers (Claude Code, OpenAI Codex)
  - `analyzers/` — pattern detection, grading, trends, remediation
  - `audit/` — repo legibility scoring (9 metrics, 0-100)
  - `formatters/` — output formatting (report, handoff, JSON)
  - `web/` — browser dashboard server
- `tests/` — pytest test suite
- `eval/` — evaluation harnesses for testing remediation effectiveness
- `docs/` — landing page and documentation

## Conventions

- Zero required dependencies. The stdlib is the dependency.
- All new features need tests. Run `pytest tests/ -v` before committing.
- Use `ruff` for linting. Run `ruff check sesh/` before committing.
- CLI subcommands live in `sesh/commands/` — dispatch is in `cli.py`.
- Pattern detectors register via `register_pattern()` in `analyzers/patterns.py`.
- Audit metric detectors register via `register_metric()` in `audit/engine.py`.
- Every command supports `--json` for machine-readable output.

## Key Entry Points

| Command | What it does | Needs DB? |
|---------|-------------|-----------|
| `sesh analyze <file>` | One-shot session diagnostic | No |
| `sesh audit [path]` | Repo legibility score | No |
| `sesh reflect` | Analyze most recent session | Yes |
| `sesh replay` | Step-by-step playback | Yes |
| `sesh debug` | Search agent thinking blocks | Yes |
| `sesh fix` | Generate CLAUDE.md remediation patch | Yes |
| `sesh watch` | Auto-ingest new sessions | Yes |
