"""Separate audience content from execution controls without mutating evidence."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import re

from app.domain.models import CreatorFit, GraphicCardPlan, IPProfile, Project, ProjectFormat, ScenePlan, SourceKind


SEMANTIC_EMPHASIS_RULE = "semantic_graphic_not_full_copy_v1"
_CONTROL_KEYS = {"planning_preferences", "production_feasibility", "source_use_constraints", "narration_timing", "continuous_production", "visual_revision"}
# Bounded regression guard for the observed workflow leak, not a general
# classifier or a ban on discussing preflight in audience-facing tutorials.
_WORKFLOW_PHRASES = ("退回正常预检", "缺短句→预检", "missingalternativesstillusenormalpreflight",
                     "missingalternativesstillfailnormalpreflight")
_PROFILE_QUERY_STOP_TERMS = {
    "一个", "这个", "那个", "什么", "为什么", "如何", "怎么", "可以", "需要",
    "视频", "内容", "短视", "短视频",
}


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text).casefold()


def explicit_visual_constraints(topic: str | None, script: str | None) -> tuple[bool, bool]:
    """Read explicit creator-absence and local-graphics-only instructions."""
    compact = _compact("\n".join(value for value in (topic, script) if value))
    no_creator = any(pattern in compact for pattern in (
        "不要本人出镜", "不需要本人出镜", "不使用本人出镜", "不要创作者出镜",
        "不需要创作者出镜", "不使用创作者出镜", "不要a-roll", "不要aroll",
        "nocreatoroncamera", "withoutcreatoroncamera",
    ))
    local_graphics_only = any(pattern in compact for pattern in (
        "全片只用本地图解", "全片只使用本地图解", "全片只用图解",
        "只使用本地图解和文字要点", "全片只用文字要点",
        "不使用任何外部实拍或ai画面", "不使用外部实拍或ai画面",
    ))
    return no_creator, local_graphics_only


def normalize_explicit_visual_constraints(
    scenes: Sequence[ScenePlan], *, topic: str | None, script: str | None,
) -> tuple[ScenePlan, ...]:
    """Make planner output obey explicit no-creator or local-graphics-only requests."""
    no_creator, local_only = explicit_visual_constraints(topic, script)
    if not no_creator and not local_only:
        return tuple(scenes)
    normalized = []
    for scene in scenes:
        local_text_missing = local_only and (
            scene.graphic_plan is None
            or scene.graphic_plan.kind in {"unsupported", "none"}
        )
        must_replace = (
            (no_creator and scene.visual_requirement == "creator_speaking")
            or (local_only and scene.preferred_sources and scene.preferred_sources[0] is not SourceKind.TYPOGRAPHY)
            or local_text_missing
        )
        if not must_replace:
            normalized.append(scene)
            continue
        graphic = scene.graphic_plan
        candidates = _graphic_label_candidates(scene)
        if local_text_missing:
            purpose = scene.purpose.casefold()
            if scene.order == 0 or "hook" in purpose or "提出" in purpose:
                treatment, points = "headline", candidates[:1]
            elif ("对比" in purpose or "versus" in purpose) and len(candidates) >= 2:
                treatment, points = "contrast", candidates[:2]
            else:
                treatment, points = "key_point", candidates[:4]
            if not points:
                normalized.append(scene)
                continue
            graphic = GraphicCardPlan(kind="text_card", treatment=treatment, points=points)
        elif graphic is None or graphic.kind != "diagram":
            points = []
            for candidate in candidates:
                if len(points) == 4 or sum(map(len, points)) + len(candidate) > 72:
                    continue
                points.append(candidate)
            if len(points) < 2:
                normalized.append(scene)
                continue
            graphic = GraphicCardPlan(kind="diagram", treatment="diagram_flow", points=points)
        reason = "；".join(
            item for item in (
                "创作者明确要求不使用本人出镜" if no_creator else "",
                "创作者明确要求仅使用本地图解和文字要点" if local_only else "",
            ) if item
        )
        normalized.append(scene.model_copy(update={
            "visual_requirement": "explanatory",
            "visual_requirement_reason": reason,
            "supporting_visual_intent": None,
            "graphic_plan": graphic,
            "preferred_sources": [SourceKind.TYPOGRAPHY],
            "fallback_sources": [],
            "preferred_clip_id": None,
            "material_request": None,
            "visual_intent": scene.visual_intent.model_copy(update={
                "subject": "本地文字卡" if graphic.kind == "text_card" else "本地流程图解",
                "action": ("用本地文字卡突出场景要点" if graphic.kind == "text_card"
                           else "按场景逻辑呈现要点与关系"),
                "framing": "竖屏全幅图解",
            }),
        }))
    return tuple(normalized)


def _graphic_label_candidates(scene: ScenePlan) -> list[str]:
    candidates = []
    for value in (*scene.caption_emphasis, scene.viewer_takeaway or "", scene.purpose):
        compact = re.sub(r"\s+", "", value or "").strip()
        if not compact:
            continue
        parts = [part.strip("，,。；;：:！？!? ") for part in re.split(r"[，,。；;：:！？!?]", compact)]
        candidate = next((part for part in parts if part and len(part) <= 24),
                         compact[:24].rstrip("，,。；;：:！？!? "))
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    return candidates


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


def automatic_graphic_gaps(
    scenes: Sequence[ScenePlan],
    *,
    allow_typography_text_card: bool = False,
) -> tuple[str, ...]:
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
        elif graphic.kind == "text_card" and not allow_typography_text_card:
            gaps.append(f"automatic_text_card_requires_media:{scene.scene_id}")
        elif graphic.kind == "diagram":
            narration=_compact(scene.voice_text)
            if len(narration)>=12 and any(_compact(point)==narration for point in graphic.points):
                gaps.append(f"diagram_repeats_narration:{scene.scene_id}")
    return tuple(gaps)


def _profile_text(value: str, limit: int) -> str:
    return re.sub(
        r"https?://\S+|[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}|(?<!\d)\+?\d[\d -]{9,}\d(?!\d)",
        "[redacted]",
        value,
    ).strip()[:limit]


def _query_terms(value: str | None) -> frozenset[str]:
    compact = re.sub(r"\s+", "", (value or "").casefold())
    terms = set(re.findall(r"[a-z0-9][a-z0-9_+-]{1,}", compact))
    for run in re.findall(r"[\u3400-\u9fff]+", compact):
        if len(run) <= 6:
            terms.add(run)
        terms.update(run[index:index + 2] for index in range(max(0, len(run) - 1)))
    return frozenset(term for term in terms if term not in _PROFILE_QUERY_STOP_TERMS)


def _relevant_profile_values(
    values: Sequence[str],
    query: frozenset[str],
    *,
    limit: int,
    fallback: bool,
) -> list[str]:
    unique = list(dict.fromkeys(value for value in values if isinstance(value, str) and value.strip()))
    if not query:
        return unique[:limit]
    scored = []
    for index, value in enumerate(unique):
        terms = _query_terms(value)
        overlap = len(query.intersection(terms))
        if overlap:
            scored.append((-overlap, index, value))
    selected = [value for _, _, value in sorted(scored)[:limit]]
    if fallback and not selected:
        return unique[:limit]
    return list(dict.fromkeys(selected))[:limit]


def creator_planning_brief(
    profile: IPProfile,
    *,
    topic: str | None = None,
    script: str | None = None,
) -> dict[str, object]:
    """Return a bounded, topic-relevant planning brief without identity or metadata."""

    query = _query_terms("\n".join(part for part in (topic, script) if part))
    redact = lambda value: _profile_text(value, 1_000)
    lists = {
        "domains": [redact(value)[:80] for value in profile.domains],
        "content_pillars": [redact(value)[:300] for value in profile.content_pillars],
        "platform_categories": [redact(value)[:160] for value in profile.platform_categories],
        "topics": [redact(value)[:300] for value in profile.topics],
        "knowledge": [redact(value)[:500] for value in profile.knowledge],
        "opinions": [redact(value)[:500] for value in profile.opinions],
        "vocabulary": [redact(value)[:120] for value in profile.vocabulary],
        "style_notes": [redact(value)[:160] for value in profile.style_notes],
        "boundaries": [redact(value)[:500] for value in profile.boundaries],
        "avoided_expressions": [redact(value)[:200] for value in profile.avoided_expressions],
        "common_hooks": [redact(value)[:200] for value in profile.common_hooks],
        "evidence_refs": [redact(value)[:300] for value in profile.evidence_refs],
    }
    selected = {
        "domains": lists["domains"][:5],
        "content_pillars": _relevant_profile_values(
            lists["content_pillars"], query, limit=6, fallback=True,
        ),
        "platform_categories": _relevant_profile_values(
            lists["platform_categories"], query, limit=6, fallback=True,
        ),
        "topics": _relevant_profile_values(lists["topics"], query, limit=6, fallback=True),
        "knowledge": _relevant_profile_values(lists["knowledge"], query, limit=6, fallback=False),
        "opinions": _relevant_profile_values(lists["opinions"], query, limit=6, fallback=False),
        "vocabulary": _relevant_profile_values(lists["vocabulary"], query, limit=8, fallback=True),
        "style_notes": lists["style_notes"][:5],
        "boundaries": lists["boundaries"][:12],
        "avoided_expressions": lists["avoided_expressions"][:12],
        "common_hooks": _relevant_profile_values(
            lists["common_hooks"], query, limit=4, fallback=True,
        ),
        "evidence_refs": lists["evidence_refs"][:20],
        "public_account_link_count": len(profile.public_account_links),
    }
    structured = bool(profile.positioning or profile.content_pillars or profile.knowledge or profile.opinions)
    topical = bool(profile.domains or profile.audience or profile.topics or profile.style_notes or profile.common_hooks)
    profile_state = "structured" if structured else "topical_only" if topical else "empty"
    return {
        "profile_state": profile_state,
        "selection_policy": "topic_relevant_bounded_creator_brief_v1",
        "positioning": _profile_text(profile.positioning or "", 1_000) or None,
        "audience": _profile_text(profile.audience or "", 500) or None,
        **selected,
        "omitted_counts": {
            key: max(0, len(values) - len(selected[key]))
            for key, values in lists.items()
        },
    }


def normalize_creator_fit(
    scenes: Sequence[ScenePlan],
    context: Mapping[str, object] | None,
) -> tuple[ScenePlan, ...]:
    """Validate cited IP evidence and expose missing fit instead of hiding it."""

    brief = (context or {}).get("ip_profile")
    if not isinstance(brief, Mapping):
        return tuple(scenes)
    profile_state = brief.get("profile_state")
    pillars = {value for value in brief.get("content_pillars", ()) if isinstance(value, str)}
    knowledge = {value for value in brief.get("knowledge", ()) if isinstance(value, str)}
    opinions = {value for value in brief.get("opinions", ()) if isinstance(value, str)}
    normalized = []
    for scene in scenes:
        fit = scene.creator_fit
        if fit is None:
            fit = CreatorFit(
                fit_state="generic",
                evidence_gaps=["本次规划未返回可核验的 IP 适配依据。"],
            )
        else:
            gaps = list(fit.evidence_gaps)
            risks = list(fit.deviation_risks)
            pillar = fit.content_pillar
            invalid_pillar = False
            if pillar is not None and pillar not in pillars:
                pillar = None
                invalid_pillar = True
                risks.append("模型引用的内容支柱未出现在本次 Creator Brief 中，已移除。")
            kept_knowledge = [value for value in fit.knowledge_refs if value in knowledge]
            kept_opinions = [value for value in fit.opinion_refs if value in opinions]
            if len(kept_knowledge) != len(fit.knowledge_refs):
                gaps.append("部分知识引用无法对应本次 Creator Brief，已移除。")
            if len(kept_opinions) != len(fit.opinion_refs):
                gaps.append("部分观点引用无法对应本次 Creator Brief，已移除。")
            fit_state = fit.fit_state
            if invalid_pillar and fit_state == "matched":
                fit_state = "partial"
            if profile_state == "empty":
                fit_state = "generic"
                gaps.append("当前 IP 资料不足，本场只能作为泛化建议。")
            elif profile_state == "topical_only":
                if fit_state == "matched":
                    fit_state = "partial"
                gaps.append("已有主题范围但缺少明确知识与观点，不能判为完整 IP 匹配。")
            elif fit_state == "matched" and not (pillar or kept_knowledge or kept_opinions):
                fit_state = "partial"
                gaps.append("缺少可核验的内容支柱、知识或观点引用，降级为部分匹配。")
            fit = fit.model_copy(update={
                "fit_state": fit_state,
                "content_pillar": pillar,
                "knowledge_refs": list(dict.fromkeys(kept_knowledge)),
                "opinion_refs": list(dict.fromkeys(kept_opinions)),
                "evidence_gaps": list(dict.fromkeys(gaps))[:8],
                "deviation_risks": list(dict.fromkeys(risks))[:8],
            })
        normalized.append(scene.model_copy(update={"creator_fit": fit}))
    return tuple(normalized)


def creator_scope_gaps(
    scenes: Sequence[ScenePlan],
    context: Mapping[str, object] | None,
) -> tuple[str, ...]:
    """Return scenes that do not cite the structured creator scope."""

    brief = (context or {}).get("ip_profile")
    if not isinstance(brief, Mapping) or brief.get("profile_state") != "structured":
        return ()
    gaps = []
    for scene in scenes:
        fit = scene.creator_fit
        if (
            fit is None
            or fit.fit_state == "generic"
            or not (fit.content_pillar or fit.knowledge_refs or fit.opinion_refs)
        ):
            gaps.append(scene.scene_id)
    return tuple(gaps)


def automatic_identity_anchor_required(
    project: Project, context: Mapping[str, object] | None, *, topic: str | None = None,
    script: str | None = None,
) -> bool:
    """Keep visible IP identity when the current project can execute it."""
    if explicit_visual_constraints(topic or project.topic, script)[0]:
        return False
    if (project.ip_profile_id is None or project.presentation_style != "auto"
            or not isinstance(context, Mapping)):
        return False
    feasibility = context.get("production_feasibility")
    if not isinstance(feasibility, Mapping):
        return False
    creator = feasibility.get("creator_options")
    voice = creator.get("voice") if isinstance(creator, Mapping) else None
    sources = feasibility.get("selected_talking_sources")
    pool = feasibility.get("talking_motion_pool")
    return (isinstance(voice, Mapping) and voice.get("default_binding") == "matched"
        and isinstance(sources, list) and bool(sources)
        and isinstance(pool, Mapping) and pool.get("state") == "available")


def planning_request(project: Project, *, topic: str, script: str | None,
                     context: Mapping[str, object] | None) -> dict[str, object]:
    project = short_video_project(project)
    source = dict(context or {})
    controls = {key: source.pop(key) for key in sorted(_CONTROL_KEYS) if key in source}
    identity_required = automatic_identity_anchor_required(
        project, context, topic=topic, script=script,
    )
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
            "presentation_style": "creator_led" if identity_required else project.presentation_style,
            "identity_presence_required": identity_required,
        },
    }


def presentation_plan_gaps(project: Project, scenes: Sequence[ScenePlan], *,
                           identity_anchor_required: bool = False) -> tuple[str, ...]:
    """Keep the creator's saved presentation requirement through planning and dispatch."""
    if project.presentation_style != "creator_led" and not identity_anchor_required:
        return ()
    gaps: list[str] = []
    speaking_sources = {SourceKind.USER_ASSET, SourceKind.HISTORICAL_ASSET,
                        SourceKind.AI_VIDEO, SourceKind.TALKING_PROFILE, SourceKind.CAPTURE}
    if not any(scene.visual_requirement == "creator_speaking"
               and scene.preferred_sources[0] in speaking_sources for scene in scenes):
        gaps.append("creator_led_missing_speaking" if project.presentation_style == "creator_led"
                    else "automatic_identity_presence_missing")
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
