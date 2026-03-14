# Agent Guidelines

Cross-agent/cross-tool guidance for working with the AgentSesh codebase.

## For Claude Code / Cursor / Copilot

- **Zero dependencies.** Do not add runtime dependencies. If you need a library, implement the functionality using stdlib.
- **Test first.** Run `pytest tests/ -v` before committing any changes.
- **Lint check.** Run `ruff check sesh/` before committing.
- Use `make test`, `make lint`, `make format` for common tasks.

## Code Structure

- CLI subcommands: `sesh/cli.py` — add new commands as `cmd_<name>(args)` functions
- Pattern detectors: `sesh/analyzers/patterns.py` — register via `register_pattern()`
- Audit metrics: `sesh/audit/metrics.py` — register via `register_metric()`
- Parsers: `sesh/parsers/` — implement `BaseParser` protocol
- Formatters: `sesh/formatters/` — display-only, no analysis logic

## Testing

- All tests in `tests/` using pytest
- Helper functions named `_tc()`, `_make_*()` for building test fixtures
- Test files mirror source structure: `test_<module>.py`
- Run specific tests: `pytest tests/test_analyze.py -v`

## Common Patterns

- Every command supports `--json` for machine-readable output
- DB commands use `_get_db(args)` for database resolution
- Non-DB commands (`analyze`, `audit`) work with just a file/directory path
- MCP tools in `mcp_server.py` mirror CLI commands but return strings
