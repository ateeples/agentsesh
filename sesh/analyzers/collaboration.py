"""Collaboration analysis — grade the human-AI partnership, not just the agent.

810 sessions proved: how the human participates predicts outcomes better than
how the agent behaves. Corrections predict shipping (r=0.242). Affirmation
predicts shipping (32% vs 8%). Prompt length is slightly negative (r=-0.096).

Five archetypes:
  - The Partnership (43% ship rate): short directives, corrections, affirmation
  - The Struggle (44%): heavy corrections but the human cares
  - The Autopilot (35%): human disappears after direction
  - The Spec Dump (7%): 500+ word specs, human disappears
  - The Micromanager (7%): checks every 1-2 tool calls

This module extracts human turns from JSONL transcripts, detects behavioral
signals (corrections, affirmations, delegation), computes a collaboration
score, and classifies the archetype. No external dependencies.
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean

from ..parsers.base import ToolCall


# --- Pattern lists ---

CORRECTION_PATTERNS: list[re.Pattern] = [
    re.compile(p) for p in [
        r"\bno[,.]?\s",
        r"\bnot that\b",
        r"\bwrong\b",
        r"\binstead\b",
        r"\bdon'?t\b",
        r"\bstop\b",
        r"\bwait\b",
        r"\bactually\b",
        r"\blet'?s not\b",
        r"\btry again\b",
        r"\bthat'?s not\b",
        r"\bnope\b",
        r"\bhold on\b",
        r"\bgo back\b",
        r"\bundo\b",
        r"\brevert\b",
    ]
]

AFFIRMATION_PATTERNS: list[re.Pattern] = [
    re.compile(p) for p in [
        r"\blove it\b",
        r"\bperfect\b",
        r"\bgreat\b",
        r"\bnice\b",
        r"\bawesome\b",
        r"\byep\b",
        r"\byeah\b",
        r"\byes\b",
        r"\bgood\b",
        r"\bship it\b",
        r"\bdo it\b",
        r"\bgo for it\b",
        r"\blgtm\b",
        r"\bthat works\b",
        r"\bnailed it\b",
        r"\bexactly\b",
    ]
]

DELEGATION_PATTERNS: list[re.Pattern] = [
    re.compile(p) for p in [
        r"\byou (?:decide|choose|pick|figure)\b",
        r"\bwhatever (?:you|comes)\b",
        r"\bup to you\b",
        r"\bsurprise me\b",
        r"\bmake (?:us|it|me) proud\b",
        r"\bgo deep\b",
        r"\bkeep going\b",
        r"\bwhat do you think\b",
        r"\bwhat.?s (?:missing|next)\b",
        r"\byour call\b",
    ]
]

# Patterns to strip from user turns (system noise)
_SYSTEM_TAG_RE = re.compile(
    r"<(?:system-reminder|local-command-caveat|command-\w+|local-command-stdout)>"
    r".*?"
    r"</(?:system-reminder|local-command-caveat|command-\w+|local-command-stdout)>",
    re.DOTALL,
)

# Skill expansion pattern — when user types /tdd or /ship, the JSONL stores
# the full skill prompt (500+ words) as a "user" message. We need to strip
# the skill body and keep only the user's actual arguments.
_SKILL_EXPANSION_RE = re.compile(
    r"Base directory for this skill:.*?(?=ARGUMENTS:|$)",
    re.DOTALL,
)
# Also strip the ARGUMENTS: prefix itself
_SKILL_ARGS_PREFIX_RE = re.compile(r"^ARGUMENTS:\s*", re.MULTILINE)


# --- Data types ---


@dataclass
class HumanTurn:
    """A single human turn in the conversation."""

    text: str
    word_count: int
    timestamp: str | None = None
    is_correction: bool = False
    is_affirmation: bool = False
    is_delegation: bool = False


@dataclass
class ConversationArc:
    """Shape of the conversation over time."""

    opening_style: str = ""  # "delegation", "question", "spec", "short-directive", "medium-directive"
    closing_style: str = ""  # "affirmation", "correction", "delegation", "slash-command", "other"
    length_trend: str = ""  # "shortening", "lengthening", "stable", "too-short"
    early_corrections: int = 0
    late_corrections: int = 0


@dataclass
class CollaborationAnalysis:
    """Complete collaboration analysis for a session."""

    # Core metrics
    human_turns: int = 0
    avg_words_per_turn: float = 0.0
    corrections: int = 0
    affirmations: int = 0
    delegations: int = 0

    # Derived
    correction_rate: float = 0.0  # corrections / human_turns
    affirmation_rate: float = 0.0
    engagement_rate: float = 0.0  # (corrections + affirmations) / human_turns
    tc_per_turn: float = 0.0  # tool_calls / human_turns (autonomy)

    # Score and classification
    score: int | None = None  # 0-100, None for single-turn
    grade: str = "N/A"
    archetype: str = ""
    archetype_description: str = ""

    # Conversation arc
    arc: ConversationArc = field(default_factory=ConversationArc)

    # Recommendation
    recommendation: str = ""


# --- Archetype descriptions ---

_ARCHETYPE_DESCRIPTIONS = {
    "The Partnership": (
        "Short directives, corrections when needed, affirmation when earned. "
        "The human stays engaged without micromanaging. 43% ship rate."
    ),
    "The Struggle": (
        "Heavy corrections but the human cares about getting it right. "
        "Messy but productive — the feedback loop drives outcomes. 44% ship rate."
    ),
    "The Autopilot": (
        "Human gives direction then disappears. Sometimes works for well-defined "
        "tasks, but there's no feedback loop to course-correct. 35% ship rate."
    ),
    "The Spec Dump": (
        "Long, detailed specifications upfront, then the human disappears. "
        "The prompt engineering ideal — but it almost never works. 7% ship rate."
    ),
    "The Micromanager": (
        "Checking every 1-2 tool calls. The AI can't build momentum or "
        "explore solutions. Both sides get stuck. 7% ship rate."
    ),
}

_ARCHETYPE_RECOMMENDATIONS = {
    "The Partnership": (
        "This is working. Keep doing what you're doing — short directives, "
        "corrections when the AI drifts, affirmation when it delivers."
    ),
    "The Struggle": (
        "The corrections are helping. Consider adding affirmation when things "
        "go right — sessions with both corrections AND affirmation ship more."
    ),
    "The Autopilot": (
        "Try staying more engaged. Check in after each logical unit. "
        "A quick 'yes, keep going' or 'no, try X instead' makes a big difference."
    ),
    "The Spec Dump": (
        "Shorter prompts, more turns. Instead of one 500-word spec, try: "
        "a 2-sentence direction, then correct and guide as the AI works. "
        "Sessions where prompts shorten over time ship 6x more."
    ),
    "The Micromanager": (
        "Give the AI more room to work. Try delegating a complete subtask "
        "and reviewing the result instead of checking every step."
    ),
}


# --- Core functions ---


def extract_human_turns(path: Path) -> list[HumanTurn]:
    """Extract and clean human turns from a JSONL transcript.

    Reads the JSONL file, extracts text blocks from user turns,
    strips system tags and noise, and classifies each turn for
    correction/affirmation/delegation signals.

    Args:
        path: Path to JSONL transcript file.

    Returns:
        List of HumanTurn objects with classified signals.
    """
    turns: list[HumanTurn] = []

    with open(path) as f:
        for line in f:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue

            if d.get("type") != "user":
                continue

            timestamp = d.get("timestamp")
            content = d.get("message", {}).get("content", "")

            # Extract text blocks (skip tool_result blocks)
            if isinstance(content, list):
                text_parts = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                text = " ".join(text_parts)
            elif isinstance(content, str):
                text = content
            else:
                continue

            # Strip system noise
            text = _SYSTEM_TAG_RE.sub("", text).strip()

            # Strip skill expansions — keep only the user's actual arguments
            text = _SKILL_EXPANSION_RE.sub("", text).strip()
            text = _SKILL_ARGS_PREFIX_RE.sub("", text).strip()

            # Skip empty or trivial turns
            if not text or len(text) <= 1:
                continue

            # Classify signals
            lower = text.lower()
            is_correction = any(p.search(lower) for p in CORRECTION_PATTERNS)
            is_affirmation = any(p.search(lower) for p in AFFIRMATION_PATTERNS)
            is_delegation = any(p.search(lower) for p in DELEGATION_PATTERNS)

            turns.append(HumanTurn(
                text=text,
                word_count=len(text.split()),
                timestamp=timestamp,
                is_correction=is_correction,
                is_affirmation=is_affirmation,
                is_delegation=is_delegation,
            ))

    return turns


def _analyze_arc(turns: list[HumanTurn]) -> ConversationArc:
    """Analyze the shape of the conversation over time."""
    arc = ConversationArc()

    if not turns:
        return arc

    # Opening style
    first = turns[0]
    if first.is_delegation:
        arc.opening_style = "delegation"
    elif first.text.rstrip().endswith("?"):
        arc.opening_style = "question"
    elif first.word_count > 100:
        arc.opening_style = "spec"
    elif first.word_count < 20:
        arc.opening_style = "short-directive"
    else:
        arc.opening_style = "medium-directive"

    # Closing style
    last = turns[-1]
    if last.is_affirmation:
        arc.closing_style = "affirmation"
    elif last.is_correction:
        arc.closing_style = "correction"
    elif last.is_delegation:
        arc.closing_style = "delegation"
    elif last.text.strip().startswith("/"):
        arc.closing_style = "slash-command"
    else:
        arc.closing_style = "other"

    # Correction distribution
    n = len(turns)
    midpoint = n / 2
    for i, turn in enumerate(turns):
        if turn.is_correction:
            if i < midpoint:
                arc.early_corrections += 1
            else:
                arc.late_corrections += 1

    # Length trend
    if n < 4:
        arc.length_trend = "too-short"
    else:
        words = [t.word_count for t in turns]
        half = n // 2
        first_half_avg = mean(words[:half])
        second_half_avg = mean(words[half:])
        if second_half_avg < first_half_avg * 0.5:
            arc.length_trend = "shortening"
        elif second_half_avg > first_half_avg * 2:
            arc.length_trend = "lengthening"
        else:
            arc.length_trend = "stable"

    return arc


def _compute_score(
    turns: list[HumanTurn],
    corrections: int,
    affirmations: int,
    delegations: int,
    tc_per_turn: float,
) -> int | None:
    """Compute collaboration score (0-100).

    Returns None for single-turn sessions.
    """
    n = len(turns)
    if n < 2:
        return None

    score = 50  # Base

    # Engagement bonus
    engagement = (corrections + affirmations) / n
    if engagement >= 0.6:
        score += 20
    elif engagement >= 0.3:
        score += 10
    elif engagement == 0:
        score -= 15

    # Affirmation bonus
    aff_rate = affirmations / n
    if aff_rate >= 0.3:
        score += 15
    elif aff_rate >= 0.15:
        score += 8

    # Correction bonus (sweet spot: 10-50%)
    corr_rate = corrections / n
    if 0.1 <= corr_rate <= 0.5:
        score += 10
    elif corr_rate > 0.5:
        score += 5

    # Delegation bonus
    if delegations >= 1:
        score += 5

    # Prompt length
    avg_words = mean(t.word_count for t in turns)
    if 15 <= avg_words <= 200:
        score += 10
    elif avg_words > 500:
        score -= 10

    # Autonomy (tool calls per human turn)
    if 5 <= tc_per_turn <= 30:
        score += 10
    elif tc_per_turn < 2:
        score -= 10

    # Turn count bonuses
    if n >= 5:
        score += 5
    if n >= 10:
        score += 5

    # Length trend bonus (trust building)
    if n >= 4:
        words = [t.word_count for t in turns]
        half = n // 2
        first_half = mean(words[:half])
        second_half = mean(words[half:])
        if second_half <= first_half:
            score += 5

    return max(0, min(100, score))


def _classify_archetype(
    turns: list[HumanTurn],
    corrections: int,
    affirmations: int,
    delegations: int,
    tc_per_turn: float,
) -> str:
    """Classify collaboration archetype.

    Priority order matters — first match wins.
    """
    n = len(turns)
    if n == 0:
        return ""

    avg_words = mean(t.word_count for t in turns)

    # Priority 1: Spec Dump — long prompts, no feedback
    if avg_words > 300 and corrections < 2 and affirmations < 2:
        return "The Spec Dump"

    # Priority 2: Micromanager — very low autonomy, many turns
    if tc_per_turn < 3 and n > 5:
        return "The Micromanager"

    # Priority 3: Partnership — engaged with both correction and affirmation
    if affirmations >= 3 and corrections >= 2:
        return "The Partnership"

    # Priority 4: Autopilot — high autonomy, no corrections
    if tc_per_turn > 20 and corrections < 2:
        return "The Autopilot"

    # Priority 5: Struggle — corrections dominate
    if corrections > affirmations * 2 and corrections >= 3:
        return "The Struggle"

    # Default — doesn't fit a clear pattern
    return ""


def _grade_from_score(score: int | None) -> str:
    """Convert numeric score to letter grade."""
    if score is None:
        return "N/A"
    if score >= 90:
        return "A+"
    if score >= 80:
        return "A"
    if score >= 70:
        return "B"
    if score >= 60:
        return "C"
    if score >= 50:
        return "D"
    return "F"


def analyze_collaboration(
    path: Path,
    tool_calls: list[ToolCall],
) -> CollaborationAnalysis:
    """Full collaboration analysis for a session.

    Reads human turns from the JSONL, classifies signals, computes
    score, and identifies the collaboration archetype.

    Args:
        path: Path to JSONL transcript.
        tool_calls: Parsed tool calls from the same session.

    Returns:
        CollaborationAnalysis with score, archetype, and metrics.
    """
    turns = extract_human_turns(path)
    result = CollaborationAnalysis()

    if not turns:
        return result

    n = len(turns)
    total_tc = len(tool_calls)

    # Core metrics
    result.human_turns = n
    result.avg_words_per_turn = round(mean(t.word_count for t in turns), 1)
    result.corrections = sum(1 for t in turns if t.is_correction)
    result.affirmations = sum(1 for t in turns if t.is_affirmation)
    result.delegations = sum(1 for t in turns if t.is_delegation)

    # Rates
    result.correction_rate = round(result.corrections / n, 3)
    result.affirmation_rate = round(result.affirmations / n, 3)
    result.engagement_rate = round(
        (result.corrections + result.affirmations) / n, 3
    )
    result.tc_per_turn = round(total_tc / n, 1) if n > 0 else 0

    # Score
    result.score = _compute_score(
        turns,
        result.corrections,
        result.affirmations,
        result.delegations,
        result.tc_per_turn,
    )
    result.grade = _grade_from_score(result.score)

    # Archetype
    result.archetype = _classify_archetype(
        turns,
        result.corrections,
        result.affirmations,
        result.delegations,
        result.tc_per_turn,
    )
    result.archetype_description = _ARCHETYPE_DESCRIPTIONS.get(
        result.archetype, ""
    )
    result.recommendation = _ARCHETYPE_RECOMMENDATIONS.get(
        result.archetype, ""
    )

    # Arc
    result.arc = _analyze_arc(turns)

    return result


def format_collaboration(collab: CollaborationAnalysis) -> str:
    """Format collaboration analysis as human-readable text."""
    if collab.human_turns == 0:
        return ""

    lines: list[str] = []

    lines.append("Collaboration")
    lines.append("\u2500" * 13)

    # Score and grade
    if collab.score is not None:
        lines.append(
            f"  Score: {collab.grade} ({collab.score}/100)"
        )
    else:
        lines.append("  Score: N/A (single turn)")

    # Archetype
    if collab.archetype:
        lines.append(f"  Style: {collab.archetype}")
        lines.append(f"         {collab.archetype_description}")

    # Metrics
    lines.append("")
    lines.append(
        f"  Turns: {collab.human_turns} "
        f"({collab.avg_words_per_turn:.0f} words/turn avg)"
    )
    lines.append(
        f"  Corrections: {collab.corrections} "
        f"({collab.correction_rate:.0%})"
        f"  |  Affirmations: {collab.affirmations} "
        f"({collab.affirmation_rate:.0%})"
    )
    if collab.delegations > 0:
        lines.append(f"  Delegations: {collab.delegations}")
    lines.append(f"  Autonomy: {collab.tc_per_turn:.0f} tool calls/turn")

    # Arc
    if collab.arc.opening_style:
        lines.append("")
        lines.append(
            f"  Arc: {collab.arc.opening_style} "
            f"\u2192 {collab.arc.closing_style}"
        )
        if collab.arc.length_trend and collab.arc.length_trend != "too-short":
            lines.append(f"  Prompt trend: {collab.arc.length_trend}")

    # Recommendation
    if collab.recommendation:
        lines.append("")
        lines.append(f"  Tip: {collab.recommendation}")

    return "\n".join(lines)
