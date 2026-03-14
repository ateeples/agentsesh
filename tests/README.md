# tests/ — Test Suite

355+ tests covering all major subsystems. Run with `pytest tests/ -v`.

## Test Files

| File | What it tests |
|------|--------------|
| `test_parser.py` | Claude Code transcript parsing |
| `test_openai_parser.py` | OpenAI Codex transcript parsing |
| `test_patterns.py` | Anti-pattern detection (12 pattern types) |
| `test_grader.py` | Session grading (A+ to F scale) |
| `test_cli.py` | CLI subcommand integration |
| `test_watch.py` | Auto-discovery and watch mode |
| `test_replay.py` | Timeline reconstruction and filtering |
| `test_debug.py` | Thinking block search and correlation |
| `test_remediation.py` | Remediation generation and CLAUDE.md patches |
| `test_outcomes.py` | Outcome extraction and comparison |
| `test_analyze.py` | One-command analysis pipeline |
| `test_audit_engine.py` | Audit engine and scoring |
| `test_audit_metrics.py` | Individual audit metric detectors |
