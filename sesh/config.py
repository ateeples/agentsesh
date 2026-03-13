"""Configuration management for sesh.

Config lives at .sesh/config.json in the workspace root.
All values have sensible defaults — config file is optional.
"""

import json
from pathlib import Path

DEFAULT_CONFIG = {
    "version": 1,
    "db_path": ".sesh/sesh.db",
    "default_report_count": 20,
    "patterns": {
        "enabled": ["all"],
        "disabled": [],
        "thresholds": {
            "error_rate_concern": 0.15,
            "bash_overuse_min": 3,
            "scattered_dirs_min": 8,
            "missed_parallel_min": 4,
            "error_streak_min": 3,
            "low_read_ratio": 1.5,
            "min_rw_calls": 5,
            "min_tool_calls_for_scatter": 10,
        },
    },
    "grading": {
        "error_rate_max_deduction": 20,
        "blind_edit_deduction": 5,
        "blind_edit_max": 15,
        "error_streak_deduction": 3,
        "error_streak_max": 15,
        "bash_anti_deduction": 2,
        "bash_anti_max": 10,
        "read_ratio_bonus": 5,
        "read_ratio_threshold": 3.0,
        "parallel_bonus": 5,
        "parallel_min_batches": 3,
    },
    "custom_tools": {
        "categories": {
            "read": [],
            "write": [],
            "search": [],
            "meta": [],
        }
    },
    "handoff": {
        "max_files_listed": 20,
        "include_process_notes": True,
    },
}


class Config:
    """Configuration manager for sesh."""

    def __init__(self, config_path: str | Path | None = None):
        self.data = dict(DEFAULT_CONFIG)
        self.config_path = Path(config_path) if config_path else None

        if self.config_path and self.config_path.exists():
            with open(self.config_path) as f:
                user_config = json.load(f)
            self._merge(self.data, user_config)

    @staticmethod
    def _merge(base: dict, override: dict) -> None:
        """Deep merge override into base."""
        for key, value in override.items():
            if key in base and isinstance(base[key], dict) and isinstance(value, dict):
                Config._merge(base[key], value)
            else:
                base[key] = value

    @property
    def db_path(self) -> str:
        return self.data["db_path"]

    @property
    def default_report_count(self) -> int:
        return self.data["default_report_count"]

    @property
    def pattern_thresholds(self) -> dict:
        return self.data["patterns"]["thresholds"]

    @property
    def pattern_enabled(self) -> list:
        return self.data["patterns"]["enabled"]

    @property
    def pattern_disabled(self) -> list:
        return self.data["patterns"]["disabled"]

    @property
    def grading_weights(self) -> dict:
        return self.data["grading"]

    @property
    def custom_tool_categories(self) -> dict:
        return self.data["custom_tools"]["categories"]

    @property
    def handoff_config(self) -> dict:
        return self.data["handoff"]

    def save(self, path: str | Path | None = None) -> None:
        """Save config to disk."""
        out_path = Path(path) if path else self.config_path
        if not out_path:
            raise ValueError("No config path specified")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w") as f:
            json.dump(self.data, f, indent=2)
            f.write("\n")


def find_config(start_dir: Path | None = None) -> Path | None:
    """Find .sesh/config.json by walking up from start_dir."""
    d = start_dir or Path.cwd()
    for _ in range(20):  # Max 20 levels up
        candidate = d / ".sesh" / "config.json"
        if candidate.exists():
            return candidate
        parent = d.parent
        if parent == d:
            break
        d = parent
    return None


def find_sesh_dir(start_dir: Path | None = None) -> Path | None:
    """Find .sesh/ directory by walking up from start_dir."""
    d = start_dir or Path.cwd()
    for _ in range(20):
        candidate = d / ".sesh"
        if candidate.is_dir():
            return candidate
        parent = d.parent
        if parent == d:
            break
        d = parent
    return None
