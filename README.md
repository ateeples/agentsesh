# sesh

Find out why your AI coding sessions fail — and how to prevent it.

```bash
pip install agentsesh
```

Zero dependencies. Python 3.10+. Works with Claude Code and OpenAI Codex CLI.

## Try it now

**Diagnose your last session:**

```bash
sesh analyze
```

Auto-finds your most recent Claude Code session. No config, no setup. You get:

```
Session Analysis
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Duration: 25 min | 78 tool calls | ~$1.73
Files touched: 11
Grade: A (93/100)

What Happened
─────────────
78 tool calls, 1 errors (1% error rate).
No critical failures, but 4 process issue(s) detected.

What To Fix
───────────
[ !!] Use dedicated tools instead of Bash (recommended)
[ !!] Research before implementation (recommended)
[  -] Parallelize independent operations (optional)
```

**Grade your repo's AI-readiness:**

```bash
sesh audit
```

Scores your repo on 9 metrics that determine whether an AI agent will succeed or struggle:

```
Repo Audit
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Score: 89/100  Grade: B

Metrics
────────────────────────
  bootstrap           [10/10] ██████████
  task_entry_points   [ 6/10] ██████░░░░
  validation_harness  [10/10] ██████████
  linting             [ 8/10] ████████░░
  codebase_map        [10/10] ██████████
  doc_structure       [10/10] ██████████
  decision_records    [10/10] ██████████
  agent_instructions  [ 8/10] ████████░░
  file_discipline     [ 8/10] ████████░░
```

Both commands work on any repo. No database, no prior setup.

## Close the loop

The analysis is useful. The feedback loop is the point.

```bash
# Write findings directly into CLAUDE.md so the agent sees them next session
sesh analyze --feedback

# Fail CI if repo AI-readiness drops below standard
sesh audit --threshold 80
```

## What it detects

9 pattern detectors run on every session:

| Pattern | What it catches |
|---------|----------------|
| `error_streak` | 3+ consecutive errors — agent is stuck, not fixing |
| `write_without_read` | Editing files without reading them first |
| `bash_overuse` | Using `cat`/`grep`/`find` when dedicated tools exist |
| `low_read_ratio` | Not enough reading relative to writing |
| `error_rate` | Overall error percentage above threshold |
| `repeated_searches` | Same search query run multiple times |
| `write_then_read` | Writing before understanding |
| `scattered_files` | Touching too many directories (unfocused) |
| `missed_parallelism` | Sequential reads that could have been parallel |

## More commands

`sesh analyze` and `sesh audit` require no setup. The commands below use a local database for cross-session tracking:

```bash
sesh init                    # Initialize .sesh/ in current directory
sesh watch --once            # Auto-discover and ingest all sessions
sesh reflect                 # Analyze most recent ingested session
sesh report                  # Cross-session trends
sesh replay                  # Step-by-step session replay
sesh replay --errors         # Show only where things went wrong
sesh test                    # Compare outcomes between two sessions
sesh fix --patch             # Generate CLAUDE.md patch from analysis
sesh search "auth bug"       # Full-text search across sessions
sesh-web                     # Launch browser dashboard (localhost:7433)
```

## MCP Server

Let your agent self-analyze at runtime. Add to Claude Code (`~/.claude/settings.json`):

```json
{
  "mcpServers": {
    "sesh": {
      "command": "sesh-mcp",
      "env": {
        "SESH_DB": "/path/to/your/project/.sesh/sesh.db"
      }
    }
  }
}
```

The agent gets tools like `sesh_reflect`, `sesh_report`, `sesh_sync`, and `sesh_search`. It can ingest its own transcripts at session start and review what went wrong last time.

## Grading

Sessions are scored 0–100 and graded A+ through F. Start at 100, deduct for anti-patterns, bonus for good habits. All weights configurable via `.sesh/config.json`.

## Supported formats

- **Claude Code** (.jsonl) — fully supported
- **OpenAI Codex CLI** (.jsonl) — fully supported (auto-detected)

## Install from source

```bash
git clone https://github.com/ateeples/agentsesh.git
cd agentsesh
pip install -e .
```

## License

MIT
