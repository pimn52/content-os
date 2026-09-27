"""The bounded duration edit preserves PCM order and hard-fails unsafe joins."""

from array import array
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
from evaluate_v42_tail_duration import assemble, boundary_dbfs, join_crossfade  # noqa: E402


def test_crossfade_has_predictable_length_and_endpoints() -> None:
    left = array("h", [100] * 300)
    right = array("h", [200] * 300)
    joined = join_crossfade(left, right, 24)
    assert len(joined) == 576
    assert joined[0] == 100
    assert joined[-1] == 200
    assert 100 < joined[276] < 200


def test_assembly_rejects_overlapping_or_out_of_range_windows() -> None:
    source = array("h", [0] * 24000)
    with pytest.raises(ValueError):
        assemble(source, (("a", 100, 200), ("b", 150, 250)),
                 [array("h", [0] * 1200), array("h", [0] * 1200)])
    with pytest.raises(ValueError):
        boundary_dbfs(source, 0)
