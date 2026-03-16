"""Reusable drawing primitives for the sesh TUI.

Provides box drawing, bar charts, sparklines, and text utilities.
All functions operate on curses windows/pads via addstr calls.
"""

import contextlib
import curses

# Block characters for sparklines (8 levels)
SPARK_CHARS = "▁▂▃▄▅▆▇█"

# Block characters for horizontal bars
BAR_FULL = "█"
BAR_EMPTY = "░"


def init_colors() -> None:
    """Initialize curses color pairs for grade coloring."""
    curses.start_color()
    curses.use_default_colors()

    # Color pair assignments:
    # 1 = green (A/A+ grades)
    # 2 = cyan (B grades)
    # 3 = yellow (C grades)
    # 4 = red (D/F grades)
    # 5 = white bold (headers)
    # 6 = dim/gray (secondary text)
    curses.init_pair(1, curses.COLOR_GREEN, -1)
    curses.init_pair(2, curses.COLOR_CYAN, -1)
    curses.init_pair(3, curses.COLOR_YELLOW, -1)
    curses.init_pair(4, curses.COLOR_RED, -1)
    curses.init_pair(5, curses.COLOR_WHITE, -1)
    curses.init_pair(6, curses.COLOR_WHITE, -1)


def grade_color(grade: str) -> int:
    """Return curses color pair for a letter grade."""
    if grade in ("A+", "A"):
        return curses.color_pair(1) | curses.A_BOLD
    elif grade == "B":
        return curses.color_pair(2)
    elif grade == "C":
        return curses.color_pair(3)
    elif grade in ("D", "F"):
        return curses.color_pair(4) | curses.A_BOLD
    return curses.color_pair(0)


def header_attr() -> int:
    """Return attribute for header/label text."""
    return curses.color_pair(5) | curses.A_BOLD


def dim_attr() -> int:
    """Return attribute for dim/secondary text."""
    return curses.A_DIM


def safe_addstr(win, y: int, x: int, text: str, attr: int = 0) -> None:
    """Write string to window, silently ignoring out-of-bounds writes."""
    max_y, max_x = win.getmaxyx()
    if y < 0 or y >= max_y or x >= max_x:
        return
    # Truncate text to fit within the window width
    available = max_x - x
    if available <= 0:
        return
    truncated = text[:available]
    with contextlib.suppress(curses.error):
        win.addstr(y, x, truncated, attr)


def draw_box(win, y: int, x: int, height: int, width: int, title: str = "") -> None:
    """Draw a rounded box with optional title.

    Uses ╭╮╰╯ corners and ─│ edges.
    """
    max_y, max_x = win.getmaxyx()
    if y >= max_y or x >= max_x:
        return

    # Clamp dimensions to fit
    width = min(width, max_x - x)
    height = min(height, max_y - y)
    if width < 4 or height < 2:
        return

    attr = dim_attr()

    # Top border
    top = (
        "╭─ " + title + " " + "─" * max(0, width - len(title) - 5) + "╮"
        if title
        else "╭" + "─" * (width - 2) + "╮"
    )
    safe_addstr(win, y, x, top[:width], attr)

    # Side borders
    for row in range(1, height - 1):
        safe_addstr(win, y + row, x, "│", attr)
        safe_addstr(win, y + row, x + width - 1, "│", attr)

    # Bottom border
    bottom = "╰" + "─" * (width - 2) + "╯"
    safe_addstr(win, y + height - 1, x, bottom[:width], attr)


def sparkline(values: list[int | float], width: int | None = None) -> str:
    """Render a sparkline string from numeric values.

    Maps values to 8-level block characters. If width is specified,
    resamples the values to fit.
    """
    if not values:
        return ""

    # Resample if needed
    if width and len(values) > width:
        step = len(values) / width
        resampled = []
        for i in range(width):
            idx = int(i * step)
            resampled.append(values[idx])
        values = resampled
    elif width and len(values) < width:
        # Pad with the last value or just use what we have
        pass

    lo = min(values)
    hi = max(values)
    spread = hi - lo if hi != lo else 1

    chars = []
    for v in values:
        idx = min(7, int((v - lo) / spread * 7))
        chars.append(SPARK_CHARS[idx])
    return "".join(chars)


def horizontal_bar(value: int, max_value: int, width: int) -> str:
    """Render a horizontal bar chart segment.

    Returns a string of width characters using filled/empty blocks.
    """
    if max_value <= 0:
        return BAR_EMPTY * width
    filled = min(width, max(0, int(value / max_value * width)))
    return BAR_FULL * filled + BAR_EMPTY * (width - filled)


def truncate(text: str, width: int, ellipsis: str = "...") -> str:
    """Truncate text to width, adding ellipsis if needed."""
    if len(text) <= width:
        return text
    if width <= len(ellipsis):
        return text[:width]
    return text[: width - len(ellipsis)] + ellipsis


def format_duration(minutes: float | None) -> str:
    """Format duration in minutes to a compact string."""
    if minutes is None:
        return "  ?m"
    m = int(minutes)
    if m < 60:
        return f"{m:>3d}m"
    h = m // 60
    rem = m % 60
    return f"{h}h{rem:02d}"


def format_date(timestamp: str | None) -> str:
    """Format an ISO timestamp to MM-DD HH:MM in local time."""
    if not timestamp:
        return "          "
    try:
        from datetime import datetime, timezone
        ts = timestamp.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
        # Convert to local time
        local_dt = dt.astimezone()
        return local_dt.strftime("%m-%d %H:%M")
    except (IndexError, ValueError, TypeError):
        return timestamp[:11] if len(timestamp) >= 11 else timestamp
