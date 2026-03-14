"""CLI interface for sesh — Agent Session Intelligence."""

import argparse
import sys
from pathlib import Path

from . import __version__
from .analyzers.trends import analyze_trends
from .commands.analysis import cmd_analyze, cmd_audit, cmd_fix, cmd_test

# Command handlers split into domain modules
from .commands.debug import cmd_debug
from .commands.ingest import cmd_log, cmd_watch
from .commands.replay import cmd_replay
from .config import Config, find_config, find_sesh_dir
from .db import Database
from .formatters.handoff import format_handoff
from .formatters.json_out import session_to_json, to_json, trend_to_json
from .formatters.report import (
    format_search_results,
    format_session_list,
    format_session_report,
    format_stats,
    format_trend_report,
)

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
    print("  Add .sesh/ to your project's .gitignore")


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


# --- CLI banner ---


def _session_status() -> tuple[str, str, str] | None:
    """Try to read last session info from nearest .sesh/. Returns (grade, score, trend) or None."""
    try:
        sesh_dir = find_sesh_dir()
        if not sesh_dir:
            return None
        db = Database(sesh_dir / "sesh.db")
        sessions = db.list_sessions(limit=5)
        if not sessions:
            db.close()
            return None
        latest = sessions[0]
        grade = latest.get("grade", "?")
        score = latest.get("score", 0)
        # Trend: compare last vs average of previous 4
        if len(sessions) >= 2:
            prev_scores = [s.get("score", 0) for s in sessions[1:] if s.get("score") is not None]
            if prev_scores:
                avg = sum(prev_scores) / len(prev_scores)
                diff = score - avg
                trend = "▲" if diff > 2 else "▼" if diff < -2 else "━"
            else:
                trend = " "
        else:
            trend = " "
        db.close()
        return grade, str(int(score)), trend
    except Exception:
        return None


def _sparkline(values: list[int]) -> str:
    """Render a sparkline from a list of values using block characters."""
    if not values:
        return ""
    blocks = "▁▂▃▄▅▆▇█"
    lo, hi = min(values), max(values)
    spread = hi - lo if hi != lo else 1
    return "".join(blocks[min(7, int((v - lo) / spread * 7))] for v in values)


def _grade_color(grade: str, color: bool) -> str:
    """Return ANSI color code for a letter grade."""
    if not color:
        return ""
    colors = {
        "A+": "\033[92m", "A": "\033[92m",  # bright green
        "B": "\033[36m",   # cyan
        "C": "\033[33m",   # yellow
        "D": "\033[91m",   # bright red
        "F": "\033[31m",   # red
    }
    return colors.get(grade, "\033[37m")


def _print_banner() -> None:
    """Print styled CLI banner when no subcommand is given."""
    c = sys.stdout.isatty()

    # ANSI
    B = "\033[1m" if c else ""      # bold
    D = "\033[2m" if c else ""      # dim
    IT = "\033[3m" if c else ""     # italic
    CY = "\033[36m" if c else ""    # cyan
    GN = "\033[32m" if c else ""    # green
    W = "\033[97m" if c else ""     # white
    GR = "\033[90m" if c else ""    # gray
    R = "\033[0m" if c else ""      # reset

    # Try to show live status
    status = _session_status()
    status_suffix = ""
    spark_line = ""
    if status:
        grade, score, trend = status
        gc = _grade_color(grade, c)
        trend_c = ("\033[92m" if trend == "▲" else "\033[91m" if trend == "▼" else D) if c else ""
        status_suffix = f"  {gc}{B}{grade}{R} {D}{score}{R} {trend_c}{trend}{R}"

        # Sparkline from recent sessions
        try:
            sesh_dir = find_sesh_dir()
            if sesh_dir:
                db = Database(sesh_dir / "sesh.db")
                recent = db.list_sessions(limit=20)
                scores = [s.get("score", 0) for s in reversed(recent) if s.get("score") is not None]
                if len(scores) >= 3:
                    spark_line = f"  {D}┗╸{R} {D}{_sparkline(scores)}{R}"
                db.close()
        except Exception:
            pass

    # Logo — box-drawing letterforms + inline status
    logo = [
        f"  {B}{CY}┏━┓┏━╸┏━┓╻ ╻{R}",
        f"  {B}{CY}┗━┓┣╸ ┗━┓┣━┫{R}   {D}v{__version__}{R}{status_suffix}",
        f"  {B}{CY}┗━┛┗━╸┗━┛╹ ╹{R}{spark_line}",
    ]

    # Commands
    def row(cmd_name: str, desc: str) -> str:
        return f"  {GN}{cmd_name:<20}{R} {D}{desc}{R}"

    lines = [
        "",
        *logo,
        "",
        f"  {B}{W}$ sesh analyze{R}       {IT}{D}diagnose your last session{R}",
        f"  {B}{W}$ sesh audit{R}         {IT}{D}grade your repo's AI-readiness{R}",
        "",
        f"  {GR}{'─' * 44}{R}",
        "",
        row("reflect", "analyze ingested session"),
        row("report", "cross-session trends"),
        row("replay", "step-by-step playback"),
        row("fix --patch", "generate CLAUDE.md patch"),
        row("search <query>", "full-text search"),
        row("debug <query>", "search thinking blocks"),
        "",
        f"  {D}sesh <command> --help  {GR}│{R}  {D}sesh init  {GR}│{R}  {D}sesh watch{R}",
        "",
    ]
    print("\n".join(lines))


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
    analyze_p.add_argument("--feedback", nargs="?", const=True, default=None,
                           help="Write session feedback to CLAUDE.md (or specify target file)")

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
        _print_banner()
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
