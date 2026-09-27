"""V41 must not change spoken words or cut nonquiet Master seams."""

from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
from evaluate_v41_punctuation_delivery import seam_cuts, validate_delivery  # noqa: E402


def test_delivery_removes_punctuation_only() -> None:
    validate_delivery("判断，再用例子。", "判断再用例子。")
    with pytest.raises(ValueError):
        validate_delivery("判断，再用例子。", "判断用例子。")
    with pytest.raises(ValueError):
        validate_delivery("判断，再用例子。", "判断再用示例。")


def test_known_seam_cut_requires_measured_quiet() -> None:
    quiet = [{"start_ms": 900, "end_ms": 1140, "duration_ms": 240}]
    assert seam_cuts(quiet, [1000]) == (("seam_0_1", 960, 1080),)
    with pytest.raises(ValueError):
        seam_cuts(quiet, [1200])
