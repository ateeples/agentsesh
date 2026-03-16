# sesh

See what your AI coding sessions actually look like — across all your projects, over time.

```bash
pip install agentsesh
```

Zero dependencies. Python 3.10+. Works with Claude Code and OpenAI Codex CLI.

## Your behavioral profile

```bash
sesh analyze --profile
```

Auto-discovers all sessions in your current project. Shows you patterns you can't see from inside a session:

```
Behavioral Profile
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Sessions: 93 analyzed

Session Types
─────────────
  BUILD_UNCOMMITTED       46 (49%)
  BUILD_TESTED            12 (13%)
  BUILD_UNTESTED          20 (22%)
  RESEARCH                 5 (5%)

Shipping
────────
  Sessions with commits: 32 / 93 (34%)

Where You Get Stuck
───────────────────
  Edit             9x  avg 5.7 errors  tends to happen mid
    "<tool_use_error>File has not been read y"
  Bash             5x  avg 3.6 errors  tends to happen mid

  When you get stuck:
    50-75%     5 ( 42%)  ████████

Most Reworked Files
───────────────────
  cli.py                 58 edits  across 4 session(s)
  schema.rs              86 edits  across 9 session(s)

Recommendations
───────────────
[!!!] Low commit rate (critical)
      Only 34% of sessions produced commits.
      Action: Commit after each logical unit of work.

[!!!] Read-before-edit violations (critical)
      Stuck on "file not read" errors 9 times.
      Action: Always read a file before editing it.

[ !!] Chronically reworked files (recommended)
      cli.py thrashed across 4 sessions — consider splitting.
```

The profile is the point. Not a grade on one session — patterns across all of them. Where you get stuck, what files you keep reworking, whether you're shipping or churning.

## Single session analysis

```bash
sesh analyze
```

Outcome-based grading. Measures what matters: did you ship, did tests pass, did you get stuck.

```
Session Analysis
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Duration: 47 min | 312 tool calls | ~$8.20
Files touched: 14
Grade: A (90/100)
Session type: BUILD_TESTED

What Happened
─────────────
312 tool calls, 3 errors (1% error rate).
11 commits. Tests: 398 passing.
```

Process grades are anti-correlated with shipping — [we tested this](https://boldfaceline.com/posts/the-inversion). Sessions that score high on "process quality" ship less. So we measure outcomes: commits, test results, stuck events, rework.

## Repo audit

```bash
sesh audit
```

Scores your repo on 9 metrics that determine whether an AI agent will succeed or struggle:

```
Repo Audit: 89/100  Grade: B

  bootstrap           [10/10] ██████████
  task_entry_points   [ 6/10] ██████░░░░
  validation_harness  [10/10] ██████████
  linting             [ 8/10] ████████░░
  agent_instructions  [ 8/10] ████████░░
```

## Close the loop

```bash
# Generate CLAUDE.md rules from your behavioral profile
sesh analyze --fix

# Write session feedback directly into CLAUDE.md
sesh analyze --feedback

# Fail CI if repo AI-readiness drops below standard
sesh audit --threshold 80
```

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
sesh tui                     # Live terminal dashboard (monitors active session)
sesh live                    # Lightweight live monitor (for small panes)
sesh fix --patch             # Generate CLAUDE.md patch from analysis
sesh search "auth bug"       # Full-text search across sessions
sesh debug                   # Prompt debugger — trace decisions
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
