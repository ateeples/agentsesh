"""Lightweight live session analysis for active (still-being-written) sessions.

Reads an actively growing JSONL file and extracts stats without waiting
for settle time. Designed to be called repeatedly (every few seconds)
with minimal overhead.

Unlike analyze_session(), this skips heavy operations:
- No timeline building
- No decision point extraction
- No full remediation generation
- No detailed failure enrichment

Returns a LiveSnapshot: tool count, errors, files, tests, cost,
collaboration basics, and actionable nudges.
"""

import json
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class LiveNudge:
    """A real-time suggestion for the current session."""

    level: str  # "info", "warn", "alert"
    message: str


@dataclass
class LiveSnapshot:
    """Lightweight snapshot of an active session."""

    path: str = ""
    project: str = ""
    tool_calls: int = 0
    errors: int = 0
    error_rate: float = 0.0
    files_read: int = 0
    files_written: int = 0
    test_runs: int = 0
    test_passes: int = 0
    test_failures: int = 0
    human_turns: int = 0
    corrections: int = 0
    affirmations: int = 0
    avg_prompt_words: float = 0.0
    archetype: str = ""
    collab_score: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost: float = 0.0
    duration_seconds: float = 0.0
    nudges: list[LiveNudge] = field(default_factory=list)
    last_tool: str = ""
    error_streak: int = 0
    file_edit_counts: dict[str, int] = field(default_factory=dict)
    active: bool = False  # True if file modified within last 60s


# Patterns for test/build detection (inline to avoid import overhead)
import re

_TEST_RE = re.compile(
    r"\b(pytest|python\s+-m\s+pytest|cargo\s+test|npm\s+test|go\s+test|"
    r"jest|vitest|mocha|rspec|make\s+test|yarn\s+test)\b"
)

_CORRECTION_WORDS = re.compile(
    r"\b(no|not|don'?t|stop|instead|wrong|actually|rather|fix|change|undo|revert)\b",
    re.IGNORECASE,
)
_AFFIRMATION_WORDS = re.compile(
    r"\b(yes|good|great|perfect|nice|exactly|correct|right|keep|love|awesome|sweet)\b",
    re.IGNORECASE,
)


def find_active_session() -> Path | None:
    """Find the most recently modified session JSONL.

    Returns the file only if it was modified within the last 5 minutes
    (likely still being written to).
    """
    sessions_dir = Path.home() / ".claude" / "projects"
    if not sessions_dir.is_dir():
        return None

    newest: Path | None = None
    newest_mtime: float = 0

    for path in sessions_dir.rglob("*.jsonl"):
        if not path.is_file():
            continue
        # Skip subagent sessions
        if "subagents" in str(path):
            continue
        try:
            mtime = path.stat().st_mtime
            if mtime > newest_mtime:
                newest = path
                newest_mtime = mtime
        except OSError:
            continue

    if newest and (time.time() - newest_mtime) < 300:  # 5 minutes
        return newest
    return None


def extract_project_name(path: Path) -> str:
    """Extract human-friendly project name from transcript path."""
    parent_name = path.parent.name
    if not parent_name or parent_name == "projects":
        return ""
    parts = parent_name.split("-")
    skip = {"Users", "Documents", "Projects", "home", "var", "tmp", ""}
    meaningful = [p for p in parts if p not in skip]
    if meaningful:
        return "-".join(meaningful[-2:]) if len(meaningful) >= 2 else meaningful[-1]
    return ""


def snapshot(path: Path | None = None) -> LiveSnapshot | None:
    """Take a lightweight snapshot of an active session.

    If no path given, auto-discovers the active session.
    Returns None if no active session found.
    """
    if path is None:
        path = find_active_session()
    if path is None:
        return None

    snap = LiveSnapshot(
        path=str(path),
        project=extract_project_name(path),
    )

    # Check if actively being written
    try:
        mtime = path.stat().st_mtime
        snap.active = (time.time() - mtime) < 60
        snap.duration_seconds = time.time() - path.stat().st_ctime
    except OSError:
        return None

    # Parse JSONL line by line
    read_files: set[str] = set()
    written_files: set[str] = set()
    file_edit_counts: dict[str, int] = {}
    current_error_streak = 0
    max_error_streak = 0
    prompt_words: list[int] = []
    first_ts: str | None = None
    last_ts: str | None = None

    try:
        with open(path) as f:
            for line in f:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue

                msg_type = d.get("type", "")

                if msg_type == "assistant":
                    usage = d.get("message", {}).get("usage", {})
                    snap.input_tokens += usage.get("input_tokens", 0)
                    snap.output_tokens += usage.get("output_tokens", 0)

                    # Extract tool calls from assistant message content
                    content = d.get("message", {}).get("content", [])
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            snap.tool_calls += 1
                            snap.last_tool = block.get("name", "")
                            inp = block.get("input", {})

                            tool_name = block.get("name", "")
                            fp = inp.get("file_path", "") or inp.get("path", "")

                            if tool_name == "Read" and fp:
                                read_files.add(fp)
                            elif tool_name in ("Edit", "Write") and fp:
                                written_files.add(fp)
                                file_edit_counts[fp] = file_edit_counts.get(fp, 0) + 1
                            elif tool_name == "Bash":
                                cmd = inp.get("command", "")
                                if cmd and _TEST_RE.search(cmd):
                                    snap.test_runs += 1

                elif msg_type == "tool_result":
                    is_error = d.get("is_error", False)
                    if is_error:
                        snap.errors += 1
                        current_error_streak += 1
                        max_error_streak = max(max_error_streak, current_error_streak)
                    else:
                        # Check test pass/fail from tool result content
                        if snap.test_runs > 0:
                            content_str = str(d.get("content", ""))
                            if "FAILED" in content_str or "Error" in content_str:
                                snap.test_failures += 1
                            elif "passed" in content_str:
                                snap.test_passes += 1
                        current_error_streak = 0

                elif msg_type in ("human", "user"):
                    # Track human messages for collaboration
                    content = d.get("message", {}).get("content", "")
                    if isinstance(content, list):
                        text = " ".join(
                            b.get("text", "") for b in content
                            if isinstance(b, dict) and b.get("type") == "text"
                        )
                    elif isinstance(content, str):
                        text = content
                    else:
                        text = ""

                    if text.strip():
                        snap.human_turns += 1
                        words = len(text.split())
                        prompt_words.append(words)

                        if _CORRECTION_WORDS.search(text):
                            snap.corrections += 1
                        if _AFFIRMATION_WORDS.search(text):
                            snap.affirmations += 1

    except (OSError, UnicodeDecodeError):
        return snap  # Return what we have

    # Computed fields
    snap.files_read = len(read_files)
    snap.files_written = len(written_files)
    snap.file_edit_counts = {k: v for k, v in file_edit_counts.items() if v >= 3}
    snap.error_streak = current_error_streak  # Current streak, not max
    snap.error_rate = snap.errors / snap.tool_calls if snap.tool_calls > 0 else 0.0

    if prompt_words:
        snap.avg_prompt_words = sum(prompt_words) / len(prompt_words)

    # Estimate cost (sonnet pricing as default)
    snap.estimated_cost = (
        snap.input_tokens * 3.0 + snap.output_tokens * 15.0
    ) / 1_000_000

    # Quick collaboration archetype
    if snap.human_turns >= 2:
        snap.archetype, snap.collab_score = _quick_archetype(
            snap.human_turns, snap.avg_prompt_words,
            snap.corrections, snap.affirmations,
            snap.tool_calls,
        )

    # Generate nudges
    snap.nudges = _generate_nudges(snap)

    return snap


def _quick_archetype(
    human_turns: int,
    avg_words: float,
    corrections: int,
    affirmations: int,
    tool_calls: int,
) -> tuple[str, int]:
    """Quick archetype classification without full collaboration analysis.

    Returns (archetype_name, collaboration_score).
    """
    tc_per_turn = tool_calls / human_turns if human_turns > 0 else 0
    correction_rate = corrections / human_turns if human_turns > 0 else 0
    affirmation_rate = affirmations / human_turns if human_turns > 0 else 0

    # Classify
    if avg_words > 400 and tc_per_turn > 15:
        archetype = "Spec Dump"
        score = max(20, 50 - int(avg_words / 20))
    elif tc_per_turn < 3 and human_turns > 5:
        archetype = "Micromanager"
        score = max(20, 40)
    elif correction_rate > 0.4 and affirmation_rate < 0.1:
        archetype = "Struggle"
        score = 55
    elif human_turns <= 2 and tool_calls > 30:
        archetype = "Autopilot"
        score = 50
    elif correction_rate > 0.1 and affirmation_rate > 0.1:
        archetype = "Partnership"
        score = min(95, 70 + int(affirmation_rate * 30))
    elif affirmation_rate > 0.2:
        archetype = "Partnership"
        score = min(90, 65 + int(affirmation_rate * 30))
    else:
        archetype = "Developing"
        score = 55

    return archetype, score


def _generate_nudges(snap: LiveSnapshot) -> list[LiveNudge]:
    """Generate real-time nudges based on current session state."""
    nudges = []

    # No tests after significant work
    if snap.tool_calls >= 25 and snap.test_runs == 0:
        nudges.append(LiveNudge(
            "warn",
            f"{snap.tool_calls} tool calls, 0 test runs. Consider testing.",
        ))

    # Active error streak
    if snap.error_streak >= 3:
        nudges.append(LiveNudge(
            "alert",
            f"Error streak: {snap.error_streak} consecutive errors. Step back?",
        ))

    # High error rate
    if snap.tool_calls >= 10 and snap.error_rate > 0.25:
        nudges.append(LiveNudge(
            "warn",
            f"Error rate: {snap.error_rate:.0%}. Something may be off.",
        ))

    # File thrashing
    for fp, count in snap.file_edit_counts.items():
        if count >= 5:
            short = Path(fp).name
            nudges.append(LiveNudge(
                "warn",
                f"{short} edited {count}x. Read the full file first?",
            ))

    # NOTE: Removed "quality drops after 100 tool calls" nudge — that was
    # based on process grades, not outcomes. Long sessions ship fine.
    #
    # NOTE: Removed "Spec Dump" nudge — skill expansions (/tdd, /ship etc)
    # inflate word counts, making short commands look like spec dumps.
    # The archetype detection needs to filter skill text before this is useful.

    # Positive reinforcement
    if snap.test_runs >= 3 and snap.test_passes >= 2 and not nudges:
        nudges.append(LiveNudge(
            "info",
            f"Tests running ({snap.test_passes}/{snap.test_runs} pass). Looking good.",
        ))

    return nudges
