"""Core types and parser protocol for sesh."""

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ToolCall:
    """A single tool invocation within a session."""

    name: str
    tool_id: str
    input_data: dict
    output_preview: str
    output_length: int
    is_error: bool
    timestamp: str | None = None
    categories: list[str] = field(default_factory=list)
    seq: int = 0  # Order within session
    output_tail: str = ""  # Last 300 chars of output (test summaries live here)


@dataclass
class Event:
    """A non-tool event in a session (user message, assistant text, thinking)."""

    type: str  # "user_message", "assistant_text", "thinking"
    length: int
    preview: str = ""
    timestamp: str | None = None


@dataclass
class Pattern:
    """A detected behavioral antipattern."""

    type: str
    severity: str  # "info", "warning", "concern"
    detail: str
    tool_indices: list[int] = field(default_factory=list)


@dataclass
class SessionGrade:
    """Process quality assessment for a session."""

    grade: str  # "A+", "A", "B", "C", "D", "F"
    score: int  # 0-100
    deductions: list[str] = field(default_factory=list)
    bonuses: list[str] = field(default_factory=list)


@dataclass
class NormalizedSession:
    """Canonical internal representation of a session, regardless of source format."""

    session_id: str
    source_format: str  # "claude_code", "openai", "generic"
    source_path: str
    start_time: str | None = None
    end_time: str | None = None
    duration_minutes: float | None = None
    model: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    raw_text: str = ""
    metadata: dict = field(default_factory=dict)


# Tool classification map — maps behavioral roles to tool names.
# A tool can belong to multiple categories (e.g., Grep is both read and search).
# Used by pattern detectors to reason about tool call sequences.
TOOL_CATEGORIES: dict[str, set[str]] = {
    "read": {"Read", "Grep", "Glob", "Bash"},
    "write": {"Write", "Edit", "NotebookEdit"},
    "search": {"Grep", "Glob", "ToolSearch", "WebSearch", "WebFetch"},
    "meta": {"ToolSearch", "AskUserQuestion", "Agent", "Skill"},
}


def classify_tool(name: str, custom_categories: dict[str, list[str]] | None = None) -> list[str]:
    """Classify a tool into behavioral categories.

    A tool can belong to multiple categories. Unknown tools get ["other"].
    Custom categories from config are merged with defaults.
    """
    merged = {k: set(v) for k, v in TOOL_CATEGORIES.items()}
    if custom_categories:
        for cat, tools in custom_categories.items():
            if cat in merged:
                merged[cat].update(tools)
            else:
                merged[cat] = set(tools)

    categories = []
    for cat, tools in merged.items():
        if name in tools:
            categories.append(cat)
    return categories or ["other"]


class BaseParser:
    """Protocol for transcript parsers.

    Each parser must implement:
    - format_name: str identifying the format
    - can_parse(path): whether this parser handles the file
    - parse(path): produce a NormalizedSession
    """

    format_name: str = ""

    @staticmethod
    def can_parse(file_path: Path) -> bool:
        """Return True if this parser can handle the file."""
        raise NotImplementedError

    @staticmethod
    def parse(file_path: Path) -> NormalizedSession:
        """Parse transcript file into NormalizedSession."""
        raise NotImplementedError
