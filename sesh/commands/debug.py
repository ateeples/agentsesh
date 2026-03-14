"""Debug command — search thinking blocks to understand agent reasoning.

Four modes: thinking search (default), action reverse lookup (--action),
dotnotes path search (--dotnotes), pattern correlation (--correlate).
"""

import json
import sys
from pathlib import Path

from ._resolve import get_db
from ..replay import build_timeline
from ..debug import (
    extract_decision_points,
    search_thinking,
    lookup_by_action,
    search_dotnotes,
    correlate_patterns,
)
from ..analyzers.patterns import detect_all_patterns as redetect_patterns
from ..parsers.base import ToolCall


def cmd_debug(args) -> None:
    """Debug a session — search thinking blocks to understand agent reasoning."""
    db = get_db(args)

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

    # Mode dispatch: correlate, dotnotes, action lookup, or thinking search
    query = args.query
    if args.correlate:
        _handle_correlate(args, db, session, dps, tool_calls, steps)
        return

    if args.dotnotes:
        _handle_dotnotes(args, db, session, dps, query)
        return

    # Default mode: search thinking text, or reverse-lookup by action
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


# --- Mode handlers ---


def _handle_correlate(args, db, session, dps, tool_calls, steps) -> None:
    """Correlate mode: map each antipattern to the thinking that caused it."""
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


def _handle_dotnotes(args, db, session, dps, query) -> None:
    """Dotnotes mode: index/search dot-notation paths."""
    dn_results = search_dotnotes(dps, query or "")
    if args.json:
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


# --- Output formatters ---


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


# --- Helpers ---


def _tool_calls_to_objects(tool_calls: list[dict]) -> list:
    """Convert DB tool_call dicts back to ToolCall objects for pattern re-detection."""
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
