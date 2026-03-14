"""Main curses application loop for the sesh TUI dashboard.

Handles layout management, key bindings, data loading, and rendering.
Uses curses.wrapper() for safe terminal cleanup on exit.
"""

import curses

from ..db import Database
from .panels import draw_details, draw_header, draw_patterns, draw_sessions, draw_trend
from .widgets import dim_attr, init_colors, safe_addstr

# Minimum terminal size
MIN_COLS = 60
MIN_ROWS = 16


class TuiApp:
    """Main TUI application state and rendering."""

    def __init__(self, db: Database):
        self.db = db

        # UI state
        self.selected = 0
        self.scroll_offset = 0

        # Data (loaded from DB)
        self.sessions: list[dict] = []
        self.stats: dict = {}
        self.all_patterns: list[dict] = []
        self.selected_session: dict | None = None
        self.selected_tool_calls: list[dict] = []
        self.selected_patterns: list[dict] = []

        self.load_data()

    def load_data(self) -> None:
        """Load/refresh all data from the database."""
        raw_sessions = self.db.list_sessions(limit=500)
        # Sort by start_time descending (DB sorts by ingestion time)
        self.sessions = sorted(
            raw_sessions,
            key=lambda s: s.get("start_time") or "0",
            reverse=True,
        )
        self.stats = self.db.get_stats()

        # Load all patterns for frequency analysis
        self.all_patterns = []
        for s in self.sessions[:100]:  # Cap to avoid slow loads
            patterns = self.db.get_patterns(s["id"])
            self.all_patterns.extend(patterns)

        # Load details for selected session
        self._load_selected_details()

    def _load_selected_details(self) -> None:
        """Load tool calls and patterns for the currently selected session."""
        if self.sessions and 0 <= self.selected < len(self.sessions):
            sid = self.sessions[self.selected]["id"]
            self.selected_session = self.db.get_session(sid)
            self.selected_tool_calls = self.db.get_tool_calls(sid)
            self.selected_patterns = self.db.get_patterns(sid)
        else:
            self.selected_session = None
            self.selected_tool_calls = []
            self.selected_patterns = []

    def move_selection(self, delta: int) -> None:
        """Move the session selection by delta rows."""
        if not self.sessions:
            return
        self.selected = max(0, min(len(self.sessions) - 1, self.selected + delta))
        self._load_selected_details()

    def jump_top(self) -> None:
        """Jump selection to the first session."""
        self.selected = 0
        self.scroll_offset = 0
        self._load_selected_details()

    def jump_bottom(self) -> None:
        """Jump selection to the last session."""
        if self.sessions:
            self.selected = len(self.sessions) - 1
        self._load_selected_details()

    def render(self, stdscr) -> None:
        """Render the full dashboard layout."""
        stdscr.erase()
        max_y, max_x = stdscr.getmaxyx()

        # Check minimum size
        if max_y < MIN_ROWS or max_x < MIN_COLS:
            msg = f"Terminal too small ({max_x}x{max_y}). Minimum: {MIN_COLS}x{MIN_ROWS}"
            safe_addstr(stdscr, max_y // 2, max(0, (max_x - len(msg)) // 2), msg, dim_attr())
            stdscr.refresh()
            return

        # Decide layout: side-by-side or stacked
        wide = max_x >= 100  # Side-by-side threshold

        # --- Header (always full width) ---
        header_h = draw_header(stdscr, 0, 0, max_x, self.stats, self.sessions)

        if wide:
            self._render_wide(stdscr, header_h, max_y, max_x)
        else:
            self._render_narrow(stdscr, header_h, max_y, max_x)

        stdscr.refresh()

    def _render_wide(self, stdscr, start_y: int, max_y: int, max_x: int) -> None:
        """Render side-by-side layout for wide terminals (100+ cols)."""
        remaining = max_y - start_y
        # Give left column 45%, right 55% (patterns need bar room)
        left_w = int(max_x * 0.45)
        right_w = max_x - left_w

        # Left column: Trend (top), Sessions (bottom)
        trend_h = min(7, remaining // 3)
        sessions_h = remaining - trend_h

        draw_trend(stdscr, start_y, 0, left_w, trend_h, self.sessions[:50])

        # Ensure scroll offset keeps selected row visible
        visible_rows = sessions_h - 3
        if visible_rows > 0:
            if self.selected < self.scroll_offset:
                self.scroll_offset = self.selected
            elif self.selected >= self.scroll_offset + visible_rows:
                self.scroll_offset = self.selected - visible_rows + 1

        draw_sessions(
            stdscr, start_y + trend_h, 0, left_w, sessions_h,
            self.sessions, self.selected, self.scroll_offset,
        )

        # Right column: Patterns (top), Details (bottom)
        patterns_h = min(remaining // 2, max(5, len(set(
            p.get("type", "") for p in self.all_patterns
        )) + 2))
        patterns_h = min(patterns_h, remaining // 2)
        details_h = remaining - patterns_h

        draw_patterns(stdscr, start_y, left_w, right_w, patterns_h, self.all_patterns)
        draw_details(
            stdscr, start_y + patterns_h, left_w, right_w, details_h,
            self.selected_session, self.selected_tool_calls, self.selected_patterns,
        )

    def _render_narrow(self, stdscr, start_y: int, max_y: int, max_x: int) -> None:
        """Render stacked layout for narrow terminals (<100 cols)."""
        remaining = max_y - start_y
        width = max_x

        # Stack: Trend, Sessions, Details
        trend_h = min(5, remaining // 4)
        details_h = min(7, remaining // 3)
        sessions_h = remaining - trend_h - details_h

        draw_trend(stdscr, start_y, 0, width, trend_h, self.sessions[:50])

        # Ensure scroll offset keeps selected row visible
        visible_rows = sessions_h - 3
        if visible_rows > 0:
            if self.selected < self.scroll_offset:
                self.scroll_offset = self.selected
            elif self.selected >= self.scroll_offset + visible_rows:
                self.scroll_offset = self.selected - visible_rows + 1

        draw_sessions(
            stdscr, start_y + trend_h, 0, width, sessions_h,
            self.sessions, self.selected, self.scroll_offset,
        )

        draw_details(
            stdscr, start_y + trend_h + sessions_h, 0, width, details_h,
            self.selected_session, self.selected_tool_calls, self.selected_patterns,
        )


def _curses_main(stdscr, db: Database) -> None:
    """Curses main loop — called by curses.wrapper()."""
    # Setup
    curses.curs_set(0)  # Hide cursor
    stdscr.timeout(100)  # 100ms timeout for getch (allows resize detection)
    init_colors()

    app = TuiApp(db)
    app.render(stdscr)

    while True:
        key = stdscr.getch()

        if key == -1:
            # Timeout — no input, check for resize
            continue

        if key == ord("q") or key == ord("Q"):
            break

        elif key == curses.KEY_UP or key == ord("k"):
            app.move_selection(-1)

        elif key == curses.KEY_DOWN or key == ord("j"):
            app.move_selection(1)

        elif key == ord("g"):
            app.jump_top()

        elif key == ord("G"):
            app.jump_bottom()

        elif key == ord("r"):
            app.load_data()

        elif key == curses.KEY_RESIZE:
            stdscr.clear()

        app.render(stdscr)


def main(db: Database) -> None:
    """Entry point for the TUI dashboard.

    Wraps the curses application in curses.wrapper() for proper
    terminal cleanup on exit or crash.
    """
    curses.wrapper(lambda stdscr: _curses_main(stdscr, db))
