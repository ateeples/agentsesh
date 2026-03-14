"""Tests for the remediation engine.

Covers: pattern-to-remediation mapping, severity classification,
CLAUDE.md patch generation, and human-readable formatting.
"""

import pytest
from sesh.analyzers.remediation import (
    get_remediation,
    get_all_remediations,
    format_remediations,
    generate_claude_md_patch,
    Remediation,
)


# --- Pattern-to-remediation mapping ---


# --- Pattern type → Remediation lookup ---


class TestGetRemediation:
    def test_known_pattern_types(self):
        known = [
            "repeated_search",
            "write_without_read",
            "error_rate",
            "error_streak",
            "low_read_ratio",
            "bash_overuse",
            "write_then_read",
            "scattered_files",
            "missed_parallelism",
        ]
        for ptype in known:
            rem = get_remediation(ptype)
            assert rem is not None, f"No remediation for {ptype}"
            assert rem.pattern_type == ptype
            assert rem.title
            assert rem.severity in ("critical", "recommended", "optional")
            assert rem.description
            assert len(rem.actions) > 0

    def test_unknown_pattern_returns_none(self):
        assert get_remediation("nonexistent_pattern") is None

    def test_detail_passthrough(self):
        rem = get_remediation("bash_overuse", "5/8 Bash calls used cat/grep")
        assert rem is not None
        assert rem.pattern_type == "bash_overuse"

    def test_write_without_read_is_critical(self):
        rem = get_remediation("write_without_read")
        assert rem.severity == "critical"

    def test_error_streak_is_critical(self):
        rem = get_remediation("error_streak")
        assert rem.severity == "critical"

    def test_scattered_files_is_optional(self):
        rem = get_remediation("scattered_files")
        assert rem.severity == "optional"


class TestGetAllRemediations:
    def test_empty_patterns(self):
        assert get_all_remediations([]) == []

    def test_deduplicates_by_type(self):
        patterns = [
            {"type": "bash_overuse", "detail": "first"},
            {"type": "bash_overuse", "detail": "second"},
        ]
        rems = get_all_remediations(patterns)
        assert len(rems) == 1

    def test_sorted_by_severity(self):
        patterns = [
            {"type": "bash_overuse", "detail": ""},        # recommended
            {"type": "write_without_read", "detail": ""},  # critical
            {"type": "scattered_files", "detail": ""},     # optional
        ]
        rems = get_all_remediations(patterns)
        assert rems[0].severity == "critical"
        assert rems[-1].severity == "optional"

    def test_handles_pattern_type_key_variant(self):
        """DB returns 'type', but support 'pattern_type' as fallback."""
        patterns = [{"pattern_type": "error_streak", "detail": "3 consecutive"}]
        rems = get_all_remediations(patterns)
        assert len(rems) == 1
        assert rems[0].pattern_type == "error_streak"

    def test_multiple_patterns(self):
        patterns = [
            {"type": "write_without_read", "detail": "2 blind edits"},
            {"type": "bash_overuse", "detail": "5/8 calls"},
            {"type": "missed_parallelism", "detail": "6 pairs"},
        ]
        rems = get_all_remediations(patterns)
        assert len(rems) == 3


class TestFormatRemediations:
    def test_empty_remediations(self):
        result = format_remediations([])
        assert "Clean session" in result

    def test_includes_title_and_actions(self):
        rem = get_remediation("bash_overuse")
        result = format_remediations([rem])
        assert "dedicated tools" in result.lower() or "Bash" in result
        assert "Actions:" in result

    def test_includes_snippets_when_requested(self):
        rem = get_remediation("bash_overuse")
        with_snippets = format_remediations([rem], include_snippets=True)
        without_snippets = format_remediations([rem], include_snippets=False)
        assert "CLAUDE.md" in with_snippets
        assert len(with_snippets) > len(without_snippets)

    def test_severity_icons(self):
        patterns = [
            {"type": "error_streak", "detail": ""},     # critical -> !!!
            {"type": "bash_overuse", "detail": ""},      # recommended -> !!
            {"type": "scattered_files", "detail": ""},   # optional -> -
        ]
        rems = get_all_remediations(patterns)
        result = format_remediations(rems)
        assert "!!!" in result
        assert " !!" in result


class TestGenerateClaudeMdPatch:
    def test_empty_remediations(self):
        assert generate_claude_md_patch([]) == ""

    def test_generates_valid_patch(self):
        patterns = [
            {"type": "write_without_read", "detail": ""},
            {"type": "bash_overuse", "detail": ""},
        ]
        rems = get_all_remediations(patterns)
        patch = generate_claude_md_patch(rems)
        assert "# Process Rules" in patch
        assert "auto-generated by sesh" in patch
        assert "Read" in patch  # from read-before-write rule

    def test_no_snippets_returns_empty(self):
        """Remediation without claude_md_snippet should not appear in patch."""
        rem = Remediation(
            pattern_type="test",
            title="Test",
            severity="optional",
            description="Test",
            actions=["Do something"],
            claude_md_snippet=None,
        )
        assert generate_claude_md_patch([rem]) == ""

    def test_combines_multiple_snippets(self):
        patterns = [
            {"type": "write_without_read", "detail": ""},
            {"type": "bash_overuse", "detail": ""},
            {"type": "error_streak", "detail": ""},
        ]
        rems = get_all_remediations(patterns)
        patch = generate_claude_md_patch(rems)
        # Should have all three sections
        assert "Read-before-write" in patch or "read" in patch.lower()
        assert "Tool usage" in patch or "Bash" in patch
        assert "Error recovery" in patch or "error" in patch.lower()
