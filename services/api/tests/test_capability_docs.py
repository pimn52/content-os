from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))

from sync_capability_docs import check_generated_documents, validate_registry


def test_capability_registry_and_generated_indexes_are_current() -> None:
    errors, registry = validate_registry(ROOT)
    assert errors == []
    assert registry is not None
    assert check_generated_documents(ROOT) == []

    identifiers = {module["id"] for module in registry["modules"]}
    assert {"scene_planning", "voice_generation", "talking_generation", "production_scheduling"} <= identifiers


def test_legacy_voice_talking_page_is_only_a_redirect() -> None:
    text = (ROOT / "docs/product/VOICE_TALKING.md").read_text(encoding="utf-8")
    assert "VOICE.md" in text
    assert "TALKING.md" in text
    assert "本页不再定义行为" in text
