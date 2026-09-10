"""Strict local SRT/VTT import for real, user-provided timed subtitles."""
from __future__ import annotations

import re
from pathlib import Path

from app.providers.asr import TranscriptionSegment


class SubtitleParseError(ValueError):
    """The supplied subtitle file is not a usable timed transcript."""


_TIMING = re.compile(
    r"^\s*(?P<start>(?:\d{1,2}:)?\d{2}:\d{2}[,.]\d{3}|\d{1,2}:\d{2}[,.]\d{3})\s+-->\s+"
    r"(?P<end>(?:\d{1,2}:)?\d{2}:\d{2}[,.]\d{3}|\d{1,2}:\d{2}[,.]\d{3})(?:\s+.*)?$"
)
_MARKUP = re.compile(r"<[^>]+>")


def parse_subtitle_file(path: str | Path) -> tuple[TranscriptionSegment, ...]:
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(source)
    if source.suffix.lower() not in {".srt", ".vtt"}:
        raise SubtitleParseError("subtitle file must use .srt or .vtt")
    try:
        text = source.read_text(encoding="utf-8-sig")
    except UnicodeError as exc:
        raise SubtitleParseError("subtitle file must be UTF-8") from exc
    return parse_subtitle_text(text)


def parse_subtitle_text(text: str) -> tuple[TranscriptionSegment, ...]:
    if not isinstance(text, str) or not text.strip():
        raise SubtitleParseError("subtitle file is empty")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    blocks = re.split(r"\n\s*\n", normalized.strip())
    segments: list[TranscriptionSegment] = []
    for block in blocks:
        lines = [line.strip() for line in block.split("\n")]
        if not lines:
            continue
        if lines[0].upper() == "WEBVTT":
            continue
        if lines[0].upper() in {"NOTE", "STYLE", "REGION"}:
            continue
        timing_index = next((index for index, line in enumerate(lines) if "-->" in line), None)
        if timing_index is None:
            # SRT index or VTT NOTE/STYLE/REGION block.
            continue
        match = _TIMING.match(lines[timing_index])
        if match is None:
            raise SubtitleParseError("subtitle timing line is invalid")
        caption = " ".join(_MARKUP.sub("", line) for line in lines[timing_index + 1:] if line)
        caption = " ".join(caption.split())
        if not caption:
            continue
        start_ms = _timestamp_ms(match.group("start"))
        end_ms = _timestamp_ms(match.group("end"))
        try:
            segments.append(TranscriptionSegment(start_ms, end_ms, caption))
        except ValueError as exc:
            raise SubtitleParseError("subtitle interval is invalid") from exc
    if not segments:
        raise SubtitleParseError("subtitle file contains no timed text")
    ordered = tuple(sorted(segments, key=lambda segment: (segment.start_ms, segment.end_ms, segment.text)))
    return ordered


def _timestamp_ms(value: str) -> int:
    parts = value.replace(",", ".").split(":")
    try:
        if len(parts) == 2:
            hours = 0
            minutes, seconds_text = int(parts[0]), parts[1]
        elif len(parts) == 3:
            hours, minutes, seconds_text = int(parts[0]), int(parts[1]), parts[2]
        else:
            raise ValueError
        whole_seconds, separator, fraction = seconds_text.partition(".")
        if not separator or len(fraction) != 3 or not fraction.isdigit():
            raise ValueError
        seconds = int(whole_seconds)
        milliseconds_part = int(fraction)
    except (TypeError, ValueError) as exc:
        raise SubtitleParseError("subtitle timestamp is invalid") from exc
    if hours < 0 or minutes < 0 or minutes >= 60 or seconds < 0 or seconds >= 60:
        raise SubtitleParseError("subtitle timestamp is invalid")
    milliseconds = (hours * 3_600 + minutes * 60 + seconds) * 1_000 + milliseconds_part
    if milliseconds < 0:
        raise SubtitleParseError("subtitle timestamp is invalid")
    return milliseconds
