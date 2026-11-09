"""以真实旁白安排本人出镜和原稿允许的切题实拍，不改文案。"""
from app.domain.models import (
    GRAPHIC_LABEL_MAX_CHARS,
    GraphicCardPlan,
    ScenePlan,
    SourceKind,
)
from .pool import allocate_talking_windows, TalkingPoolCapacityError


def supporting_footage_scene(scene):
    """仅投影原稿明确允许的切题画面；估算、实测和外取共用。"""
    footage = {SourceKind.USER_ASSET, SourceKind.HISTORICAL_ASSET, SourceKind.STOCK, SourceKind.AI_BROLL}
    sources = [source for source in scene.fallback_sources if source in footage]
    if scene.visual_requirement != 'creator_speaking' or scene.supporting_visual_intent is None or not sources:
        raise TalkingPoolCapacityError()
    # A supporting cutaway still needs a low-cost external fallback when the
    # local/historical pool cannot cover it. Stock remains a source candidate,
    # never evidence; interval QA still decides whether the footage is usable.
    if SourceKind.STOCK not in sources:
        sources.append(SourceKind.STOCK)
    return ScenePlan.model_validate({**scene.model_dump(),
        "visual_intent": scene.supporting_visual_intent.model_dump(),
        "supporting_visual_intent": None, "preferred_clip_id": None,
        "preferred_sources": sources, "fallback_sources": [], "graphic_plan": None,
        "material_request": None, "visual_requirement": "explanatory",
        "visual_requirement_reason": "沿用原稿允许的切题实拍覆盖同一段完整旁白，保留其它本人出镜。"})


def local_graphic_control_scene(scene):
    """Carry excess measured speech parts with a local key-point card."""
    if (
        scene.visual_requirement != "creator_speaking"
        or "--speech-" not in scene.scene_id
        or scene.supporting_visual_intent is not None
        or scene.graphic_plan is not None
    ):
        return None
    labels = [" ".join(item.split()) for item in scene.caption_emphasis if item.strip()]
    if not labels:
        labels = [" ".join(scene.voice_text.strip("，。！？!?；;：:、 ").split())]
    if (
        not labels
        or len(labels[0]) > GRAPHIC_LABEL_MAX_CHARS
    ):
        return None
    return ScenePlan.model_validate({
        **scene.model_dump(),
        "visual_requirement": "explanatory",
        "visual_requirement_reason": (
            "人物参考容量不足；保留原旁白和开场 A-roll，"
            "用本地要点卡承载同一实测 speech part 的非动作型说明。"
        ),
        "preferred_sources": [SourceKind.TYPOGRAPHY],
        "fallback_sources": [],
        "supporting_visual_intent": None,
        "preferred_clip_id": None,
        "graphic_plan": GraphicCardPlan(
            kind="text_card",
            treatment="key_point",
            points=[labels[0]],
        ).model_dump(mode="json"),
        "material_request": None,
    })


def schedule_talking_scenes(pool, scenes, *, talking_provider=None):
    scenes = tuple(scenes)
    speaking = [index for index, scene in enumerate(scenes) if scene.visual_requirement == "creator_speaking"]
    if not speaking:
        return scenes, ()

    def allocate(indices):
        return allocate_talking_windows(pool, [scenes[index].duration_target_ms for index in sorted(indices)],
            talking_provider=talking_provider)

    try:
        return scenes, allocate(speaking)
    except TalkingPoolCapacityError as failure:
        if failure.reason != "talking_motion_capacity_insufficient":
            raise

    # 替代意图由初稿规划；不能从「本人讲述」臆造B-roll或把不可缺的本人表演降级。
    flexible = [
        index for index in speaking
        if scenes[index].supporting_visual_intent is not None
        or local_graphic_control_scene(scenes[index]) is not None
    ]
    kept = set(speaking) - set(flexible)
    allocate(kept)  # 必需人物段仍须完整可行。
    # 开场、收束优先保留本人；其它可替代段按原顺序。每次都重算整批，避免部分派发。
    priorities = [index for index in dict.fromkeys([speaking[0], speaking[-1], *speaking]) if index in flexible]
    def ending(index):
        return scenes[index].voice_text.rstrip().rstrip('\"\'”’」』）)]').rstrip()
    # 仅在有终止句可选时后移逗号/冒号铺垫；没有句末标点也仍可正常出镜。
    if any(ending(index).endswith(("。", "！", "？", ".", "!", "?")) for index in priorities):
        priorities.sort(key=lambda index: ending(index).endswith(("，", ",", "：", ":")))
    for index in priorities:
        try:
            allocate(kept | {index})
        except TalkingPoolCapacityError as failure:
            if failure.reason != "talking_motion_capacity_insufficient":
                raise
            continue
        kept.add(index)
    if not kept:
        raise TalkingPoolCapacityError()

    revised = []
    for index, scene in enumerate(scenes):
        if index in flexible and index not in kept:
            # 重新校验：旧记录/内部model_copy不得绕过原计划来源边界。
            scene = (
                supporting_footage_scene(scene)
                if scene.supporting_visual_intent is not None
                else local_graphic_control_scene(scene)
            )
        revised.append(scene)
    return tuple(revised), allocate(kept)
