"""V20B rollback must preserve the entire V19E first-clause performance."""
from __future__ import annotations

import math
import struct
import wave

from app.runtime import resolve_local_executable
from scripts.evaluate_c_focus_rollback import render_landing_only


def test_rollback_retains_original_first_clause_and_only_edits_landing(tmp_path) -> None:
    count = 10_000 * 24
    original = struct.pack(
        f"<{count}h", *(round(2000 * math.sin(2 * math.pi * 180 * index / 24_000)) for index in range(count))
    )
    source = tmp_path / "source.wav"
    with wave.open(str(source), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(24_000)
        handle.writeframes(original)
    with wave.open(str(source), "rb") as handle:
        pcm = handle.readframes(handle.getnframes())
    rendered, gains, tail = render_landing_only(pcm, ffmpeg=resolve_local_executable("ffmpeg"))
    assert rendered[:8960 * 48] == pcm[:8960 * 48]
    assert len(gains) == 1
    assert gains[0]["start_ms"] == 8960
    assert gains[0]["end_ms"] == 9240
    assert tail["output_frame_delta"] > 0
    assert len(rendered) > len(original)
    output = tmp_path / "rollback.wav"
    with wave.open(str(output), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(24_000)
        handle.writeframes(rendered)
    with wave.open(str(output), "rb") as handle:
        assert handle.getnframes() == len(rendered) // 2
