# sesh

Agent session intelligence. Behavioral analysis, grading, replay, and outcome testing for AI coding agents.

sesh parses agent session transcripts, detects anti-patterns, grades sessions, replays sessions step by step, compares outcomes between sessions, and generates remediation patches. Built so agents can analyze themselves — and so humans can see what their agents are doing.

## Three interfaces, one engine

| Interface | For | How |
|-----------|-----|-----|
| **CLI** | Developers in the terminal | `sesh analyze`, `sesh reflect`, `sesh replay`, `sesh test`, `sesh fix` |
| **MCP Server** | Agents at runtime | Add to your MCP config, agent calls `sesh_reflect` |
| **Web Dashboard** | Humans who want observability | `sesh-web` → browser dashboard on localhost |

All three use the same analysis engine and database. Install once, use from anywhere.

## Install

```bash
pip install agentsesh
```

Or from source:

```bash
git clone https://github.com/ateeples/agentsesh.git
cd agentsesh
pip install -e .
```

## Quick start

```bash
# Initialize in your project
sesh init

# Auto-discover and ingest all Claude Code sessions
sesh watch --once

# See your most recent session analysis
sesh reflect

# Cross-session trends
sesh report

# Search past sessions
sesh search "authentication bug"

# Keep ingesting new sessions in the background
sesh watch

# Launch the dashboard
sesh-web
```

## MCP Server (agent-native)

Add sesh to your agent's MCP configuration so it can self-analyze at runtime.

**Claude Code** (`~/.claude/settings.json`):

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

**Claude Desktop** (`claude_desktop_config.json`):

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

Once configured, the agent has access to these tools:

| Tool | What it does |
|------|-------------|
| `sesh_reflect` | Full session analysis — grade, patterns, tool usage |
| `sesh_report` | Cross-session trends — improving, stable, or declining |
| `sesh_handoff` | Structured handoff doc for session continuity |
| `sesh_search` | Full-text search across all sessions |
| `sesh_list` | List recent sessions with grades |
| `sesh_stats` | Lifetime aggregate statistics |
| `sesh_log` | Ingest a new transcript |
| `sesh_sync` | Auto-discover and ingest new sessions |
| `sesh_patterns` | Detailed pattern breakdown for a session |

An agent can call `sesh_sync` then `sesh_reflect` at session start to auto-ingest new transcripts and review its last session, or `sesh_report` to see if it's been improving or repeating the same mistakes.

## Web Dashboard

```bash
sesh-web                # http://127.0.0.1:7433
sesh-web --port 8080    # custom port
```

The dashboard shows:
- Session grades and score trends
- Grade distribution across all sessions
- Tool usage breakdown
- Recurring anti-patterns
- Full-text session search

Auto-refreshes every 30 seconds — leave it open while your agent works.

## One-command analysis

Point `sesh analyze` at any Claude Code session transcript. No database, no setup.

```bash
# Full diagnostic
sesh analyze ~/.claude/projects/my-project/session.jsonl

# JSON output (for CI, dashboards, scripts)
sesh analyze session.jsonl --json

# Just the CLAUDE.md patch — paste it and go
sesh analyze session.jsonl --fix

# Include thinking context and grade breakdown
sesh analyze session.jsonl -v
```

Output: duration, cost estimate, grade, failure points with timestamps, remediation recommendations, and effective time (how much of the session was productive before things went wrong).

This is the fastest way to answer "what happened in that session?" — no `sesh init`, no database, no prior setup. Parse → analyze → report in one shot.

## Session replay

Reconstruct exactly what happened, step by step.

```bash
# Replay most recent session
sesh replay

# Replay with inline pattern annotations
sesh replay --annotate

# Show only errors — where did things go wrong?
sesh replay --errors

# Show only tool calls (no user/assistant text)
sesh replay --tools

# Zoom into a specific range
sesh replay --range 15-30

# Filter to specific tools
sesh replay --tool Edit,Bash

# Compact mode (no output previews)
sesh replay --compact

# Full output for every step
sesh replay -v
```

Replay prefers the original JSONL source file for full fidelity (user messages, assistant text, thinking blocks, complete tool output). Falls back to the database if the source file is gone (tool calls only, 300-char output previews).

## Outcome testing

Compare what actually happened between sessions — not just process quality, but results.

```bash
# Compare two most recent sessions
sesh test

# Compare specific sessions
sesh test <baseline_id> <candidate_id>
```

Measures: error-retry loops, files reworked, rework edits, terminal error state, success rate, test/build/lint pass rates. Verdict: improved, regressed, mixed, or unchanged.

## Remediation

Turn session analysis into actionable fixes.

```bash
# Full remediation report
sesh fix

# Output a CLAUDE.md patch (ready to paste)
sesh fix --patch
```

Each anti-pattern maps to specific remediation actions and a CLAUDE.md snippet. `--patch` generates a combined patch you can paste directly into your agent's instruction file.

## What it detects

sesh runs 9 pattern detectors on every session:

| Pattern | What it catches |
|---------|----------------|
| `repeated_searches` | Same search query run multiple times |
| `write_without_read` | Editing a file that was never read |
| `error_rate` | Overall error percentage above threshold |
| `error_streak` | Consecutive errors (agent stuck in a loop) |
| `low_read_ratio` | Not enough reading relative to writing |
| `bash_overuse` | Using bash for cat/grep/find when dedicated tools exist |
| `write_then_read` | Writing before understanding (acted before reading) |
| `scattered_files` | Touching too many directories (unfocused session) |
| `missed_parallelism` | Sequential reads that could have been parallel |

## Grading

Sessions are scored 0–100 and graded A+ through F:

- Start at 100, apply deductions for anti-patterns
- Bonuses for strong read/write ratios and good parallelism
- A+ (95+), A (90+), B (75+), C (60+), D (45+), F (<45)
- All weights configurable via `.sesh/config.json`

## CLI reference

```
sesh analyze <file>          One-command diagnostic (no database required)
sesh init                    Initialize .sesh/ in current directory
sesh log <file>              Ingest a session transcript
sesh log --dir <dir>         Batch ingest all transcripts in a directory
sesh reflect [session_id]    Analyze a session (default: most recent)
sesh replay [session_id]     Step-by-step session replay
sesh test [a] [b]            Compare outcome metrics between sessions
sesh fix [session_id]        Generate remediation recommendations
sesh report [--last N]       Cross-session trend analysis
sesh handoff [session_id]    Generate handoff document
sesh search <query>          Full-text search
sesh list [--last N]         List sessions
sesh stats                   Aggregate statistics
sesh export <session_id>     Export session as JSON
sesh watch [dirs...]         Auto-ingest new sessions (polls continuously)
sesh watch --once [dirs...]  One-shot scan and ingest

Replay flags:
  --errors                   Show only error steps
  --tools                    Show only tool calls
  --range 5-15               Show specific step range
  --tool Edit,Bash           Filter to specific tool(s)
  --annotate                 Show inline pattern annotations
  --compact                  No output previews
  -v, --verbose              Show full output for each step
  --db-only                  Skip source file, use DB only

Analyze flags:
  --fix                      Output CLAUDE.md patch only (ready to paste)
  -v, --verbose              Include thinking context and grade breakdown

Fix flags:
  --patch                    Output CLAUDE.md patch only

Watch flags:
  --interval N               Poll interval in seconds (default: 30)
  --settle N                 Seconds since last modification (default: 60)

Global flags:
  --json                     Output as JSON
  --db <path>                Override database path
  --quiet                    Suppress non-essential output
```

## Configuration

`sesh init` creates `.sesh/config.json` with sensible defaults. Everything is tunable:

```json
{
  "patterns": {
    "thresholds": {
      "error_rate_concern": 0.15,
      "bash_overuse_min": 3,
      "error_streak_min": 3
    }
  },
  "grading": {
    "error_rate_max_deduction": 20,
    "blind_edit_deduction": 5,
    "read_ratio_bonus": 5
  }
}
```

## Supported formats

- **Claude Code** (.jsonl) — fully supported
- **OpenAI Codex CLI** (.jsonl) — fully supported (auto-detected, maps `exec_command`→Bash, `apply_patch`→Edit)
- Generic — planned

## Requirements

- Python 3.10+
- `mcp` package (for MCP server only)
- No other external dependencies

## License

MIT
