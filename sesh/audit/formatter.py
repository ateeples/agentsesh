"""Audit report formatting — human-readable and JSON output."""

import json

from .engine import AuditResult


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
