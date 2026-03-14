"""File watcher for auto-ingesting new session transcripts.

Polls directories for new/modified transcript files and ingests them
automatically. Uses stdlib only — no external filesystem watchers.

Design decisions:
- Settle time: only ingest files untouched for N seconds (default: 60).
  This avoids parsing sessions still being written to.
- Dedup: checks db.session_exists() before ingesting, so re-scanning
  the same directory is cheap.
- Recursive: walks subdirectories (Claude Code stores sessions in
  ~/.claude/projects/<project-hash>/<file>.jsonl).
"""

import sys
import time
from pathlib import Path

from .config import Config
from .db import Database
from .parsers import parse_transcript, auto_detect_parser


# Well-known session directories
CLAUDE_CODE_SESSIONS = Path.home() / ".claude" / "projects"


def discover_session_dirs() -> list[Path]:
    """Find well-known agent session directories on this system."""
    dirs = []
    if CLAUDE_CODE_SESSIONS.is_dir():
        dirs.append(CLAUDE_CODE_SESSIONS)
    return dirs


def find_latest_transcript() -> Path | None:
    """Find the most recently modified session transcript.

    Searches well-known session directories (e.g. ~/.claude/projects/)
    and returns the newest JSONL file, or None if nothing found.
    No settle time — returns whatever is newest, even if still being written.
    """
    dirs = discover_session_dirs()
    if not dirs:
        return None

    newest: Path | None = None
    newest_mtime: float = 0

    for dir_path in dirs:
        if not dir_path.is_dir():
            continue
        for path in dir_path.rglob("*.jsonl"):
            if not path.is_file():
                continue
            try:
                mtime = path.stat().st_mtime
                if mtime > newest_mtime:
                    newest = path
                    newest_mtime = mtime
            except OSError:
                continue

    return newest


def find_transcript_files(
    directories: list[Path],
    settle_seconds: float = 60.0,
) -> list[Path]:
    """Find transcript files that are ready for ingestion.

    Only returns files that:
    1. Have a parseable extension (.jsonl, .json)
    2. Haven't been modified in the last settle_seconds
    3. Are regular files (not symlinks to directories, etc.)
    """
    now = time.time()
    candidates = []

    for dir_path in directories:
        if not dir_path.is_dir():
            continue
        for path in dir_path.rglob("*.jsonl"):
            if not path.is_file():
                continue
            mtime = path.stat().st_mtime
            if (now - mtime) >= settle_seconds:
                candidates.append(path)
        for path in dir_path.rglob("*.json"):
            if not path.is_file():
                continue
            mtime = path.stat().st_mtime
            if (now - mtime) >= settle_seconds:
                candidates.append(path)

    return sorted(candidates)


def ingest_new_files(
    db: Database,
    config: Config,
    directories: list[Path],
    settle_seconds: float = 60.0,
    quiet: bool = False,
) -> int:
    """Scan directories and ingest any new transcript files.

    Returns the number of newly ingested sessions.
    """
    files = find_transcript_files(directories, settle_seconds)
    ingested = 0

    for path in files:
        # Quick check: can any parser handle this?
        parser_cls = auto_detect_parser(path)
        if parser_cls is None:
            continue

        try:
            session = parse_transcript(path)
        except (ValueError, Exception):
            continue

        if not session.tool_calls:
            continue

        if db.session_exists(session.session_id):
            continue

        try:
            result = db.ingest_session(
                session,
                thresholds=config.pattern_thresholds,
                grading_weights=config.grading_weights,
            )
            ingested += 1
            if not quiet:
                print(
                    f"  [{result['grade']}] {path.name} — "
                    f"{result['tool_calls']} calls, {result['errors']} errors, "
                    f"{result['patterns']} patterns"
                )
        except ValueError:
            # Already exists (race condition with another process)
            pass
        except Exception as e:
            if not quiet:
                print(f"  Error: {path.name}: {e}", file=sys.stderr)

    return ingested


def watch_loop(
    db: Database,
    config: Config,
    directories: list[Path],
    interval: float = 30.0,
    settle_seconds: float = 60.0,
    quiet: bool = False,
) -> None:
    """Poll directories for new sessions until interrupted.

    Args:
        db: Database connection.
        config: Sesh config.
        directories: Directories to watch.
        interval: Seconds between polls (default: 30).
        settle_seconds: Minimum seconds since last file modification (default: 60).
        quiet: Suppress per-file output.
    """
    dir_list = ", ".join(str(d) for d in directories)
    print(f"Watching: {dir_list}")
    print(f"Poll interval: {interval}s | Settle time: {settle_seconds}s")
    print("Press Ctrl+C to stop.\n")

    total_ingested = 0

    try:
        while True:
            count = ingest_new_files(db, config, directories, settle_seconds, quiet)
            if count > 0:
                total_ingested += count
                print(f"  +{count} session(s) ingested (total: {total_ingested})\n")
            time.sleep(interval)
    except KeyboardInterrupt:
        print(f"\nStopped. Ingested {total_ingested} session(s) this run.")
