# AgentSesh Roadmap

Last updated: 2026-03-14

## Vision

**v0.x**: Diagnostic — "Here's what happened."
**v1.0**: Closed loop — "Here's what happened, and it's in your agent's context for next time."
**v2.0**: Adaptive — "Your agent improved 15% over 20 sessions. Here's what's still trending wrong."

## Shipped

### v0.10.0 (2026-03-14)
- `sesh analyze --feedback` — closed-loop improvement, writes to CLAUDE.md
- `sesh analyze` auto-discovers most recent session (no args needed)
- `sesh audit --threshold` — CI gate with exit codes
- Audit metrics: mypy, pyright, pre-commit, tox, nox, justfile, Python docstrings
- cli.py split into commands/ package

### v0.9.0 (2026-03-14)
- `sesh audit` — repo legibility scoring, 9 metrics, letter grades

### v0.8.0 (2026-03-13)
- `sesh analyze` — one-command diagnostic, no database required

### v0.7.0 (2026-03-13)
- `sesh debug` — prompt debugger (thinking search, action lookup, dotnotes, correlate)

### v0.6.0 (2026-03-13)
- `sesh replay`, `sesh test`, `sesh fix` — playback, regression testing, remediation

### v0.1.0–0.5.0 (2026-03-13)
- Core: parse, grade, detect patterns, store, trend, search, handoff
- MCP server, web dashboard, watch mode
- Claude Code + OpenAI Codex parsers

## Next (v0.11)

### Specific remediations
The `--fix` output is generic boilerplate ("use Read instead of cat"). It should be session-specific: "you ran `wc -l sesh/cli.py` at call #47 — that's a Read." Map each detected instance to the concrete alternative.

### `sesh analyze --previous`
Skip the current in-progress session and analyze the most recently completed one. Agents analyzing themselves mid-session get incomplete data.

### Cursor/Windsurf support
Parse `.cursorrules` for `--feedback` target. Detect Cursor session transcripts if they exist. Audit metric already checks `.cursorrules` — connect the dots.

## v1.0 — The Closed Loop

The minimum viable version shipped in v0.10.0 (`--feedback`). v1.0 is the polished version:

### Automatic analysis
`sesh watch` + `sesh analyze` combined: watch for completed sessions, auto-analyze, auto-write feedback. No human intervention. Daemon mode.

### Longitudinal feedback
`--feedback` currently shows last session only. v1.0 shows trends: "bash_overuse in 8 of your last 10 sessions, always with `wc` and `find`." Specificity from accumulation.

### Multi-target feedback
Write to CLAUDE.md, .cursorrules, custom files, webhooks. CI integration (PR comments with session analysis).

### Exit codes everywhere
`sesh analyze` returns non-zero if grade below threshold. Same CI-gate pattern as `sesh audit --threshold`.

## v2.0 — Adaptive

### Improvement tracking
"Your bash_overuse rate dropped from 47% to 12% over 20 sessions." Prove that the feedback loop works with data.

### Pattern evolution
Detect when an agent develops new antipatterns as old ones get fixed. "You eliminated bash_overuse but missed_parallelism increased 3x."

### Custom pattern definitions
Let users define their own antipatterns. "Flag any session where the agent creates more than 3 new files" or "warn if test coverage drops."

### Agent benchmarking
Compare agents against each other or against cohort averages. "Your agent's B average is above median for this codebase size."

## Ideas (backlog)

### From the 100 Ideas list
- **Voice drift detector** — analyze writing/communication patterns across sessions
- **Context budget visualizer** — track how agents spend their context window
- **Thread continuity scorer** — rate context maintenance across conversations
- **Session cost estimator** — predict token usage before a session runs
- **Diff as essay** — generate narrative changelog from session tool calls
- **Session archaeology** — deep-dive old sessions with full context reconstruction
- **Agent personality tests** — structured prompts that reveal behavioral tendencies

### Product ideas
- **GitHub Action** — `sesh audit --threshold 80` as a one-line CI step
- **VS Code extension** — show session grade in status bar after completion
- **Leaderboard** — opt-in anonymized benchmarking across the community
- **sesh init --guided** — interactive setup that asks about your workflow
- **Plugin system** — custom parsers, custom patterns, custom audit metrics

### Technical debt
- Split test_debug.py (926 lines)
- Audit metric weights should be configurable per-project
- MCP server needs tests
- Web dashboard needs authentication
