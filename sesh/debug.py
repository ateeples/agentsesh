"""Prompt debugger — why did the agent do that?

Extract thinking→action decision points from a session timeline, then
search by thinking text, action target, or dot-notation paths.

The pipeline:
  Source JSONL → build_timeline → extract_decision_points → search/lookup/dotnotes → results
"""

import re
from dataclasses import dataclass, field
from fnmatch import fnmatch

from .parsers.base import Pattern
from .replay import ReplayStep

# Regex for dot-notation paths: two+ segments, each starting with a letter/underscore.
# Rejects version numbers (3.14.1), IPs, file extensions (.py, .ts), sentence-ending dots.
_DOTNOTE_RE = re.compile(
    r'(?<![.\d])'                        # Not preceded by dot or digit
    r'\b'
    r'([a-zA-Z_][a-zA-Z0-9_]*'          # First segment
    r'(?:\.[a-zA-Z_][a-zA-Z0-9_]*)+)'   # Dot + segment, at least once
    r'\b'
)

# Common file extensions to exclude (these look like dotnotes but aren't)
_FILE_EXTENSIONS = frozenset({
    "py", "ts", "js", "tsx", "jsx", "rs", "go", "rb", "java", "kt", "swift",
    "c", "h", "cpp", "hpp", "cs", "md", "json", "yaml", "yml", "toml", "xml",
    "html", "css", "scss", "sql", "sh", "bash", "zsh", "fish",
})


@dataclass
class DecisionPoint:
    """A thinking block paired with the actions it led to.

    Represents a single reasoning→action moment in a session.
    The thinking block contains what the agent was considering,
    and the actions are the tool calls that followed before the
    next thinking block.
    """

    seq: int  # Position in decision point sequence
    thinking: ReplayStep  # The thinking block
    actions: list[ReplayStep] = field(default_factory=list)  # Subsequent tool calls

    @property
    def timestamp(self) -> str | None:
        return self.thinking.timestamp


def extract_decision_points(steps: list[ReplayStep]) -> list[DecisionPoint]:
    """Walk a timeline and pair each thinking block with its subsequent actions.

    A "decision point" is a thinking block followed by zero or more tool calls
    before the next thinking block (or end of timeline). Assistant text and user
    messages between thinking and actions are skipped — only tool_call steps
    count as actions.
    """
    decision_points: list[DecisionPoint] = []
    current: DecisionPoint | None = None
    seq = 0

    for step in steps:
        if step.type == "thinking":
            # Close previous decision point if open
            if current is not None:
                decision_points.append(current)
            # Start new decision point
            current = DecisionPoint(seq=seq, thinking=step)
            seq += 1
        elif step.type == "tool_call" and current is not None:
            current.actions.append(step)

    # Close final decision point
    if current is not None:
        decision_points.append(current)

    return decision_points


def lookup_by_action(
    decision_points: list[DecisionPoint],
    query: str,
) -> list[DecisionPoint]:
    """Reverse lookup: find thinking that led to a given action.

    Searches action summaries, tool names, and tool_input values for the query.
    Returns the parent DecisionPoint (with full thinking context) for each match.
    Case-insensitive substring search. Empty query returns all DPs that have actions.
    """
    if not query:
        return [dp for dp in decision_points if dp.actions]

    query_lower = query.lower()
    results: list[DecisionPoint] = []
    for dp in decision_points:
        for action in dp.actions:
            if _action_matches(action, query_lower):
                results.append(dp)
                break  # One match per DP is enough
    return results


def _action_matches(action, query_lower: str) -> bool:
    """Check if a tool_call ReplayStep matches the query."""
    # Check tool name
    if action.tool_name and query_lower in action.tool_name.lower():
        return True
    # Check summary (contains tool name + path/command)
    if query_lower in action.summary.lower():
        return True
    # Check tool_input values (file_path, command, pattern, etc.)
    if action.tool_input:
        for v in action.tool_input.values():
            if isinstance(v, str) and query_lower in v.lower():
                return True
    return False


def search_thinking(
    decision_points: list[DecisionPoint],
    query: str,
) -> list[DecisionPoint]:
    """Search thinking blocks for query text, return matching decision points.

    Case-insensitive substring search. Empty query returns all decision points.
    """
    if not query:
        return list(decision_points)

    query_lower = query.lower()
    return [
        dp for dp in decision_points
        if query_lower in dp.thinking.detail.lower()
    ]


# --- Dotnotes: dot-notation path indexing ---
# Dotnotes extract structured references from thinking text,
# like "config.database.pool_size" or "auth.middleware.verify".
# This lets you search agent reasoning by concept path rather
# than substring, revealing which parts of the codebase the
# agent was thinking about at each decision point.


def extract_dotnotes(text: str) -> list[str]:
    """Extract dot-notation paths from text.

    Returns deduplicated list of paths like 'config.database.pool_size'.
    Filters out file extensions (auth.py), version numbers, IPs.
    """
    if not text:
        return []

    seen: set[str] = set()
    results: list[str] = []

    for match in _DOTNOTE_RE.finditer(text):
        path = match.group(1)

        # Skip if it looks like a file extension (2-segment, second is known ext)
        parts = path.split(".")
        if len(parts) == 2 and parts[1].lower() in _FILE_EXTENSIONS:
            continue

        if path not in seen:
            seen.add(path)
            results.append(path)

    return results


def index_dotnotes(
    decision_points: list[DecisionPoint],
) -> dict[str, list[DecisionPoint]]:
    """Build an index of dotnote paths → decision points that mention them.

    Returns {path: [dp, ...]} sorted by path.
    """
    index: dict[str, list[DecisionPoint]] = {}

    for dp in decision_points:
        paths = extract_dotnotes(dp.thinking.detail)
        for path in paths:
            if path not in index:
                index[path] = []
            index[path].append(dp)

    return index


def search_dotnotes(
    decision_points: list[DecisionPoint],
    pattern: str,
) -> list[tuple[str, DecisionPoint]]:
    """Search dotnotes with glob-style patterns.

    Pattern examples:
      'auth.*'                → all paths starting with auth.
      'config.database.*'     → all paths under config.database.
      'config.database.pool_size' → exact match
      ''                      → all dotnotes

    Returns list of (path, decision_point) tuples.
    Case-insensitive matching.
    """
    index = index_dotnotes(decision_points)

    if not pattern:
        return [(path, dp) for path, dps in sorted(index.items()) for dp in dps]

    pattern_lower = pattern.lower()
    results: list[tuple[str, DecisionPoint]] = []

    for path, dps in sorted(index.items()):
        if fnmatch(path.lower(), pattern_lower):
            for dp in dps:
                results.append((path, dp))

    return results


# --- Pattern correlation: connect antipatterns to decision points ---
# This bridges two coordinate systems:
# - Patterns use tool_indices (position in flat tool-call list)
# - Decision points use ReplayStep.seq (position in full timeline)
# The correlation maps each detected anti-pattern back to the
# thinking block that caused the problematic behavior.


def correlate_patterns(
    steps: list[ReplayStep],
    decision_points: list[DecisionPoint],
    patterns: list[Pattern],
) -> list[tuple[Pattern, list[DecisionPoint]]]:
    """Map each pattern's tool_indices back to the decision points that caused them.

    Bridges two coordinate systems:
      - Pattern.tool_indices: position in the flat tool-call-only list (0 = first tool call)
      - DecisionPoint.actions: ReplaySteps from the full timeline

    Returns list of (pattern, [decision_points]) for patterns that have correlated DPs.
    Patterns with empty tool_indices or indices pointing to orphan tool calls are excluded.
    """
    if not steps or not decision_points or not patterns:
        return []

    # Build ReplayStep.seq → DP mapping from DP actions
    step_seq_to_dp: dict[int, DecisionPoint] = {}
    for dp in decision_points:
        for action in dp.actions:
            step_seq_to_dp[action.seq] = dp

    # Build tool_call_index → DP mapping by walking timeline
    tool_idx_to_dp: dict[int, DecisionPoint] = {}
    tool_idx = 0
    for step in steps:
        if step.type == "tool_call":
            if step.seq in step_seq_to_dp:
                tool_idx_to_dp[tool_idx] = step_seq_to_dp[step.seq]
            tool_idx += 1

    # Map each pattern to its correlated DPs
    results: list[tuple[Pattern, list[DecisionPoint]]] = []
    for pattern in patterns:
        if not pattern.tool_indices:
            continue

        seen_dp_seqs: set[int] = set()
        matched_dps: list[DecisionPoint] = []
        for ti in pattern.tool_indices:
            dp = tool_idx_to_dp.get(ti)
            if dp is not None and dp.seq not in seen_dp_seqs:
                seen_dp_seqs.add(dp.seq)
                matched_dps.append(dp)

        if matched_dps:
            results.append((pattern, matched_dps))

    return results
