"""Replay command — step-by-step session replay with filtering and annotation."""

import json
import sys

from ..analyzers.patterns import detect_all_patterns as redetect_patterns
from ..parsers.base import ToolCall
from ..replay import (
    annotate_timeline,
    build_timeline,
    filter_steps,
    format_replay,
    parse_range,
)
from ._resolve import get_db


def cmd_replay(args) -> None:
    """Replay a session step by step."""
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

    tool_calls = db.get_tool_calls(session["id"])

    # Build timeline — prefer source JSONL for full fidelity (includes thinking blocks),
    # fall back to DB-only (tool calls only, no thinking context)
    source_path = session.get("source_path") if not args.db_only else None
    steps, source = build_timeline(tool_calls, source_path=source_path)

    if not steps:
        print(f"No steps found for session {session['id'][:16]}...", file=sys.stderr)
        db.close()
        sys.exit(5)

    # Annotate with patterns if requested
    if args.annotate:
        # Re-detect patterns from raw tool calls to get tool_indices
        # (DB-stored patterns don't preserve index info)
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
        print(json.dumps({
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
