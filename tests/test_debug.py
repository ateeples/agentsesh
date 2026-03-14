"""Tests for sesh debug — thinking block extraction, decision points, and search.

Walking skeleton integration tests. These define the contract:
  Source JSONL → timeline → decision points → search → results
"""

import json
import tempfile
from pathlib import Path

import pytest

from sesh.replay import ReplayStep, build_timeline_from_source
from sesh.debug import (
    DecisionPoint, extract_decision_points, search_thinking, lookup_by_action,
    extract_dotnotes, index_dotnotes, search_dotnotes, correlate_patterns,
)
from sesh.parsers.base import Pattern


# --- Fixtures ---

def _jsonl_line(msg_type, content, timestamp="2026-03-13T14:00:00Z"):
    d = {"type": msg_type, "timestamp": timestamp}
    if msg_type == "assistant":
        d["message"] = {"content": content}
    elif msg_type == "user":
        d["message"] = {"content": content}
    return json.dumps(d)


def _write_jsonl(lines):
    f = tempfile.NamedTemporaryFile(suffix=".jsonl", mode="w", delete=False)
    for line in lines:
        f.write(line + "\n")
    f.close()
    return f.name


def _make_steps(*specs):
    """Build ReplayStep list from specs like ("thinking", "text"), ("tool_call", "Read")."""
    steps = []
    for i, (stype, content) in enumerate(specs):
        if stype == "thinking":
            steps.append(ReplayStep(
                seq=i, type="thinking",
                summary=f"[thinking] {len(content)} chars",
                detail=content,
            ))
        elif stype == "tool_call":
            # content can be "Read" or ("Read", "/src/auth.py") for custom path
            if isinstance(content, tuple):
                tool_name, path = content
                summary = f"{tool_name} -> {path}"
                tool_input = {"file_path": path}
            else:
                tool_name = content
                summary = f"{content} -> /some/path"
                tool_input = {"file_path": "/some/path"}
            steps.append(ReplayStep(
                seq=i, type="tool_call",
                tool_name=tool_name,
                summary=summary,
                detail="output",
                tool_input=tool_input,
            ))
        elif stype == "assistant":
            steps.append(ReplayStep(
                seq=i, type="assistant",
                summary=content,
                detail=content,
            ))
        elif stype == "user":
            steps.append(ReplayStep(
                seq=i, type="user",
                summary=content,
                detail=content,
            ))
    return steps


# === DecisionPoint extraction ===

class TestExtractDecisionPoints:
    def test_empty_timeline(self):
        assert extract_decision_points([]) == []

    def test_no_thinking_blocks(self):
        """Timeline with only tool calls produces no decision points."""
        steps = _make_steps(
            ("tool_call", "Read"),
            ("tool_call", "Edit"),
        )
        assert extract_decision_points(steps) == []

    def test_single_thinking_then_action(self):
        """One thinking block followed by one tool call = one decision point."""
        steps = _make_steps(
            ("thinking", "I should read the config file to understand the setup."),
            ("tool_call", "Read"),
        )
        dps = extract_decision_points(steps)
        assert len(dps) == 1
        assert dps[0].thinking.type == "thinking"
        assert "config file" in dps[0].thinking.detail
        assert len(dps[0].actions) == 1
        assert dps[0].actions[0].tool_name == "Read"

    def test_thinking_then_multiple_actions(self):
        """One thinking block followed by multiple tool calls before next thinking."""
        steps = _make_steps(
            ("thinking", "I need to read the file, then edit it."),
            ("tool_call", "Read"),
            ("tool_call", "Edit"),
            ("tool_call", "Bash"),
        )
        dps = extract_decision_points(steps)
        assert len(dps) == 1
        assert len(dps[0].actions) == 3
        assert [a.tool_name for a in dps[0].actions] == ["Read", "Edit", "Bash"]

    def test_multiple_decision_points(self):
        """Multiple thinking→action sequences produce multiple decision points."""
        steps = _make_steps(
            ("thinking", "First I'll read the file."),
            ("tool_call", "Read"),
            ("thinking", "Now I understand the bug. Let me fix it."),
            ("tool_call", "Edit"),
            ("tool_call", "Bash"),
        )
        dps = extract_decision_points(steps)
        assert len(dps) == 2
        assert len(dps[0].actions) == 1
        assert dps[0].actions[0].tool_name == "Read"
        assert len(dps[1].actions) == 2
        assert dps[1].actions[0].tool_name == "Edit"

    def test_thinking_with_no_following_actions(self):
        """Thinking block at the end of timeline (no actions after) still captured."""
        steps = _make_steps(
            ("thinking", "I should check the tests but the session ended."),
        )
        dps = extract_decision_points(steps)
        assert len(dps) == 1
        assert dps[0].actions == []

    def test_interleaved_with_assistant_and_user(self):
        """Assistant text and user messages between thinking and actions are non-action."""
        steps = _make_steps(
            ("thinking", "I need to read auth.py to find the bug."),
            ("assistant", "Let me check the auth module."),
            ("tool_call", "Read"),
            ("user", "Good, what did you find?"),
            ("thinking", "The bug is in the validation logic."),
            ("tool_call", "Edit"),
        )
        dps = extract_decision_points(steps)
        assert len(dps) == 2
        # First DP: thinking → assistant text + Read (actions = only tool calls)
        assert len(dps[0].actions) == 1
        assert dps[0].actions[0].tool_name == "Read"
        # Second DP: thinking → Edit
        assert len(dps[1].actions) == 1
        assert dps[1].actions[0].tool_name == "Edit"

    def test_sequential_numbering(self):
        """Decision points are numbered sequentially."""
        steps = _make_steps(
            ("thinking", "First thought."),
            ("tool_call", "Read"),
            ("thinking", "Second thought."),
            ("tool_call", "Edit"),
            ("thinking", "Third thought."),
            ("tool_call", "Bash"),
        )
        dps = extract_decision_points(steps)
        assert [dp.seq for dp in dps] == [0, 1, 2]

    def test_consecutive_thinking_blocks(self):
        """Two thinking blocks in a row — each becomes its own decision point."""
        steps = _make_steps(
            ("thinking", "Hmm, let me think about this more."),
            ("thinking", "Actually, I should read the file first."),
            ("tool_call", "Read"),
        )
        dps = extract_decision_points(steps)
        assert len(dps) == 2
        # First thinking has no actions (next step is another thinking)
        assert dps[0].actions == []
        # Second thinking has the Read action
        assert len(dps[1].actions) == 1


# === Search ===

class TestSearchThinking:
    def _sample_dps(self):
        """Three decision points with distinct thinking content."""
        steps = _make_steps(
            ("thinking", "I need to read the database configuration to understand connection pooling."),
            ("tool_call", "Read"),
            ("thinking", "The authentication middleware has a bug in token validation."),
            ("tool_call", "Edit"),
            ("thinking", "Let me run the test suite to verify the fix works."),
            ("tool_call", "Bash"),
        )
        return extract_decision_points(steps)

    def test_basic_search(self):
        dps = self._sample_dps()
        results = search_thinking(dps, "database")
        assert len(results) == 1
        assert "database configuration" in results[0].thinking.detail

    def test_search_multiple_matches(self):
        dps = self._sample_dps()
        results = search_thinking(dps, "the")
        # "the" appears in all three thinking blocks
        assert len(results) == 3

    def test_search_no_matches(self):
        dps = self._sample_dps()
        results = search_thinking(dps, "kubernetes")
        assert results == []

    def test_search_case_insensitive(self):
        dps = self._sample_dps()
        results = search_thinking(dps, "DATABASE")
        assert len(results) == 1

    def test_search_returns_actions_context(self):
        """Search results include the actions that followed the thinking."""
        dps = self._sample_dps()
        results = search_thinking(dps, "authentication")
        assert len(results) == 1
        assert results[0].actions[0].tool_name == "Edit"

    def test_empty_query(self):
        dps = self._sample_dps()
        results = search_thinking(dps, "")
        # Empty query returns all decision points
        assert len(results) == 3

    def test_empty_decision_points(self):
        results = search_thinking([], "anything")
        assert results == []


# === Reverse lookup: action → thinking ===

class TestLookupByAction:
    def _sample_dps(self):
        """Three DPs with distinct actions on distinct files."""
        steps = _make_steps(
            ("thinking", "I need to read the database config to understand pooling."),
            ("tool_call", ("Read", "/src/config/database.ts")),
            ("thinking", "The auth middleware has a token validation bug."),
            ("tool_call", ("Edit", "/src/middleware/auth.py")),
            ("thinking", "Let me run the tests to verify."),
            ("tool_call", ("Bash", "pytest")),
        )
        return extract_decision_points(steps)

    def test_lookup_by_file_path(self):
        """Find thinking that led to editing auth.py."""
        dps = self._sample_dps()
        results = lookup_by_action(dps, "auth.py")
        assert len(results) == 1
        assert "token validation" in results[0].thinking.detail

    def test_lookup_by_tool_name(self):
        """Find all DPs where agent used Bash."""
        dps = self._sample_dps()
        results = lookup_by_action(dps, "Bash")
        assert len(results) == 1
        assert "run the tests" in results[0].thinking.detail

    def test_lookup_by_full_path(self):
        """Full path matches."""
        dps = self._sample_dps()
        results = lookup_by_action(dps, "/src/config/database.ts")
        assert len(results) == 1
        assert "database config" in results[0].thinking.detail

    def test_lookup_case_insensitive(self):
        dps = self._sample_dps()
        results = lookup_by_action(dps, "AUTH.PY")
        assert len(results) == 1

    def test_lookup_no_match(self):
        dps = self._sample_dps()
        results = lookup_by_action(dps, "models.py")
        assert results == []

    def test_lookup_empty_query(self):
        """Empty query returns all DPs that have actions."""
        dps = self._sample_dps()
        results = lookup_by_action(dps, "")
        assert len(results) == 3

    def test_lookup_empty_dps(self):
        results = lookup_by_action([], "auth.py")
        assert results == []

    def test_lookup_matches_summary(self):
        """Query matches against the action summary string."""
        dps = self._sample_dps()
        results = lookup_by_action(dps, "Edit -> /src/middleware/auth.py")
        assert len(results) == 1

    def test_lookup_dp_with_no_actions_excluded(self):
        """DPs with no actions are never returned by lookup."""
        steps = _make_steps(
            ("thinking", "I'm not sure what to do."),
            ("thinking", "Actually, let me read the config."),
            ("tool_call", ("Read", "/src/config.ts")),
        )
        dps = extract_decision_points(steps)
        assert len(dps) == 2
        results = lookup_by_action(dps, "config")
        # Only the second DP has actions matching "config"
        assert len(results) == 1
        assert results[0].seq == 1

    def test_lookup_multiple_actions_in_dp(self):
        """When a DP has multiple actions, any match returns the DP."""
        steps = _make_steps(
            ("thinking", "I need to read config, then edit auth."),
            ("tool_call", ("Read", "/src/config.ts")),
            ("tool_call", ("Edit", "/src/auth.py")),
        )
        dps = extract_decision_points(steps)
        # Searching for auth.py should find this DP
        results = lookup_by_action(dps, "auth.py")
        assert len(results) == 1
        assert len(results[0].actions) == 2

    def test_lookup_matches_tool_input_values(self):
        """Query matches against tool_input dict values (command, pattern, etc)."""
        dps = self._sample_dps()
        # "pytest" is in the Bash tool_input, not just summary
        results = lookup_by_action(dps, "pytest")
        assert len(results) == 1
        assert "run the tests" in results[0].thinking.detail


# === Dotnotes: extract and search dot-notation paths from thinking ===

class TestExtractDotnotes:
    """Test extraction of dot-notation paths from thinking text."""

    def test_simple_dotpath(self):
        result = extract_dotnotes("I need to check config.database")
        assert "config.database" in result

    def test_three_segment_path(self):
        result = extract_dotnotes("The config.database.pool_size is too low")
        assert "config.database.pool_size" in result

    def test_multiple_paths(self):
        result = extract_dotnotes("Check req.body.email and res.status.code")
        assert "req.body.email" in result
        assert "res.status.code" in result

    def test_backtick_quoted(self):
        result = extract_dotnotes("The `auth.middleware.token` field is wrong")
        assert "auth.middleware.token" in result

    def test_no_single_word(self):
        """Single words without dots are not dotnotes."""
        result = extract_dotnotes("The middleware handles authentication")
        assert result == []

    def test_ignores_sentence_ending_dots(self):
        """Prose sentence endings are not dotnotes."""
        result = extract_dotnotes("This is a sentence. Another sentence.")
        assert result == []

    def test_ignores_version_numbers(self):
        """Version strings like 3.14.1 are not dotnotes."""
        result = extract_dotnotes("Using Python 3.14.1 and Node 18.2")
        assert result == []

    def test_ignores_ip_addresses(self):
        result = extract_dotnotes("Connect to 192.168.1.1 for the API")
        assert result == []

    def test_ignores_file_extensions(self):
        """auth.py is a file extension, not a dotnote."""
        result = extract_dotnotes("I need to edit auth.py and config.ts")
        assert result == []

    def test_underscore_segments(self):
        result = extract_dotnotes("The pool_config.max_connections value")
        assert "pool_config.max_connections" in result

    def test_mixed_with_noise(self):
        """Extract real paths from messy prose."""
        text = (
            "The user reports 500 errors. I should check req.headers.authorization "
            "and also the session.config.cookie_secure flag. Version 2.1 is deployed."
        )
        result = extract_dotnotes(text)
        assert "req.headers.authorization" in result
        assert "session.config.cookie_secure" in result
        assert len(result) == 2

    def test_empty_text(self):
        assert extract_dotnotes("") == []

    def test_deduplication(self):
        """Same path mentioned twice returns once."""
        result = extract_dotnotes("Check config.db and then config.db again")
        assert result.count("config.db") == 1

    def test_camelcase_segments(self):
        result = extract_dotnotes("The authConfig.tokenExpiry is set to 30m")
        assert "authConfig.tokenExpiry" in result


class TestIndexDotnotes:
    """Test building a dotnote index across decision points."""

    def test_basic_index(self):
        steps = _make_steps(
            ("thinking", "Check config.database.pool_size for the connection issue."),
            ("tool_call", "Read"),
            ("thinking", "The auth.middleware.token validation is wrong."),
            ("tool_call", "Edit"),
        )
        dps = extract_decision_points(steps)
        index = index_dotnotes(dps)
        assert "config.database.pool_size" in index
        assert "auth.middleware.token" in index
        assert len(index["config.database.pool_size"]) == 1
        assert index["config.database.pool_size"][0].seq == 0

    def test_same_path_multiple_dps(self):
        """Same dotnote in multiple thinking blocks maps to multiple DPs."""
        steps = _make_steps(
            ("thinking", "First look at config.database for pool settings."),
            ("tool_call", "Read"),
            ("thinking", "Still investigating config.database — the timeout is wrong."),
            ("tool_call", "Edit"),
        )
        dps = extract_decision_points(steps)
        index = index_dotnotes(dps)
        assert len(index["config.database"]) == 2

    def test_empty_dps(self):
        assert index_dotnotes([]) == {}

    def test_no_dotnotes_in_thinking(self):
        steps = _make_steps(
            ("thinking", "I should read the file and check the logic."),
            ("tool_call", "Read"),
        )
        dps = extract_decision_points(steps)
        assert index_dotnotes(dps) == {}


class TestSearchDotnotes:
    """Test glob-style dotnote search."""

    def _sample_dps(self):
        steps = _make_steps(
            ("thinking", "The config.database.pool_size value is too low."),
            ("tool_call", "Read"),
            ("thinking", "Now checking auth.middleware.token validation."),
            ("tool_call", "Edit"),
            ("thinking", "Also auth.session.cookie needs the secure flag."),
            ("tool_call", "Edit"),
        )
        return extract_decision_points(steps)

    def test_exact_match(self):
        dps = self._sample_dps()
        results = search_dotnotes(dps, "config.database.pool_size")
        assert len(results) == 1
        path, dp = results[0]
        assert path == "config.database.pool_size"
        assert dp.seq == 0

    def test_wildcard_match(self):
        """auth.* matches auth.middleware.token and auth.session.cookie."""
        dps = self._sample_dps()
        results = search_dotnotes(dps, "auth.*")
        assert len(results) >= 2
        paths = [r[0] for r in results]
        assert "auth.middleware.token" in paths
        assert "auth.session.cookie" in paths

    def test_prefix_match(self):
        """config.* matches config.database.pool_size."""
        dps = self._sample_dps()
        results = search_dotnotes(dps, "config.*")
        assert len(results) == 1
        assert results[0][0] == "config.database.pool_size"

    def test_no_match(self):
        dps = self._sample_dps()
        results = search_dotnotes(dps, "redis.*")
        assert results == []

    def test_empty_pattern_returns_all(self):
        dps = self._sample_dps()
        results = search_dotnotes(dps, "")
        assert len(results) == 3  # Three distinct dotnotes

    def test_case_insensitive(self):
        dps = self._sample_dps()
        results = search_dotnotes(dps, "AUTH.*")
        assert len(results) >= 2

    def test_middle_segment_wildcard(self):
        """config.database.* matches config.database.pool_size."""
        dps = self._sample_dps()
        results = search_dotnotes(dps, "config.database.*")
        assert len(results) == 1
        assert results[0][0] == "config.database.pool_size"


# === Pattern correlation: connect antipatterns to decision points ===

class TestCorrelatePatterns:
    """Test mapping pattern tool_indices back to the decision points that caused them."""

    def test_single_pattern_single_dp(self):
        """A pattern touching one tool call maps to the DP that owns it."""
        steps = _make_steps(
            ("thinking", "I should just edit the file without reading it."),
            ("tool_call", ("Edit", "/src/auth.py")),
        )
        dps = extract_decision_points(steps)
        patterns = [Pattern(
            type="write_without_read",
            severity="concern",
            detail="1 edit to unread files: auth.py",
            tool_indices=[0],  # First tool call in timeline
        )]
        results = correlate_patterns(steps, dps, patterns)
        assert len(results) == 1
        pattern, matched_dps = results[0]
        assert pattern.type == "write_without_read"
        assert len(matched_dps) == 1
        assert matched_dps[0].seq == 0

    def test_pattern_spanning_multiple_dps(self):
        """Pattern tool_indices across multiple DPs return all affected DPs."""
        steps = _make_steps(
            ("thinking", "Let me grep for the config."),
            ("tool_call", ("Bash", "grep -r config .")),
            ("thinking", "Now let me grep for the auth."),
            ("tool_call", ("Bash", "grep -r auth .")),
        )
        dps = extract_decision_points(steps)
        patterns = [Pattern(
            type="bash_overuse",
            severity="info",
            detail="2/2 Bash calls used grep",
            tool_indices=[0, 1],
        )]
        results = correlate_patterns(steps, dps, patterns)
        assert len(results) == 1
        _, matched_dps = results[0]
        assert len(matched_dps) == 2
        assert matched_dps[0].seq == 0
        assert matched_dps[1].seq == 1

    def test_multiple_patterns(self):
        """Multiple patterns each map to their respective DPs."""
        steps = _make_steps(
            ("thinking", "Let me edit without reading."),
            ("tool_call", ("Edit", "/src/auth.py")),
            ("thinking", "Let me grep with bash."),
            ("tool_call", ("Bash", "grep -r config .")),
        )
        dps = extract_decision_points(steps)
        patterns = [
            Pattern(type="write_without_read", severity="concern",
                    detail="blind edit", tool_indices=[0]),
            Pattern(type="bash_overuse", severity="info",
                    detail="bash grep", tool_indices=[1]),
        ]
        results = correlate_patterns(steps, dps, patterns)
        assert len(results) == 2
        # First pattern maps to first DP
        assert results[0][1][0].seq == 0
        # Second pattern maps to second DP
        assert results[1][1][0].seq == 1

    def test_pattern_with_no_tool_indices(self):
        """Patterns without tool_indices (like low_read_ratio) have no correlated DPs."""
        steps = _make_steps(
            ("thinking", "I should read."),
            ("tool_call", "Read"),
        )
        dps = extract_decision_points(steps)
        patterns = [Pattern(
            type="low_read_ratio",
            severity="info",
            detail="Read/write ratio: 0.5",
            tool_indices=[],
        )]
        results = correlate_patterns(steps, dps, patterns)
        assert len(results) == 0  # No tool indices to correlate

    def test_orphan_tool_calls_before_thinking(self):
        """Tool calls before any thinking block don't belong to any DP."""
        steps = _make_steps(
            ("tool_call", ("Bash", "cat /etc/hosts")),  # Orphan — no thinking yet
            ("thinking", "Now let me check the config."),
            ("tool_call", ("Read", "/src/config.ts")),
        )
        dps = extract_decision_points(steps)
        # Pattern on the orphan tool call (index 0)
        patterns = [Pattern(
            type="bash_overuse", severity="info",
            detail="cat", tool_indices=[0],
        )]
        results = correlate_patterns(steps, dps, patterns)
        # Orphan tool call doesn't belong to any DP
        assert len(results) == 0

    def test_error_streak_across_dps(self):
        """Error streak pattern spanning actions in different DPs."""
        steps = _make_steps(
            ("thinking", "First attempt at fixing."),
            ("tool_call", ("Bash", "make build")),
            ("tool_call", ("Bash", "make build")),
            ("thinking", "Still failing, try different approach."),
            ("tool_call", ("Bash", "make build")),
        )
        # Mark all as errors
        for step in steps:
            if step.type == "tool_call":
                step.is_error = True
        dps = extract_decision_points(steps)
        patterns = [Pattern(
            type="error_streak", severity="warning",
            detail="3 consecutive errors",
            tool_indices=[0, 1, 2],
        )]
        results = correlate_patterns(steps, dps, patterns)
        assert len(results) == 1
        _, matched_dps = results[0]
        # Both DPs are involved
        assert len(matched_dps) == 2

    def test_empty_patterns(self):
        steps = _make_steps(
            ("thinking", "Something."),
            ("tool_call", "Read"),
        )
        dps = extract_decision_points(steps)
        assert correlate_patterns(steps, dps, []) == []

    def test_empty_dps(self):
        patterns = [Pattern(type="x", severity="info", detail="x", tool_indices=[0])]
        assert correlate_patterns([], [], patterns) == []

    def test_deduplicates_dps(self):
        """If multiple tool_indices in same pattern point to same DP, don't duplicate."""
        steps = _make_steps(
            ("thinking", "Read sequentially instead of parallel."),
            ("tool_call", ("Read", "/src/a.ts")),
            ("tool_call", ("Read", "/src/b.ts")),
        )
        dps = extract_decision_points(steps)
        patterns = [Pattern(
            type="missed_parallelism", severity="info",
            detail="sequential reads", tool_indices=[0, 1],
        )]
        results = correlate_patterns(steps, dps, patterns)
        assert len(results) == 1
        _, matched_dps = results[0]
        # Both tool calls are in the same DP — should only appear once
        assert len(matched_dps) == 1


# === End-to-end integration: JSONL → timeline → decision points → search ===

class TestEndToEnd:
    def test_full_pipeline(self):
        """Source JSONL → build_timeline → extract_decision_points → search."""
        tool_id = "t1"
        lines = [
            _jsonl_line("user", "Why is the API returning 500?"),
            _jsonl_line("assistant", [
                {"type": "thinking", "thinking": "The user reports a 500 error on the API. I should check the error handler middleware and the route definitions."},
                {"type": "text", "text": "Let me investigate the API error."},
                {"type": "tool_use", "name": "Read", "id": tool_id,
                 "input": {"file_path": "/src/middleware/error.ts"}},
            ], timestamp="2026-03-13T14:00:01Z"),
            _jsonl_line("user", [
                {"type": "tool_result", "tool_use_id": tool_id,
                 "content": "export function errorHandler(err, req, res, next) { ... }"},
            ]),
        ]
        path = _write_jsonl(lines)
        try:
            steps = build_timeline_from_source(path)
            dps = extract_decision_points(steps)
            assert len(dps) == 1

            # Search for what the agent was thinking about
            results = search_thinking(dps, "error handler")
            assert len(results) == 1
            assert results[0].actions[0].tool_name == "Read"

            # Search for something not in thinking
            results = search_thinking(dps, "database migration")
            assert results == []
        finally:
            Path(path).unlink()

    def test_multi_step_investigation(self):
        """Agent thinks multiple times during a debugging session."""
        t1, t2 = "t1", "t2"
        lines = [
            _jsonl_line("user", "The login page is broken"),
            _jsonl_line("assistant", [
                {"type": "thinking", "thinking": "Login page broken. I should check the auth controller and the session store configuration."},
                {"type": "tool_use", "name": "Read", "id": t1,
                 "input": {"file_path": "/src/auth/controller.ts"}},
            ], timestamp="2026-03-13T14:00:01Z"),
            _jsonl_line("user", [
                {"type": "tool_result", "tool_use_id": t1,
                 "content": "class AuthController { login() { ... } }"},
            ]),
            _jsonl_line("assistant", [
                {"type": "thinking", "thinking": "The controller looks fine. The issue must be in the session middleware — the cookie settings might have changed after the security update."},
                {"type": "tool_use", "name": "Read", "id": t2,
                 "input": {"file_path": "/src/middleware/session.ts"}},
            ], timestamp="2026-03-13T14:00:03Z"),
            _jsonl_line("user", [
                {"type": "tool_result", "tool_use_id": t2,
                 "content": "export const sessionConfig = { cookie: { secure: true } }"},
            ]),
        ]
        path = _write_jsonl(lines)
        try:
            steps = build_timeline_from_source(path)
            dps = extract_decision_points(steps)
            assert len(dps) == 2

            # "Why did the agent look at session middleware?"
            results = search_thinking(dps, "session middleware")
            assert len(results) == 1
            assert results[0].actions[0].tool_name == "Read"
            assert "session.ts" in results[0].actions[0].summary

            # "What was the agent thinking about cookies?"
            results = search_thinking(dps, "cookie")
            assert len(results) == 1
            assert results[0].seq == 1  # Second decision point
        finally:
            Path(path).unlink()

    def test_reverse_lookup_e2e(self):
        """Reverse lookup: 'why did you edit auth.py?' → find the thinking."""
        t1, t2 = "t1", "t2"
        lines = [
            _jsonl_line("user", "Fix the login bug"),
            _jsonl_line("assistant", [
                {"type": "thinking", "thinking": "I should check the auth controller to understand the login flow."},
                {"type": "tool_use", "name": "Read", "id": t1,
                 "input": {"file_path": "/src/auth/controller.ts"}},
            ], timestamp="2026-03-13T14:00:01Z"),
            _jsonl_line("user", [
                {"type": "tool_result", "tool_use_id": t1,
                 "content": "class AuthController { login() { ... } }"},
            ]),
            _jsonl_line("assistant", [
                {"type": "thinking", "thinking": "Found the bug — the token expiry check uses <= instead of <. Need to fix auth.py."},
                {"type": "tool_use", "name": "Edit", "id": t2,
                 "input": {"file_path": "/src/auth/auth.py"}},
            ], timestamp="2026-03-13T14:00:03Z"),
            _jsonl_line("user", [
                {"type": "tool_result", "tool_use_id": t2,
                 "content": "File edited successfully."},
            ]),
        ]
        path = _write_jsonl(lines)
        try:
            steps = build_timeline_from_source(path)
            dps = extract_decision_points(steps)

            # "Why did you edit auth.py?"
            results = lookup_by_action(dps, "auth.py")
            assert len(results) == 1  # Only second DP edits auth.py
            assert "token expiry" in results[0].thinking.detail

            # Broader search: "auth" matches both DPs (controller.ts and auth.py both under /auth/)
            results = lookup_by_action(dps, "/auth/")
            assert len(results) == 2

            # "Why did you edit?" (tool name match)
            results = lookup_by_action(dps, "Edit")
            assert len(results) == 1
            assert "token expiry" in results[0].thinking.detail
        finally:
            Path(path).unlink()

    def test_dotnotes_e2e(self):
        """Dotnotes: JSONL → timeline → decision points → extract + search dotnotes."""
        t1, t2 = "t1", "t2"
        lines = [
            _jsonl_line("user", "The API pool is exhausting"),
            _jsonl_line("assistant", [
                {"type": "thinking", "thinking": "I should check the config.database.pool_size setting and see if it matches the expected connection limit."},
                {"type": "tool_use", "name": "Read", "id": t1,
                 "input": {"file_path": "/src/config.ts"}},
            ], timestamp="2026-03-13T14:00:01Z"),
            _jsonl_line("user", [
                {"type": "tool_result", "tool_use_id": t1,
                 "content": "export const config = { database: { pool_size: 5 } }"},
            ]),
            _jsonl_line("assistant", [
                {"type": "thinking", "thinking": "Pool size is 5 — too low. Also the config.database.timeout is 30s which might cause stacking. Let me also check auth.session.max_age."},
                {"type": "tool_use", "name": "Edit", "id": t2,
                 "input": {"file_path": "/src/config.ts"}},
            ], timestamp="2026-03-13T14:00:03Z"),
            _jsonl_line("user", [
                {"type": "tool_result", "tool_use_id": t2,
                 "content": "File edited."},
            ]),
        ]
        path = _write_jsonl(lines)
        try:
            steps = build_timeline_from_source(path)
            dps = extract_decision_points(steps)

            # Build index
            index = index_dotnotes(dps)
            assert "config.database.pool_size" in index
            assert "config.database.timeout" in index
            assert "auth.session.max_age" in index
            # pool_size in first DP only, timeout in second DP only
            assert len(index["config.database.pool_size"]) == 1
            assert len(index["config.database.timeout"]) == 1

            # Search: "what did the agent think about config.database.*?"
            results = search_dotnotes(dps, "config.database.*")
            paths = [r[0] for r in results]
            assert "config.database.pool_size" in paths
            assert "config.database.timeout" in paths

            # Search: "auth.*" finds only auth.session.max_age
            results = search_dotnotes(dps, "auth.*")
            assert len(results) == 1
            assert results[0][0] == "auth.session.max_age"
        finally:
            Path(path).unlink()

    def test_pattern_correlation_e2e(self):
        """Pattern correlation: JSONL → timeline → DPs + patterns → correlation."""
        t1, t2 = "t1", "t2"
        lines = [
            _jsonl_line("user", "Fix the auth module"),
            _jsonl_line("assistant", [
                {"type": "thinking", "thinking": "I'll just edit auth.py directly, should be fine."},
                {"type": "tool_use", "name": "Edit", "id": t1,
                 "input": {"file_path": "/src/auth.py"}},
            ], timestamp="2026-03-13T14:00:01Z"),
            _jsonl_line("user", [
                {"type": "tool_result", "tool_use_id": t1,
                 "content": "File edited."},
            ]),
            _jsonl_line("assistant", [
                {"type": "thinking", "thinking": "Let me also cat the config to check."},
                {"type": "tool_use", "name": "Bash", "id": t2,
                 "input": {"command": "cat /src/config.ts"}},
            ], timestamp="2026-03-13T14:00:03Z"),
            _jsonl_line("user", [
                {"type": "tool_result", "tool_use_id": t2,
                 "content": "export const config = {}"},
            ]),
        ]
        path = _write_jsonl(lines)
        try:
            steps = build_timeline_from_source(path)
            dps = extract_decision_points(steps)

            # Simulate patterns that a real detector would produce
            patterns = [
                Pattern(type="write_without_read", severity="concern",
                        detail="1 edit to unread files: auth.py", tool_indices=[0]),
                Pattern(type="bash_overuse", severity="info",
                        detail="1/1 Bash calls used cat", tool_indices=[1]),
            ]
            results = correlate_patterns(steps, dps, patterns)
            assert len(results) == 2

            # Blind edit → DP 0 ("I'll just edit auth.py directly")
            assert results[0][0].type == "write_without_read"
            assert results[0][1][0].seq == 0
            assert "edit auth.py directly" in results[0][1][0].thinking.detail

            # Bash overuse → DP 1 ("Let me also cat the config")
            assert results[1][0].type == "bash_overuse"
            assert results[1][1][0].seq == 1
            assert "cat the config" in results[1][1][0].thinking.detail
        finally:
            Path(path).unlink()
