"""共享普通分镜规划：HTTP 与后台 Job 使用同一保存、记账与校验路径。"""
from __future__ import annotations
import os
import re
from pathlib import Path
from uuid import UUID
from pydantic import BaseModel,ConfigDict,Field
from app.db import (Database,ProjectDraftRepository,IPProfileRepository,AssetRepository,ClipRepository,ContentOpportunityRepository)
from app.domain.models import Project,ProjectDraft,ScenePlan,SourceKind,CostCategory
from app.search import ClipTextSearchService
from app.search.index import _lexical_terms
from app.planning_feasibility import planning_feasibility,planning_identity_context
from app.planning_input import creator_planning_brief, short_video_project, automatic_graphic_gaps, normalize_automatic_graphics
from app.feedback_learning import FeedbackLearningService,apply_preferences
from app.source_use_constraints import SourceUseConstraintService,digest as use_constraint_digest
from app.provider_execution import ProviderExecutionService,runtime_provider_identity
from app.runtime import scene_planner_api_key
from app.project_drafts import save_project_draft as persist_project_draft
from app.providers.scene_planner import (validate_audience_boundary,validate_presentation_requirements,validate_opening_context,validate_observed_materials,validate_automatic_material_requests,
    OpenAICompatibleScenePlanner,ScenePlanner,ScenePlannerAuthenticationError,ScenePlannerConfigurationError,
    ScenePlannerConnectionError,ScenePlannerHTTPError,ScenePlannerInputError,ScenePlannerProviderResponseError,
    ScenePlannerRateLimitError,ScenePlannerTimeout)

class PlanningContextChanged(ValueError):
    pass

class ScenePlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    script: str | None = Field(default=None, min_length=1, max_length=100_000)
    topic: str | None = Field(default=None, min_length=1, max_length=5_000)
    persist: bool = False


class ScenePlanResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: UUID
    scenes: tuple[ScenePlan, ...]
    script: str = Field(min_length=1, max_length=100_000)
    generated_script: bool
    draft: ProjectDraft | None = None
    replaceability_reasons: dict[str, str] = Field(default_factory=dict)


def _scene_planner_error_code(error: Exception) -> str:
    if isinstance(error, ScenePlannerAuthenticationError):
        return "scene_planner_authentication_rejected"
    if isinstance(error, ScenePlannerConfigurationError):
        return "scene_planner_not_configured"
    if isinstance(error, (ScenePlannerRateLimitError, ScenePlannerTimeout, ScenePlannerConnectionError)):
        return "scene_planner_temporarily_unavailable"
    if isinstance(error, ScenePlannerHTTPError):
        return "scene_planner_request_failed"
    if isinstance(error, ScenePlannerProviderResponseError):
        return "scene_planner_response_invalid"
    if isinstance(error, ScenePlannerInputError):
        return "scene_planner_input_invalid"
    return "scene_planner_failed"


def _scene_planning_context(db: Database, project: Project, *, topic: str | None = None, script: str | None = None,
                            material_clip_ids=None, talking_provider=None) -> tuple[dict[str, object], tuple[str, ...]]:
    """Small local hints; full retrieval/admission remains the Router's job."""
    profile = IPProfileRepository(db).get(project.ip_profile_id)
    refs: list[str] = []
    context: dict[str, object] = {}
    context["production_feasibility"] = planning_feasibility(db, project.id, talking_provider=talking_provider)
    # Replanning keeps independently checked narration timing available. This
    # supplies clause boundaries, not a replacement script or visual choices.
    draft = ProjectDraftRepository(db).get(project.id)
    if draft is not None and script is not None and script == draft.script:
        from app.production_preflight import _approved_master
        from app.voice_qa import comparison_tokens, verified_generated_copy_tokens
        audio = _approved_master(db, project.id, draft.script)
        if audio is not None:
            target = comparison_tokens(draft.script)
            observed = tuple(token for segment in audio.transcript_segments for token in comparison_tokens(segment.text))
            if target and (target == observed or target == verified_generated_copy_tokens(audio)):
                offset = 0
                timing = []
                for segment in audio.transcript_segments:
                    size = len(comparison_tokens(segment.text))
                    timing.append({"start_ms": segment.start_ms, "end_ms": segment.end_ms,
                        "script_words": "".join(target[offset:offset + size])})
                    offset += size
                context["narration_timing"] = {"audio_asset_id": str(audio.id),
                    "duration_ms": audio.duration_ms, "clauses": timing,
                    "authority": "checked_timing_hint_not_visual_or_human_quality_approval"}
    if profile is not None:
        revisions = IPProfileRepository(db).revisions(profile.id)
        profile_version = revisions[-1][0] if revisions else 1
        refs.append(f"ip_profile:{profile.id}:v{profile_version}")
        context.update({
            "ip_profile_version": profile_version,
            "ip_profile": creator_planning_brief(profile),
        })
    materials: list[dict[str, object]] = []
    from app.media.vision import planning_visual_evidence
    from app.media.visual_facts import material_origin
    # Retrieval hints follow the topic, not appended workflow instructions.
    query = re.split(r"[。？！?!\n]", topic or project.topic, maxsplit=1)[0][:10000]
    anchors = {t for t in _lexical_terms(query) if len(t) > 1} - {
        "视频", "画面", "素材", "内容", "什么", "为什么", "如何", "怎么", "不是",
        "video", "footage", "content",
    }
    assets = AssetRepository(db)
    hits = ClipTextSearchService(db).search(query, top_k=20) if anchors and material_clip_ids is None else []
    for hit in hits:
        clip, asset = hit.clip, assets.get(hit.clip.asset_id)
        from app.external_media import external_catalogue_text
        summary = " ".join(v for v in (clip.visual_description, clip.action) if v) or (external_catalogue_text(asset) if asset else "")
        if (asset is None or asset.source_kind not in {SourceKind.USER_ASSET, SourceKind.HISTORICAL_ASSET, SourceKind.STOCK, SourceKind.AI_BROLL}
                or asset.metadata.get("project_id") not in (None, str(project.id))
                or asset.metadata.get("ip_profile_id") not in (None, str(project.ip_profile_id))
                or not anchors.intersection(_lexical_terms(summary))):
            continue
        materials.append({"asset_id": str(asset.id), "clip_id": str(clip.id),
            "source_kind": asset.source_kind.value, "start_ms": clip.start_ms, "end_ms": clip.end_ms,
            "reuse_role": asset.metadata.get("reuse_role", "ordinary"),
            "visual_evidence": planning_visual_evidence(db, asset, clip,project=project),
            "summary": summary[:240], "evidence_level": "lexical_hint_not_source_admission"})
        refs.append(f"clip:{clip.id}")
        analysis = asset.metadata.get("r1_analysis")
        if isinstance(analysis, dict) and isinstance(analysis.get("bundle_id"), str):
            refs.append(f"analysis_bundle:{analysis['bundle_id']}")
        if len(materials) == 3:
            break
    # 单个主题命中不等于整片只能用一个镜头；提供同源已描述画面供规划择用。
    # 同源关系只扩充候选，不提高应景分，也不把未分析镜头伪装成已知画面。
    matched_assets=list(dict.fromkeys(item['asset_id'] for item in materials))
    known_clips={item['clip_id'] for item in materials}
    for asset_id in matched_assets:
        asset=assets.get(UUID(asset_id))
        for clip in ClipRepository(db).list_by_asset(asset.id):
            summary=' '.join(value for value in (clip.visual_description,clip.action) if value)
            if str(clip.id) in known_clips or not summary:continue
            materials.append({'asset_id':str(asset.id),'clip_id':str(clip.id),'source_kind':asset.source_kind.value,
                'start_ms':clip.start_ms,'end_ms':clip.end_ms,'reuse_role':asset.metadata.get('reuse_role','ordinary'),
                'visual_evidence':planning_visual_evidence(db,asset,clip,project=project),
                'summary':summary[:240],'evidence_level':'same_source_visual_option_not_topic_match'})
            known_clips.add(str(clip.id));refs.append(f'clip:{clip.id}')
            if len(materials)>=8:break
        if len(materials)>=8:break
    if material_clip_ids is not None:
        # 自动制作的素材准备结果替代粗召回，不能把未核验长区间重新混进来。
        materials=[]
        refs=[ref for ref in refs if not ref.startswith(("clip:","analysis_bundle:"))]
        for clip_id in material_clip_ids:
            clip=ClipRepository(db).get(clip_id)
            asset=assets.get(clip.asset_id) if clip else None
            if (asset is None or asset.source_kind not in {SourceKind.USER_ASSET,SourceKind.HISTORICAL_ASSET,SourceKind.STOCK,SourceKind.AI_BROLL}
                or asset.metadata.get("project_id") not in (None,str(project.id))
                or asset.metadata.get("ip_profile_id") not in (None,str(project.ip_profile_id))):
                raise PlanningContextChanged("planning_material_changed")
            evidence=planning_visual_evidence(db,asset,clip,project=project)
            usable=evidence.get("usable_interval")
            if not usable:
                raise PlanningContextChanged("planning_material_changed")
            materials.append(dict(asset_id=str(asset.id),clip_id=str(clip.id),source_kind=asset.source_kind.value,
                start_ms=usable["start_ms"],end_ms=usable["end_ms"],width=asset.width,height=asset.height,
                reuse_role=asset.metadata.get("reuse_role","ordinary"),visual_evidence=evidence,
                origin=material_origin(asset),
                summary=evidence["visual_description"][:240],evidence_level="observed_source_interval_not_scene_approval"))
            refs.extend((f"clip:{clip.id}",evidence["evidence_reference"]))
    opportunities = ContentOpportunityRepository(db).list_for_planning()
    opportunity_context: list[dict[str, object]] = []
    for opportunity in opportunities:
        if not query or (opportunity.title not in query and query not in opportunity.title):
            continue
        refs.append(f"opportunity:{opportunity.id}")
        refs.extend(opportunity.evidence_refs)
        opportunity_context.append({"id": str(opportunity.id), "title": opportunity.title,
            "angle": opportunity.angle[:240], "uncertainty": (opportunity.uncertainty or "")[:240]})
        if len(opportunity_context) == 3:
            break
    # Adopted feedback is a fresh-planning input, not a grant of rights or QA.
    render_root = getattr(db, "render_output_root", Path(db.path).parent / "renders")
    preferences = FeedbackLearningService(db, render_root).active(project.id)
    for preference in preferences:
        refs.append(f"planning_preference:{preference['id']}:v{preference['version']}")
        if preference.get("authority_source") == "creator_selection":
            refs.append(f"creator_selection:{preference['id']}:v{preference['version']}")
        else:
            refs.append(f"presentation_observation:{preference['observation_id']}")
    context["planning_preferences"] = preferences
    constraints = SourceUseConstraintService(db).records(project.id)
    context["source_use_constraints"] = [
        {**{k: value[k] for k in ("id", "version", "rule", "state", "adopted_use_ids", "current_stop_reasons")},
         "uses": [use for use in value["uses"] if use["use_id"] in value["adopted_use_ids"]],
         "authority": "explicit_future_use_policy_not_scene_rejection_or_positive_qa",
         "applicability": "held" if value["current_stop_reasons"] else "active"}
        for value in constraints if value["state"] == "enabled"
    ]
    # Tombstones also fence pending provider results; disabled rules are not instructions.
    refs.extend(f"source_use_constraint:{v['id']}:v{v['version']}:{v['state']}:{use_constraint_digest(v['current_stop_reasons'])}" for v in constraints)
    unique_refs = tuple(dict.fromkeys(refs))
    context.update({
        "evidence_refs": list(unique_refs),
        "materials": materials,
        "material_supply": {"existing_sources":["user_asset","historical_asset","stock","ai_broll"],
            "external_acquisition":"manual_search_import", "ai_broll_generation":"not_configured",
            "missing_visuals":"retain_explicit_material_request_not_available_footage"},
        "material_gap": None if materials else ("已检查相关原片，当前没有通过核验的实拍区间。" if material_clip_ids is not None else
            "No related visual summary confirmed; normal Router must find eligible media or report the actual gap."),
        "opportunities": opportunity_context,
    })
    return context, unique_refs


def _scene_planner_from_env() -> OpenAICompatibleScenePlanner:
    api_key = scene_planner_api_key(os.environ)
    if not api_key and os.environ.get("ANTHROPIC_API_KEY"):
        output_limit = os.environ.get("CONTENT_OS_LLM_MAX_OUTPUT_TOKENS")
        try:
            max_output_tokens = int(output_limit) if output_limit is not None else None
        except ValueError:
            raise ScenePlannerConfigurationError("output token limit must be a positive integer") from None
        return OpenAICompatibleScenePlanner(
            os.environ["ANTHROPIC_API_KEY"],
            base_url=os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com/v1"),
            model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
            protocol="anthropic_messages",
            max_output_tokens=max_output_tokens,
            timeout_seconds=os.environ.get("CONTENT_OS_LLM_TIMEOUT_SECONDS", "60"),
        )
    if not api_key:
        raise ScenePlannerConfigurationError("scene planner API key must be supplied at runtime")
    output_limit = os.environ.get("CONTENT_OS_LLM_MAX_OUTPUT_TOKENS")
    try:
        max_output_tokens = int(output_limit) if output_limit is not None else None
    except ValueError:
        raise ScenePlannerConfigurationError("output token limit must be a positive integer") from None
    return OpenAICompatibleScenePlanner(
        api_key,
        base_url=os.environ.get("CONTENT_OS_LLM_BASE_URL") or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        model=os.environ.get("CONTENT_OS_LLM_MODEL", "gpt-4.1-mini"),
        protocol=os.environ.get("CONTENT_OS_LLM_PROTOCOL", "responses"),
        reasoning_effort=os.environ.get("CONTENT_OS_LLM_REASONING_EFFORT"),
        thinking_type=os.environ.get("CONTENT_OS_LLM_THINKING_TYPE"),
        max_output_tokens=max_output_tokens,
        timeout_seconds=os.environ.get("CONTENT_OS_LLM_TIMEOUT_SECONDS", "60"),
        chat_output_mode=os.environ.get("CONTENT_OS_LLM_CHAT_OUTPUT_MODE"),
    )


def generate_scene_plan(db,project,payload,planner,cost_estimator,idempotency_key,*,automatic=False,origin_job_id=None,material_clip_ids=None,external_sources=None,talking_provider=None,visual_revision=None,reservation_guard=None):
    resolved = short_video_project(project)
    if resolved != project and payload.persist:
        from app.db import ProjectRepository
        ProjectRepository(db).update(resolved)
    project = resolved
    project_id=project.id
    planning_context, evidence_refs = _scene_planning_context(db, project, topic=payload.topic, script=payload.script,
        material_clip_ids=material_clip_ids,talking_provider=talking_provider)
    if visual_revision is not None:
        planning_context["visual_revision"]=visual_revision
    if automatic:
        if external_sources:
            planning_context["material_supply"].update(external_acquisition="bounded_search_import_then_interval_qa",
                external_sources=list(external_sources), max_searches=3, max_initial_imports=3,
                max_supplement_imports=2, max_total_imports=5,
                public_web_fallback="search_fetch_public_file_then_same_qa" if "public_web" in external_sources else "not_configured",
                query_requirement="stock 查询用简短对象与动作关键词，不把完整镜头描述或抽象主张直接当检索词。建议 2–8 个词，中文约 30 字内；摄影构图、必要细节和事实要求保留在 visual_intent 与 reason。目录结果不等于用途核验通过")
        planning_context["continuous_production"]={"target":"从一次主题输入完成可用短视频",
            "talking_motion_pool":planning_context.get("production_feasibility",{}).get("talking_motion_pool",{}),
            "timing_state":"先保留完整文案与镜头意图，核心素材可行后生成克隆旁白；合格旁白的真实时长用于精排 A/B-roll 和对口型。",
            "creator_motion":"人物素材总容量与最长连续区间约束出镜画面调度，不约束文案长度。可跨多条已登记的本人素材分配，保持连续前向动作；声音参考独立，静音画面可对口型。保留完整开场、否定、问题与主张，不能按字数截短。本人讲解呈现仍须保留本人出镜。",
            "instructions":"以应景真实画面与本人表达共同规划。人物参考可不流利或不说话，新旁白由已配置的语音克隆生成。参考不足、横屏不适合竖裁、检索不足都不是改推解释图文或大字卡的理由；先选择竖屏适配的其他画面与布局，横版须创作者明确选择。图解须有独立的内容表达需要，不能为运行过关而发明。复用按本次语音/口型改动和原样引用目的判断，不能按人物标签机械禁重。补拍只作为低成本可选建议，不等待补拍。"}

    def plan_response(planner: ScenePlanner, *, include_context: bool) -> ScenePlanResponse:
        result = planner.plan(
            project,
            script=payload.script,
            topic=payload.topic,
            **({"context": planning_context} if include_context else {}),
        )
        # 窄画面调整沿原稿保留主张证据；新的候选观察另记在取材QA和当前调用上下文。
        scenes = (tuple(result.scenes) if visual_revision is not None else
            tuple(scene.model_copy(update={"evidence_refs": list(evidence_refs)}) for scene in result.scenes))
        if visual_revision is None:
            scenes = apply_preferences(scenes, planning_context.get("planning_preferences", []))
        if automatic and visual_revision is None:
            validate_automatic_material_requests(scenes, planning_context)
        if material_clip_ids is not None:validate_observed_materials(scenes,planning_context["materials"],require_clip_selection=True)
        validate_presentation_requirements(project, scenes)
        validate_audience_boundary(scenes, topic=payload.topic or project.topic,
                                   script=payload.script, context=planning_context)
        validate_opening_context(scenes, topic=payload.topic or project.topic, script=payload.script)
        if automatic and automatic_graphic_gaps(normalize_automatic_graphics(scenes)):
            raise ScenePlannerProviderResponseError("automatic graphics require media or a real diagram",
                validation_code="scene_values",domain_rule="presentation_requirement",domain_field="graphic_plan")
        generated_script = payload.script is None
        script = payload.script or "\n\n".join(scene.voice_text for scene in scenes)
        return ScenePlanResponse(
            project_id=result.project_id,
            scenes=scenes,
            script=script,
            generated_script=generated_script,
            replaceability_reasons=dict(getattr(result, "replaceability_reasons", ())),
        )

    identity = runtime_provider_identity(planner)
    if identity is not None:
        response = ProviderExecutionService(db, cost_estimator).execute(
            project_id=project_id,
            idempotency_key=idempotency_key,
            operation="scene_planning",
            input_source=f"project:{project_id}:scene-plan",
            provider=identity,
            category=CostCategory.LLM,
            input_document={
                "presentation_style": project.presentation_style,
                "output_format": {"policy": project.format_policy, "format": project.format,
                    "width": project.resolution_width, "height": project.resolution_height},
                "chat_output_mode": getattr(planner, "chat_output_mode", None),
                "request": payload.model_dump(mode="json"),
                "planning_context": planning_identity_context(planning_context),
                "evidence_refs": list(evidence_refs),
            },
            action=lambda: plan_response(planner, include_context=True),
            encode_result=lambda value: value.model_dump(mode="json"),
            decode_result=ScenePlanResponse.model_validate,
            error_code=_scene_planner_error_code,
            reservation_guard=reservation_guard,
        )
    else:
        # Deterministic/local test adapters retain the narrow original
        # protocol and do not claim an externally metered identity.
        if reservation_guard is not None:reservation_guard()
        response = plan_response(planner, include_context=material_clip_ids is not None or visual_revision is not None)
    # Keep the accounted provider result intact; derived graphics also apply to replay.
    if automatic:
        response=response.model_copy(update={"scenes":normalize_automatic_graphics(response.scenes)})
        if visual_revision is None:
            validate_automatic_material_requests(response.scenes, planning_context)
    # Accounted replay is not allowed to bypass current content guards.
    validate_presentation_requirements(project, response.scenes)
    validate_audience_boundary(response.scenes, topic=payload.topic or project.topic,
                               script=payload.script, context=planning_context)
    validate_opening_context(response.scenes, topic=payload.topic or project.topic, script=payload.script)
    if material_clip_ids is not None:validate_observed_materials(response.scenes,planning_context["materials"],require_clip_selection=True)
    if automatic and automatic_graphic_gaps(response.scenes):
        raise ScenePlannerProviderResponseError("automatic graphics require media or a real diagram",
            validation_code="scene_values",domain_rule="presentation_requirement",domain_field="graphic_plan")
    # A preference changed while the provider was planning: preserve
    # its accounted result, but never save stale policy as current.
    _, current_refs = _scene_planning_context(db, project, topic=payload.topic,material_clip_ids=material_clip_ids,
        talking_provider=talking_provider)
    if current_refs != evidence_refs:
        raise PlanningContextChanged("planning_context_changed_replan")
    if payload.persist:
        with db.transaction():
            draft = persist_project_draft(
                db,
                project,
                script=response.script,
                topic=payload.topic or project.topic,
                scenes=response.scenes,
                routes=(),
                confirmed=(),
                video_spec=None,
                evidence_refs=(*evidence_refs, f"content_plan_job:{origin_job_id}") if origin_job_id else evidence_refs,
                accept_scene_plan_with_new_script=True,
            )
        response = response.model_copy(update={"draft": draft})
    return response

