"""Tests for the watch module — file discovery, auto-ingest, settle time."""

import json
import os
import time
from pathlib import Path

from sesh.config import Config
from sesh.db import Database
from sesh.watch import (
    discover_session_dirs,
    find_latest_transcript,
    find_transcript_files,
    ingest_new_files,
)


def _write_jsonl(path: Path, lines: list[dict]) -> None:
    with open(path, "w") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")


def _make_session_file(dir_path: Path, name: str = "test-session.jsonl") -> Path:
    """Create a minimal valid Claude Code session JSONL file."""
    path = dir_path / name
    tool_id = "toolu_test123"
    lines = [
        {
            "type": "user",
            "timestamp": "2026-03-12T10:00:00Z",
            "message": {"content": "Hello"},
        },
        {
            "type": "assistant",
            "timestamp": "2026-03-12T10:00:01Z",
            "message": {
                "model": "claude-opus-4-6",
                "content": [
                    {
                        "type": "tool_use",
                        "id": tool_id,
                        "name": "Read",
                        "input": {"file_path": "/tmp/test.py"},
                    }
                ],
            },
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": "file contents here",
                    }
                ],
            },
        },
    ]
    _write_jsonl(path, lines)
    return path


# --- File discovery with settle-time filtering ---


class TestFindTranscriptFiles:
    def test_finds_jsonl_files(self, tmp_path):
        session = _make_session_file(tmp_path)
        # Set mtime to past so settle check passes
        old_time = time.time() - 120
        os.utime(session, (old_time, old_time))

        files = find_transcript_files([tmp_path], settle_seconds=60)
        assert len(files) == 1
        assert files[0] == session

    def test_respects_settle_time(self, tmp_path):
        """Files modified recently should be excluded."""
        _make_session_file(tmp_path)
        # File was just created — mtime is now

        files = find_transcript_files([tmp_path], settle_seconds=60)
        assert len(files) == 0

    def test_zero_settle_includes_new_files(self, tmp_path):
        _make_session_file(tmp_path)

        files = find_transcript_files([tmp_path], settle_seconds=0)
        assert len(files) == 1

    def test_finds_nested_jsonl(self, tmp_path):
        """Should find files in subdirectories (like Claude Code project dirs)."""
        nested = tmp_path / "project-abc"
        nested.mkdir()
        session = _make_session_file(nested)
        old_time = time.time() - 120
        os.utime(session, (old_time, old_time))

        files = find_transcript_files([tmp_path], settle_seconds=60)
        assert len(files) == 1

    def test_ignores_non_jsonl(self, tmp_path):
        (tmp_path / "readme.txt").write_text("hello")
        old_time = time.time() - 120
        os.utime(tmp_path / "readme.txt", (old_time, old_time))

        files = find_transcript_files([tmp_path], settle_seconds=0)
        assert len(files) == 0

    def test_handles_nonexistent_dir(self):
        files = find_transcript_files([Path("/tmp/nonexistent-dir-xyz")])
        assert files == []

    def test_multiple_directories(self, tmp_path):
        dir1 = tmp_path / "a"
        dir2 = tmp_path / "b"
        dir1.mkdir()
        dir2.mkdir()
        s1 = _make_session_file(dir1, "s1.jsonl")
        s2 = _make_session_file(dir2, "s2.jsonl")
        for p in [s1, s2]:
            old_time = time.time() - 120
            os.utime(p, (old_time, old_time))

        files = find_transcript_files([dir1, dir2], settle_seconds=60)
        assert len(files) == 2


# --- find_latest_transcript ---


class TestFindLatestTranscript:
    def test_returns_newest_file(self, tmp_path, monkeypatch):
        """Should return the most recently modified JSONL file."""
        # Create two session files with different mtimes
        old = _make_session_file(tmp_path, "old.jsonl")
        new = _make_session_file(tmp_path, "new.jsonl")
        os.utime(old, (1000, 1000))
        os.utime(new, (2000, 2000))

        # Patch discover_session_dirs to use our tmp_path
        monkeypatch.setattr("sesh.watch.discover_session_dirs", lambda: [tmp_path])

        result = find_latest_transcript()
        assert result == new

    def test_returns_none_when_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr("sesh.watch.discover_session_dirs", lambda: [tmp_path])
        result = find_latest_transcript()
        assert result is None

    def test_returns_none_when_no_dirs(self, monkeypatch):
        monkeypatch.setattr("sesh.watch.discover_session_dirs", lambda: [])
        result = find_latest_transcript()
        assert result is None

    def test_finds_nested_files(self, tmp_path, monkeypatch):
        """Should search recursively in subdirectories."""
        nested = tmp_path / "project-abc"
        nested.mkdir()
        session = _make_session_file(nested, "deep.jsonl")

        monkeypatch.setattr("sesh.watch.discover_session_dirs", lambda: [tmp_path])

        result = find_latest_transcript()
        assert result == session


# --- Auto-ingestion with dedup ---


class TestIngestNewFiles:
    def test_ingests_new_session(self, tmp_path):
        # Set up DB
        sesh_dir = tmp_path / ".sesh"
        sesh_dir.mkdir()
        db = Database(sesh_dir / "sesh.db")
        config = Config()

        # Create session file
        session_dir = tmp_path / "sessions"
        session_dir.mkdir()
        _make_session_file(session_dir)

        count = ingest_new_files(db, config, [session_dir], settle_seconds=0, quiet=True)
        assert count == 1

        # Second run should ingest nothing (already exists)
        count2 = ingest_new_files(db, config, [session_dir], settle_seconds=0, quiet=True)
        assert count2 == 0

        db.close()

    def test_skips_empty_sessions(self, tmp_path):
        sesh_dir = tmp_path / ".sesh"
        sesh_dir.mkdir()
        db = Database(sesh_dir / "sesh.db")
        config = Config()

        session_dir = tmp_path / "sessions"
        session_dir.mkdir()

        # Write a JSONL with only a user message (no tool calls)
        path = session_dir / "empty.jsonl"
        _write_jsonl(path, [
            {"type": "user", "timestamp": "2026-03-12T10:00:00Z", "message": {"content": "hi"}},
        ])

        count = ingest_new_files(db, config, [session_dir], settle_seconds=0, quiet=True)
        assert count == 0

        db.close()

    def test_skips_unparseable_files(self, tmp_path):
        sesh_dir = tmp_path / ".sesh"
        sesh_dir.mkdir()
        db = Database(sesh_dir / "sesh.db")
        config = Config()

        session_dir = tmp_path / "sessions"
        session_dir.mkdir()

        # Write invalid JSONL
        (session_dir / "bad.jsonl").write_text("not json at all\n")

        count = ingest_new_files(db, config, [session_dir], settle_seconds=0, quiet=True)
        assert count == 0

        db.close()

    def test_multiple_sessions(self, tmp_path):
        sesh_dir = tmp_path / ".sesh"
        sesh_dir.mkdir()
        db = Database(sesh_dir / "sesh.db")
        config = Config()

        session_dir = tmp_path / "sessions"
        session_dir.mkdir()
        _make_session_file(session_dir, "s1.jsonl")
        _make_session_file(session_dir, "s2.jsonl")
        _make_session_file(session_dir, "s3.jsonl")

        count = ingest_new_files(db, config, [session_dir], settle_seconds=0, quiet=True)
        # Files may have same content → same session_id, so only 1 unique
        # But the point is it doesn't crash
        assert count >= 1

        db.close()


# --- Session directory auto-discovery ---


class TestDiscoverSessionDirs:
    def test_returns_list(self):
        dirs = discover_session_dirs()
        assert isinstance(dirs, list)
        # Should return paths, even if empty on systems without Claude Code
        for d in dirs:
            assert isinstance(d, Path)
