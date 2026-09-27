"""Narrow, auditable provider-facing punctuation deletion for Voice copy."""
from __future__ import annotations

import unicodedata

from app.voice_qa import comparison_tokens


class VoiceDeliveryTextError(ValueError):
    pass


def validate_voice_delivery_text(editorial_copy: str, delivery_text: str) -> None:
    """Allow deletion of existing punctuation/whitespace, never spoken glyphs.

    Token equivalence alone is too permissive because QA normalization can
    treat distinct glyphs as equivalent. The exact-character subsequence
    check prevents that from becoming an unintended copy-rewrite route.
    """
    if not editorial_copy.strip() or not delivery_text.strip():
        raise VoiceDeliveryTextError("Voice copy and delivery text must be non-empty")
    cursor = 0
    deleted = False
    for character in editorial_copy:
        if cursor < len(delivery_text) and character == delivery_text[cursor]:
            cursor += 1
        elif character.isspace() or unicodedata.category(character).startswith("P"):
            deleted = True
        else:
            raise VoiceDeliveryTextError("Voice delivery text may delete only existing punctuation or whitespace")
    if cursor != len(delivery_text) or not deleted:
        raise VoiceDeliveryTextError("Voice delivery text must be a punctuation/spacing deletion of the editorial copy")
    if comparison_tokens(editorial_copy) != comparison_tokens(delivery_text):
        raise VoiceDeliveryTextError("Voice delivery text changes the spoken token sequence")


__all__ = ["VoiceDeliveryTextError", "validate_voice_delivery_text"]
