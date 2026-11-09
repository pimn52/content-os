from app.media.fingerprint import FingerprintError, dhash64, fingerprint_distance, hamming


def _ramp(reverse: bool = False) -> bytes:
    values = bytes(index * 3 for index in range(9))
    return b"".join(values[::-1] if reverse else values for _ in range(8))


def test_dhash_is_stable_for_gray_9x8_frames() -> None:
    increasing = dhash64(_ramp())
    decreasing = dhash64(_ramp(reverse=True))

    assert increasing == "0000000000000000"
    assert decreasing == "ffffffffffffffff"
    assert hamming(increasing, decreasing) == 64


def test_distance_is_tolerant_of_changed_scene_boundaries() -> None:
    same = {"samples": [{"hash": "ffffffffffffffff"}, {"hash": "0000000000000000"}]}
    shifted = {"samples": [{"hash": "0000000000000000"}, {"hash": "ffffffffffffffff"}]}
    unrelated = {"samples": [{"hash": "aaaaaaaaaaaaaaaa"}, {"hash": "5555555555555555"}]}

    assert fingerprint_distance(same, shifted) == 0
    assert fingerprint_distance(same, unrelated) == 32


def test_invalid_fingerprint_input_fails_explicitly() -> None:
    try:
        dhash64(b"short")
    except FingerprintError:
        pass
    else:
        raise AssertionError("invalid gray buffer must fail")
