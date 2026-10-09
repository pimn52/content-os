from scripts.evaluate_v73a_face_coverage import _status


def test_face_outside_exact_crop_is_a_negative_geometry_decision() -> None:
    crop = (460, 0, 360, 640)
    assert _status({"x": 459, "y": 200, "width": 200, "height": 200}, crop) == "face_outside_crop"
    assert _status({"x": 460, "y": 200, "width": 200, "height": 200}, crop) == "face_box_inside_crop"


def test_missing_detection_remains_unknown() -> None:
    assert _status(None, (460, 0, 360, 640)) == "unknown_face_detection"
