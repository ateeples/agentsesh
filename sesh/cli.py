"""CLI interface for sesh — Agent Session Intelligence."""

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .config import Config, find_config, find_sesh_dir, DEFAULT_CONFIG
from .db import Database
from .parsers import parse_transcript
from .analyzers.trends import analyze_trends
from .analyzers.remediation import (
    get_all_remediations,
    format_remediations,
    generate_claude_md_patch,
)
from .formatters.report import (
    format_session_report,
    format_trend_report,
    format_session_list,
    format_stats,
    format_search_results,
)
from .formatters.handoff import format_handoff
from .formatters.json_out import session_to_json, trend_to_json, to_json
from .watch import discover_session_dirs, ingest_new_files, watch_loop


def _get_db(args) -> Database:
    """Get database connection, finding .sesh/ dir automatically."""
    if hasattr(args, "db") and args.db:
        return Database(args.db)

    config_path = find_config()
    if config_path:
        config = Config(config_path)
        # Resolve db_path relative to .sesh/ parent
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


def cmd_init(args) -> None:
    """Initialize .sesh/ in current directory."""
    sesh_dir = Path.cwd() / ".sesh"
    if sesh_dir.exists():
        print(f".sesh/ already exists at {sesh_dir}")
        return

    sesh_dir.mkdir()

    # Create config
    config = Config()
    config.save(sesh_dir / "config.json")

    # Create database (triggers schema creation)
    db = Database(sesh_dir / "sesh.db")
    db.close()

    # Create .gitignore for .sesh/
    gitignore = sesh_dir / ".gitignore"
    gitignore.write_text("# sesh database may contain sensitive data\n*\n!.gitignore\n!config.json\n")

    print(f"Initialized .sesh/ at {sesh_dir}")
    print(f"  Database: {sesh_dir / 'sesh.db'}")
    print(f"  Config:   {sesh_dir / 'config.json'}")
    print(f"  Add .sesh/ to your project's .gitignore")


def cmd_log(args) -> None:
    """Ingest a session transcript."""
    config = _get_config()
    db = _get_db(args)

    paths = []
    if args.dir:
        dir_path = Path(args.dir)
        if not dir_path.is_dir():
            print(f"Error: {args.dir} is not a directory", file=sys.stderr)
            sys.exit(1)
        paths = sorted(dir_path.iterdir())
    elif args.file:
        paths = [Path(args.file)]
    else:
        print("Error: Provide a file or --dir", file=sys.stderr)
        sys.exit(2)

    ingested = 0
    skipped = 0
    errors = 0

    for path in paths:
        if not path.is_file():
            continue
        try:
            fmt = args.format if args.format != "auto" else None
            session = parse_transcript(path, format_hint=fmt)
        except ValueError as e:
            if not args.quiet:
                print(f"  Skip {path.name}: {e}", file=sys.stderr)
            skipped += 1
            continue

        if not session.tool_calls:
            if not args.quiet:
                print(f"  Skip {path.name}: no tool calls", file=sys.stderr)
            skipped += 1
            continue

        try:
            result = db.ingest_session(
                session,
                thresholds=config.pattern_thresholds,
                grading_weights=config.grading_weights,
            )
            ingested += 1
            if not args.quiet:
                print(
                    f"  [{result['grade']}] {path.name} — "
                    f"{result['tool_calls']} calls, {result['errors']} errors, "
                    f"{result['patterns']} patterns"
                )
        except ValueError:
            # Already exists
            skipped += 1
        except Exception as e:
            print(f"  Error ingesting {path.name}: {e}", file=sys.stderr)
            errors += 1

    db.close()

    if not args.quiet:
        print(f"\nIngested {ingested}, skipped {skipped}, errors {errors}")


def cmd_reflect(args) -> None:
    """Analyze a session."""
    db = _get_db(args)

    if args.session_id:
        session = db.get_session(args.session_id)
    else:
        # Most recent
        sessions = db.list_sessions(limit=1)
        if not sessions:
            print("No sessions found. Run `sesh log` first.", file=sys.stderr)
            sys.exit(3)
        session = db.get_session(sessions[0]["id"])

    if not session:
        print(f"Session not found: {args.session_id}", file=sys.stderr)
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
        print(f"Session not found: {args.session_id}", file=sys.stderr)
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
        print(f"Session not found: {args.session_id}", file=sys.stderr)
        sys.exit(4)

    tool_calls = db.get_tool_calls(session["id"])
    patterns = db.get_patterns(session["id"])

    print(session_to_json(session, tool_calls, patterns))
    db.close()


def cmd_fix(args) -> None:
    """Generate remediation recommendations for a session."""
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
        print(f"Session not found: {args.session_id}", file=sys.stderr)
        sys.exit(4)

    patterns = db.get_patterns(session["id"])
    remediations = get_all_remediations(patterns)

    if not remediations:
        print(f"Session {session['id'][:16]}... ({session.get('grade', '?')}) — no anti-patterns detected. Clean session.")
        db.close()
        return

    if args.json:
        import json as json_mod
        data = []
        for r in remediations:
            data.append({
                "pattern_type": r.pattern_type,
                "title": r.title,
                "severity": r.severity,
                "description": r.description,
                "actions": r.actions,
                "claude_md_snippet": r.claude_md_snippet,
                "impact": r.impact,
            })
        print(json_mod.dumps(data, indent=2))
    elif args.patch:
        # Output just the CLAUDE.md patch
        patch = generate_claude_md_patch(remediations)
        if patch:
            print(patch)
        else:
            print("No CLAUDE.md changes recommended.")
    else:
        # Full remediation report
        print(f"# Remediations for {session['id'][:16]}...")
        print(f"  Grade: {session.get('grade', '?')} (score: {session.get('score', 0)})")
        print(f"  Patterns: {len(patterns)} detected")
        print()
        print(format_remediations(remediations, include_snippets=True))

    db.close()


def cmd_watch(args) -> None:
    """Watch directories for new sessions and auto-ingest."""
    config = _get_config()
    db = _get_db(args)

    directories = []
    if args.dirs:
        directories = [Path(d) for d in args.dirs]
    else:
        # Auto-discover
        directories = discover_session_dirs()
        if not directories:
            print(
                "No session directories found. Specify directories:\n"
                "  sesh watch ~/.claude/projects/",
                file=sys.stderr,
            )
            sys.exit(1)

    # Validate directories exist
    for d in directories:
        if not d.is_dir():
            print(f"Warning: {d} is not a directory, skipping", file=sys.stderr)
    directories = [d for d in directories if d.is_dir()]

    if not directories:
        print("Error: No valid directories to watch", file=sys.stderr)
        sys.exit(1)

    if args.once:
        # One-shot scan
        count = ingest_new_files(
            db, config, directories,
            settle_seconds=args.settle,
            quiet=args.quiet,
        )
        if not args.quiet:
            print(f"\nIngested {count} new session(s)")
        db.close()
    else:
        try:
            watch_loop(
                db, config, directories,
                interval=args.interval,
                settle_seconds=args.settle,
                quiet=args.quiet,
            )
        finally:
            db.close()


def main() -> None:
    """Main CLI entry point."""
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

    # init
    sub.add_parser("init", help="Initialize .sesh/ in current directory")

    # log
    log_p = sub.add_parser("log", help="Ingest session transcript(s)")
    log_p.add_argument("file", nargs="?", help="Transcript file path")
    log_p.add_argument("--dir", help="Batch ingest all files in directory")
    log_p.add_argument("--format", choices=["claude_code", "openai", "generic", "auto"], default="auto",
                       help="Transcript format (default: auto-detect)")

    # reflect
    reflect_p = sub.add_parser("reflect", help="Analyze a session")
    reflect_p.add_argument("session_id", nargs="?", help="Session ID (default: most recent)")
    reflect_p.add_argument("--json", action="store_true", help="Output as JSON")

    # report
    report_p = sub.add_parser("report", help="Cross-session trend analysis")
    report_p.add_argument("--last", type=int, help="Number of sessions to analyze")
    report_p.add_argument("--json", action="store_true", help="Output as JSON")

    # handoff
    handoff_p = sub.add_parser("handoff", help="Generate handoff summary")
    handoff_p.add_argument("session_id", nargs="?", help="Session ID (default: most recent)")

    # search
    search_p = sub.add_parser("search", help="Full-text search across sessions")
    search_p.add_argument("query", help="Search query")
    search_p.add_argument("--limit", type=int, default=10, help="Max results")
    search_p.add_argument("--json", action="store_true", help="Output as JSON")

    # list
    list_p = sub.add_parser("list", help="List logged sessions")
    list_p.add_argument("--last", type=int, help="Number of sessions to show")
    list_p.add_argument("--json", action="store_true", help="Output as JSON")

    # stats
    stats_p = sub.add_parser("stats", help="Aggregate statistics dashboard")
    stats_p.add_argument("--json", action="store_true", help="Output as JSON")

    # export
    export_p = sub.add_parser("export", help="Export session data as JSON")
    export_p.add_argument("session_id", help="Session ID to export")

    # fix
    fix_p = sub.add_parser("fix", help="Generate remediation recommendations")
    fix_p.add_argument("session_id", nargs="?", help="Session ID (default: most recent)")
    fix_p.add_argument("--json", action="store_true", help="Output as JSON")
    fix_p.add_argument("--patch", action="store_true",
                       help="Output CLAUDE.md patch only (ready to paste)")

    # watch
    watch_p = sub.add_parser("watch", help="Auto-ingest new sessions from directories")
    watch_p.add_argument("dirs", nargs="*", help="Directories to watch (default: auto-discover)")
    watch_p.add_argument("--interval", type=float, default=30.0,
                         help="Poll interval in seconds (default: 30)")
    watch_p.add_argument("--settle", type=float, default=60.0,
                         help="Seconds since last modification before ingesting (default: 60)")
    watch_p.add_argument("--once", action="store_true",
                         help="Scan once and exit (don't poll)")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

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
        "watch": cmd_watch,
    }

    cmd_func = commands.get(args.command)
    if cmd_func:
        cmd_func(args)
    else:
        parser.print_help()
        sys.exit(2)
