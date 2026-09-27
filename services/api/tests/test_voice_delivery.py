import pytest

from app.voice_delivery import VoiceDeliveryTextError, validate_voice_delivery_text


def test_delivery_text_can_delete_only_punctuation_and_spacing_from_exact_copy() -> None:
    copy = "观众刷到视频的前几秒，会先问：这件事跟我有什么关系？"
    validate_voice_delivery_text(copy, "观众刷到视频的前几秒会先问这件事跟我有什么关系？")
    validate_voice_delivery_text("先回答， 再说明。", "先回答再说明。")


@pytest.mark.parametrize("copy, delivery", [
    ("先回答，再说明。", "先回答，再介绍。"),
    ("先回答，再说明。", "先回答，再说明！"),
    ("先回答，再说明。", "先回答，再说明。还有一句"),
    ("先回答，再说明。", "先回答，再说明。"),
    ("先回答，再说明。", "先回答，再说明。。"),
])
def test_delivery_text_rejects_glyph_changes_insertion_or_noop(copy: str, delivery: str) -> None:
    with pytest.raises(VoiceDeliveryTextError):
        validate_voice_delivery_text(copy, delivery)
