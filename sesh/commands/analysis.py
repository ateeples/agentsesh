"""Analysis commands — fix, test, analyze, audit."""

import json
import sys
from pathlib import Path

from ..analyze import analysis_to_json, analyze_session, format_analysis
from ..analyzers.outcomes import (
    compare_outcomes,
    extract_outcomes,
    format_comparison,
    format_outcome_metrics,
)
from ..analyzers.remediation import (
    format_remediations,
    generate_claude_md_patch,
    get_all_remediations,
)
from ..audit import _metrics  # noqa: F401 — triggers detector registration
from ..audit.engine import run_audit
from ..audit.formatter import audit_to_json, format_audit_report
from ._resolve import get_db


def cmd_fix(args) -> None:
    """Generate remediation recommendations for a session."""
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

    patterns = db.get_patterns(session["id"])
    remediations = get_all_remediations(patterns)

    if not remediations:
        print(f"Session {session['id'][:16]}... ({session.get('grade', '?')}) — no anti-patterns detected. Clean session.")
        db.close()
        return

    if args.json:
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
        print(json.dumps(data, indent=2))
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
    """Compare outcome metrics between sessions (behavioral regression testing).

    Extracts test/build/lint results from two sessions and compares them.
    Shows improvements, regressions, and unchanged metrics.
    """
    db = get_db(args)

    sessions = db.list_sessions(limit=20)
    if not sessions:
        print("No sessions found. Run `sesh log` first.", file=sys.stderr)
        sys.exit(3)

    # Session resolution: explicit pair, one vs most-recent, or two most-recent
    if args.session_a and args.session_b:
        session_a = db.get_session(args.session_a)
        session_b = db.get_session(args.session_b)
        if not session_a:
            print(f"Session not found: {args.session_a}", file=sys.stderr)
            sys.exit(4)
        if not session_b:
            print(f"Session not found: {args.session_b}", file=sys.stderr)
            sys.exit(4)
    elif args.session_a:
        session_a = db.get_session(args.session_a)
        if not session_a:
            print(f"Session not found: {args.session_a}", file=sys.stderr)
            sys.exit(4)
        for s in sessions:
            if s["id"] != args.session_a:
                session_b = db.get_session(s["id"])
                break
        else:
            print("Need at least 2 sessions to compare.", file=sys.stderr)
            sys.exit(3)
    else:
        if len(sessions) < 2:
            print("Need at least 2 sessions to compare.", file=sys.stderr)
            sys.exit(3)
        session_a = db.get_session(sessions[1]["id"])  # older = baseline
        session_b = db.get_session(sessions[0]["id"])  # newer = candidate

    tc_a = db.get_tool_calls(session_a["id"])
    tc_b = db.get_tool_calls(session_b["id"])

    outcomes_a = extract_outcomes(tc_a)
    outcomes_b = extract_outcomes(tc_b)

    if args.json:
        comp = compare_outcomes(outcomes_a, outcomes_b)
        print(json.dumps({
            "baseline": _outcome_to_dict(outcomes_a),
            "candidate": _outcome_to_dict(outcomes_b),
            "improvements": comp.improvements,
            "regressions": comp.regressions,
            "unchanged": comp.unchanged,
            "verdict": comp.verdict,
        }, indent=2))
    else:
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


def cmd_analyze(args) -> None:
    """One-command session analysis — no database required.

    With no file argument, auto-discovers the most recent Claude Code
    session from ~/.claude/projects/. Zero friction.
    """
    from ..watch import find_latest_transcript

    if args.file:
        path = Path(args.file)
        if not path.exists():
            print(f"Error: File not found: {args.file}", file=sys.stderr)
            sys.exit(1)
    else:
        # Auto-discover most recent transcript
        path = find_latest_transcript()
        if not path:
            print(
                "No session transcripts found.\n"
                "  Provide a file:  sesh analyze path/to/session.jsonl\n"
                "  Or ensure Claude Code sessions exist in ~/.claude/projects/",
                file=sys.stderr,
            )
            sys.exit(1)
        if not args.json:
            print(f"Auto-detected: {path}\n", file=sys.stderr)

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
    elif args.feedback:
        _write_feedback(result, args)
    else:
        print(format_analysis(result, verbose=args.verbose))


def _write_feedback(result, args) -> None:
    """Generate session-specific feedback and write to CLAUDE.md."""
    from ..feedback import generate_feedback, write_feedback

    target = Path(args.feedback) if isinstance(args.feedback, str) and args.feedback is not True else Path("CLAUDE.md")

    content = generate_feedback(result)
    wrote = write_feedback(content, target)

    grade = result.grade
    if wrote:
        print(f"Feedback written to {target} ({grade.grade}, {grade.score}/100)")
    else:
        print(f"No changes — {target} already has current feedback")


def cmd_audit(args) -> None:
    """Grade a repo's agent-readiness — no database required.

    Exit code reflects score vs threshold: 0 if score >= threshold,
    1 if below. Default threshold is 0 (always pass). Use --threshold
    for CI gates.
    """
    path = Path(args.path) if args.path else Path.cwd()
    if not path.exists() or not path.is_dir():
        print(f"Error: Not a directory: {path}", file=sys.stderr)
        sys.exit(1)

    enabled = [args.metric] if args.metric else None
    threshold = getattr(args, "threshold", None)

    # Animated output for interactive TTY (not JSON, not CI threshold)
    if not args.json and sys.stdout.isatty() and threshold is None:
        from ..audit.formatter import print_audit_animated
        result = print_audit_animated(path, enabled=enabled)
    else:
        result = run_audit(path, enabled=enabled)
        if args.json:
            if threshold is not None and result.score < threshold:
                print(f"Failed: score {result.score} < threshold {threshold}", file=sys.stderr)
            print(audit_to_json(result))
        else:
            if threshold is not None and result.score < threshold:
                print(f"Failed: score {result.score} < threshold {threshold}", file=sys.stderr)
            print(format_audit_report(result))

    # CI gate: non-zero exit if score below threshold
    if threshold is not None and result.score < threshold:
        sys.exit(1)
