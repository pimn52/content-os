"""Separate audience content from execution controls without mutating evidence."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import re

from app.domain.models import IPProfile, Project, ProjectFormat, ScenePlan, SourceKind


SEMANTIC_EMPHASIS_RULE = "semantic_graphic_not_full_copy_v1"
_CONTROL_KEYS = {"planning_preferences", "production_feasibility", "source_use_constraints", "narration_timing", "continuous_production", "visual_revision"}
# Bounded regression guard for the observed workflow leak, not a general
# classifier or a ban on discussing preflight in audience-facing tutorials.
_WORKFLOW_PHRASES = ("退回正常预检", "缺短句→预检", "missingalternativesstillusenormalpreflight",
                     "missingalternativesstillfailnormalpreflight")


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text).casefold()


def short_video_project(project: Project) -> Project:
    """默认短视频始终先规划竖屏；横版由创作者明确选择。"""
    if project.format_policy == "fixed":
        return project
    short, long = sorted((project.resolution_width, project.resolution_height))
    if short * 16 != long * 9:
        short, long = 1080, 1920
    return project.model_copy(update={"format": ProjectFormat.VERTICAL,
        "resolution_width": short, "resolution_height": long})


def opening_context_gap(scenes: Sequence[ScenePlan], *, topic: str, script: str | None) -> bool:
    """拦住此次暴露的无主体比较残句；不改写用户原稿，不冒充通用语义审核。"""
    if script is not None or not scenes:
        return False
    opening = min(scenes, key=lambda scene: scene.order).voice_text.strip()
    sentence = re.split(r"[。？！?!]", opening, maxsplit=1)[0]
    # “码率越高越清晰吗”有主体；“越高越清晰吗”没有主体。
    return bool(re.match(r"^(?:那)?越(?:高|低|大|小|快|慢|多|少|贵|便宜)(?:就)?越", sentence))


def _factor_counts(scene: ScenePlan):
    graphic=scene.graphic_plan
    if graphic is None or graphic.kind!="diagram" or graphic.treatment!="diagram_factors":
        return ()
    counts={"二":2,"两":2,"三":3,"四":4}
    return tuple((match,counts[match[1]]) for match in re.finditer(r"(?<!第)([二两三四])者",scene.voice_text))


def normalize_automatic_graphics(scenes: Sequence[ScenePlan]) -> tuple[ScenePlan, ...]:
    """仅删除明确枚举之外的因素标签；不改旁白、不推断同义词或图形关系。"""
    normalized=[]
    for scene in scenes:
        counts=_factor_counts(scene)
        graphic=scene.graphic_plan
        if len(counts)!=1 or len(graphic.points)<=counts[0][1]:
            normalized.append(scene)
            continue
        match,count=counts[0]
        narration=scene.voice_text
        points=graphic.points
        kept=[point for point in points if point in narration]
        if (len(kept)!=count or len(set(points))!=len(points)
            or any(left!=right and (left in right or right in left) for left in kept for right in kept)
            or any(narration.find(point)>=match.start() for point in kept)):
            normalized.append(scene)
            continue
        ordered=sorted(kept,key=narration.index)
        enumeration=re.escape(ordered[0])
        for index,point in enumerate(ordered[1:],start=1):
            separator=r"(?:、|，|,)" if index<count-1 else r"(?:、|和|与|及)"
            enumeration+=r"\s*"+separator+r"\s*"+re.escape(point)
        # 只接受独立分句中的明确列举，避免把“高码率”中的“码率”等子串误当整项。
        enumeration=r"(?:^|[，,。；;：:！？!?])\s*"+enumeration+r"\s*[，,：:]?\s*(?:这)?"+re.escape(match[0])
        if re.search(enumeration,narration) is None:
            normalized.append(scene)
            continue
        normalized.append(scene.model_copy(update={"graphic_plan":graphic.model_copy(update={"points":kept})}))
    return tuple(normalized)


def automatic_graphic_gaps(scenes: Sequence[ScenePlan]) -> tuple[str, ...]:
    """自动制作不能通过申报独立字卡绕过缺素材，也不把旁白抄进图解。"""
    gaps=[]
    for scene in scenes:
        graphic=scene.graphic_plan
        if any(count!=len(graphic.points) for _,count in _factor_counts(scene)):
            gaps.append(f"graphic_narration_count_mismatch:{scene.scene_id}")
        if scene.preferred_sources[0] is not SourceKind.TYPOGRAPHY:
            continue
        if graphic is None or graphic.kind=="none":
            gaps.append(f"automatic_graphic_requires_plan:{scene.scene_id}")
        elif graphic.kind == "text_card":
            gaps.append(f"automatic_text_card_requires_media:{scene.scene_id}")
        elif graphic.kind == "diagram":
            narration=_compact(scene.voice_text)
            if len(narration)>=12 and any(_compact(point)==narration for point in graphic.points):
                gaps.append(f"diagram_repeats_narration:{scene.scene_id}")
    return tuple(gaps)


def creator_planning_brief(profile: IPProfile) -> dict[str, object]:
    """Positioning only: omit identity, private knowledge and arbitrary metadata."""
    def text(value: str) -> str:
        return re.sub(r"https?://\S+|[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}|(?<!\d)\+?\d[\d -]{9,}\d(?!\d)", "[redacted]", value)
    return {
        "domains": [text(v)[:80] for v in profile.domains[:5]],
        "audience": text(profile.audience or "")[:200],
        "style_notes": [text(v)[:160] for v in profile.style_notes[:5]],
        "boundaries": [text(v) for v in profile.boundaries],
        "avoided_expressions": [text(v) for v in profile.avoided_expressions],
    }


def planning_request(project: Project, *, topic: str, script: str | None,
                     context: Mapping[str, object] | None) -> dict[str, object]:
    project = short_video_project(project)
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
            "presentation_style": project.presentation_style,
        },
    }


def presentation_plan_gaps(project: Project, scenes: Sequence[ScenePlan]) -> tuple[str, ...]:
    """Keep the creator's saved presentation requirement through planning and dispatch."""
    if project.presentation_style != "creator_led":
        return ()
    gaps: list[str] = []
    speaking_sources = {SourceKind.USER_ASSET, SourceKind.HISTORICAL_ASSET,
                        SourceKind.AI_VIDEO, SourceKind.TALKING_PROFILE, SourceKind.CAPTURE}
    if not any(scene.visual_requirement == "creator_speaking"
               and scene.preferred_sources[0] in speaking_sources for scene in scenes):
        gaps.append("creator_led_missing_speaking")
    # 本人讲解可由已授权 Talking 承担；是否需要实拍由具体事实决定。
    # 内容质量另做检查，来源标签和固定字卡比例不能代替整片判断。
    return tuple(gaps)


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
