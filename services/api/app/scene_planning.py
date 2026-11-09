"""共享普通分镜规划：HTTP 与后台 Job 使用同一保存、记账与校验路径。"""
from __future__ import annotations
import os
import re
from pathlib import Path
from uuid import UUID
from pydantic import BaseModel,ConfigDict,Field
from app.db import (Database,ProjectDraftRepository,IPProfileRepository,AssetRepository,ClipRepository,
                    ContentOpportunityRepository,AccountConnectionRepository,HistoricalContentRepository)
from app.asset_management import asset_is_active, asset_scope_allows_project
from app.domain.models import Project,ProjectDraft,ScenePlan,SourceKind,CostCategory
from app.search import ClipTextSearchService
from app.search.index import _lexical_terms
from app.planning_feasibility import planning_feasibility,planning_identity_context
from app.planning_input import (automatic_graphic_gaps, automatic_identity_anchor_required,
    creator_planning_brief, creator_scope_gaps, normalize_automatic_graphics, normalize_creator_fit,
    normalize_explicit_visual_constraints, explicit_visual_constraints,
    short_video_project)
from app.feedback_learning import FeedbackLearningService,apply_preferences
from app.source_use_constraints import SourceUseConstraintService,digest as use_constraint_digest
from app.provider_execution import ProviderExecutionService,runtime_provider_identity
from app.runtime import scene_planner_api_key
from app.project_drafts import save_project_draft as persist_project_draft
from app.providers.scene_planner import (validate_audience_boundary,validate_presentation_requirements,validate_opening_context,validate_observed_materials,validate_automatic_material_requests,normalize_talking_reference_hints,
    OpenAICompatibleScenePlanner,ScenePlanner,ScenePlannerAuthenticationError,ScenePlannerConfigurationError,
    ScenePlannerConnectionError,ScenePlannerHTTPError,ScenePlannerInputError,ScenePlannerProviderResponseError,
    ScenePlannerRateLimitError,ScenePlannerTimeout)

class PlanningContextChanged(ValueError):
    pass


_VISUAL_MOTIF_GROUPS = (
    (("哲学", "思想", "经典", "历史", "宗教", "文明", "古典", "文学", "阅读"),
     ("古籍", "书页", "图书馆", "书写", "烛光", "遗址", "石柱", "古典建筑", "沉思", "窗边")),
    (("科技", "数码", "视频", "摄影", "编码", "平台", "相机"),
     ("电脑", "屏幕", "软件", "工作台", "设备", "摄像机", "实验室", "剪辑")),
    (("城市", "生活", "社会", "职场", "焦虑", "情绪", "自我"),
     ("城市", "街道", "室内", "人物", "办公", "窗边", "沉默", "独处")),
    (("自然", "环境", "户外", "旅行"),
     ("山川", "森林", "河流", "道路", "游客", "遗址", "天空")),
)


def _visual_motif_terms(profile: IPProfile | None, topic: str) -> tuple[str, ...]:
    """Expand a creator's declared subject into bounded real-footage motifs."""
    if profile is None:
        return ()
    values = [
        topic,
        profile.positioning or "",
        profile.audience or "",
        *profile.domains,
        *profile.content_pillars,
        *profile.platform_categories,
        *profile.topics,
        *profile.knowledge,
    ]
    text = " ".join(values)
    motifs: list[str] = []
    for triggers, candidates in _VISUAL_MOTIF_GROUPS:
        if any(trigger in text for trigger in triggers):
            motifs.extend(term for term in candidates if term not in motifs)
    return tuple(motifs)


def _positive_motif_match(summary: str, motif: str) -> bool:
    """Reject a visual motif when every occurrence is explicitly negated."""
    for match in re.finditer(re.escape(motif), summary):
        prefix = summary[max(0, match.start() - 8):match.start()]
        if not re.search(r"(?:没有|无|非|并非|不含|未出现|未有|而不是|而非)[^，。；]{0,5}$", prefix):
            return True
    return False

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


def _normalize_supporting_visual_sources(scenes: tuple[ScenePlan, ...], planning_context: dict[str, object]) -> tuple[ScenePlan, ...]:
    """Keep explicitly referenced cutaway sources eligible after estimation."""
    source_by_clip = {
        str(item.get("clip_id")): item.get("source_kind")
        for item in planning_context.get("materials", ())
        if isinstance(item, dict) and item.get("clip_id") and item.get("source_kind")
    }
    footage = {SourceKind.USER_ASSET, SourceKind.HISTORICAL_ASSET, SourceKind.STOCK, SourceKind.AI_BROLL}
    normalized = []
    for scene in scenes:
        if scene.visual_requirement != "creator_speaking" or scene.supporting_visual_intent is None:
            normalized.append(scene)
            continue
        referenced = []
        for reference in scene.evidence_refs:
            if not reference.startswith("clip:"):
                continue
            try:
                source = SourceKind(source_by_clip.get(reference.removeprefix("clip:")))
            except ValueError:
                continue
            if source in footage:
                referenced.append(source)
        fallback = list(dict.fromkeys([*scene.fallback_sources, *referenced]))
        normalized.append(scene.model_copy(update={"fallback_sources": fallback}) if fallback != scene.fallback_sources else scene)
    return tuple(normalized)


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
            "ip_profile": creator_planning_brief(profile, topic=topic or project.topic, script=script),
        })
        accounts = [
            account for account in AccountConnectionRepository(db).list()
            if account.ip_profile_id == profile.id
        ][:10]
        history_items = []
        account_history = []
        for account in accounts:
            refs.append(f"account_connection:{account.id}")
            items = HistoricalContentRepository(db).list_for_account(account.id)
            account_history.append([
                {
                    "account_id": str(account.id),
                    "provider": account.provider,
                    "account_name": account.display_name or account.account_external_id,
                    "profile_url": account.profile_url,
                    "title": item.title[:500],
                    "description": (item.description or "")[:800] or None,
                    "published_at": None if item.published_at is None else item.published_at.isoformat(),
                    "metrics": dict(list(item.metrics.items())[:12]),
                    "asset_id": None if item.asset_id is None else str(item.asset_id),
                    "content_url": item.content_url,
                    "availability": item.availability,
                    "source_evidence": item.evidence_refs[:8],
                    "evidence_ref": f"historical_content:{item.id}",
                }
                for item in reversed(items)
            ])
        # Keep cross-platform signal near the bounded prompt budget. Taking
        # eight newest items from the first account would hide every later
        # account whenever the first one has enough history.
        item_offset = 0
        while len(history_items) < 8:
            added = False
            for items in account_history:
                if item_offset >= len(items):
                    continue
                item = items[item_offset]
                refs.append(item["evidence_ref"])
                history_items.append(item)
                added = True
                if len(history_items) == 8:
                    break
            if not added:
                break
            item_offset += 1
        context["creator_account_history"] = {
            "state": (
                "historical_content_available" if history_items
                else "account_linked_history_not_imported" if accounts
                else "profile_links_only" if profile.public_account_links
                else "not_configured"
            ),
            "accounts": [
                {
                    "provider": account.provider,
                    "account_name": account.display_name or account.account_external_id,
                    "profile_url": account.profile_url,
                }
                for account in accounts
            ],
            "items": history_items,
            "limitations": [
                "Account links and imported history are evidence, not authorization to invent recent videos or audience metrics.",
                "Only explicitly imported historical content is analyzed; a link alone does not prove current platform category or audience.",
            ],
        }
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
    exact_assets: set[str] = set()
    for hit in hits:
        clip, asset = hit.clip, assets.get(hit.clip.asset_id)
        from app.external_media import external_catalogue_text
        summary = " ".join(v for v in (clip.visual_description, clip.action) if v) or (external_catalogue_text(asset) if asset else "")
        if (asset is None or not asset_is_active(asset)
                or asset.source_kind not in {SourceKind.USER_ASSET, SourceKind.HISTORICAL_ASSET, SourceKind.STOCK, SourceKind.AI_BROLL}
                or asset.metadata.get("project_id") not in (None, str(project.id))
                or asset.source_kind is SourceKind.AI_BROLL and asset.metadata.get("project_id") != str(project.id)
                or not asset_scope_allows_project(asset, project.ip_profile_id)
                or not anchors.intersection(_lexical_terms(summary))):
            continue
        if str(asset.id) in exact_assets:
            continue
        materials.append({"asset_id": str(asset.id), "clip_id": str(clip.id),
            "source_kind": asset.source_kind.value, "start_ms": clip.start_ms, "end_ms": clip.end_ms,
            "reuse_role": asset.metadata.get("reuse_role", "ordinary"),
            "visual_evidence": planning_visual_evidence(db, asset, clip,project=project),
            "summary": summary[:240], "evidence_level": "lexical_hint_not_source_admission"})
        exact_assets.add(str(asset.id))
        refs.append(f"clip:{clip.id}")
        analysis = asset.metadata.get("r1_analysis")
        if isinstance(analysis, dict) and isinstance(analysis.get("bundle_id"), str):
            refs.append(f"analysis_bundle:{analysis['bundle_id']}")
        if len(materials) == 3:
            break
    # Creator-level visual vocabulary bridges topics such as Stoicism or video
    # engineering to reusable, already verified stock motifs. The planner still
    # decides whether a hint fits each scene; these entries never become an
    # automatic selection or a claim of scene-specific usefulness.
    motifs = _visual_motif_terms(profile, query)
    if material_clip_ids is None and motifs and len(materials) < 8:
        motif_hits = ClipTextSearchService(db).search(" ".join(motifs), top_k=80)
        motif_materials = []
        seen_assets = {item["asset_id"] for item in materials}
        for motif_index, motif in enumerate(motifs):
            for hit in motif_hits:
                if str(hit.clip.asset_id) in seen_assets:
                    continue
                asset = assets.get(hit.clip.asset_id)
                if (asset is None or asset.source_kind is not SourceKind.STOCK
                        or not asset_is_active(asset)
                        or asset.metadata.get("project_id") not in (None, str(project.id))
                        or not asset_scope_allows_project(asset, project.ip_profile_id)):
                    continue
                summary = " ".join(value for value in (hit.clip.visual_description, hit.clip.action) if value)
                if not _positive_motif_match(summary, motif):
                    continue
                evidence = planning_visual_evidence(db, asset, hit.clip, project=project)
                if not evidence.get("usable_interval"):
                    continue
                motif_materials.append((motif_index, hit, asset, evidence, summary))
                seen_assets.add(str(asset.id))
                break
            if len(motif_materials) >= 16:
                break
        motif_materials.sort(key=lambda item: (
            0 if item[2].height > item[2].width else 1,
            item[0],
            -item[1].score,
            str(item[1].clip.id),
        ))
        for _, hit, asset, evidence, summary in motif_materials:
            if len(materials) >= 8:
                break
            usable = evidence["usable_interval"]
            materials.append({
                "asset_id": str(asset.id),
                "clip_id": str(hit.clip.id),
                "source_kind": asset.source_kind.value,
                "start_ms": usable["start_ms"],
                "end_ms": usable["end_ms"],
                "width": asset.width,
                "height": asset.height,
                "reuse_role": asset.metadata.get("reuse_role", "ordinary"),
                "visual_evidence": evidence,
                "summary": summary[:240],
                "evidence_level": "content_motif_verified_stock_hint_not_scene_approval",
            })
            refs.extend((f"clip:{hit.clip.id}", evidence["evidence_reference"]))
    # 单个主题命中不等于整片只能用一个镜头；提供同源已描述画面供规划择用。
    # 同源关系只扩充候选，不提高应景分，也不把未分析镜头伪装成已知画面。
    matched_assets=list(dict.fromkeys(item['asset_id'] for item in materials))
    known_clips={item['clip_id'] for item in materials}
    for asset_id in matched_assets:
        asset=assets.get(UUID(asset_id))
        for clip in ClipRepository(db).list_by_asset(asset.id):
            summary=' '.join(value for value in (clip.visual_description,clip.action) if value)
            if not asset_is_active(asset) or str(clip.id) in known_clips or not summary:continue
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
            if (asset is None or not asset_is_active(asset)
                or asset.source_kind not in {SourceKind.USER_ASSET,SourceKind.HISTORICAL_ASSET,SourceKind.STOCK,SourceKind.AI_BROLL}
                or asset.metadata.get("project_id") not in (None,str(project.id))
                or asset.source_kind is SourceKind.AI_BROLL and asset.metadata.get("project_id") != str(project.id)
                or not asset_scope_allows_project(asset, project.ip_profile_id)):
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


def _scene_planner_from_env(values=None) -> OpenAICompatibleScenePlanner:
    values = os.environ if values is None else values
    api_key = scene_planner_api_key(values)
    if not api_key and values.get("ANTHROPIC_API_KEY"):
        output_limit = values.get("CONTENT_OS_LLM_MAX_OUTPUT_TOKENS")
        try:
            max_output_tokens = int(output_limit) if output_limit is not None else None
        except ValueError:
            raise ScenePlannerConfigurationError("output token limit must be a positive integer") from None
        return OpenAICompatibleScenePlanner(
            values["ANTHROPIC_API_KEY"],
            base_url=values.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com/v1"),
            model=values.get("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
            protocol="anthropic_messages",
            max_output_tokens=max_output_tokens,
            timeout_seconds=values.get("CONTENT_OS_LLM_TIMEOUT_SECONDS", "60"),
        )
    if not api_key:
        raise ScenePlannerConfigurationError("scene planner API key must be supplied at runtime")
    output_limit = values.get("CONTENT_OS_LLM_MAX_OUTPUT_TOKENS")
    try:
        max_output_tokens = int(output_limit) if output_limit is not None else None
    except ValueError:
        raise ScenePlannerConfigurationError("output token limit must be a positive integer") from None
    return OpenAICompatibleScenePlanner(
        api_key,
        base_url=values.get("CONTENT_OS_LLM_BASE_URL") or values.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        model=values.get("CONTENT_OS_LLM_MODEL", "gpt-4.1-mini"),
        protocol=values.get("CONTENT_OS_LLM_PROTOCOL", "responses"),
        reasoning_effort=values.get("CONTENT_OS_LLM_REASONING_EFFORT"),
        thinking_type=values.get("CONTENT_OS_LLM_THINKING_TYPE"),
        max_output_tokens=max_output_tokens,
        timeout_seconds=values.get("CONTENT_OS_LLM_TIMEOUT_SECONDS", "60"),
        chat_output_mode=values.get("CONTENT_OS_LLM_CHAT_OUTPUT_MODE"),
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
    identity_anchor_required = automatic and automatic_identity_anchor_required(
        project, planning_context, topic=payload.topic or project.topic, script=payload.script,
    )
    if visual_revision is not None:
        planning_context["visual_revision"]=visual_revision
    if automatic:
        _, local_only = explicit_visual_constraints(payload.topic or project.topic, payload.script)
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
            "identity_presence_required": identity_anchor_required,
            "visual_mix_policy": ({
                "mode": "explicit_local_only",
                "typography_allowed": True,
                "footage_minimum_ratio": 0,
                "max_consecutive_typography": None,
            } if local_only else {
                "mode": "short_video_footage_led",
                "typography_allowed": True,
                "footage_minimum_ratio": 0.4,
                "max_consecutive_typography": 2,
                "stock_preference": "use topical real-world stock for concrete subjects when creator/source footage does not express the beat",
            }),
            "instructions":"以应景真实画面与本人表达共同规划。人物参考可不流利或不说话，新旁白由已配置的语音克隆生成。参考不足、横屏不适合竖裁、检索不足都不是改推解释图文或大字卡的理由；先选择竖屏适配的其他画面与布局，横版须创作者明确选择。图解须有独立的内容表达需要，不能为运行过关而发明。复用按本次语音/口型改动和原样引用目的判断，不能按人物标签机械禁重。补拍只作为低成本可选建议，不等待补拍。"}

    def plan_response(planner: ScenePlanner, *, include_context: bool) -> ScenePlanResponse:
        result = planner.plan(
            project,
            script=payload.script,
            topic=payload.topic,
            **({"context": planning_context} if include_context else {}),
        )
        raw_scenes = result.scenes
        if automatic and visual_revision is None:
            raw_scenes = _normalize_supporting_visual_sources(tuple(raw_scenes), planning_context)
        # 窄画面调整沿原稿保留主张证据；新的候选观察另记在取材QA和当前调用上下文。
        scenes = (tuple(raw_scenes) if visual_revision is not None else
            tuple(scene.model_copy(update={"evidence_refs": list(evidence_refs)}) for scene in raw_scenes))
        scenes = normalize_explicit_visual_constraints(
            scenes, topic=payload.topic or project.topic, script=payload.script,
        )
        if automatic and visual_revision is None:
            scenes = normalize_talking_reference_hints(scenes, planning_context)
        if visual_revision is None:
            scenes = normalize_creator_fit(scenes, planning_context)
            if creator_scope_gaps(scenes, planning_context):
                raise ScenePlannerProviderResponseError(
                    "scene plan does not cite the current creator scope",
                    validation_code="scene_values",
                    domain_rule="creator_scope_unverified",
                    domain_field="creator_fit",
                )
        if visual_revision is None:
            scenes = apply_preferences(scenes, planning_context.get("planning_preferences", []))
        if automatic and visual_revision is None:
            validate_automatic_material_requests(scenes, planning_context)
        if material_clip_ids is not None:validate_observed_materials(scenes,planning_context["materials"],require_clip_selection=True)
        validate_presentation_requirements(project, scenes,
            identity_anchor_required=identity_anchor_required)
        validate_audience_boundary(scenes, topic=payload.topic or project.topic,
                                   script=payload.script, context=planning_context)
        validate_opening_context(scenes, topic=payload.topic or project.topic, script=payload.script)
        _, local_only = explicit_visual_constraints(payload.topic or project.topic, payload.script)
        if automatic and automatic_graphic_gaps(
            normalize_automatic_graphics(scenes),
            allow_typography_text_card=local_only,
        ):
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
            usage_callback=(lambda error: planner.usage_accounting(error if hasattr(error, "provider_usage") else None))
                if type(planner) is OpenAICompatibleScenePlanner else None,
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
    if visual_revision is None:
        response=response.model_copy(update={"scenes":normalize_creator_fit(response.scenes, planning_context)})
        if creator_scope_gaps(response.scenes, planning_context):
            raise ScenePlannerProviderResponseError(
                "scene plan does not cite the current creator scope",
                validation_code="scene_values",
                domain_rule="creator_scope_unverified",
                domain_field="creator_fit",
            )
    # Accounted replay is not allowed to bypass current content guards.
    validate_presentation_requirements(project, response.scenes,
        identity_anchor_required=identity_anchor_required)
    validate_audience_boundary(response.scenes, topic=payload.topic or project.topic,
                               script=payload.script, context=planning_context)
    validate_opening_context(response.scenes, topic=payload.topic or project.topic, script=payload.script)
    if material_clip_ids is not None:validate_observed_materials(response.scenes,planning_context["materials"],require_clip_selection=True)
    _, local_only = explicit_visual_constraints(payload.topic or project.topic, payload.script)
    if automatic and automatic_graphic_gaps(
        response.scenes,
        allow_typography_text_card=local_only,
    ):
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

