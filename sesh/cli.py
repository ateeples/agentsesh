"""CLI interface for sesh — Agent Session Intelligence."""

import argparse
import sys
from pathlib import Path

from . import __version__
from .config import Config, find_config, find_sesh_dir, DEFAULT_CONFIG
from .db import Database
from .formatters.report import (
    format_session_report,
    format_trend_report,
    format_session_list,
    format_stats,
    format_search_results,
)
from .formatters.handoff import format_handoff
from .formatters.json_out import session_to_json, trend_to_json, to_json
from .analyzers.trends import analyze_trends

# Command handlers split into domain modules
from .commands.debug import cmd_debug
from .commands.replay import cmd_replay
from .commands.ingest import cmd_log, cmd_watch
from .commands.analysis import cmd_fix, cmd_test, cmd_analyze, cmd_audit


# --- Database and config resolution ---


def _get_db(args) -> Database:
    """Get database connection, finding .sesh/ dir automatically."""
    # Priority: explicit --db flag > .sesh/config.json > .sesh/ discovery
    if hasattr(args, "db") and args.db:
        return Database(args.db)

    config_path = find_config()
    if config_path:
        config = Config(config_path)
        # Resolve db_path relative to .sesh/ parent (config lives in .sesh/)
        sesh_parent = config_path.parent.parent
        return Database(sesh_parent / config.db_path)

    sesh_dir = find_sesh_dir()
    if sesh_dir:
        return Database(sesh_dir / "sesh.db")

    print("Error: No .sesh/ directory found. Run `sesh init` first.", file=sys.stderr)
    sys.exit(1)


def _get_config() -> Config:
    """Load config, using defaults if no config file found."""
    config_path = find_config()
    return Config(config_path)


# --- Simple subcommands (kept here — small, self-contained) ---


def cmd_init(args) -> None:
    """Initialize .sesh/ in current directory."""
    sesh_dir = Path.cwd() / ".sesh"
    if sesh_dir.exists():
        print(f".sesh/ already exists at {sesh_dir}")
        return

    sesh_dir.mkdir()

    # Create config with defaults
    config = Config()
    config.save(sesh_dir / "config.json")

    # Create database (constructor triggers schema creation)
    db = Database(sesh_dir / "sesh.db")
    db.close()

    # Gitignore: allow config but exclude DB (may contain transcript data)
    gitignore = sesh_dir / ".gitignore"
    gitignore.write_text("# sesh database may contain sensitive data\n*\n!.gitignore\n!config.json\n")

    print(f"Initialized .sesh/ at {sesh_dir}")
    print(f"  Database: {sesh_dir / 'sesh.db'}")
    print(f"  Config:   {sesh_dir / 'config.json'}")
    print(f"  Add .sesh/ to your project's .gitignore")


def cmd_reflect(args) -> None:
    """Analyze a session."""
    db = _get_db(args)

    if args.session_id:
        session = db.get_session(args.session_id)
    else:
        sessions = db.list_sessions(limit=1)
        if not sessions:
            print("No sessions found. Run `sesh log` first.", file=sys.stderr)
            sys.exit(3)
        session = db.get_session(sessions[0]["id"])

    if not session:
        print(f"Session not found: {args.session_id or '(most recent)'}", file=sys.stderr)
        sys.exit(4)

    tool_calls = db.get_tool_calls(session["id"])
    patterns = db.get_patterns(session["id"])

    if args.json:
        print(session_to_json(session, tool_calls, patterns))
    else:
        print(format_session_report(session, tool_calls, patterns))

    db.close()


def cmd_report(args) -> None:
    """Cross-session trend analysis."""
    db = _get_db(args)
    config = _get_config()

    limit = args.last or config.default_report_count
    summaries = db.get_session_summaries(limit=limit)

    if not summaries:
        print("No sessions found. Run `sesh log` first.", file=sys.stderr)
        sys.exit(3)

    report = analyze_trends(summaries)

    if args.json:
        print(trend_to_json(report))
    else:
        print(format_trend_report(report))

    db.close()


def cmd_handoff(args) -> None:
    """Generate session handoff summary."""
    db = _get_db(args)

    if args.session_id:
        session = db.get_session(args.session_id)
    else:
        sessions = db.list_sessions(limit=1)
        if not sessions:
            print("No sessions found.", file=sys.stderr)
            sys.exit(3)
        session = db.get_session(sessions[0]["id"])

    if not session:
        print(f"Session not found: {args.session_id or '(most recent)'}", file=sys.stderr)
        sys.exit(4)

    tool_calls = db.get_tool_calls(session["id"])
    patterns = db.get_patterns(session["id"])

    print(format_handoff(session, tool_calls, patterns))
    db.close()


def cmd_search(args) -> None:
    """Full-text search across sessions."""
    db = _get_db(args)

    results = db.search(args.query, limit=args.limit or 10)

    if args.json:
        print(to_json(results))
    else:
        print(format_search_results(results))

    db.close()


def cmd_list(args) -> None:
    """List logged sessions."""
    db = _get_db(args)

    sessions = db.list_sessions(limit=args.last or 20)

    if args.json:
        print(to_json(sessions))
    else:
        print(format_session_list(sessions))

    db.close()


def cmd_stats(args) -> None:
    """Show aggregate statistics."""
    db = _get_db(args)

    stats = db.get_stats()
    tool_stats = db.get_tool_stats()

    if args.json:
        print(to_json({"stats": stats, "tool_stats": tool_stats}))
    else:
        print(format_stats(stats, tool_stats))

    db.close()


def cmd_export(args) -> None:
    """Export session data."""
    db = _get_db(args)

    session = db.get_session(args.session_id)
    if not session:
        print(f"Session not found: {args.session_id or '(most recent)'}", file=sys.stderr)
        sys.exit(4)

    tool_calls = db.get_tool_calls(session["id"])
    patterns = db.get_patterns(session["id"])

    print(session_to_json(session, tool_calls, patterns))
    db.close()


# --- CLI entry point and argument parsing ---


def main() -> None:
    """Main CLI entry point — parse args and dispatch to subcommand handler."""
    parser = argparse.ArgumentParser(
        prog="sesh",
        description="Agent Session Intelligence — behavioral analysis, grading, and handoff for AI agent sessions",
    )
    parser.add_argument("--version", action="version", version=f"sesh {__version__}")
    parser.add_argument("--db", help="Override database path")
    parser.add_argument("--config", help="Override config path")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--quiet", "-q", action="store_true", help="Suppress non-essential output")

    sub = parser.add_subparsers(dest="command", help="Available commands")

    # --- Setup commands ---
    sub.add_parser("init", help="Initialize .sesh/ in current directory")

    # --- Ingestion commands ---
    log_p = sub.add_parser("log", help="Ingest session transcript(s)")
    log_p.add_argument("file", nargs="?", help="Transcript file path")
    log_p.add_argument("--dir", help="Batch ingest all files in directory")
    log_p.add_argument("--format", choices=["claude_code", "openai", "generic", "auto"], default="auto",
                       help="Transcript format (default: auto-detect)")

    # --- Analysis commands (require DB) ---
    reflect_p = sub.add_parser("reflect", help="Analyze a session")
    reflect_p.add_argument("session_id", nargs="?", help="Session ID (default: most recent)")
    reflect_p.add_argument("--json", action="store_true", help="Output as JSON")

    report_p = sub.add_parser("report", help="Cross-session trend analysis")
    report_p.add_argument("--last", type=int, help="Number of sessions to analyze")
    report_p.add_argument("--json", action="store_true", help="Output as JSON")

    handoff_p = sub.add_parser("handoff", help="Generate handoff summary")
    handoff_p.add_argument("session_id", nargs="?", help="Session ID (default: most recent)")

    search_p = sub.add_parser("search", help="Full-text search across sessions")
    search_p.add_argument("query", help="Search query")
    search_p.add_argument("--limit", type=int, default=10, help="Max results")
    search_p.add_argument("--json", action="store_true", help="Output as JSON")

    list_p = sub.add_parser("list", help="List logged sessions")
    list_p.add_argument("--last", type=int, help="Number of sessions to show")
    list_p.add_argument("--json", action="store_true", help="Output as JSON")

    stats_p = sub.add_parser("stats", help="Aggregate statistics dashboard")
    stats_p.add_argument("--json", action="store_true", help="Output as JSON")

    export_p = sub.add_parser("export", help="Export session data as JSON")
    export_p.add_argument("session_id", help="Session ID to export")

    fix_p = sub.add_parser("fix", help="Generate remediation recommendations")
    fix_p.add_argument("session_id", nargs="?", help="Session ID (default: most recent)")
    fix_p.add_argument("--json", action="store_true", help="Output as JSON")
    fix_p.add_argument("--patch", action="store_true",
                       help="Output CLAUDE.md patch only (ready to paste)")

    test_p = sub.add_parser("test", help="Compare outcome metrics between sessions")
    test_p.add_argument("session_a", nargs="?", help="Baseline session ID (default: second most recent)")
    test_p.add_argument("session_b", nargs="?", help="Candidate session ID (default: most recent)")
    test_p.add_argument("--json", action="store_true", help="Output as JSON")

    replay_p = sub.add_parser("replay", help="Step-by-step session replay")
    replay_p.add_argument("session_id", nargs="?", help="Session ID (default: most recent)")
    replay_p.add_argument("--errors", action="store_true", help="Show only error steps")
    replay_p.add_argument("--tools", action="store_true", help="Show only tool calls (no user/assistant text)")
    replay_p.add_argument("--range", help="Show step range (e.g. 5-15)")
    replay_p.add_argument("--tool", help="Filter to specific tool(s) (e.g. Edit,Bash)")
    replay_p.add_argument("--annotate", action="store_true", help="Show inline pattern annotations")
    replay_p.add_argument("--compact", action="store_true", help="Compact output (no output previews)")
    replay_p.add_argument("--verbose", "-v", action="store_true", help="Show full output for each step")
    replay_p.add_argument("--db-only", action="store_true", help="Use DB data only (skip source file)")
    replay_p.add_argument("--json", action="store_true", help="Output as JSON")

    debug_p = sub.add_parser("debug", help="Search thinking blocks — why did the agent do that?")
    debug_p.add_argument("query", nargs="?", help="Search query (searches thinking blocks, or actions with --action)")
    debug_p.add_argument("session_id", nargs="?", help="Session ID (default: most recent)")
    debug_p.add_argument("--action", "-a", action="store_true", help="Reverse lookup: search actions instead of thinking (e.g. 'why did you edit auth.py?')")
    debug_p.add_argument("--dotnotes", "-d", action="store_true", help="Index/search dot-notation paths in thinking (e.g. 'auth.*', 'config.database.*')")
    debug_p.add_argument("--correlate", action="store_true", help="Correlate antipatterns to the decision points that caused them")
    debug_p.add_argument("--json", action="store_true", help="Output as JSON")
    debug_p.add_argument("--verbose", "-v", action="store_true", help="Show full thinking blocks")

    # --- Standalone commands (no DB required) ---
    analyze_p = sub.add_parser("analyze", help="Session diagnostic (auto-detects most recent, or pass a file)")
    analyze_p.add_argument("file", nargs="?", help="Path to session transcript (default: auto-detect most recent)")
    analyze_p.add_argument("--json", action="store_true", help="Output as JSON")
    analyze_p.add_argument("--verbose", "-v", action="store_true",
                           help="Include thinking context and grade breakdown")
    analyze_p.add_argument("--fix", action="store_true",
                           help="Output CLAUDE.md patch only (ready to paste)")

    audit_p = sub.add_parser("audit", help="Grade a repo's agent-readiness (0-100, A+ to F)")
    audit_p.add_argument("path", nargs="?", help="Path to repo (default: current directory)")
    audit_p.add_argument("--metric", help="Run only one metric (e.g. bootstrap, file_discipline)")
    audit_p.add_argument("--json", action="store_true", help="Output as JSON")
    audit_p.add_argument("--threshold", type=int,
                         help="Minimum score to pass (exit 1 if below). For CI gates.")

    # --- Background/daemon commands ---
    watch_p = sub.add_parser("watch", help="Auto-ingest new sessions from directories")
    watch_p.add_argument("dirs", nargs="*", help="Directories to watch (default: auto-discover)")
    watch_p.add_argument("--interval", type=float, default=30.0,
                         help="Poll interval in seconds (default: 30)")
    watch_p.add_argument("--settle", type=float, default=60.0,
                         help="Seconds since last modification before ingesting (default: 60)")
    watch_p.add_argument("--once", action="store_true",
                         help="Scan once and exit (don't poll)")

    # --- Dispatch ---
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    # Command dispatch table — maps subcommand name to handler function
    commands = {
        "init": cmd_init,
        "log": cmd_log,
        "reflect": cmd_reflect,
        "report": cmd_report,
        "handoff": cmd_handoff,
        "search": cmd_search,
        "list": cmd_list,
        "stats": cmd_stats,
        "export": cmd_export,
        "fix": cmd_fix,
        "test": cmd_test,
        "replay": cmd_replay,
        "debug": cmd_debug,
        "analyze": cmd_analyze,
        "audit": cmd_audit,
        "watch": cmd_watch,
    }

    cmd_func = commands.get(args.command)
    if cmd_func:
        cmd_func(args)
    else:
        parser.print_help()
        sys.exit(2)
