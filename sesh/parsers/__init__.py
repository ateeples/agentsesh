"""Parser registry with auto-detection."""

from pathlib import Path

from .base import BaseParser, NormalizedSession
from .claude_code import ClaudeCodeParser
from .openai_codex import OpenAICodexParser

# Parser registry — tried in priority order
_PARSERS: list[type[BaseParser]] = [
    ClaudeCodeParser,
    OpenAICodexParser,
    # GenericParser,  # future
]


def auto_detect_parser(file_path: Path) -> type[BaseParser] | None:
    """Find the first parser that can handle this file."""
    for parser_cls in _PARSERS:
        if parser_cls.can_parse(file_path):
            return parser_cls
    return None


def parse_transcript(file_path: Path, format_hint: str | None = None) -> NormalizedSession:
    """Parse a transcript file, auto-detecting format.

    Args:
        file_path: Path to the transcript file.
        format_hint: Optional format name to skip auto-detection.

    Returns:
        NormalizedSession with all data extracted.

    Raises:
        ValueError: If no parser can handle the file.
    """
    path = Path(file_path)

    if format_hint:
        for parser_cls in _PARSERS:
            if parser_cls.format_name == format_hint:
                return parser_cls.parse(path)
        raise ValueError(f"Unknown format: {format_hint}")

    parser_cls = auto_detect_parser(path)
    if parser_cls is None:
        raise ValueError(
            f"Cannot auto-detect format for {path.name}. "
            f"Supported formats: {', '.join(p.format_name for p in _PARSERS)}"
        )
    return parser_cls.parse(path)
