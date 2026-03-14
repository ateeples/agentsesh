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
from .analyzers.outcomes import (
    extract_outcomes,
    compare_outcomes,
    format_outcome_metrics,
    format_comparison,
)
from .replay import (
    build_timeline,
    annotate_timeline,
    filter_steps,
    format_replay,
    parse_range,
)
from .analyzers.patterns import detect_all_patterns as redetect_patterns
from .parsers.base import ToolCall
from .debug import extract_decision_points, search_thinking, lookup_by_action, index_dotnotes, search_dotnotes, correlate_patterns
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
from .analyze import analyze_session, format_analysis, analysis_to_json


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


def cmd_test(args) -> None:
    """Compare outcome metrics between sessions (behavioral regression testing)."""
    db = _get_db(args)

    # Resolve session IDs
    sessions = db.list_sessions(limit=20)
    if not sessions:
        print("No sessions found. Run `sesh log` first.", file=sys.stderr)
        sys.exit(3)

    if args.session_a and args.session_b:
        # Compare two specific sessions
        session_a = db.get_session(args.session_a)
        session_b = db.get_session(args.session_b)
        if not session_a:
            print(f"Session not found: {args.session_a}", file=sys.stderr)
            sys.exit(4)
        if not session_b:
            print(f"Session not found: {args.session_b}", file=sys.stderr)
            sys.exit(4)
    elif args.session_a:
        # Compare one session against most recent
        session_a = db.get_session(args.session_a)
        if not session_a:
            print(f"Session not found: {args.session_a}", file=sys.stderr)
            sys.exit(4)
        # Most recent that isn't session_a
        for s in sessions:
            if s["id"] != args.session_a:
                session_b = db.get_session(s["id"])
                break
        else:
            print("Need at least 2 sessions to compare.", file=sys.stderr)
            sys.exit(3)
    else:
        # Compare two most recent sessions
        if len(sessions) < 2:
            print("Need at least 2 sessions to compare.", file=sys.stderr)
            sys.exit(3)
        session_a = db.get_session(sessions[1]["id"])  # older
        session_b = db.get_session(sessions[0]["id"])  # newer

    tc_a = db.get_tool_calls(session_a["id"])
    tc_b = db.get_tool_calls(session_b["id"])

    outcomes_a = extract_outcomes(tc_a)
    outcomes_b = extract_outcomes(tc_b)

    if args.json:
        import json as json_mod
        if args.compare:
            comp = compare_outcomes(outcomes_a, outcomes_b)
            print(json_mod.dumps({
                "baseline": _outcome_to_dict(outcomes_a),
                "candidate": _outcome_to_dict(outcomes_b),
                "improvements": comp.improvements,
                "regressions": comp.regressions,
                "unchanged": comp.unchanged,
                "verdict": comp.verdict,
            }, indent=2))
        else:
            print(json_mod.dumps({
                "session_a": {
                    "id": session_a["id"],
                    "grade": session_a.get("grade"),
                    "outcomes": _outcome_to_dict(outcomes_a),
                },
                "session_b": {
                    "id": session_b["id"],
                    "grade": session_b.get("grade"),
                    "outcomes": _outcome_to_dict(outcomes_b),
                },
            }, indent=2))
    else:
        # Show outcomes side by side, then comparison
        print(f"# Baseline: {session_a['id'][:16]}... "
              f"({session_a.get('grade', '?')})")
        print(format_outcome_metrics(outcomes_a))
        print()
        print(f"# Candidate: {session_b['id'][:16]}... "
              f"({session_b.get('grade', '?')})")
        print(format_outcome_metrics(outcomes_b))
        print()

        comp = compare_outcomes(outcomes_a, outcomes_b)
        print(format_comparison(comp))

    db.close()


def _outcome_to_dict(m) -> dict:
    """Convert OutcomeMetrics to a plain dict for JSON output."""
    return {
        "error_retry_loops": m.error_retry_loops,
        "files_reworked": m.files_reworked,
        "rework_edits": m.rework_edits,
        "ended_on_error": m.ended_on_error,
        "final_error_streak": m.final_error_streak,
        "total_tool_calls": m.total_tool_calls,
        "total_errors": m.total_errors,
        "success_rate": round(m.success_rate, 4),
        "test_runs": m.test_runs,
        "test_passes": m.test_passes,
        "test_failures": m.test_failures,
        "build_runs": m.build_runs,
        "build_passes": m.build_passes,
        "build_failures": m.build_failures,
        "lint_runs": m.lint_runs,
        "lint_passes": m.lint_passes,
        "lint_failures": m.lint_failures,
        "rework_files": m.rework_files,
        "error_retry_details": m.error_retry_details,
    }


def cmd_replay(args) -> None:
    """Replay a session step by step."""
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

    tool_calls = db.get_tool_calls(session["id"])

    # Build timeline — prefer source file for full fidelity
    source_path = session.get("source_path") if not args.db_only else None
    steps, source = build_timeline(tool_calls, source_path=source_path)

    if not steps:
        print(f"No steps found for session {session['id'][:16]}...", file=sys.stderr)
        db.close()
        sys.exit(5)

    # Annotate with patterns if requested
    if args.annotate:
        # Re-detect patterns to get tool_indices (DB patterns don't store them)
        tc_objects = [
            ToolCall(
                name=tc["name"],
                tool_id=tc.get("tool_id", ""),
                input_data=json.loads(tc["input_json"]) if isinstance(tc.get("input_json"), str) else tc.get("input_json", {}),
                output_preview=tc.get("output_preview", ""),
                output_length=tc.get("output_length", 0),
                is_error=bool(tc.get("is_error", False)),
                timestamp=tc.get("timestamp"),
                categories=(tc.get("categories") or "").split(","),
                seq=tc["seq"],
            )
            for tc in tool_calls
        ]
        patterns = redetect_patterns(tc_objects)
        annotate_timeline(steps, patterns)

    # Apply filters
    step_range = None
    if args.range:
        try:
            step_range = parse_range(args.range)
        except ValueError as e:
            print(f"Invalid range: {e}", file=sys.stderr)
            sys.exit(2)

    steps = filter_steps(
        steps,
        errors_only=args.errors,
        tools_only=args.tools,
        step_range=step_range,
        tool_filter=args.tool,
    )

    if args.json:
        import json as json_mod
        data = []
        for s in steps:
            entry = {
                "seq": s.seq,
                "type": s.type,
                "timestamp": s.timestamp,
                "summary": s.summary,
            }
            if s.tool_name:
                entry["tool_name"] = s.tool_name
            if s.is_error:
                entry["is_error"] = True
            if s.annotations:
                entry["annotations"] = s.annotations
            if args.verbose and s.detail:
                entry["detail"] = s.detail
            data.append(entry)
        print(json_mod.dumps({
            "session_id": session["id"],
            "grade": session.get("grade"),
            "source": source,
            "steps": data,
        }, indent=2))
    else:
        print(format_replay(
            steps, session,
            source=source,
            compact=args.compact,
            verbose=args.verbose,
        ))

    db.close()


def cmd_debug(args) -> None:
    """Debug a session — search thinking blocks to understand agent reasoning."""
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

    source_path = session.get("source_path")
    if not source_path or not Path(source_path).exists():
        print(
            f"Source file not found for session {session['id'][:16]}...\n"
            "sesh debug requires the original JSONL (thinking blocks aren't stored in DB).",
            file=sys.stderr,
        )
        db.close()
        sys.exit(5)

    tool_calls = db.get_tool_calls(session["id"])
    steps, source = build_timeline(tool_calls, source_path=source_path)

    if not steps:
        print(f"No steps found for session {session['id'][:16]}...", file=sys.stderr)
        db.close()
        sys.exit(5)

    dps = extract_decision_points(steps)

    if not dps:
        print(
            f"No thinking blocks found in session {session['id'][:16]}...\n"
            "This session may not have used extended thinking.",
            file=sys.stderr,
        )
        db.close()
        sys.exit(6)

    # Correlate mode: map antipatterns to decision points
    query = args.query
    if args.correlate:
        # Reconstruct ToolCall objects and re-detect patterns to get tool_indices
        tc_objects = _tool_calls_to_objects(tool_calls)
        patterns_with_indices = redetect_patterns(tc_objects)

        if not patterns_with_indices:
            print("No antipatterns detected in this session.", file=sys.stderr)
            db.close()
            return

        correlated = correlate_patterns(steps, dps, patterns_with_indices)

        if args.json:
            data = []
            for pattern, matched_dps in correlated:
                entry = {
                    "pattern_type": pattern.type,
                    "severity": pattern.severity,
                    "detail": pattern.detail,
                    "decision_points": [{
                        "seq": dp.seq,
                        "timestamp": dp.timestamp,
                        "thinking_preview": dp.thinking.detail[:300],
                    } for dp in matched_dps],
                }
                if args.verbose:
                    for i, dp in enumerate(matched_dps):
                        entry["decision_points"][i]["thinking_full"] = dp.thinking.detail
                data.append(entry)
            print(json.dumps({
                "session_id": session["id"],
                "mode": "correlate",
                "total_patterns": len(patterns_with_indices),
                "correlated_patterns": len(correlated),
                "total_decision_points": len(dps),
                "correlations": data,
            }, indent=2))
        else:
            _print_correlate_results(session, dps, patterns_with_indices, correlated, verbose=args.verbose)
        db.close()
        return

    # Dotnotes mode: index/search dot-notation paths
    if args.dotnotes:
        dn_results = search_dotnotes(dps, query or "")
        if args.json:
            # Group by path
            by_path: dict[str, list] = {}
            for path, dp in dn_results:
                if path not in by_path:
                    by_path[path] = []
                entry = {
                    "seq": dp.seq,
                    "timestamp": dp.timestamp,
                    "thinking_preview": dp.thinking.detail[:300],
                    "action_count": len(dp.actions),
                }
                if args.verbose:
                    entry["thinking_full"] = dp.thinking.detail
                by_path[path].append(entry)
            print(json.dumps({
                "session_id": session["id"],
                "pattern": query or "*",
                "mode": "dotnotes",
                "total_decision_points": len(dps),
                "unique_paths": len(by_path),
                "total_references": len(dn_results),
                "dotnotes": by_path,
            }, indent=2))
        else:
            _print_dotnotes_results(session, dps, dn_results, query, verbose=args.verbose)
        db.close()
        return

    # Search or list all decision points
    if query and args.action:
        results = lookup_by_action(dps, query)
    elif query:
        results = search_thinking(dps, query)
    else:
        results = dps

    if args.json:
        data = []
        for dp in results:
            entry = {
                "seq": dp.seq,
                "timestamp": dp.timestamp,
                "thinking_length": len(dp.thinking.detail),
                "thinking_preview": dp.thinking.detail[:300],
                "action_count": len(dp.actions),
                "actions": [
                    {"tool_name": a.tool_name, "summary": a.summary, "is_error": a.is_error}
                    for a in dp.actions
                ],
            }
            if args.verbose:
                entry["thinking_full"] = dp.thinking.detail
            data.append(entry)
        out = {
            "session_id": session["id"],
            "query": query,
            "mode": "action" if args.action else "thinking",
            "total_decision_points": len(dps),
            "matches": len(results),
            "decision_points": data,
        }
        print(json.dumps(out, indent=2))
    else:
        _print_debug_results(session, dps, results, query, verbose=args.verbose, action_mode=args.action)

    db.close()


def _print_debug_results(
    session: dict,
    all_dps: list,
    results: list,
    query: str | None,
    verbose: bool = False,
    action_mode: bool = False,
) -> None:
    """Format debug results as human-readable output."""
    sid = session.get("id", "?")[:16]
    grade = session.get("grade", "?")
    model = session.get("model", "?")

    print(f"{'=' * 60}")
    mode_label = "Reverse Lookup" if action_mode else "Debug"
    print(f"  {mode_label}: {sid}... ({grade}, {model or '?'})")
    print(f"  {len(all_dps)} decision points in session")
    if query and action_mode:
        print(f"  Action: \"{query}\" → {len(results)} match(es)")
    elif query:
        print(f"  Query: \"{query}\" → {len(results)} match(es)")
    print(f"{'=' * 60}")
    print()

    for dp in results:
        thinking_preview = dp.thinking.detail
        if not verbose:
            thinking_preview = thinking_preview[:200]
            if len(dp.thinking.detail) > 200:
                thinking_preview += "..."

        print(f"[DP {dp.seq}] {dp.timestamp or '?'}")
        print(f"  THINKING ({len(dp.thinking.detail)} chars):")
        for line in thinking_preview.split("\n"):
            print(f"    {line}")

        if dp.actions:
            print(f"  ACTIONS ({len(dp.actions)}):")
            for a in dp.actions:
                status = "x" if a.is_error else "+"
                # Highlight matching action in reverse lookup mode
                marker = " ◀" if action_mode and query and query.lower() in a.summary.lower() else ""
                print(f"    [{status}] {a.summary}{marker}")
        else:
            print("  ACTIONS: (none — session ended or next thinking began)")
        print()

    print(f"{'=' * 60}")
    if query:
        search_type = "action" if action_mode else "thinking"
        print(f"  {len(results)}/{len(all_dps)} decision points match \"{query}\" ({search_type})")
    else:
        print(f"  {len(all_dps)} decision points total")
    print(f"{'=' * 60}")


def _print_dotnotes_results(
    session: dict,
    all_dps: list,
    results: list[tuple[str, object]],
    pattern: str | None,
    verbose: bool = False,
) -> None:
    """Format dotnotes results as human-readable output."""
    sid = session.get("id", "?")[:16]
    grade = session.get("grade", "?")
    model = session.get("model", "?")

    # Group by path
    by_path: dict[str, list] = {}
    for path, dp in results:
        if path not in by_path:
            by_path[path] = []
        by_path[path].append(dp)

    print(f"{'=' * 60}")
    print(f"  Dotnotes: {sid}... ({grade}, {model or '?'})")
    print(f"  {len(all_dps)} decision points | {len(by_path)} unique paths | {len(results)} references")
    if pattern:
        print(f"  Pattern: \"{pattern}\"")
    print(f"{'=' * 60}")
    print()

    for path, dps in sorted(by_path.items()):
        print(f"  {path}  ({len(dps)} reference{'s' if len(dps) != 1 else ''})")
        for dp in dps:
            thinking_preview = dp.thinking.detail
            if not verbose:
                thinking_preview = thinking_preview[:120]
                if len(dp.thinking.detail) > 120:
                    thinking_preview += "..."
            thinking_preview = " ".join(thinking_preview.split())
            print(f"    [DP {dp.seq}] {thinking_preview}")
        print()

    print(f"{'=' * 60}")
    print(f"  {len(by_path)} paths, {len(results)} total references")
    print(f"{'=' * 60}")


def _tool_calls_to_objects(tool_calls: list[dict]) -> list:
    """Convert DB tool_call dicts back to ToolCall objects for pattern re-detection."""
    from .parsers.base import ToolCall
    result = []
    for tc in tool_calls:
        input_data = tc.get("input_json", "{}")
        if isinstance(input_data, str):
            try:
                input_data = json.loads(input_data)
            except json.JSONDecodeError:
                input_data = {}
        cats = tc.get("categories", "")
        categories = cats.split(",") if cats else []
        result.append(ToolCall(
            name=tc["name"],
            tool_id=tc.get("tool_id", ""),
            input_data=input_data,
            output_preview=tc.get("output_preview", ""),
            output_length=tc.get("output_length", 0),
            is_error=bool(tc.get("is_error", False)),
            timestamp=tc.get("timestamp"),
            categories=categories,
            seq=tc.get("seq", 0),
        ))
    return result


def _print_correlate_results(
    session: dict,
    all_dps: list,
    all_patterns: list,
    correlated: list[tuple],
    verbose: bool = False,
) -> None:
    """Format pattern correlation results."""
    sid = session.get("id", "?")[:16]
    grade = session.get("grade", "?")
    model = session.get("model", "?")

    severity_icon = {"warning": "!!", "concern": "!", "info": "~"}

    print(f"{'=' * 60}")
    print(f"  Pattern Correlation: {sid}... ({grade}, {model or '?'})")
    print(f"  {len(all_dps)} decision points | {len(all_patterns)} patterns | {len(correlated)} correlated")
    print(f"{'=' * 60}")
    print()

    for pattern, matched_dps in correlated:
        icon = severity_icon.get(pattern.severity, "~")
        print(f"  [{icon}] {pattern.type} ({pattern.severity})")
        print(f"      {pattern.detail}")
        print(f"      Caused by {len(matched_dps)} decision point(s):")
        for dp in matched_dps:
            thinking_preview = dp.thinking.detail
            if not verbose:
                thinking_preview = thinking_preview[:150]
                if len(dp.thinking.detail) > 150:
                    thinking_preview += "..."
            thinking_preview = " ".join(thinking_preview.split())
            print(f"        [DP {dp.seq}] {thinking_preview}")
        print()

    # Show uncorrelated patterns (those without tool_indices)
    correlated_types = {p.type for p, _ in correlated}
    uncorrelated = [p for p in all_patterns if p.type not in correlated_types and not p.tool_indices]
    if uncorrelated:
        print("  Uncorrelated patterns (no specific tool calls):")
        for p in uncorrelated:
            icon = severity_icon.get(p.severity, "~")
            print(f"    [{icon}] {p.type}: {p.detail}")
        print()

    print(f"{'=' * 60}")
    print(f"  {len(correlated)}/{len(all_patterns)} patterns correlated to decision points")
    print(f"{'=' * 60}")


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


def cmd_analyze(args) -> None:
    """One-command session analysis — no database required."""
    path = Path(args.file)
    if not path.exists():
        print(f"Error: File not found: {args.file}", file=sys.stderr)
        sys.exit(1)

    try:
        result = analyze_session(path)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)

    if args.json:
        print(analysis_to_json(result, verbose=args.verbose))
    elif args.fix:
        patch = generate_claude_md_patch(result.remediations)
        if patch:
            print(patch)
        else:
            print("No CLAUDE.md changes recommended. Clean session.")
    else:
        print(format_analysis(result, verbose=args.verbose))


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

    # test
    test_p = sub.add_parser("test", help="Compare outcome metrics between sessions")
    test_p.add_argument("session_a", nargs="?", help="Baseline session ID (default: second most recent)")
    test_p.add_argument("session_b", nargs="?", help="Candidate session ID (default: most recent)")
    test_p.add_argument("--compare", action="store_true", default=True,
                        help="Show comparison (default)")
    test_p.add_argument("--json", action="store_true", help="Output as JSON")

    # replay
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

    # debug
    debug_p = sub.add_parser("debug", help="Search thinking blocks — why did the agent do that?")
    debug_p.add_argument("query", nargs="?", help="Search query (searches thinking blocks, or actions with --action)")
    debug_p.add_argument("session_id", nargs="?", help="Session ID (default: most recent)")
    debug_p.add_argument("--action", "-a", action="store_true", help="Reverse lookup: search actions instead of thinking (e.g. 'why did you edit auth.py?')")
    debug_p.add_argument("--dotnotes", "-d", action="store_true", help="Index/search dot-notation paths in thinking (e.g. 'auth.*', 'config.database.*')")
    debug_p.add_argument("--correlate", action="store_true", help="Correlate antipatterns to the decision points that caused them")
    debug_p.add_argument("--json", action="store_true", help="Output as JSON")
    debug_p.add_argument("--verbose", "-v", action="store_true", help="Show full thinking blocks")

    # analyze
    analyze_p = sub.add_parser("analyze", help="One-command session diagnostic (no database required)")
    analyze_p.add_argument("file", help="Path to session transcript (JSONL)")
    analyze_p.add_argument("--json", action="store_true", help="Output as JSON")
    analyze_p.add_argument("--verbose", "-v", action="store_true",
                           help="Include thinking context and grade breakdown")
    analyze_p.add_argument("--fix", action="store_true",
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
        "test": cmd_test,
        "replay": cmd_replay,
        "debug": cmd_debug,
        "analyze": cmd_analyze,
        "watch": cmd_watch,
    }

    cmd_func = commands.get(args.command)
    if cmd_func:
        cmd_func(args)
    else:
        parser.print_help()
        sys.exit(2)
