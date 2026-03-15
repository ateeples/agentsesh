"""Session type classification.

Not all sessions are builds. A relay conversation and a feature build
shouldn't be graded on the same scale. This module classifies sessions
by what they're actually doing, so the scorer can evaluate appropriately.

Types:
- BUILD_TESTED: Commits + test runs — the gold standard
- BUILD_UNTESTED: Commits but no tests — shipped without verification
- BUILD_UNCOMMITTED: Significant edits but no commits — unfinished work
- CONVERSATION: Relay, skill invocations, mostly reads — not a build
- WORKSPACE: Editing personal workspace files only (heartbeat, memory, etc.)
- RESEARCH: Exploring codebase — reads/searches dominate
- MINIMAL: Too few tool calls to classify
- MIXED: Doesn't fit other categories
"""

import re
from dataclasses import dataclass

from ..parsers.base import ToolCall

# Files that are personal workspace — committing is optional.
# These patterns match against the file basename or path segments.
_WORKSPACE_FILE_PATTERNS = [
    r"heartbeat\.md$",
    r"SOUL\.md$",
    r"MEMORY\.md$",
    r"north-star\.md$",
    r"decision-journal\.md$",
    r"100-things\.md$",
    r"CLAUDE\.md$",
    r"AGENTS\.md$",
    r"SKILL\.md$",
    r"\.cursorrules$",
    r"-summary\.md$",
    r"-plan\.md$",
    r"-spec\.md$",
    r"thread-summary",
    r"goodnight-summary",
    r"/memory/",
    r"/skills/",
]

_WORKSPACE_RE = re.compile("|".join(_WORKSPACE_FILE_PATTERNS))

# Git commit detection in Bash commands
_GIT_COMMIT_RE = re.compile(r"git\s+commit")
_TEST_CMD_RE = re.compile(
    r"pytest|python[3]?\s+-m\s+pytest|npm\s+test|cargo\s+test|go\s+test|jest|vitest|mocha"
)


@dataclass
class SessionClassification:
    """Result of session type classification."""

    session_type: str
    commit_count: int
    test_run_count: int
    project_edit_count: int
    workspace_edit_count: int
    total_tools: int


def is_workspace_file(filepath: str) -> bool:
    """Check if a file path is a personal workspace file (not project code)."""
    return bool(_WORKSPACE_RE.search(filepath))


def classify_session(tool_calls: list[ToolCall]) -> SessionClassification:
    """Classify a session by what it's doing.

    Examines tool call patterns to determine whether this is a build,
    conversation, research session, etc.

    Args:
        tool_calls: Ordered tool calls from the session.

    Returns:
        SessionClassification with type and supporting counts.
    """
    total = len(tool_calls)

    # Count key signals
    commits = 0
    test_runs = 0
    project_edits = 0
    workspace_edits = 0
    reads = 0
    has_relay = False

    for tc in tool_calls:
        if tc.name == "Bash":
            cmd = tc.input_data.get("command", "")
            if _GIT_COMMIT_RE.search(cmd):
                commits += 1
            if _TEST_CMD_RE.search(cmd):
                test_runs += 1

        elif tc.name in ("Edit", "Write"):
            fp = tc.input_data.get("file_path", "")
            if fp:
                if is_workspace_file(fp):
                    workspace_edits += 1
                else:
                    project_edits += 1

        elif tc.name in ("Read", "Grep", "Glob"):
            reads += 1

    # Check user messages for relay/skill context
    # (handled via events in the full pipeline, but tool_calls
    # carry enough signal via Bash commands and file paths)

    # Classification logic
    bash_count = sum(1 for tc in tool_calls if tc.name == "Bash")

    if total < 10:
        session_type = "MINIMAL"
    elif workspace_edits > 0 and project_edits == 0 and commits == 0:
        session_type = "WORKSPACE"
    elif commits > 0 and test_runs > 0:
        session_type = "BUILD_TESTED"
    elif commits > 0:
        session_type = "BUILD_UNTESTED"
    elif project_edits >= 3:
        # Lowered from 5 — sessions with 3-4 project edits are still builds
        session_type = "BUILD_UNCOMMITTED"
    elif reads > total * 0.4 and project_edits < 3:
        # Read-dominant sessions with minimal edits are research/exploration
        session_type = "RESEARCH"
    elif project_edits == 0 and workspace_edits == 0:
        session_type = "CONVERSATION"
    elif bash_count > total * 0.4 and project_edits < 3:
        # Bash-heavy sessions with minimal edits — scripting/exploration
        session_type = "RESEARCH"
    else:
        session_type = "MIXED"

    return SessionClassification(
        session_type=session_type,
        commit_count=commits,
        test_run_count=test_runs,
        project_edit_count=project_edits,
        workspace_edit_count=workspace_edits,
        total_tools=total,
    )
