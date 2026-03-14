"""MCP server for sesh — Agent Session Intelligence.

Exposes sesh analysis tools via the Model Context Protocol,
letting agents self-analyze their own sessions at runtime.

Usage:
    # stdio transport (default, for Claude Code MCP config)
    python -m sesh.mcp_server

    # Or via the entry point
    sesh-mcp
"""

import json
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .config import Config, find_config, find_sesh_dir
from .db import Database
from .parsers import parse_transcript
from .analyzers.trends import analyze_trends
from .formatters.handoff import format_handoff
from .formatters.report import format_session_report, format_trend_report
from .watch import discover_session_dirs, ingest_new_files

# Initialize FastMCP server with instructions that guide agents
# to use the right tool for their current need.
mcp = FastMCP(
    "sesh",
    instructions=(
        "Agent Session Intelligence — behavioral analysis, grading, and self-improvement. "
        "Use these tools to understand your own session history. "
        "Start with sesh_reflect to review your most recent session, "
        "sesh_report for cross-session trends, or sesh_search to find "
        "specific past sessions by content."
    ),
)


# --- Database resolution ---
# MCP tools resolve the database on each call (no persistent state).
# Priority: SESH_DB env var > .sesh/config.json > .sesh/ directory walk.


def _get_db() -> Database:
    """Resolve the sesh database, checking env var then walking up from cwd."""
    # Env var takes priority — set in MCP config for explicit paths
    db_path_env = os.environ.get("SESH_DB")
    if db_path_env:
        return Database(db_path_env)

    # Walk up from cwd looking for .sesh/config.json
    config_path = find_config()
    if config_path:
        config = Config(config_path)
        sesh_parent = config_path.parent.parent
        return Database(sesh_parent / config.db_path)

    # Fall back to any .sesh/ directory
    sesh_dir = find_sesh_dir()
    if sesh_dir:
        return Database(sesh_dir / "sesh.db")

    raise FileNotFoundError(
        "No .sesh/ directory found. Run `sesh init` in your project root first."
    )


def _get_config() -> Config:
    config_path = find_config()
    return Config(config_path)


# --- MCP Tools ---
# Each tool maps to a CLI command but returns string output
# suitable for an agent to read. All tools handle their own
# database lifecycle (open → work → close in finally block).


@mcp.tool()
def sesh_reflect(session_id: str = "") -> str:
    """Analyze a single session — grade, patterns, tool usage, behavioral breakdown.

    Returns a detailed report for the specified session, or the most recent
    session if no ID is provided. Use this at session start to review what
    happened last time and avoid repeating mistakes.

    Args:
        session_id: Session ID to analyze. Leave empty for most recent.
    """
    db = _get_db()
    try:
        if session_id:
            session = db.get_session(session_id)
        else:
            sessions = db.list_sessions(limit=1)
            if not sessions:
                return "No sessions found. Ingest transcripts with sesh_log first."
            session = db.get_session(sessions[0]["id"])

        if not session:
            return f"Session not found: {session_id}"

        tool_calls = db.get_tool_calls(session["id"])
        patterns = db.get_patterns(session["id"])
        return format_session_report(session, tool_calls, patterns)
    finally:
        db.close()


@mcp.tool()
def sesh_report(last: int = 20) -> str:
    """Cross-session trend analysis — trajectory, recurring patterns, grade distribution.

    Analyzes multiple sessions to show whether you're improving, stable,
    or declining, and which patterns keep recurring.

    Args:
        last: Number of recent sessions to analyze (default 20).
    """
    db = _get_db()
    try:
        summaries = db.get_session_summaries(limit=last)
        if not summaries:
            return "No sessions found. Ingest transcripts with sesh_log first."
        report = analyze_trends(summaries)
        return format_trend_report(report)
    finally:
        db.close()


@mcp.tool()
def sesh_handoff(session_id: str = "") -> str:
    """Generate a handoff document for session continuity.

    Creates a structured markdown summary of what was done, files touched,
    unresolved issues, and process notes — designed to prime a new session
    with context from the previous one.

    Args:
        session_id: Session ID. Leave empty for most recent.
    """
    db = _get_db()
    try:
        if session_id:
            session = db.get_session(session_id)
        else:
            sessions = db.list_sessions(limit=1)
            if not sessions:
                return "No sessions found."
            session = db.get_session(sessions[0]["id"])

        if not session:
            return f"Session not found: {session_id}"

        tool_calls = db.get_tool_calls(session["id"])
        patterns = db.get_patterns(session["id"])
        return format_handoff(session, tool_calls, patterns)
    finally:
        db.close()


@mcp.tool()
def sesh_search(query: str, limit: int = 10) -> str:
    """Full-text search across all session transcripts.

    Search for specific code, errors, or topics across your session history.
    Uses FTS5 with Porter stemming for flexible matching.

    Args:
        query: Search query (supports FTS5 syntax: AND, OR, NOT, "phrases").
        limit: Maximum results to return (default 10).
    """
    db = _get_db()
    try:
        results = db.search(query, limit=limit)
        if not results:
            return f"No sessions found matching: {query}"

        lines = [f"Found {len(results)} session(s) matching '{query}':\n"]
        for r in results:
            lines.append(
                f"  [{r['grade']}] {r['session_id'][:12]}... — {r['snippet']}"
            )
        return "\n".join(lines)
    finally:
        db.close()


@mcp.tool()
def sesh_list(last: int = 20) -> str:
    """List recent sessions with grades and key metrics.

    Quick overview of your session history — useful for finding session IDs
    to pass to sesh_reflect or sesh_handoff.

    Args:
        last: Number of sessions to show (default 20).
    """
    db = _get_db()
    try:
        sessions = db.list_sessions(limit=last)
        if not sessions:
            return "No sessions found."

        lines = [f"{'ID':<14} {'Grade':>5} {'Score':>5} {'Tools':>5} {'Errors':>6} {'Duration':>8}"]
        lines.append("-" * 55)
        for s in sessions:
            dur = f"{s['duration_minutes']:.0f}m" if s["duration_minutes"] else "?"
            lines.append(
                f"{s['id'][:12]}.. {s['grade'] or '?':>5} {s['score'] or 0:>5} "
                f"{s['tool_call_count'] or 0:>5} {s['error_count'] or 0:>6} {dur:>8}"
            )
        return "\n".join(lines)
    finally:
        db.close()


@mcp.tool()
def sesh_stats() -> str:
    """Aggregate statistics across all sessions.

    Shows lifetime averages, total tool calls, per-tool breakdown,
    and overall error rates. Good for understanding long-term patterns.
    """
    db = _get_db()
    try:
        stats = db.get_stats()
        tool_stats = db.get_tool_stats()

        if not stats or stats.get("total_sessions", 0) == 0:
            return "No sessions found."

        lines = [
            "=== Lifetime Stats ===",
            f"Sessions:       {stats['total_sessions']}",
            f"Avg Score:      {stats['avg_score']:.1f}" if stats["avg_score"] else "Avg Score:      N/A",
            f"Avg Error Rate: {stats['avg_error_rate']:.1%}" if stats["avg_error_rate"] else "Avg Error Rate: N/A",
            f"Total Calls:    {stats['total_tool_calls']}",
            f"Total Errors:   {stats['total_errors']}",
            "",
            "=== Per-Tool Usage ===",
        ]
        for ts in tool_stats:
            lines.append(
                f"  {ts['name']:<20} {ts['uses']:>5} uses  "
                f"{ts['errors']:>3} errors ({ts['error_rate']:.1%})"
            )
        return "\n".join(lines)
    finally:
        db.close()


# --- Ingestion tools ---
# These tools modify the database (insert sessions, sync from directories).


@mcp.tool()
def sesh_log(file_path: str, format_hint: str = "auto") -> str:
    """Ingest a session transcript file into the sesh database.

    Parses a transcript (e.g., Claude Code .jsonl), analyzes it for
    behavioral patterns, grades it, and stores the results.

    Args:
        file_path: Path to the transcript file.
        format_hint: Transcript format — 'claude_code', 'auto' (default: auto-detect).
    """
    config = _get_config()
    db = _get_db()
    try:
        path = Path(file_path)
        if not path.is_file():
            return f"File not found: {file_path}"

        fmt = None if format_hint == "auto" else format_hint
        session = parse_transcript(path, format_hint=fmt)

        if not session.tool_calls:
            return f"No tool calls found in {path.name} — skipping."

        result = db.ingest_session(
            session,
            thresholds=config.pattern_thresholds,
            grading_weights=config.grading_weights,
        )
        return (
            f"Ingested: [{result['grade']}] {path.name}\n"
            f"  {result['tool_calls']} tool calls, {result['errors']} errors, "
            f"{result['patterns']} patterns detected"
        )
    except ValueError as e:
        return f"Error: {e}"
    finally:
        db.close()


@mcp.tool()
def sesh_sync(directories: list[str] | None = None, settle_seconds: float = 60.0) -> str:
    """Auto-discover and ingest new session transcripts.

    Scans known session directories (like ~/.claude/projects/) for new
    transcripts and ingests any that haven't been logged yet. Call this
    at session start to ensure your analysis data is up-to-date.

    Args:
        directories: Specific directories to scan. Leave empty for auto-discovery.
        settle_seconds: Only ingest files untouched for this many seconds (default: 60).
    """
    config = _get_config()
    db = _get_db()
    try:
        if directories:
            dirs = [Path(d) for d in directories]
        else:
            dirs = discover_session_dirs()

        if not dirs:
            return "No session directories found. Specify directories or ensure ~/.claude/projects/ exists."

        count = ingest_new_files(db, config, dirs, settle_seconds=settle_seconds, quiet=True)
        if count > 0:
            return f"Synced {count} new session(s) from {len(dirs)} directory(ies)."
        return "Already up to date — no new sessions found."
    finally:
        db.close()


@mcp.tool()
def sesh_patterns(session_id: str = "") -> str:
    """List detected behavioral patterns for a session.

    Shows anti-patterns like repeated searches, blind edits, error streaks,
    bash overuse, and missed parallelism opportunities. Each pattern includes
    severity and specific details.

    Args:
        session_id: Session ID. Leave empty for most recent.
    """
    db = _get_db()
    try:
        if session_id:
            session = db.get_session(session_id)
        else:
            sessions = db.list_sessions(limit=1)
            if not sessions:
                return "No sessions found."
            session = db.get_session(sessions[0]["id"])

        if not session:
            return f"Session not found: {session_id}"

        patterns = db.get_patterns(session["id"])
        if not patterns:
            return f"No patterns detected in session {session['id'][:12]}... (clean session)"

        lines = [f"Patterns for session {session['id'][:12]}... [{session['grade']}]:\n"]
        for p in patterns:
            icon = {"info": "·", "warning": "⚠", "concern": "✗"}.get(p["severity"], "?")
            lines.append(f"  {icon} [{p['severity']}] {p['type']}: {p['detail']}")
        return "\n".join(lines)
    finally:
        db.close()


# --- Server entry point ---


def main():
    """Entry point for the MCP server (stdio transport)."""
    # MCP uses stdio by default — the agent process talks to us over stdin/stdout
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
