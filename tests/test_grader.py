"""Tests for session grading."""

import pytest
from sesh.parsers.base import ToolCall
from sesh.analyzers.grader import grade_session, GRADE_SCALE


def _tc(name: str, input_data: dict | None = None, is_error: bool = False, seq: int = 0, timestamp: str | None = None) -> ToolCall:
    return ToolCall(
        name=name,
        tool_id=f"tc_{seq}",
        input_data=input_data or {},
        output_preview="",
        output_length=0,
        is_error=is_error,
        timestamp=timestamp,
        seq=seq,
    )


class TestGrading:
    def test_perfect_session(self):
        """Clean session with no issues should get A+."""
        calls = [
            _tc("Read", {"file_path": "/a.py"}, seq=0),
            _tc("Read", {"file_path": "/b.py"}, seq=1),
            _tc("Read", {"file_path": "/c.py"}, seq=2),
            _tc("Edit", {"file_path": "/a.py"}, seq=3),
        ]
        grade = grade_session(calls)
        assert grade.grade == "A+"
        assert grade.score >= 95
        assert len(grade.deductions) == 0

    def test_too_few_calls(self):
        calls = [_tc("Read", seq=0), _tc("Read", seq=1)]
        grade = grade_session(calls)
        assert grade.grade == "N/A"

    def test_error_deduction(self):
        calls = [_tc("Bash", is_error=True, seq=i) for i in range(10)]
        calls += [_tc("Read", seq=i + 10) for i in range(10)]
        grade = grade_session(calls)
        assert grade.score < 100
        assert any("error rate" in d for d in grade.deductions)

    def test_blind_edit_deduction(self):
        calls = [
            _tc("Edit", {"file_path": "/blind.py"}, seq=0),
            _tc("Read", {"file_path": "/a.py"}, seq=1),
            _tc("Read", {"file_path": "/b.py"}, seq=2),
            _tc("Edit", {"file_path": "/a.py"}, seq=3),
        ]
        grade = grade_session(calls)
        assert any("blind" in d for d in grade.deductions)

    def test_bash_anti_deduction(self):
        calls = [
            _tc("Bash", {"command": "cat /a.py"}, seq=0),
            _tc("Bash", {"command": "grep foo"}, seq=1),
            _tc("Bash", {"command": "find . -name '*.py'"}, seq=2),
            _tc("Bash", {"command": "sed 's/a/b/'"}, seq=3),
            _tc("Read", seq=4),
            _tc("Read", seq=5),
            _tc("Read", seq=6),
        ]
        grade = grade_session(calls)
        assert any("bash" in d for d in grade.deductions)

    def test_error_streak_deduction(self):
        calls = [
            _tc("Bash", is_error=True, seq=0),
            _tc("Bash", is_error=True, seq=1),
            _tc("Bash", is_error=True, seq=2),
            _tc("Read", seq=3),
            _tc("Read", seq=4),
        ]
        grade = grade_session(calls)
        assert any("streak" in d for d in grade.deductions)

    def test_read_ratio_bonus(self):
        """High read/write ratio should get a bonus."""
        calls = [_tc("Read", seq=i) for i in range(12)]
        calls.append(_tc("Edit", {"file_path": "/a.py"}, seq=12))
        grade = grade_session(calls)
        assert any("read/write" in b for b in grade.bonuses)

    def test_parallelism_bonus(self):
        """Multiple parallel batches should get a bonus."""
        ts = "2026-03-12T10:00:00Z"
        calls = []
        for batch in range(4):
            batch_ts = f"2026-03-12T10:0{batch}:00Z"
            calls.append(_tc("Read", {"file_path": f"/a{batch}.py"}, seq=batch * 2, timestamp=batch_ts))
            calls.append(_tc("Read", {"file_path": f"/b{batch}.py"}, seq=batch * 2 + 1, timestamp=batch_ts))
        grade = grade_session(calls)
        assert any("parallel" in b for b in grade.bonuses)

    def test_grade_scale(self):
        """Verify grade boundaries are correct."""
        assert GRADE_SCALE[0] == (95, "A+")
        assert GRADE_SCALE[-1] == (0, "F")

    def test_max_deduction_caps(self):
        """Deductions should be capped at their max values."""
        # 10 blind edits should cap at 15 points
        calls = [_tc("Edit", {"file_path": f"/f{i}.py"}, seq=i) for i in range(10)]
        calls += [_tc("Read", seq=i + 10) for i in range(5)]
        grade = grade_session(calls)
        # Blind edit max is 15, so score shouldn't drop below 85 from this alone
        blind_deduction = [d for d in grade.deductions if "blind" in d]
        assert len(blind_deduction) == 1
        assert "-15" in blind_deduction[0]
