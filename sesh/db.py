"""SQLite + FTS5 database layer for sesh.

Handles schema creation, session storage, metrics queries, and full-text search.
Zero external dependencies — uses stdlib sqlite3.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .analyzers.grader import grade_session
from .analyzers.patterns import detect_all_patterns
from .analyzers.trends import SessionSummary
from .parsers.base import NormalizedSession

SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    source_format TEXT NOT NULL,
    source_path TEXT NOT NULL,
    start_time TEXT,
    end_time TEXT,
    duration_minutes REAL,
    model TEXT,
    grade TEXT,
    score INTEGER,
    tool_call_count INTEGER,
    error_count INTEGER,
    error_rate REAL,
    max_error_streak INTEGER,
    reads INTEGER,
    writes INTEGER,
    read_write_ratio REAL,
    blind_edits INTEGER,
    parallel_missed INTEGER,
    bash_count INTEGER,
    bash_anti_pattern INTEGER,
    bash_overuse_rate REAL,
    user_messages INTEGER,
    pattern_types TEXT,
    grade_notes TEXT,
    metadata_json TEXT,
    ingested_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tool_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    seq INTEGER NOT NULL,
    name TEXT NOT NULL,
    tool_id TEXT,
    input_json TEXT,
    output_preview TEXT,
    output_length INTEGER,
    is_error BOOLEAN NOT NULL DEFAULT 0,
    timestamp TEXT,
    categories TEXT
);

CREATE INDEX IF NOT EXISTS idx_tool_calls_session ON tool_calls(session_id);
CREATE INDEX IF NOT EXISTS idx_tool_calls_name ON tool_calls(name);

CREATE TABLE IF NOT EXISTS patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    type TEXT NOT NULL,
    severity TEXT NOT NULL,
    detail TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_patterns_session ON patterns(session_id);
CREATE INDEX IF NOT EXISTS idx_patterns_type ON patterns(type);

CREATE TABLE IF NOT EXISTS sessions_fts_content (
    session_id TEXT PRIMARY KEY,
    raw_text TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS sessions_fts USING fts5(
    session_id,
    raw_text,
    content='sessions_fts_content',
    tokenize='porter unicode61'
);

-- Triggers to keep FTS in sync
CREATE TRIGGER IF NOT EXISTS sessions_fts_insert AFTER INSERT ON sessions_fts_content BEGIN
    INSERT INTO sessions_fts(rowid, session_id, raw_text)
    VALUES (new.rowid, new.session_id, new.raw_text);
END;

CREATE TRIGGER IF NOT EXISTS sessions_fts_delete AFTER DELETE ON sessions_fts_content BEGIN
    INSERT INTO sessions_fts(sessions_fts, rowid, session_id, raw_text)
    VALUES ('delete', old.rowid, old.session_id, old.raw_text);
END;
"""


class Database:
    """SQLite database for session storage and analysis."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create tables if they don't exist."""
        self.conn.executescript(SCHEMA_SQL)
        # Set schema version
        existing = self.conn.execute(
            "SELECT version FROM schema_version LIMIT 1"
        ).fetchone()
        if not existing:
            self.conn.execute(
                "INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,)
            )
        self.conn.commit()

    def session_exists(self, session_id: str) -> bool:
        """Check if a session is already in the database."""
        row = self.conn.execute(
            "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        return row is not None

    def ingest_session(
        self,
        session: NormalizedSession,
        thresholds: dict | None = None,
        grading_weights: dict | None = None,
    ) -> dict:
        """Ingest a parsed session: analyze, grade, store.

        Returns dict with grade, score, pattern count.
        """
        if self.session_exists(session.session_id):
            raise ValueError(f"Session {session.session_id} already exists")

        # Analyze
        patterns = detect_all_patterns(session.tool_calls, thresholds=thresholds)
        grade_result = grade_session(session.tool_calls, weights=grading_weights)

        # Compute metrics
        errors = sum(1 for tc in session.tool_calls if tc.is_error)
        total = len(session.tool_calls)
        reads = sum(1 for tc in session.tool_calls if tc.name in ("Read", "Grep", "Glob"))
        writes = sum(1 for tc in session.tool_calls if tc.name in ("Edit", "Write"))
        bash_count = sum(1 for tc in session.tool_calls if tc.name == "Bash")
        user_msgs = sum(1 for e in session.events if e.type == "user_message")

        # Blind edits
        files_read: set[str] = set()
        blind_edits = 0
        for tc in session.tool_calls:
            if tc.name == "Read":
                files_read.add(tc.input_data.get("file_path", ""))
            elif tc.name == "Edit":
                if tc.input_data.get("file_path", "") not in files_read:
                    blind_edits += 1

        # Bash anti-pattern count
        bash_anti = 0
        for tc in session.tool_calls:
            if tc.name == "Bash":
                cmd = tc.input_data.get("command", "")
                for anti in ("cat ", "head ", "tail ", "grep ", "rg ", "find ", "sed ", "awk "):
                    if cmd.startswith(anti) or f" | {anti}" in cmd:
                        bash_anti += 1
                        break

        # Error streak
        max_streak = 0
        current = 0
        for tc in session.tool_calls:
            if tc.is_error:
                current += 1
                max_streak = max(max_streak, current)
            else:
                current = 0

        # Parallel missed
        parallel_missed = 0
        for i in range(len(session.tool_calls) - 1):
            curr = session.tool_calls[i]
            nxt = session.tool_calls[i + 1]
            if (
                curr.name == "Read"
                and nxt.name == "Read"
                and curr.input_data.get("file_path") != nxt.input_data.get("file_path")
            ):
                parallel_missed += 1

        now = datetime.now(timezone.utc).isoformat()
        pattern_types = [p.type for p in patterns]
        grade_notes_parts = grade_result.deductions + grade_result.bonuses

        # Insert session
        self.conn.execute(
            """INSERT INTO sessions (
                id, source_format, source_path, start_time, end_time,
                duration_minutes, model, grade, score, tool_call_count,
                error_count, error_rate, max_error_streak, reads, writes,
                read_write_ratio, blind_edits, parallel_missed, bash_count,
                bash_anti_pattern, bash_overuse_rate, user_messages,
                pattern_types, grade_notes, metadata_json, ingested_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session.session_id,
                session.source_format,
                session.source_path,
                session.start_time,
                session.end_time,
                session.duration_minutes,
                session.model,
                grade_result.grade,
                grade_result.score,
                total,
                errors,
                round(errors / max(total, 1), 3),
                max_streak,
                reads,
                writes,
                round(reads / max(writes, 1), 1),
                blind_edits,
                parallel_missed,
                bash_count,
                bash_anti,
                round(bash_anti / max(bash_count, 1), 3),
                user_msgs,
                json.dumps(pattern_types),
                " | ".join(grade_notes_parts) if grade_notes_parts else "Clean session",
                json.dumps(session.metadata),
                now,
            ),
        )

        # Insert tool calls
        for tc in session.tool_calls:
            self.conn.execute(
                """INSERT INTO tool_calls (
                    session_id, seq, name, tool_id, input_json,
                    output_preview, output_length, is_error, timestamp, categories
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session.session_id,
                    tc.seq,
                    tc.name,
                    tc.tool_id,
                    json.dumps(tc.input_data),
                    tc.output_preview,
                    tc.output_length,
                    tc.is_error,
                    tc.timestamp,
                    ",".join(tc.categories),
                ),
            )

        # Insert patterns
        for p in patterns:
            self.conn.execute(
                "INSERT INTO patterns (session_id, type, severity, detail) VALUES (?, ?, ?, ?)",
                (session.session_id, p.type, p.severity, p.detail),
            )

        # Insert FTS content
        self.conn.execute(
            "INSERT INTO sessions_fts_content (session_id, raw_text) VALUES (?, ?)",
            (session.session_id, session.raw_text),
        )

        self.conn.commit()

        return {
            "session_id": session.session_id,
            "grade": grade_result.grade,
            "score": grade_result.score,
            "patterns": len(patterns),
            "tool_calls": total,
            "errors": errors,
        }

    def get_session(self, session_id: str) -> dict | None:
        """Get a session's stored data."""
        row = self.conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_tool_calls(self, session_id: str) -> list[dict]:
        """Get tool calls for a session."""
        rows = self.conn.execute(
            "SELECT * FROM tool_calls WHERE session_id = ? ORDER BY seq",
            (session_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_patterns(self, session_id: str) -> list[dict]:
        """Get detected patterns for a session."""
        rows = self.conn.execute(
            "SELECT * FROM patterns WHERE session_id = ?", (session_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def list_sessions(self, limit: int = 20) -> list[dict]:
        """List sessions ordered by ingestion time (newest first)."""
        rows = self.conn.execute(
            "SELECT id, grade, score, tool_call_count, error_count, "
            "duration_minutes, model, ingested_at, start_time "
            "FROM sessions ORDER BY ingested_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_session_summaries(self, limit: int = 20) -> list[SessionSummary]:
        """Get session summaries for trend analysis."""
        rows = self.conn.execute(
            """SELECT id, grade, score, tool_call_count, error_count, error_rate,
                      bash_overuse_rate, blind_edits, parallel_missed,
                      duration_minutes, pattern_types, ingested_at
               FROM sessions ORDER BY ingested_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()

        summaries = []
        for r in rows:
            pt = json.loads(r["pattern_types"]) if r["pattern_types"] else []
            summaries.append(SessionSummary(
                session_id=r["id"],
                grade=r["grade"] or "N/A",
                score=r["score"] or 0,
                tool_calls=r["tool_call_count"] or 0,
                errors=r["error_count"] or 0,
                error_rate=r["error_rate"] or 0.0,
                bash_overuse_rate=r["bash_overuse_rate"] or 0.0,
                blind_edits=r["blind_edits"] or 0,
                parallel_missed=r["parallel_missed"] or 0,
                duration_minutes=r["duration_minutes"],
                pattern_types=pt,
                ingested_at=r["ingested_at"],
            ))
        return summaries

    def search(self, query: str, limit: int = 10) -> list[dict]:
        """Full-text search across session transcripts."""
        rows = self.conn.execute(
            """SELECT c.session_id, s.grade, s.score, s.ingested_at,
                      snippet(sessions_fts, 1, '>>>', '<<<', '...', 64) as snippet
               FROM sessions_fts f
               JOIN sessions_fts_content c ON c.rowid = f.rowid
               JOIN sessions s ON s.id = c.session_id
               WHERE sessions_fts MATCH ?
               ORDER BY rank
               LIMIT ?""",
            (query, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_stats(self) -> dict:
        """Get aggregate statistics across all sessions."""
        row = self.conn.execute(
            """SELECT
                COUNT(*) as total_sessions,
                AVG(score) as avg_score,
                AVG(error_rate) as avg_error_rate,
                AVG(bash_overuse_rate) as avg_bash_overuse,
                AVG(blind_edits) as avg_blind_edits,
                SUM(tool_call_count) as total_tool_calls,
                SUM(error_count) as total_errors,
                AVG(duration_minutes) as avg_duration
            FROM sessions"""
        ).fetchone()
        return dict(row) if row else {}

    def get_tool_stats(self) -> list[dict]:
        """Get per-tool usage statistics."""
        rows = self.conn.execute(
            """SELECT name,
                      COUNT(*) as uses,
                      SUM(is_error) as errors,
                      ROUND(CAST(SUM(is_error) AS REAL) / COUNT(*), 3) as error_rate
               FROM tool_calls
               GROUP BY name
               ORDER BY uses DESC"""
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        """Close the database connection."""
        self.conn.close()
