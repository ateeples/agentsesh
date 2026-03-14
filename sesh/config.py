"""Configuration management for sesh.

Config lives at .sesh/config.json in the workspace root.
All values have sensible defaults — config file is optional.
"""

import json
from pathlib import Path

# Default configuration — all fields are optional in user configs.
# User values are deep-merged on top of these defaults.
DEFAULT_CONFIG = {
    "version": 1,
    # Path to the SQLite database, relative to project root
    "db_path": ".sesh/sesh.db",
    # Number of sessions shown in sesh report by default
    "default_report_count": 20,
    # --- Pattern detection settings ---
    # Patterns analyze tool call sequences for behavioral anti-patterns.
    # Thresholds control sensitivity — higher values mean fewer detections.
    "patterns": {
        # "all" enables every detector; specify names to enable selectively
        "enabled": ["all"],
        # Explicitly disable specific patterns even when "all" is enabled
        "disabled": [],
        "thresholds": {
            # Error rate above this triggers a "concern" (vs "warning")
            "error_rate_concern": 0.15,
            # Minimum Bash calls before bash_overuse can trigger
            "bash_overuse_min": 3,
            # Minimum unique directories before scattered_files triggers
            "scattered_dirs_min": 8,
            # Minimum consecutive reads to count as missed parallelism
            "missed_parallel_min": 4,
            # Minimum consecutive errors to count as an error streak
            "error_streak_min": 3,
            # Read-to-write ratio below this triggers low_read_ratio
            "low_read_ratio": 1.5,
            # Minimum read+write calls before ratio is meaningful
            "min_rw_calls": 5,
            # Minimum total tool calls before scattered_files applies
            "min_tool_calls_for_scatter": 10,
        },
    },
    # --- Grading weights ---
    # Grading starts at 100 and applies deductions/bonuses.
    # Each setting controls how much each factor costs or awards.
    "grading": {
        # Max deduction from error rate alone
        "error_rate_max_deduction": 20,
        # Points deducted per blind edit (capped at max)
        "blind_edit_deduction": 5,
        "blind_edit_max": 15,
        # Points deducted per error streak (capped at max)
        "error_streak_deduction": 3,
        "error_streak_max": 15,
        # Points deducted per bash anti-pattern (capped at max)
        "bash_anti_deduction": 2,
        "bash_anti_max": 10,
        # Bonus for high read-to-write ratio (good research before acting)
        "read_ratio_bonus": 5,
        "read_ratio_threshold": 3.0,
        # Bonus for using parallel tool calls
        "parallel_bonus": 5,
        "parallel_min_batches": 3,
    },
    # --- Custom tool classification ---
    # Map project-specific tools to behavioral categories.
    # These extend (not replace) the built-in tool categories.
    "custom_tools": {
        "categories": {
            "read": [],
            "write": [],
            "search": [],
            "meta": [],
        }
    },
    # --- Handoff document settings ---
    "handoff": {
        # Max files to list in the "files touched" section
        "max_files_listed": 20,
        # Include process quality notes in handoff
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
    """Find .sesh/config.json by walking up from start_dir.

    Walks up the directory tree (capped at 20 levels) looking for .sesh/config.json.
    This allows sesh commands to work from any subdirectory of the project.
    """
    d = start_dir or Path.cwd()
    for _ in range(20):  # Cap depth to avoid infinite walk on circular symlinks
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
