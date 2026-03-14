"""Ingestion commands — log transcripts and watch directories."""

import sys
from pathlib import Path

from ..parsers import parse_transcript
from ..watch import discover_session_dirs, ingest_new_files, watch_loop
from ._resolve import get_config, get_db


def cmd_log(args) -> None:
    """Ingest a session transcript."""
    config = get_config()
    db = get_db(args)

    # Resolve input: single file or batch directory
    paths = []
    if args.dir:
        dir_path = Path(args.dir)
        if not dir_path.is_dir():
            print(f"Error: {args.dir} is not a directory", file=sys.stderr)
            sys.exit(1)
        paths = sorted(dir_path.iterdir())
    elif args.file:
        paths = [Path(args.file)]
    else:
        print("Error: Provide a file or --dir", file=sys.stderr)
        sys.exit(2)

    # Track ingestion results for summary
    ingested = 0
    skipped = 0
    errors = 0

    for path in paths:
        if not path.is_file():
            continue
        try:
            fmt = args.format if args.format != "auto" else None
            session = parse_transcript(path, format_hint=fmt)
        except ValueError as e:
            if not args.quiet:
                print(f"  Skip {path.name}: {e}", file=sys.stderr)
            skipped += 1
            continue

        if not session.tool_calls:
            if not args.quiet:
                print(f"  Skip {path.name}: no tool calls", file=sys.stderr)
            skipped += 1
            continue

        try:
            result = db.ingest_session(
                session,
                thresholds=config.pattern_thresholds,
                grading_weights=config.grading_weights,
            )
            ingested += 1
            if not args.quiet:
                print(
                    f"  [{result['grade']}] {path.name} — "
                    f"{result['tool_calls']} calls, {result['errors']} errors, "
                    f"{result['patterns']} patterns"
                )
        except ValueError:
            # Already exists
            skipped += 1
        except Exception as e:
            print(f"  Error ingesting {path.name}: {e}", file=sys.stderr)
            errors += 1

    db.close()

    if not args.quiet:
        print(f"\nIngested {ingested}, skipped {skipped}, errors {errors}")


def cmd_watch(args) -> None:
    """Watch directories for new sessions and auto-ingest.

    Polls session directories for new JSONL files. Files must be
    unchanged for --settle seconds before ingestion (avoids partial reads).
    """
    config = get_config()
    db = get_db(args)

    # Resolve directories: explicit args or auto-discover from ~/.claude/
    directories = []
    if args.dirs:
        directories = [Path(d) for d in args.dirs]
    else:
        directories = discover_session_dirs()
        if not directories:
            print(
                "No session directories found. Specify directories:\n"
                "  sesh watch ~/.claude/projects/",
                file=sys.stderr,
            )
            sys.exit(1)

    # Validate directories exist
    for d in directories:
        if not d.is_dir():
            print(f"Warning: {d} is not a directory, skipping", file=sys.stderr)
    directories = [d for d in directories if d.is_dir()]

    if not directories:
        print("Error: No valid directories to watch", file=sys.stderr)
        sys.exit(1)

    if args.once:
        # One-shot scan
        count = ingest_new_files(
            db, config, directories,
            settle_seconds=args.settle,
            quiet=args.quiet,
        )
        if not args.quiet:
            print(f"\nIngested {count} new session(s)")
        db.close()
    else:
        try:
            watch_loop(
                db, config, directories,
                interval=args.interval,
                settle_seconds=args.settle,
                quiet=args.quiet,
            )
        finally:
            db.close()
