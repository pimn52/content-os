from pathlib import Path

import pytest

from app.media.subtitles import SubtitleParseError, parse_subtitle_file, parse_subtitle_text


def test_parse_srt_preserves_exact_millisecond_intervals_and_caption_text():
    result = parse_subtitle_text(
        "1\n00:00:01,005 --> 00:00:02,010\nfirst\n\n"
        "2\n00:00:02,011 --> 00:00:03,999\n<b>真实</b> 时间轴\n第二行\n"
    )

    assert [(item.start_ms, item.end_ms, item.text) for item in result] == [
        (1005, 2010, "first"),
        (2011, 3999, "真实 时间轴 第二行"),
    ]


def test_parse_vtt_skips_header_and_metadata_blocks():
    result = parse_subtitle_text(
        "WEBVTT\n\n"
        "NOTE\nthis is metadata\n\n"
        "00:01.005 --> 00:02.250 align:start\nfirst\n\n"
        "00:00:03.000 --> 00:00:04.001\nsecond\n"
    )

    assert [(item.start_ms, item.end_ms, item.text) for item in result] == [
        (1005, 2250, "first"),
        (3000, 4001, "second"),
    ]


def test_parse_subtitle_file_requires_supported_utf8_file(tmp_path: Path):
    source = tmp_path / "captions.srt"
    source.write_text("1\n00:00:00,000 --> 00:00:00,500\nprovided\n", encoding="utf-8")
    unsupported = tmp_path / "captions.txt"
    unsupported.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")

    assert parse_subtitle_file(source)[0].text == "provided"
    with pytest.raises(SubtitleParseError, match=r"\.srt or \.vtt"):
        parse_subtitle_file(unsupported)


def test_parse_subtitle_rejects_invalid_or_empty_timing():
    with pytest.raises(SubtitleParseError, match="no timed text"):
        parse_subtitle_text("not a subtitle")
    with pytest.raises(SubtitleParseError, match="timestamp is invalid"):
        parse_subtitle_text("1\n00:61:00,000 --> 00:62:00,000\ninvalid\n")
