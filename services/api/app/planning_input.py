"""Separate audience content from execution controls without mutating evidence."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import re

from app.domain.models import Project, ScenePlan


SEMANTIC_EMPHASIS_RULE = "semantic_graphic_not_full_copy_v1"
_CONTROL_KEYS = {"planning_preferences", "production_feasibility", "source_use_constraints"}
# Bounded regression guard for the observed workflow leak, not a general
# classifier or a ban on discussing preflight in audience-facing tutorials.
_WORKFLOW_PHRASES = ("退回正常预检", "缺短句→预检", "missingalternativesstillusenormalpreflight",
                     "missingalternativesstillfailnormalpreflight")


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text).casefold()


def planning_request(project: Project, *, topic: str, script: str | None,
                     context: Mapping[str, object] | None) -> dict[str, object]:
    source = dict(context or {})
    controls = {key: source.pop(key) for key in sorted(_CONTROL_KEYS) if key in source}
    preferences = controls.get("planning_preferences")
    if isinstance(preferences, list):
        # Known adopted rule gets a declarative style representation, not the
        # internal missing-alternative/preflight procedure. Unknown rules stay
        # in the control-only compartment, never silently interpreted as style.
        controls["planning_preferences"] = [
            {"id": value.get("id"), "version": value.get("version"), "rule": value["rule"],
             "style": "Prefer a supplied semantic short emphasis over repeating the full narration; preserve speech.",
             "authority": value.get("authority_source"), "evidence_level": value.get("evidence_level")}
            if isinstance(value, dict) and value.get("rule") == SEMANTIC_EMPHASIS_RULE else value
            for value in preferences
        ]
    return {
        "audience_brief": {"title": project.title, "topic": topic, "user_script": script},
        "background_evidence": source,
        "production_controls": {
            "output_format": {"format": project.format.value, "width": project.resolution_width,
                              "height": project.resolution_height, "fps": project.fps.model_dump(mode="json")},
            **controls,
        },
    }


def audience_control_leak_field(scenes: Sequence[ScenePlan], *, topic: str,
                               script: str | None, context: Mapping[str, object] | None) -> str | None:
    """Return only a fixed field identity; never quote creator/provider text."""
    preferences = (context or {}).get("planning_preferences", [])
    if not isinstance(preferences, list) or not any(
        isinstance(value, dict) and value.get("rule") == SEMANTIC_EMPHASIS_RULE for value in preferences
    ):
        return None
    explicit = _compact(topic + "\n" + (script or ""))
    # The topic or supplied script, not metadata/material/control prose, may
    # explicitly request a tutorial about the product's preflight procedure.
    product_tutorial = "contentos" in explicit and ("预检" in explicit or "preflight" in explicit)
    for scene in scenes:
        for field, texts in (("voice_text", [scene.voice_text]), ("caption_emphasis", scene.caption_emphasis),
                             ("graphic_plan", scene.graphic_plan.points if scene.graphic_plan is not None else [])):
            for text in texts:
                normalized = _compact(text)
                if any(phrase in normalized and phrase not in explicit for phrase in _WORKFLOW_PHRASES) and not product_tutorial:
                    return field
    return None
