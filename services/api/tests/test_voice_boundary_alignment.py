from app.voice_boundary_alignment import align_voice_boundaries


def _evidence(words: list[tuple[int, int, str]]) -> dict:
    return {"source_sha256": "sha", "words": [
        {"start_ms": start, "end_ms": end, "text": text}
        for start, end, text in words
    ]}


def test_exact_words_and_contained_pcm_quiet_identify_only_supported_boundary() -> None:
    result = align_voice_boundaries(
        "先说。再说。", _evidence([(0, 300, "先说"), (500, 800, "再说")]),
        [{"start_ms": 320, "end_ms": 480, "duration_ms": 160}],
        source_sha256="sha", duration_ms=1000,
    )
    assert result["state"] == "aligned"
    first = next(item for item in result["boundaries"] if item["char_index"] == 3)
    assert first["physical_quiet_ms"] == 160
    assert first["physical_quiet_candidate_ms"] is None


def test_word_alignment_withholds_mismatch_overlap_stale_and_crossing_quiet() -> None:
    base = _evidence([(0, 300, "先说"), (500, 800, "再说")])
    quiet = [{"start_ms": 250, "end_ms": 450, "duration_ms": 200}]
    crossing = align_voice_boundaries("先说。再说。", base, quiet,
                                      source_sha256="sha", duration_ms=1000)
    first = next(item for item in crossing["boundaries"] if item["char_index"] == 3)
    assert first["physical_quiet_ms"] is None
    assert first["physical_quiet_candidate_ms"] == 200
    assert align_voice_boundaries("先说。再说。", base, quiet,
                                  source_sha256="other", duration_ms=1000)["state"] == "unavailable"
    assert align_voice_boundaries("先说。再说。", _evidence([(0, 300, "先先说"), (500, 800, "再说")]), quiet,
                                  source_sha256="sha", duration_ms=1000)["reason"] == "asr_word_tokens_do_not_exactly_match_copy"
    assert align_voice_boundaries("先说。再说。", _evidence([(0, 500, "先说"), (400, 800, "再说")]), quiet,
                                  source_sha256="sha", duration_ms=1000)["reason"] == "asr_word_invalid_or_overlapping"


def test_boundary_inside_multicharacter_asr_word_is_unavailable() -> None:
    result = align_voice_boundaries("判断，决定。", _evidence([(0, 400, "判断决定")]), [],
                                    source_sha256="sha", duration_ms=500)
    assert result["state"] == "aligned"
    comma = next(item for item in result["boundaries"] if item["char_index"] == 3)
    assert comma["timing_state"] == "unavailable"
