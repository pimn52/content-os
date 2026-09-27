"""V39's evaluation edit must preserve every frame outside declared cuts."""

from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
from evaluate_v39_quiet_compaction import compact_pcm, verify_quiet_envelopes  # noqa: E402


def test_compact_pcm_keeps_every_noncut_frame_byte_exact() -> None:
    raw = b"".join(i.to_bytes(2, "little", signed=True) for i in range(24000))
    output = compact_pcm(raw, rate=24000, cuts=(("a", 100, 200), ("b", 500, 550)))
    expected = raw[:2400 * 2] + raw[4800 * 2:12000 * 2] + raw[13200 * 2:]
    assert output == expected
    assert len(output) == (24000 - 2400 - 1200) * 2


@pytest.mark.parametrize("cuts", [
    (("a", 200, 100),),
    (("a", 100, 200), ("b", 150, 250)),
    (("a", 900, 1100),),
])
def test_compact_pcm_rejects_unsafe_ranges(cuts: tuple) -> None:
    with pytest.raises(ValueError):
        compact_pcm(bytes(24000 * 2), rate=24000, cuts=cuts)


def test_quiet_check_requires_enclosure_and_margins() -> None:
    quiet = [{"start_ms": 100, "end_ms": 300}]
    verify_quiet_envelopes(quiet, (("safe", 140, 260),))
    with pytest.raises(ValueError):
        verify_quiet_envelopes(quiet, (("edge", 120, 260),))
