"""Handoff summary generator — structured session continuation document.

Solves context rot by extracting the essential state from a session
into a format that can be prepended to the next session's context.
"""

from collections import Counter
from pathlib import Path


def format_handoff(session: dict, tool_calls: list[dict], patterns: list[dict]) -> str:
    """Generate a structured handoff summary for session continuation.

    Args:
        session: Session row from DB.
        tool_calls: Tool call rows from DB, ordered by seq.
        patterns: Pattern rows from DB.

    Returns:
        Markdown handoff document.
    """
    lines = []
    lines.append(f"# Session Handoff: {session['id']}")
    lines.append(f"Generated from sesh analysis")
    lines.append("")

    # --- What was done ---
    lines.append("## What was done")
    actions = _extract_actions(tool_calls)
    if actions:
        for action in actions:
            lines.append(f"- {action}")
    else:
        lines.append("- (no significant actions detected)")
    lines.append("")

    # --- Files touched ---
    lines.append("## Files touched")
    file_actions = _extract_file_actions(tool_calls)
    if file_actions:
        for path, action_types in sorted(file_actions.items()):
            lines.append(f"- `{path}` ({', '.join(sorted(action_types))})")
    else:
        lines.append("- (no files touched)")
    lines.append("")

    # --- Unresolved issues ---
    errors = [tc for tc in tool_calls if tc["is_error"]]
    if errors:
        lines.append("## Unresolved issues")
        # Show last few errors — most likely to be unresolved
        for tc in errors[-5:]:
            preview = (tc.get("output_preview") or "")[:120]
            lines.append(f"- {tc['name']} error: {preview}")
        lines.append("")

    # --- Process notes ---
    if session.get("grade"):
        lines.append("## Process notes")
        lines.append(f"- Grade: {session['grade']} (score: {session['score']})")
        if session.get("grade_notes") and session["grade_notes"] != "Clean session":
            lines.append(f"- Breakdown: {session['grade_notes']}")
        if patterns:
            pattern_summary = ", ".join(p["type"] for p in patterns)
            lines.append(f"- Patterns: {pattern_summary}")
        lines.append("")

    # --- Key metrics ---
    lines.append("## Metrics")
    lines.append(f"- Tool calls: {session.get('tool_call_count', 0)}")
    lines.append(f"- Errors: {session.get('error_count', 0)}")
    if session.get("duration_minutes"):
        lines.append(f"- Duration: ~{session['duration_minutes']:.0f} min")
    lines.append("")

    return "\n".join(lines)


def _extract_actions(tool_calls: list[dict]) -> list[str]:
    """Extract high-level action descriptions from tool calls."""
    actions = []
    seen_files: set[str] = set()

    for tc in tool_calls:
        name = tc["name"]
        inp = tc.get("input_json", "{}")
        if isinstance(inp, str):
            try:
                inp_data = __import__("json").loads(inp)
            except Exception:
                inp_data = {}
        else:
            inp_data = inp

        if name == "Write":
            path = inp_data.get("file_path", "")
            fname = Path(path).name if path else "?"
            if path not in seen_files:
                actions.append(f"Created {fname}")
                seen_files.add(path)
        elif name == "Edit":
            path = inp_data.get("file_path", "")
            fname = Path(path).name if path else "?"
            if path not in seen_files:
                actions.append(f"Modified {fname}")
                seen_files.add(path)
        elif name == "Bash":
            cmd = inp_data.get("command", "")
            # Summarize interesting bash commands
            if cmd.startswith("git "):
                actions.append(f"Ran: {cmd[:80]}")
            elif cmd.startswith("python") or cmd.startswith("npm") or cmd.startswith("pip"):
                actions.append(f"Ran: {cmd[:80]}")
            elif "test" in cmd.lower():
                actions.append(f"Ran tests: {cmd[:80]}")

    return actions[:20]  # Cap at 20 actions


def _extract_file_actions(tool_calls: list[dict]) -> dict[str, set[str]]:
    """Extract which files were touched and how."""
    file_actions: dict[str, set[str]] = {}

    for tc in tool_calls:
        name = tc["name"]
        inp = tc.get("input_json", "{}")
        if isinstance(inp, str):
            try:
                inp_data = __import__("json").loads(inp)
            except Exception:
                inp_data = {}
        else:
            inp_data = inp

        path = inp_data.get("file_path", "") or inp_data.get("path", "")
        if not path:
            continue

        if name == "Read":
            file_actions.setdefault(path, set()).add("read")
        elif name == "Write":
            file_actions.setdefault(path, set()).add("created")
        elif name == "Edit":
            file_actions.setdefault(path, set()).add("edited")
        elif name in ("Grep", "Glob"):
            file_actions.setdefault(path, set()).add("searched")

    return file_actions
