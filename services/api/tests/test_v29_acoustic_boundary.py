"""Keep manufactured seam truth distinct from new-copy natural pause inference."""
import pytest

from scripts.evaluate_v29_acoustic_boundary import ROOT, annotated_seams, evaluate


def test_exact_composer_seam_frames_include_only_inserted_pause() -> None:
    assert annotated_seams([24_000, 48_000, 12_000], [80, 70, 0], 24_000) == [
        (24_000, 25_920), (73_920, 75_600)]
    with pytest.raises(ValueError, match="whole PCM frames"):
        annotated_seams([100, 100], [1, 0], 22_050)


@pytest.mark.skipif(
    not (ROOT / "content-os-data" / "evaluation-evidence" / "v24-pace-coherence" / "v24-pace-coherence-master.wav").is_file(),
    reason="local retained Voice evaluation WAVs are not part of the source checkout",
)
def test_real_retained_controls_do_not_prove_new_copy_boundary_generalization() -> None:
    result = evaluate()
    assert [len(item["annotated_seams"]) for item in result["compositions"]] == [2, 2]
    assert all(seam["pcm_quiet_overlapping_seam"] for item in result["compositions"]
               for seam in item["annotated_seams"])
    assert result["shortest_matched_composed_join_quiet_ms"] == 170
    assert len([item for item in result["different_copy_raw_controls"]
                if item["interior_quiet_at_least_shortest_composed_join"]]) == 2
    assert result["generalization_state"] == "unsupported_no_independent_copy_boundary_annotations"
