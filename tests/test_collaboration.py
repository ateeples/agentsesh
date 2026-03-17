"""Tests for collaboration analysis — human-AI partnership grading.

Tests validate:
1. Human turn extraction from JSONL
2. Signal detection (corrections, affirmations, delegation)
3. Collaboration score computation
4. Archetype classification
5. Conversation arc analysis
6. Formatting
7. Integration with analyze_session pipeline
"""

import json

import pytest

from sesh.analyzers.collaboration import (
    CollaborationAnalysis,
    ConversationArc,
    HumanTurn,
    _analyze_arc,
    _classify_archetype,
    _compute_score,
    _grade_from_score,
    analyze_collaboration,
    extract_human_turns,
    format_collaboration,
)
from sesh.parsers.base import ToolCall


def _tc(name: str, seq: int = 0, input_data: dict | None = None,
        is_error: bool = False, output_preview: str = "") -> ToolCall:
    """Helper to create a ToolCall."""
    return ToolCall(
        name=name,
        tool_id=f"tool_{seq}",
        input_data=input_data or {},
        output_preview=output_preview,
        output_length=len(output_preview),
        is_error=is_error,
        seq=seq,
    )


def _write_session(tmp_path, turns: list[dict], agent_turns: int = 10) -> str:
    """Write a minimal JSONL session with given human turns and agent responses.

    Args:
        tmp_path: pytest tmp_path fixture
        turns: list of dicts with "text" and optional "timestamp" keys
        agent_turns: number of agent tool_use turns to add

    Returns:
        Path to the written JSONL file.
    """
    path = tmp_path / "test_session.jsonl"
    lines = []

    for i, turn in enumerate(turns):
        text = turn.get("text", "")
        ts = turn.get("timestamp", f"2026-03-14T{10 + i:02d}:00:00Z")

        # User turn with text
        user_record = {
            "type": "user",
            "timestamp": ts,
            "message": {
                "content": [
                    {"type": "text", "text": text},
                ]
            }
        }
        lines.append(json.dumps(user_record))

        # Agent response with tool calls
        for j in range(max(1, agent_turns // max(len(turns), 1))):
            tool_id = f"tool_{i}_{j}"
            agent_record = {
                "type": "assistant",
                "timestamp": ts,
                "message": {
                    "model": "claude-sonnet-4-20250514",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": tool_id,
                            "name": "Read",
                            "input": {"file_path": f"/src/file_{i}_{j}.py"},
                        }
                    ]
                }
            }
            lines.append(json.dumps(agent_record))

            # Tool result
            result_record = {
                "type": "user",
                "timestamp": ts,
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_id,
                            "content": "file contents here",
                        }
                    ]
                }
            }
            lines.append(json.dumps(result_record))

    path.write_text("\n".join(lines))
    return path


def _make_turns(texts: list[str]) -> list[HumanTurn]:
    """Create HumanTurn objects from text strings."""
    turns = []
    for text in texts:
        lower = text.lower()
        from sesh.analyzers.collaboration import (
            AFFIRMATION_PATTERNS,
            CORRECTION_PATTERNS,
            DELEGATION_PATTERNS,
        )
        turns.append(HumanTurn(
            text=text,
            word_count=len(text.split()),
            is_correction=any(p.search(lower) for p in CORRECTION_PATTERNS),
            is_affirmation=any(p.search(lower) for p in AFFIRMATION_PATTERNS),
            is_delegation=any(p.search(lower) for p in DELEGATION_PATTERNS),
        ))
    return turns


# === Human Turn Extraction ===


class TestExtractHumanTurns:
    def test_basic_extraction(self, tmp_path):
        """Extracts text from user turns."""
        path = _write_session(tmp_path, [
            {"text": "Fix the auth bug"},
            {"text": "Great, now add tests"},
        ])
        turns = extract_human_turns(path)
        assert len(turns) == 2
        assert turns[0].text == "Fix the auth bug"
        assert turns[1].text == "Great, now add tests"

    def test_word_count(self, tmp_path):
        """Counts words correctly."""
        path = _write_session(tmp_path, [
            {"text": "one two three four five"},
        ])
        turns = extract_human_turns(path)
        assert turns[0].word_count == 5

    def test_strips_system_reminders(self, tmp_path):
        """Strips system-reminder tags from human turns."""
        path = tmp_path / "session.jsonl"
        record = {
            "type": "user",
            "timestamp": "2026-03-14T10:00:00Z",
            "message": {
                "content": [
                    {"type": "text", "text": "Fix the bug <system-reminder>noise here</system-reminder> please"},
                ]
            }
        }
        path.write_text(json.dumps(record))
        turns = extract_human_turns(path)
        assert len(turns) == 1
        assert "system-reminder" not in turns[0].text
        assert "noise" not in turns[0].text
        assert "Fix the bug" in turns[0].text

    def test_skips_tool_results(self, tmp_path):
        """Tool results in user turns are not extracted as human text."""
        path = tmp_path / "session.jsonl"
        record = {
            "type": "user",
            "timestamp": "2026-03-14T10:00:00Z",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "result data"},
                    {"type": "text", "text": "Now fix this"},
                ]
            }
        }
        path.write_text(json.dumps(record))
        turns = extract_human_turns(path)
        assert len(turns) == 1
        assert turns[0].text == "Now fix this"
        assert "result data" not in turns[0].text

    def test_strips_skill_expansions_with_args(self, tmp_path):
        """Skill expansions are stripped, keeping only the user's ARGUMENTS."""
        path = tmp_path / "session.jsonl"
        skill_text = (
            "Base directory for this skill: /path/to/skill\n\n"
            "# My Skill\n\nThis is a long skill prompt with instructions "
            "that goes on for many lines and hundreds of words.\n\n"
            "ARGUMENTS: implement step 5 of the plan"
        )
        record = {
            "type": "user",
            "timestamp": "2026-03-14T10:00:00Z",
            "message": {"content": [{"type": "text", "text": skill_text}]}
        }
        path.write_text(json.dumps(record))
        turns = extract_human_turns(path)
        assert len(turns) == 1
        assert "implement step 5" in turns[0].text
        assert "Base directory" not in turns[0].text
        assert "long skill prompt" not in turns[0].text
        assert turns[0].word_count < 20  # Just the args, not the 500+ word skill

    def test_strips_skill_expansions_without_args(self, tmp_path):
        """Skill expansions with no ARGUMENTS produce empty turns (skipped)."""
        path = tmp_path / "session.jsonl"
        skill_text = (
            "Base directory for this skill: /path/to/skill\n\n"
            "# Goodnight\n\nYou're going to sleep. This is how you tuck yourself in."
        )
        record = {
            "type": "user",
            "timestamp": "2026-03-14T10:00:00Z",
            "message": {"content": [{"type": "text", "text": skill_text}]}
        }
        path.write_text(json.dumps(record))
        turns = extract_human_turns(path)
        # No ARGUMENTS = empty after stripping = skipped
        assert len(turns) == 0

    def test_skips_empty_turns(self, tmp_path):
        """Empty or trivial text is skipped."""
        path = tmp_path / "session.jsonl"
        records = [
            {"type": "user", "timestamp": "t1", "message": {"content": [{"type": "text", "text": ""}]}},
            {"type": "user", "timestamp": "t2", "message": {"content": [{"type": "text", "text": "x"}]}},
            {"type": "user", "timestamp": "t3", "message": {"content": [{"type": "text", "text": "Do the thing"}]}},
        ]
        path.write_text("\n".join(json.dumps(r) for r in records))
        turns = extract_human_turns(path)
        assert len(turns) == 1
        assert turns[0].text == "Do the thing"

    def test_skips_tool_result_only_turns(self, tmp_path):
        """User turns with only tool_result (no text) are skipped."""
        path = tmp_path / "session.jsonl"
        record = {
            "type": "user",
            "timestamp": "2026-03-14T10:00:00Z",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "file contents"},
                ]
            }
        }
        path.write_text(json.dumps(record))
        turns = extract_human_turns(path)
        assert len(turns) == 0

    def test_preserves_timestamp(self, tmp_path):
        """Timestamps are preserved on human turns."""
        path = _write_session(tmp_path, [
            {"text": "Do something", "timestamp": "2026-03-14T10:30:00Z"},
        ])
        turns = extract_human_turns(path)
        assert turns[0].timestamp == "2026-03-14T10:30:00Z"


# === Signal Detection ===


class TestSignalDetection:
    def test_correction_no(self, tmp_path):
        path = _write_session(tmp_path, [{"text": "No, not that approach"}])
        turns = extract_human_turns(path)
        assert turns[0].is_correction

    def test_correction_instead(self, tmp_path):
        path = _write_session(tmp_path, [{"text": "Use X instead of Y"}])
        turns = extract_human_turns(path)
        assert turns[0].is_correction

    def test_correction_dont(self, tmp_path):
        path = _write_session(tmp_path, [{"text": "Don't do that"}])
        turns = extract_human_turns(path)
        assert turns[0].is_correction

    def test_correction_wait(self, tmp_path):
        path = _write_session(tmp_path, [{"text": "Wait, let me think"}])
        turns = extract_human_turns(path)
        assert turns[0].is_correction

    def test_affirmation_great(self, tmp_path):
        path = _write_session(tmp_path, [{"text": "Great work on that"}])
        turns = extract_human_turns(path)
        assert turns[0].is_affirmation

    def test_affirmation_ship_it(self, tmp_path):
        path = _write_session(tmp_path, [{"text": "Ship it"}])
        turns = extract_human_turns(path)
        assert turns[0].is_affirmation

    def test_affirmation_lgtm(self, tmp_path):
        path = _write_session(tmp_path, [{"text": "LGTM, merge it"}])
        turns = extract_human_turns(path)
        assert turns[0].is_affirmation

    def test_delegation_you_decide(self, tmp_path):
        path = _write_session(tmp_path, [{"text": "You decide the approach"}])
        turns = extract_human_turns(path)
        assert turns[0].is_delegation

    def test_delegation_keep_going(self, tmp_path):
        path = _write_session(tmp_path, [{"text": "Keep going"}])
        turns = extract_human_turns(path)
        assert turns[0].is_delegation

    def test_delegation_whats_next(self, tmp_path):
        path = _write_session(tmp_path, [{"text": "What's next"}])
        turns = extract_human_turns(path)
        assert turns[0].is_delegation

    def test_no_signal_on_neutral(self, tmp_path):
        path = _write_session(tmp_path, [{"text": "Implement the login endpoint"}])
        turns = extract_human_turns(path)
        assert not turns[0].is_correction
        assert not turns[0].is_affirmation
        assert not turns[0].is_delegation

    def test_multiple_signals(self, tmp_path):
        """A turn can have both correction and affirmation."""
        path = _write_session(tmp_path, [{"text": "Great start but actually use the other approach instead"}])
        turns = extract_human_turns(path)
        assert turns[0].is_correction  # "actually", "instead"
        assert turns[0].is_affirmation  # "great"


# === Score Computation ===


class TestComputeScore:
    def test_single_turn_returns_none(self):
        turns = _make_turns(["Do the thing"])
        score = _compute_score(turns, 0, 0, 0, 10.0)
        assert score is None

    def test_high_engagement_scores_well(self):
        """Sessions with corrections + affirmations score higher."""
        turns = _make_turns([
            "Fix the auth bug",
            "No, use JWT instead",
            "Great, now add tests",
            "Perfect, keep going",
            "Ship it",
        ])
        corrections = sum(1 for t in turns if t.is_correction)
        affirmations = sum(1 for t in turns if t.is_affirmation)
        delegations = sum(1 for t in turns if t.is_delegation)
        score = _compute_score(turns, corrections, affirmations, delegations, 10.0)
        assert score is not None
        assert score >= 70

    def test_zero_engagement_penalized(self):
        """Sessions with no corrections or affirmations score lower."""
        turns = _make_turns([
            "Implement feature X with these specifications",
            "Add the database schema",
            "Create the API endpoint",
        ])
        score = _compute_score(turns, 0, 0, 0, 10.0)
        assert score is not None
        assert score < 60

    def test_very_long_prompts_penalized(self):
        """Average word count > 500 gets penalized."""
        long_text = " ".join(["word"] * 600)
        turns = _make_turns([long_text, long_text])
        score = _compute_score(turns, 0, 0, 0, 10.0)
        assert score is not None
        # Should be penalized for length AND zero engagement
        assert score < 50

    def test_low_autonomy_penalized(self):
        """Very low tc_per_turn (micromanaging) is penalized."""
        turns = _make_turns(["Do X", "Now do Y", "Check Z", "Fix W", "Test it", "Ship"])
        score_low = _compute_score(turns, 1, 1, 0, 1.0)  # Very low autonomy
        score_good = _compute_score(turns, 1, 1, 0, 10.0)  # Good autonomy
        assert score_low < score_good

    def test_score_clamped_to_0_100(self):
        """Score cannot go below 0 or above 100."""
        # Worst case: no engagement, very short, bad autonomy, long prompts
        long_text = " ".join(["word"] * 600)
        turns = _make_turns([long_text, long_text])
        score = _compute_score(turns, 0, 0, 0, 1.0)
        assert score >= 0

    def test_many_turns_get_bonus(self):
        """Sessions with 10+ turns get extra bonus."""
        texts = ["Do thing"] * 12
        turns = _make_turns(texts)
        score = _compute_score(turns, 2, 2, 1, 10.0)
        assert score is not None
        assert score >= 70  # Should get turn count bonuses


# === Archetype Classification ===


class TestClassifyArchetype:
    def test_spec_dump(self):
        """Long prompts + no feedback = Spec Dump."""
        long_text = " ".join(["word"] * 400)
        turns = _make_turns([long_text, long_text, long_text])
        archetype = _classify_archetype(turns, 1, 0, 0, 15.0)
        assert archetype == "The Spec Dump"

    def test_micromanager(self):
        """Low autonomy + many turns = Micromanager."""
        turns = _make_turns(["check", "ok now?", "what about", "hmm", "fix it", "done?"])
        archetype = _classify_archetype(turns, 1, 1, 0, 2.0)
        assert archetype == "The Micromanager"

    def test_partnership(self):
        """Corrections + affirmations + delegation = Partnership."""
        turns = _make_turns([
            "Fix the auth bug",
            "No, use JWT instead",
            "Actually, the other way",
            "Great work",
            "Perfect, keep going",
            "Awesome",
            "You decide on the tests",
        ])
        corrections = sum(1 for t in turns if t.is_correction)
        affirmations = sum(1 for t in turns if t.is_affirmation)
        delegations = sum(1 for t in turns if t.is_delegation)
        archetype = _classify_archetype(turns, corrections, affirmations, delegations, 10.0)
        assert archetype == "The Partnership"

    def test_partnership_without_delegation(self):
        """Partnership doesn't require delegation — just corrections + affirmation."""
        turns = _make_turns([
            "Fix the auth bug",
            "No, use JWT instead",
            "Actually, the other way",
            "Great work",
            "Perfect, keep going",
            "Awesome output",
        ])
        corrections = sum(1 for t in turns if t.is_correction)
        affirmations = sum(1 for t in turns if t.is_affirmation)
        archetype = _classify_archetype(turns, corrections, affirmations, 0, 10.0)
        assert archetype == "The Partnership"

    def test_autopilot(self):
        """High autonomy + no corrections = Autopilot."""
        turns = _make_turns(["Build the whole feature", "Add the tests too"])
        archetype = _classify_archetype(turns, 0, 0, 0, 25.0)
        assert archetype == "The Autopilot"

    def test_struggle(self):
        """Heavy corrections = Struggle."""
        turns = _make_turns([
            "Fix the bug",
            "No, wrong file",
            "That's not right either",
            "Wait, go back",
            "Try again",
        ])
        corrections = sum(1 for t in turns if t.is_correction)
        affirmations = sum(1 for t in turns if t.is_affirmation)
        archetype = _classify_archetype(turns, corrections, affirmations, 0, 10.0)
        assert archetype == "The Struggle"

    def test_spec_dump_overrides_partnership(self):
        """Spec Dump fires before Partnership (priority order)."""
        # A session with long prompts AND some feedback still gets Spec Dump
        long_text = " ".join(["word"] * 400)
        turns = _make_turns([long_text])
        # Even with affirmations/corrections, avg_words > 300 wins
        archetype = _classify_archetype(turns, 0, 0, 0, 15.0)
        assert archetype == "The Spec Dump"

    def test_empty_turns_returns_empty(self):
        archetype = _classify_archetype([], 0, 0, 0, 0)
        assert archetype == ""

    def test_default_when_no_pattern_matches(self):
        """Sessions that don't match any archetype return empty string."""
        turns = _make_turns(["Do X", "Then Y", "Then Z"])
        archetype = _classify_archetype(turns, 0, 1, 0, 10.0)
        assert archetype == ""


# === Grade Scale ===


class TestGradeFromScore:
    def test_a_plus(self):
        assert _grade_from_score(95) == "A+"

    def test_a(self):
        assert _grade_from_score(85) == "A"

    def test_b(self):
        assert _grade_from_score(75) == "B"

    def test_c(self):
        assert _grade_from_score(65) == "C"

    def test_d(self):
        assert _grade_from_score(55) == "D"

    def test_f(self):
        assert _grade_from_score(30) == "F"

    def test_none(self):
        assert _grade_from_score(None) == "N/A"


# === Conversation Arc ===


class TestConversationArc:
    def test_delegation_opening(self):
        turns = _make_turns(["You decide what to build", "Ok looks good"])
        arc = _analyze_arc(turns)
        assert arc.opening_style == "delegation"

    def test_spec_opening(self):
        long_text = " ".join(["word"] * 150)
        turns = _make_turns([long_text, "Fix that"])
        arc = _analyze_arc(turns)
        assert arc.opening_style == "spec"

    def test_short_directive_opening(self):
        turns = _make_turns(["Fix auth", "Now add tests"])
        arc = _analyze_arc(turns)
        assert arc.opening_style == "short-directive"

    def test_question_opening(self):
        turns = _make_turns(["How does the auth system work?", "Ok thanks"])
        arc = _analyze_arc(turns)
        assert arc.opening_style == "question"

    def test_affirmation_closing(self):
        turns = _make_turns(["Fix the bug", "Perfect"])
        arc = _analyze_arc(turns)
        assert arc.closing_style == "affirmation"

    def test_correction_closing(self):
        turns = _make_turns(["Fix the bug", "No, wrong approach"])
        arc = _analyze_arc(turns)
        assert arc.closing_style == "correction"

    def test_slash_command_closing(self):
        turns = _make_turns(["Fix the bug", "/goodnight"])
        arc = _analyze_arc(turns)
        assert arc.closing_style == "slash-command"

    def test_shortening_trend(self):
        turns = _make_turns([
            " ".join(["word"] * 100),
            " ".join(["word"] * 80),
            " ".join(["word"] * 20),
            " ".join(["word"] * 10),
        ])
        arc = _analyze_arc(turns)
        assert arc.length_trend == "shortening"

    def test_lengthening_trend(self):
        turns = _make_turns([
            " ".join(["word"] * 10),
            " ".join(["word"] * 15),
            " ".join(["word"] * 80),
            " ".join(["word"] * 100),
        ])
        arc = _analyze_arc(turns)
        assert arc.length_trend == "lengthening"

    def test_too_short_for_trend(self):
        turns = _make_turns(["Fix it", "Done"])
        arc = _analyze_arc(turns)
        assert arc.length_trend == "too-short"

    def test_correction_distribution(self):
        turns = _make_turns([
            "Fix the bug",
            "No, wrong approach",
            "Actually use X",
            "Good work",
            "Don't do that",
            "Ship it",
        ])
        arc = _analyze_arc(turns)
        # "No" and "Actually" are in first half, "Don't" in second
        assert arc.early_corrections >= 1
        assert arc.late_corrections >= 1

    def test_empty_turns(self):
        arc = _analyze_arc([])
        assert arc.opening_style == ""
        assert arc.closing_style == ""


# === Full Analysis Integration ===


class TestAnalyzeCollaboration:
    def test_full_pipeline(self, tmp_path):
        """Full analysis on a realistic session."""
        path = _write_session(tmp_path, [
            {"text": "Fix the authentication bug in login.py"},
            {"text": "No, use bcrypt instead of md5"},
            {"text": "Great, now add unit tests"},
            {"text": "Perfect. Ship it"},
        ], agent_turns=40)

        # Create matching tool calls
        tool_calls = [_tc("Read", i) for i in range(40)]

        result = analyze_collaboration(path, tool_calls)

        assert result.human_turns == 4
        assert result.corrections >= 1  # "No"
        assert result.affirmations >= 2  # "Great", "Perfect"
        assert result.score is not None
        assert result.grade != "N/A"

    def test_empty_session(self, tmp_path):
        """Empty session returns zero-state analysis."""
        path = tmp_path / "empty.jsonl"
        path.write_text("")

        result = analyze_collaboration(path, [])
        assert result.human_turns == 0
        assert result.score is None
        assert result.grade == "N/A"

    def test_single_turn_no_score(self, tmp_path):
        """Single-turn sessions get no score."""
        path = _write_session(tmp_path, [
            {"text": "Build me a website"},
        ])
        result = analyze_collaboration(path, [_tc("Read", 0)])
        assert result.human_turns == 1
        assert result.score is None
        assert result.grade == "N/A"

    def test_archetype_populated(self, tmp_path):
        """Archetype is set when pattern matches."""
        # Spec dump: long prompt, no feedback
        long_text = " ".join(["word"] * 400)
        path = _write_session(tmp_path, [
            {"text": long_text},
            {"text": long_text},
        ], agent_turns=30)
        tool_calls = [_tc("Read", i) for i in range(30)]

        result = analyze_collaboration(path, tool_calls)
        assert result.archetype == "The Spec Dump"
        assert "7%" in result.archetype_description

    def test_metrics_calculated(self, tmp_path):
        """All derived metrics are computed."""
        path = _write_session(tmp_path, [
            {"text": "Fix the bug"},
            {"text": "No, wrong"},
            {"text": "Yes, good"},
        ], agent_turns=30)
        tool_calls = [_tc("Read", i) for i in range(30)]

        result = analyze_collaboration(path, tool_calls)
        assert result.tc_per_turn == 10.0
        assert result.correction_rate > 0
        assert result.affirmation_rate > 0
        assert result.engagement_rate > 0


# === Formatting ===


class TestFormatCollaboration:
    def test_basic_format(self):
        collab = CollaborationAnalysis(
            human_turns=5,
            avg_words_per_turn=25.0,
            corrections=2,
            affirmations=3,
            delegations=1,
            correction_rate=0.4,
            affirmation_rate=0.6,
            engagement_rate=1.0,
            tc_per_turn=12.0,
            score=82,
            grade="A",
            archetype="The Partnership",
            archetype_description="Short directives, corrections when needed.",
            recommendation="Keep doing what you're doing.",
            arc=ConversationArc(
                opening_style="short-directive",
                closing_style="affirmation",
                length_trend="shortening",
            ),
        )
        output = format_collaboration(collab)
        assert "Collaboration" in output
        assert "A (82/100)" in output
        assert "The Partnership" in output
        assert "Corrections: 2" in output
        assert "Affirmations: 3" in output
        assert "Delegations: 1" in output
        assert "12 tool calls/turn" in output
        assert "shortening" in output
        assert "Tip:" in output

    def test_empty_returns_empty(self):
        collab = CollaborationAnalysis()
        output = format_collaboration(collab)
        assert output == ""

    def test_no_delegation_hidden(self):
        collab = CollaborationAnalysis(
            human_turns=3,
            avg_words_per_turn=20.0,
            corrections=1,
            affirmations=1,
            delegations=0,
            tc_per_turn=10.0,
            score=60,
            grade="C",
        )
        output = format_collaboration(collab)
        assert "Delegations" not in output

    def test_no_archetype_no_tip(self):
        collab = CollaborationAnalysis(
            human_turns=3,
            avg_words_per_turn=20.0,
            tc_per_turn=10.0,
            score=60,
            grade="C",
        )
        output = format_collaboration(collab)
        assert "Tip:" not in output
