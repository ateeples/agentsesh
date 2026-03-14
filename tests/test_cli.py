"""Tests for CLI commands — roundtrip integration tests.

Tests run CLI subcommands as subprocesses to verify end-to-end behavior:
init, log, reflect, analyze, audit, and watch --once.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

# --- Test helpers ---


def _write_jsonl(path: Path, lines: list[dict]) -> None:
    with open(path, "w") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")


def _make_session_file(dir_path: Path, name: str = "test-session") -> Path:
    """Create a minimal valid Claude Code session transcript."""
    p = dir_path / f"{name}.jsonl"
    _write_jsonl(p, [
        {"type": "user", "timestamp": "2026-03-12T10:00:00Z", "message": {"content": "Fix bug"}},
        {
            "type": "assistant",
            "timestamp": "2026-03-12T10:00:30Z",
            "message": {
                "model": "claude-opus-4-6",
                "content": [
                    {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "/a.py"}},
                ],
            },
        },
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "def foo(): pass"}]}},
        {
            "type": "assistant",
            "timestamp": "2026-03-12T10:01:00Z",
            "message": {
                "model": "claude-opus-4-6",
                "content": [
                    {"type": "tool_use", "id": "t2", "name": "Edit", "input": {"file_path": "/a.py", "old_string": "pass", "new_string": "return 42"}},
                ],
            },
        },
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t2", "content": "File updated"}]}},
        {
            "type": "assistant",
            "timestamp": "2026-03-12T10:02:00Z",
            "message": {
                "model": "claude-opus-4-6",
                "content": [
                    {"type": "tool_use", "id": "t3", "name": "Read", "input": {"file_path": "/b.py"}},
                ],
            },
        },
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t3", "content": "def bar(): return 1"}]}},
    ])
    return p


AGENTSESH_ROOT = str(Path(__file__).parent.parent)

def _run_sesh(*args, cwd=None) -> subprocess.CompletedProcess:
    import os
    env = os.environ.copy()
    env["PYTHONPATH"] = AGENTSESH_ROOT + ((":" + env.get("PYTHONPATH", "")) if env.get("PYTHONPATH") else "")
    return subprocess.run(
        [sys.executable, "-m", "sesh", *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
    )


# --- CLI integration tests (subprocess-based) ---


class TestCLIRoundtrip:
    @pytest.fixture
    def sesh_dir(self, tmp_path):
        """Set up a .sesh directory with one ingested session."""
        result = _run_sesh("init", cwd=tmp_path)
        assert result.returncode == 0

        session_file = _make_session_file(tmp_path)
        result = _run_sesh("log", str(session_file), cwd=tmp_path)
        assert result.returncode == 0
        return tmp_path

    def test_init(self, tmp_path):
        result = _run_sesh("init", cwd=tmp_path)
        assert result.returncode == 0
        assert (tmp_path / ".sesh" / "sesh.db").exists()
        assert (tmp_path / ".sesh" / "config.json").exists()

    def test_init_idempotent(self, sesh_dir):
        result = _run_sesh("init", cwd=sesh_dir)
        assert result.returncode == 0
        assert ".sesh/ already exists" in result.stdout

    def test_log_and_list(self, sesh_dir):
        result = _run_sesh("list", cwd=sesh_dir)
        assert result.returncode == 0
        assert "test-session" in result.stdout

    def test_reflect(self, sesh_dir):
        result = _run_sesh("reflect", cwd=sesh_dir)
        assert result.returncode == 0
        assert "test-session" in result.stdout
        assert "Grade" in result.stdout or "grade" in result.stdout.lower()

    def test_stats(self, sesh_dir):
        result = _run_sesh("stats", cwd=sesh_dir)
        assert result.returncode == 0
        assert "Sessions" in result.stdout or "sessions" in result.stdout.lower()

    def test_report(self, sesh_dir):
        result = _run_sesh("report", cwd=sesh_dir)
        assert result.returncode == 0

    def test_handoff(self, sesh_dir):
        result = _run_sesh("handoff", cwd=sesh_dir)
        assert result.returncode == 0
        assert "Handoff" in result.stdout or "handoff" in result.stdout.lower()

    def test_search(self, sesh_dir):
        result = _run_sesh("search", "bug", cwd=sesh_dir)
        assert result.returncode == 0

    def test_json_flag_on_subcommand(self, sesh_dir):
        """--json flag should work after the subcommand name."""
        result = _run_sesh("stats", "--json", cwd=sesh_dir)
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert "stats" in data

    def test_list_json(self, sesh_dir):
        result = _run_sesh("list", "--json", cwd=sesh_dir)
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert isinstance(data, list)

    def test_version(self):
        result = _run_sesh("--version")
        assert result.returncode == 0
        assert "sesh" in result.stdout

    def test_no_sessions_error(self, tmp_path):
        _run_sesh("init", cwd=tmp_path)
        result = _run_sesh("reflect", cwd=tmp_path)
        assert result.returncode == 3

    def test_batch_ingest(self, tmp_path):
        _run_sesh("init", cwd=tmp_path)
        sessions_dir = tmp_path / "transcripts"
        sessions_dir.mkdir()
        for i in range(3):
            _make_session_file(sessions_dir, f"session-{i}")
        result = _run_sesh("log", "--dir", str(sessions_dir), cwd=tmp_path)
        assert result.returncode == 0
        assert "Ingested 3" in result.stdout


# --- sesh analyze (no-DB) CLI tests ---


class TestAnalyzeCLI:
    """Tests for sesh analyze — no database required."""

    def test_analyze_basic(self, tmp_path):
        session_file = _make_session_file(tmp_path)
        result = _run_sesh("analyze", str(session_file))
        assert result.returncode == 0
        assert "Session Analysis" in result.stdout
        assert "Grade" in result.stdout

    def test_analyze_json(self, tmp_path):
        session_file = _make_session_file(tmp_path)
        result = _run_sesh("analyze", str(session_file), "--json")
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert "session_id" in data
        assert "grade" in data
        assert "stats" in data

    def test_analyze_verbose(self, tmp_path):
        session_file = _make_session_file(tmp_path)
        result = _run_sesh("analyze", str(session_file), "--verbose")
        assert result.returncode == 0
        assert "Session Analysis" in result.stdout

    def test_analyze_fix(self, tmp_path):
        session_file = _make_session_file(tmp_path)
        result = _run_sesh("analyze", str(session_file), "--fix")
        assert result.returncode == 0
        # Either shows a patch or says no changes needed

    def test_analyze_missing_file(self):
        result = _run_sesh("analyze", "/nonexistent/file.jsonl")
        assert result.returncode != 0
        assert "not found" in result.stderr.lower() or "error" in result.stderr.lower()

    def test_analyze_no_db_needed(self, tmp_path):
        """analyze should work without any .sesh/ directory."""
        session_file = _make_session_file(tmp_path)
        # No sesh init — still works
        result = _run_sesh("analyze", str(session_file))
        assert result.returncode == 0
        assert "Session Analysis" in result.stdout

    def test_analyze_feedback_writes_claude_md(self, tmp_path):
        """--feedback should write session findings to CLAUDE.md."""
        session_file = _make_session_file(tmp_path)
        result = _run_sesh("analyze", str(session_file), "--feedback", cwd=tmp_path)
        assert result.returncode == 0
        assert "Feedback written" in result.stdout
        claude_md = tmp_path / "CLAUDE.md"
        assert claude_md.exists()
        text = claude_md.read_text()
        assert "Last Session" in text
        assert "sesh:feedback" in text

    def test_analyze_feedback_custom_file(self, tmp_path):
        """--feedback path should write to specified file."""
        session_file = _make_session_file(tmp_path)
        target = tmp_path / "MY_RULES.md"
        result = _run_sesh("analyze", str(session_file), "--feedback", str(target), cwd=tmp_path)
        assert result.returncode == 0
        assert target.exists()
        assert "sesh:feedback" in target.read_text()

    def test_analyze_no_args_no_sessions(self):
        """analyze with no file and no discoverable sessions should error clearly."""
        import os
        env = os.environ.copy()
        # Point HOME at an empty dir so discovery finds nothing
        env["HOME"] = "/tmp/sesh-test-empty-home"
        result = subprocess.run(
            [sys.executable, "-m", "sesh", "analyze"],
            capture_output=True, text=True, env=env,
        )
        assert result.returncode != 0
        assert "no session" in result.stderr.lower() or "provide a file" in result.stderr.lower()


# --- sesh audit CLI tests ---


class TestAuditCLI:
    """Tests for sesh audit — threshold exit codes."""

    def test_audit_basic(self, tmp_path):
        result = _run_sesh("audit", str(tmp_path))
        assert result.returncode == 0

    def test_audit_json(self, tmp_path):
        result = _run_sesh("audit", str(tmp_path), "--json")
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert "score" in data
        assert "grade" in data

    def test_audit_threshold_pass(self, tmp_path):
        """Score above threshold → exit 0."""
        result = _run_sesh("audit", str(tmp_path), "--threshold", "0")
        assert result.returncode == 0

    def test_audit_threshold_fail(self, tmp_path):
        """Empty repo scores low — threshold 100 should fail."""
        result = _run_sesh("audit", str(tmp_path), "--threshold", "100")
        assert result.returncode == 1

    def test_audit_threshold_fail_json(self, tmp_path):
        """--json + --threshold should still output JSON and exit 1."""
        result = _run_sesh("audit", str(tmp_path), "--threshold", "100", "--json")
        assert result.returncode == 1
        data = json.loads(result.stdout)
        assert "score" in data
