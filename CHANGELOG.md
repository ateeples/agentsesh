# Changelog

All notable changes to AgentSesh are documented here.

## [0.9.0] - 2026-03-14

### Added
- `sesh audit` — repo legibility scoring with 9 metric detectors (bootstrap, task entry points, validation harness, linting, codebase map, doc structure, decision records, agent instructions, file discipline)
- Audit formatter with letter grades (A+ to F), findings, and actionable recommendations
- 49 new tests for audit engine and metrics

### Fixed
- Removed 3 redundant imports in CLI
- Removed dead `--compare` flag from report command
- Fixed `parse_range` validation for edge cases
- Fixed `None` session_id errors in DB queries

## [0.8.0] - 2026-03-13

### Added
- `sesh analyze <file>` — one-command session diagnostic, no database required
- Pipeline: parse → stats → patterns → grade → timeline → decisions → summary
- Cost estimation from model token counts
- Failure point identification (blind edits, error loops, flailing)
- 44 new tests (306 total → 355 total with audit)

## [0.7.0] - 2026-03-13

### Added
- `sesh debug` — prompt debugger for searching agent thinking blocks
- `sesh debug --correlate` — map antipatterns to the decision points that caused them
- `sesh debug --dotnotes` — dot-notation path indexing and glob search in thinking
- `sesh debug --action` — reverse lookup to find thinking behind any action

## [0.6.0] - 2026-03-13

### Added
- `sesh replay` — step-by-step session playback with source file reconstruction
- `sesh test` — outcome comparison between sessions (behavioral regression testing)
- `sesh fix` — remediation engine generating actionable fixes for detected anti-patterns
- `sesh fix --patch` — direct CLAUDE.md patch output

## [0.5.0] - 2026-03-13

### Added
- Prompt-phrasing evaluation harness
- MiniMax evaluation harness for controlled remediation testing

## [0.4.0] - 2026-03-13

### Added
- OpenAI Codex CLI parser

## [0.3.0] - 2026-03-13

### Added
- `sesh watch` — auto-discovery and continuous ingestion of new sessions
- `sesh watch --once` — one-shot scan mode

## [0.2.0] - 2026-03-13

### Added
- MCP server (`sesh-mcp`) for agent-native access
- Web dashboard (`sesh-web`) with session browser
- `sesh search` — full-text search across sessions
- `sesh handoff` — session handoff summaries

## [0.1.0] - 2026-03-13

### Added
- Initial release: CLI with `sesh init`, `sesh log`, `sesh reflect`, `sesh report`, `sesh stats`
- Claude Code transcript parser
- Behavioral anti-pattern detection (12 patterns)
- Session grading (A+ to F)
- SQLite + FTS5 storage
- Cross-session trend analysis
