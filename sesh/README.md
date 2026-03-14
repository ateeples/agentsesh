# sesh/ — Core Package

## Module Map

| Module | Purpose |
|--------|---------|
| `cli.py` | CLI entry point — argparse subcommands dispatch to `cmd_*` functions |
| `analyze.py` | One-command analysis pipeline (no DB required) |
| `db.py` | SQLite + FTS5 session storage and querying |
| `config.py` | Config loading from `.sesh/config.json` with defaults |
| `debug.py` | Thinking block extraction, search, and correlation |
| `replay.py` | Timeline reconstruction from tool calls and source files |
| `watch.py` | Filesystem polling to auto-discover and ingest sessions |
| `mcp_server.py` | MCP server exposing analysis as agent-callable tools |

## Subpackages

- `parsers/` — Format-specific transcript parsers (Claude Code, OpenAI Codex)
- `analyzers/` — Pattern detection, grading, trends, remediation, outcomes
- `audit/` — Repo legibility scoring (9 metrics)
- `formatters/` — Output formatting (report, handoff, JSON)
- `web/` — Browser dashboard server
