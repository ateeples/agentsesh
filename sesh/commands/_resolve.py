"""Shared helpers for CLI command resolution — DB and config lookup."""

import sys

from ..config import Config, find_config, find_sesh_dir
from ..db import Database


def get_db(args) -> Database:
    """Get database connection, finding .sesh/ dir automatically."""
    # Priority: explicit --db flag > .sesh/config.json > .sesh/ discovery
    if hasattr(args, "db") and args.db:
        return Database(args.db)

    config_path = find_config()
    if config_path:
        config = Config(config_path)
        # Resolve db_path relative to .sesh/ parent (config lives in .sesh/)
        sesh_parent = config_path.parent.parent
        return Database(sesh_parent / config.db_path)

    sesh_dir = find_sesh_dir()
    if sesh_dir:
        return Database(sesh_dir / "sesh.db")

    print("Error: No .sesh/ directory found. Run `sesh init` first.", file=sys.stderr)
    sys.exit(1)


def get_config() -> Config:
    """Load config, using defaults if no config file found."""
    config_path = find_config()
    return Config(config_path)
