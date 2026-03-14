"""Audit report formatting — human-readable, animated, and JSON output."""

import json
import sys
import time
from pathlib import Path

from .engine import AuditResult, combine_metrics, get_registered_metrics


def format_audit_report(result: AuditResult) -> str:
    """Format audit result as human-readable text with bar charts."""
    lines = []

    lines.append("")
    lines.append("Repo Audit")
    lines.append("\u2501" * 40)
    lines.append("")
    lines.append(f"Path: {result.path}")
    lines.append(f"Score: {result.score}/100  Grade: {result.grade}")
    lines.append("")

    if result.metrics:
        lines.append("Metrics")
        lines.append("\u2500" * 24)

        # Find longest metric name for alignment
        max_name = max(len(m.name) for m in result.metrics)

        for m in result.metrics:
            bar = "\u2588" * m.score + "\u2591" * (10 - m.score)
            flag = "  \u2190 NEEDS ATTENTION" if m.score <= 4 else ""
            lines.append(f"  {m.name:<{max_name}}  [{m.score:2d}/10] {bar}{flag}")

        lines.append("")

    # Findings detail
    has_findings = any(
        f.status != "found"
        for m in result.metrics
        for f in m.findings
    )
    if has_findings:
        lines.append("Findings")
        lines.append("\u2500" * 24)
        for m in result.metrics:
            issues = [f for f in m.findings if f.status != "found"]
            if issues:
                for f in issues:
                    icon = "!" if f.status == "missing" else "~"
                    lines.append(f"  [{icon}] {m.name}: {f.description}")
        lines.append("")

    # Recommendations
    if result.recommendations:
        lines.append("What To Fix")
        lines.append("\u2500" * 11)

        # Deduplicate and limit
        seen = set()
        for rec in result.recommendations:
            if rec not in seen:
                seen.add(rec)
                lines.append(f"  - {rec}")

        lines.append("")

    return "\n".join(lines)


def print_audit_animated(repo_path: Path, enabled: list[str] | None = None) -> AuditResult:
    """Run audit with progressive reveal — bars fill in, score counts up.

    Only call when stdout is a TTY and not in JSON/quiet mode.
    Returns the AuditResult for threshold checking.
    """
    w = sys.stdout.write
    flush = sys.stdout.flush

    registry = get_registered_metrics()
    if not registry:
        print("No metrics registered.")
        return AuditResult(path=str(repo_path), score=0, grade="F", metrics=[], recommendations=[])

    max_name = max(len(name) for name, _ in registry)
    results = []

    # Header
    print("\n\033[1mRepo Audit\033[0m")
    print("\u2501" * 40)
    print(f"\n\033[2mPath: {repo_path}\033[0m\n")

    # Metrics — progressive reveal
    print("Metrics")
    print("\u2500" * 24)

    for name, detector in registry:
        if enabled and name not in enabled:
            continue

        # Show scanning state
        w(f"  {name:<{max_name}}  \033[2m{'·' * 16}\033[0m")
        flush()

        # Run detector
        result = detector(repo_path, {})
        results.append(result)

        # Clear line
        w(f"\r  {name:<{max_name}}  [{result.score:2d}/10] ")
        flush()

        # Animate bar filling
        for _i in range(result.score):
            w("\u2588")
            flush()
            time.sleep(0.025)

        # Fill remainder
        w("\u2591" * (10 - result.score))

        if result.score <= 4:
            w("  \u2190 NEEDS ATTENTION")

        w("\n")
        flush()
        time.sleep(0.07)

    # Combine results
    audit = combine_metrics(results)
    audit.path = str(repo_path)

    # Brief pause before score
    time.sleep(0.2)
    print()

    # Score count-up
    target = audit.score
    steps = min(target, 30)
    for i in range(steps):
        val = int(target * (i + 1) / steps)
        w(f"\r\033[1mScore: {val}/100\033[0m")
        flush()
        time.sleep(0.02)

    # Final score + grade reveal
    time.sleep(0.15)
    w(f"\r\033[1mScore: {target}/100  Grade: {audit.grade}\033[0m\n")
    flush()

    # Findings (no animation — just appear)
    has_findings = any(
        f.status != "found"
        for m in audit.metrics
        for f in m.findings
    )
    if has_findings:
        print("\nFindings")
        print("\u2500" * 24)
        for m in audit.metrics:
            issues = [f for f in m.findings if f.status != "found"]
            for f in issues:
                icon = "!" if f.status == "missing" else "~"
                print(f"  [{icon}] {m.name}: {f.description}")

    # Recommendations
    if audit.recommendations:
        print("\nWhat To Fix")
        print("\u2500" * 11)
        seen = set()
        for rec in audit.recommendations:
            if rec not in seen:
                seen.add(rec)
                print(f"  - {rec}")

    print()
    return audit


def audit_to_json(result: AuditResult) -> str:
    """Format audit result as JSON."""
    data = {
        "path": result.path,
        "score": result.score,
        "grade": result.grade,
        "metrics": [
            {
                "name": m.name,
                "score": m.score,
                "findings": [
                    {"status": f.status, "description": f.description}
                    for f in m.findings
                ],
                "recommendations": m.recommendations,
            }
            for m in result.metrics
        ],
        "recommendations": result.recommendations,
    }
    return json.dumps(data, indent=2)
