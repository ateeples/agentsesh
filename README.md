# sesh

Agent session intelligence. Behavioral analysis, grading, and self-improvement for AI coding agents.

sesh parses agent session transcripts, detects anti-patterns, grades sessions, and tracks trends over time. Built so agents can analyze themselves — and so humans can see what their agents are doing.

## Three interfaces, one engine

| Interface | For | How |
|-----------|-----|-----|
| **CLI** | Developers in the terminal | `sesh reflect`, `sesh report`, `sesh search` |
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

# Ingest session transcripts
sesh log ~/.claude/projects/your-project/sessions/*.jsonl

# See your most recent session analysis
sesh reflect

# Cross-session trends
sesh report

# Search past sessions
sesh search "authentication bug"

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
| `sesh_patterns` | Detailed pattern breakdown for a session |

An agent can call `sesh_reflect` at session start to review its last session, or `sesh_report` to see if it's been improving or repeating the same mistakes.

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
sesh init                    Initialize .sesh/ in current directory
sesh log <file>              Ingest a session transcript
sesh log --dir <dir>         Batch ingest all transcripts in a directory
sesh reflect [session_id]    Analyze a session (default: most recent)
sesh report [--last N]       Cross-session trend analysis
sesh handoff [session_id]    Generate handoff document
sesh search <query>          Full-text search
sesh list [--last N]         List sessions
sesh stats                   Aggregate statistics
sesh export <session_id>     Export session as JSON

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
- OpenAI, generic — planned

## Requirements

- Python 3.10+
- `mcp` package (for MCP server only)
- No other external dependencies

## License

MIT
