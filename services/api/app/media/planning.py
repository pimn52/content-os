"""普通制作的选段核验与自动换材，发生在语音、口型投入前。"""
from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from uuid import UUID, uuid5

from app.db import AssetRepository, ClipRepository, ProviderCallRepository
from app.domain.models import CandidateAsset, Clip, CostCategory, SourceKind, UsageCost
from app.provider_execution import (ProviderExecutionService, ProviderExecutionRecordedFailure,
    ProviderExecutionInProgress, runtime_provider_identity)
from app.providers.vision import VideoIntervalBatch, VisionTimeout
from app.search import ClipTextSearchService
from app.source_usage import SourceUsageService, SourceUsageConflict
from app.assembly.edit_plan import source_overlap_ms, same_source_information_change, intentional_reuse
from .local_paths import existing_local_media_file
from .vision import planning_visual_evidence,source_output_presentation
from .visual_facts import footage_geometry, full_interval_geometry, scene_visual_key, subtitle_keep_height, generated_footage_admitted, material_origin, matches_scene

_WINDOWS = UUID("75269028-6947-4fc9-b655-d5d21c7cdc02")
_FOOTAGE = {SourceKind.USER_ASSET, SourceKind.HISTORICAL_ASSET, SourceKind.STOCK, SourceKind.AI_BROLL}
_MIN_MATCH = .65
_BATCH_CLIPS = 3
_BATCH_DURATION_MS = 24000
_BATCH_BYTES = 12 * 1024 * 1024
_MAX_BATCHES = 3


class MaterialResolutionError(ValueError):
    def __init__(self, *reasons, candidate_rejections=None):
        self.reasons = reasons
        self.candidate_rejections = candidate_rejections or {}
        super().__init__(";".join(reasons))


def estimated_material_calls(db, project, topic):
    """估计目录准备和场景核验的调用数，不发起模型调用。"""
    known,windows=_catalogue_candidates(db,project,topic)
    return (1+math.ceil(len(windows)/_BATCH_CLIPS)) if known or windows else 0


def _talking_source_candidates(db, project, *, excluded_assets=frozenset()):
    """动作目录的同一候选构造供预算和执行读取，不生成代理、不修改事实。"""
    from app.talking.source_suitability import motion_source_binding
    clips, candidates = ClipRepository(db), []
    for asset in AssetRepository(db).list():
        if (asset.id in excluded_assets or asset.height <= asset.width or not _allowed(asset, project)
            or motion_source_binding(db, asset, project=project) is None):
            continue
        records = asset.metadata.get("talking_motion_analysis", {})
        if not isinstance(records, dict):
            records = {}
        native = [clip for clip in clips.list_by_asset(asset.id) if clip.reference_source_timebase is None]
        derived_ids = set(records) | {identity for record in records.values() if isinstance(record, dict)
            for identity in record.get("reference_clip_ids", [])}
        native = [clip for clip in native if str(clip.id) not in derived_ids]
        if not native:
            native = [Clip(asset_id=asset.id, start_ms=0, end_ms=asset.duration_ms,
                asset_duration_ms=asset.duration_ms)]
        covered = []
        for clip in sorted(native, key=lambda value: (-(value.end_ms-value.start_ms), value.start_ms)):
            if any(start <= clip.start_ms and end >= clip.end_ms for start, end in covered):
                continue
            covered.append((clip.start_ms, clip.end_ms))
            for start in range(clip.start_ms, clip.end_ms, _BATCH_DURATION_MS):
                end = min(clip.end_ms, start+_BATCH_DURATION_MS)
                identity = uuid5(_WINDOWS, f"motion:{asset.id}:{asset.content_hash}:{start}:{end}")
                previous = records.get(str(identity), {})
                if (previous.get("source_content_hash") == asset.content_hash
                    and previous.get("analysed_interval") == {"start_ms": start, "end_ms": end}):
                    continue
                window = clips.get(identity) or Clip(id=identity, asset_id=asset.id,
                    start_ms=start, end_ms=end, asset_duration_ms=asset.duration_ms)
                candidates.append((asset, window))
    candidates.sort(key=lambda value: (-(value[1].end_ms-value[1].start_ms), str(value[0].id), value[1].start_ms))
    return {str(clip.id):(asset,clip) for asset,clip in candidates[:8]}


def estimated_talking_calls(db, project):
    """纯读的调用保守上界；代理大小未测时每个窗口可单独成批，整轮最多三批。"""
    return min(_MAX_BATCHES, len(_talking_source_candidates(db, project)))


def _catalogue_candidates(db,project,topic):
    """先按主题召回，按实际采样点取短区间，不预设将来脚本的画面。"""
    from app.scene_planning import _scene_planning_context
    materials=_scene_planning_context(db,project,topic=topic)[0]["materials"]
    assets,clips=AssetRepository(db),ClipRepository(db)
    known,windows=[],{}
    for material in materials:
        hint=clips.get(UUID(material["clip_id"]))
        if hint is None:raise MaterialResolutionError("material_catalogue_changed")
        asset=assets.get(hint.asset_id)
        if not _allowed(asset,project):continue
        evidence=planning_visual_evidence(db,asset,hint,project=project)
        if evidence.get("output_presentation",{}).get("available") is False:
            continue
        if evidence.get("usable_interval"):
            if hint.id not in known:known.append(hint.id)
            continue
        anchors=[int(o["timestamp_ms"]) for o in evidence.get("observations",[])
            if type(o.get("timestamp_ms")) in (int,float) and hint.start_ms<=o["timestamp_ms"]<hint.end_ms]
        anchor=anchors[0] if anchors else (hint.start_ms+hint.end_ms)//2
        containing=[c for c in clips.list_by_asset(asset.id) if hint.start_ms<=c.start_ms<=anchor<c.end_ms<=hint.end_ms]
        nearest=min(containing,key=lambda c:c.end_ms-c.start_ms) if containing else hint
        span=min(6000,nearest.end_ms-nearest.start_ms)
        start=min(nearest.end_ms-span,max(nearest.start_ms,anchor-span//2));end=start+span
        identity=uuid5(_WINDOWS,f"{asset.id}:{asset.content_hash}:{start}:{end}")
        existing=clips.get(identity)
        if existing and planning_visual_evidence(db,asset,existing,project=project).get("usable_interval"):
            if identity not in known:known.append(identity)
        else:
            windows[str(identity)]=(asset,Clip(id=identity,asset_id=asset.id,start_ms=start,end_ms=end,asset_duration_ms=asset.duration_ms))
    # 同一确切区间不重复分析；整批候选上限沿用现有视频接口。
    return tuple(known[:8]),dict(list(windows.items())[:max(0,8-len(known))])


def _allowed(asset, project):
    return (asset is not None and asset.source_kind in _FOOTAGE
        and (asset.source_kind is not SourceKind.AI_BROLL or generated_footage_admitted(asset))
        and asset.metadata.get("project_id") in (None, str(project.id))
        and asset.metadata.get("ip_profile_id") in (None, str(project.ip_profile_id)))


def _needs_footage(scene):
    return (scene.visual_requirement != "creator_speaking" and scene.preferred_sources[0] is not SourceKind.TYPOGRAPHY
        and any(kind in _FOOTAGE for kind in (*scene.preferred_sources, *scene.fallback_sources)))


def _distinct_observed_windows(project,windows):
    """同起点且已核验的连续大区间可代表其前缀，不移动画面起点或合并未知区间。"""
    ready={identity:(asset,clip) for identity,(asset,clip) in windows.items()
        if source_output_presentation(project,asset,clip,(clip.start_ms,clip.end_ms))["available"]}
    selected={}
    for identity,(asset,clip) in windows.items():
        covering=[(other,source,window) for other,(source,window) in ready.items()
            if source.id==asset.id and source.content_hash==asset.content_hash
            and window.start_ms==clip.start_ms and window.end_ms>=clip.end_ms]
        if covering:
            identity,asset,clip=min(covering,key=lambda value:(-(value[2].end_ms-value[2].start_ms),value[0]))
        selected.setdefault(identity,(asset,clip))
    return selected


def split_footage_clauses(scenes):
    """长场景只在现有标点处分开，保留原文，不把一句话掐成残句。"""
    result = []
    for scene in sorted(scenes, key=lambda s:s.order):
        parts = re.split(r"(?<=[，。？！；])|(?<=[.!?;])(?=\s)", scene.voice_text)
        parts = [part for part in parts if part]
        can_split = (_needs_footage(scene) and (scene.graphic_plan is None or scene.graphic_plan.kind == "none")
            and scene.duration_target_ms > 8000 and len(parts) > 1
            and any(kind in _FOOTAGE for kind in (*scene.preferred_sources, *scene.fallback_sources)))
        if not can_split:
            result.append(scene)
            continue
        grouped, current = [], ""
        for part in parts:
            if current and (len(current)+len(part))*scene.duration_target_ms > 8000*len(scene.voice_text):
                grouped.append(current); current = ""
            current += part
        if current: grouped.append(current)
        used = 0
        for index, text in enumerate(grouped):
            duration = (scene.duration_target_ms-used if index == len(grouped)-1 else
                max(1, round(scene.duration_target_ms*len(text)/len(scene.voice_text))))
            used += duration
            result.append(scene.model_copy(update={"id":uuid5(scene.id, f"clause:{index}:{text}"),
                "scene_id":f"{scene.scene_id[:85]}-part-{index+1}", "voice_text":text, "duration_target_ms":duration}))
    return tuple(scene.model_copy(update={"order":index}) for index, scene in enumerate(result))


def subtitle_clearance(regions, protected, height):
    """仅裁除已定位的底部字幕；关键人物、标签和操作区不能被裁掉。"""
    if regions is None:
        return False
    if not regions:
        return True
    if protected is None:
        return False
    top = min(box[1] for box in regions)
    keep = subtitle_keep_height(height,regions,machine=True)/height
    return top >= .75 and all(box[1]+box[3] <= keep for box in protected)


def _analysis_attempt_key(job_id, attempt_id):
    return (f"initial:{job_id}" if attempt_id is None else
        hashlib.sha256(str(attempt_id).encode()).hexdigest()[:16])


def _invalid_analysis_fields(issues, clip_id):
    """字段错误是未知用途，不能转成画面确实不支持该用途。"""
    relevant=[issue for issue in issues if isinstance(issue,dict)
        and issue.get("clip_id")==clip_id and issue.get("code")=="invalid_clip_fields"] if isinstance(issues,list) else []
    if not relevant:return None
    return sorted({field for issue in relevant if isinstance(issue.get("fields"),list) for field in issue["fields"]
        if isinstance(field,str)})


def _invalid_analysis_facts(facts, fields, attempt_key, call_id=None):
    result={**facts,"analysis_valid":False,"analysis_attempt_key":attempt_key,
        "analysis_invalid_fields":fields,"coverage":"invalid_video_interval_analysis",
        "source_subtitle_clearance_verified":False}
    # 旧版为缺失的每个用途填造否定项；隔离只保留先前真正返回的其它用途事实。
    result["scene_matches"]={key:match for key,match in facts.get("scene_matches",{}).items()
        if match.get("reason")!="本次核验未认可该用途"}
    if call_id is not None:result["analysis_call_id"]=str(call_id)
    return result


def _legacy_invalid_analysis(facts, clip_id, calls):
    """从原任务留存的隔离结果恢复标记；纯读取，不以文案猜测有效性。"""
    reference=str(facts.get("evidence_reference","")).split(":")
    if (len(reference)!=4 or reference[0] not in {"video-analysis","catalogue-analysis"}
        or reference[2]!=clip_id or reference[3]!=facts.get("analysis_video_sha256")
        or not re.fullmatch(r"[0-9a-f]{64}",reference[3])):
        return None
    try:UUID(reference[1])
    except ValueError:return None
    stage="catalogue-analysis" if reference[0]=="catalogue-analysis" else "source-analysis"
    prefix=f"video-production:{reference[1]}:{stage}"
    for call in reversed(calls):
        if (call.status!="completed" or call.operation!="vision"
            or not (call.idempotency_key==prefix or call.idempotency_key.startswith(prefix+":batch:"))):continue
        payload=(call.result_payload or {}).get("result",{})
        if not isinstance(payload,dict):continue
        fields=_invalid_analysis_fields(payload.get("validation_issues"),clip_id)
        if fields is None:continue
        batch=payload.get("batch",{})
        rows=batch.get("clips",[]) if isinstance(batch,dict) else []
        row=next((row for row in rows if isinstance(row,dict) and row.get("clip_id")==clip_id),None)
        # 同一clip后来可能已有合法结果，只修正与当前保存隔离行一致的旧事实。
        if (row is None or row.get("description")!=facts.get("visual_description")
            or row.get("reason")!=facts.get("reason") or row.get("content_consistent") is not False
            or facts.get("content_consistent") is not False
            or row.get("comparison_layout")!=facts.get("comparison_layout")
            or row.get("subtitle_regions") is not None or row.get("protected_regions") is not None
            or row.get("matches")!=[]):continue
        attempt=call.idempotency_key.partition(":attempt:")[2] or _analysis_attempt_key(reference[1],None)
        return _invalid_analysis_facts(facts,fields,attempt,call.id)
    return None


def _analysis_pending(facts, complete, job_id, attempt_id):
    if facts.get("analysis_valid") is False:
        return attempt_id is not None and facts.get("analysis_attempt_key")!=_analysis_attempt_key(job_id,attempt_id)
    return not complete


class VisualMaterialResolver:
    max_analysis_batches = _MAX_BATCHES

    def __init__(self, db, data_root, provider, extractor, probe, cost_estimator):
        self.db, self.root, self.provider = db, Path(data_root), provider
        self.extractor, self.probe, self.cost_estimator = extractor, probe, cost_estimator
        self.assets, self.clips = AssetRepository(db), ClipRepository(db)

    def prepare_talking_sources(self, project, job_id, *, attempt_id=None, reservation_guard=None):
        """已登记动作来源先做有界整段分析；复用同Job范围与已持久结果，不发隐式重试。"""
        from app.talking.source_suitability import motion_source_binding, restore_observed_motion_references
        unavailable = {}
        for asset in self.assets.list():
            registered = asset.metadata.get("talking_motion_source")
            if isinstance(registered, dict) and registered.get("source_content_hash") != asset.content_hash:
                unavailable[str(asset.id)] = "talking_motion_source_content_hash_changed"
                continue
            if motion_source_binding(self.db, asset, project=project) is None:
                continue
            # 横片不会自动裁成竖片；保留其登记，等待合适的已认可衍生或其它来源。
            if asset.height <= asset.width:
                unavailable[str(asset.id)] = "portrait_source_required"
                continue
            try:
                self._require_bytes([asset])
            except MaterialResolutionError as error:
                unavailable[str(asset.id)] = error.reasons[0]
                continue
            except (ValueError, OSError):
                unavailable[str(asset.id)] = "talking_motion_source_file_unavailable"
                continue
            with self.db.transaction(immediate=True):
                asset = restore_observed_motion_references(self.db, asset)
        # 先长连续区间，再跨来源；请求上限沿既有素材核验，不扫描整份长母片。
        windows = _talking_source_candidates(self.db, project, excluded_assets=frozenset(UUID(value) for value in unavailable))
        windows, _ = self._bound_scope(project, windows, [], job_id, "talking-analysis")
        pending = {}
        for identity, (asset, clip) in windows.items():
            if str(asset.id) in unavailable:
                continue
            if motion_source_binding(self.db, asset, project=project) is None:
                raise MaterialResolutionError("talking_motion_source_registration_changed")
            record = asset.metadata.get("talking_motion_analysis", {}).get(identity, {})
            if (record.get("source_content_hash") != asset.content_hash
                or record.get("analysed_interval") != {"start_ms":clip.start_ms,"end_ms":clip.end_ms}):
                pending[identity] = asset, clip
        if pending and not getattr(self.provider, "supports_video_input", False):
            raise MaterialResolutionError("talking_motion_analysis_not_configured")
        analysed = 0
        prefix = f"video-production:{job_id}:talking-analysis:batch:"
        completed_batches = {record.get("analysis_batch_key") for asset, _ in windows.values()
            for record in asset.metadata.get("talking_motion_analysis", {}).values()
            if isinstance(record, dict) and str(record.get("analysis_batch_key", "")).startswith(prefix)}
        remaining_batches = max(0, _MAX_BATCHES-len(completed_batches))
        for index, (batch, videos) in enumerate(self._batches(pending)):
            if index >= remaining_batches:
                break
            for video in videos:
                video["talking_motion_reference"] = True
            self._analyze(project, batch, [], job_id, stage="talking-analysis", videos=videos,
                attempt_id=attempt_id, reservation_guard=reservation_guard)
            analysed += len(batch)
        return {"analysed_intervals": analysed, "bounded_pending_intervals": max(0,len(pending)-analysed),
            "coverage": "machine_video_interval", "unavailable_sources": unavailable}

    def check_configuration(self, project, topic):
        if estimated_material_calls(self.db, project, topic) and not getattr(self.provider, "supports_video_input", False):
            raise MaterialResolutionError("material_analysis_not_configured")

    def check_external_configuration(self, scenes):
        if any(s.material_request and s.material_request.source_kind == "stock" for s in scenes) and not getattr(self.provider, "supports_video_input", False):
            raise MaterialResolutionError("material_analysis_not_configured")

    def selected_materials_ready(self, project, scenes):
        """选材已持久保存时无需重取；后续 resolve 仍查文件、原核验范围及占用。"""
        if any(scene.material_request is not None for scene in scenes):return False
        for scene in split_footage_clauses(scenes):
            if not _needs_footage(scene):continue
            clip=self.clips.get(scene.preferred_clip_id) if scene.preferred_clip_id else None
            asset=self.assets.get(clip.asset_id) if clip else None
            if (clip is None or not _allowed(asset,project)
                or clip.end_ms-clip.start_ms<scene.duration_target_ms):
                return False
            facts=full_interval_geometry(asset,clip)
            if facts.get("coverage")!="video_interval_analysis" or not matches_scene(facts,scene):
                return False
            from app.media.semantic_rejections import rejected_window
            from app.assembly.video_spec import milliseconds_to_frames
            from app.assembly.edit_plan import source_frame_interval_ms
            first,last=source_frame_interval_ms(clip.start_ms,milliseconds_to_frames(scene.duration_target_ms,project.fps),project.fps,clip.end_ms)
            if rejected_window(self.db,scene_visual_key(scene),asset.id,first,last):return False
        return True

    def _require_bytes(self, assets):
        for asset in assets:
            source = existing_local_media_file(asset.source_file, self.root)
            with source.open("rb") as file:
                if hashlib.file_digest(file,"sha256").hexdigest() != asset.content_hash:
                    raise MaterialResolutionError("source_content_hash_mismatch")

    def _analysis_facts(self,project,windows):
        """无效标记读取原区间事实；旧账目仅在正常恢复时补写，不修改调用证据。"""
        result,updates,calls={}, {}, None
        for identity,(asset,clip) in windows.items():
            current=self.assets.get(asset.id)
            facts=footage_geometry(current,clip)
            raw=current.metadata.get("visual_analysis",{}).get(identity,{})
            # 原始标记只在哈希与确切区间仍一致时适用，不依赖下游的有效coverage筛选。
            if (isinstance(raw,dict) and raw.get("source_content_hash")==current.content_hash
                and raw.get("clip_interval")=={"start_ms":clip.start_ms,"end_ms":clip.end_ms}):facts=raw
            if "analysis_valid" not in facts:
                if calls is None:calls=ProviderCallRepository(self.db).list_for_project(project.id)
                recovered=_legacy_invalid_analysis(facts,identity,calls)
                if recovered is not None:
                    facts=recovered
                    updates.setdefault(asset.id,{})[identity]=facts
            result[identity]=facts
        if updates:
            with self.db.transaction(immediate=True):
                for asset_id,changed in updates.items():
                    current=self.assets.get(asset_id)
                    metadata=dict(current.metadata)
                    metadata["visual_analysis"]={**metadata.get("visual_analysis",{}),**changed}
                    self.assets.update(current.model_copy(update={"metadata":metadata}))
        return result

    def _recovered(self,project,job_id,prefix):
        recovered={}
        selection_ids = None
        for asset in self.assets.list():
            if not _allowed(asset,project):continue
            for clip_id,facts in asset.metadata.get("visual_analysis",{}).items():
                if isinstance(facts,dict) and str(facts.get("evidence_reference","")).startswith(f"{prefix}:{job_id}:"):
                    clip=self.clips.get(UUID(clip_id))
                    if clip is not None and clip.asset_id==asset.id:recovered[clip_id]=(asset,clip)
                    membership = facts.get("selection_clip_ids")
                    if membership is not None:
                        if not isinstance(membership, list) or selection_ids is not None and selection_ids != membership:
                            raise MaterialResolutionError("material_catalogue_changed")
                        selection_ids = membership
        if selection_ids is not None:
            recovered = {}
            for clip_id in selection_ids:
                clip = self.clips.get(UUID(clip_id))
                asset = self.assets.get(clip.asset_id) if clip else None
                if not _allowed(asset, project):
                    raise MaterialResolutionError("material_catalogue_changed")
                recovered[clip_id] = (asset, clip)
        return recovered

    def _bound_scope(self, project, windows, requests, job_id, stage, *, known=()):
        """保存原任务的确切候选，失败恢复不重新召回或改变区间。"""
        path = self.root/"render-diagnostics"/str(job_id)/f"material-{stage}-scope.json"
        path.parent.mkdir(parents=True,exist_ok=True)
        document = {"project_id":str(project.id), "stage":stage, "requests":requests,
            "known_clip_ids":[str(identity) for identity in known], "windows":[
                {"clip_id":identity,"asset_id":str(asset.id),"source_content_hash":asset.content_hash,
                    "start_ms":clip.start_ms,"end_ms":clip.end_ms} for identity,(asset,clip) in windows.items()]}
        try:
            with path.open("x",encoding="utf-8") as stream:
                json.dump(document,stream,ensure_ascii=False,sort_keys=True)
        except FileExistsError:
            pass
        try:
            stored=json.loads(path.read_text(encoding="utf-8"))
            if (stored["project_id"]!=str(project.id) or stored["stage"]!=stage or stored["requests"]!=requests
                    or not isinstance(stored["windows"],list) or len(stored["windows"])>8):
                raise ValueError
            bound={}
            for record in stored["windows"]:
                asset=self.assets.get(UUID(record["asset_id"]))
                if not _allowed(asset,project) or record["source_content_hash"]!=asset.content_hash:
                    raise ValueError
                clip=Clip(id=UUID(record["clip_id"]),asset_id=asset.id,start_ms=record["start_ms"],
                    end_ms=record["end_ms"],asset_duration_ms=asset.duration_ms)
                previous=self.clips.get(clip.id)
                if previous and (previous.asset_id,previous.start_ms,previous.end_ms)!=(asset.id,clip.start_ms,clip.end_ms):
                    raise ValueError
                bound[str(clip.id)]=(asset,previous or clip)
            if len(bound)!=len(stored["windows"]):raise ValueError
            return bound,tuple(UUID(identity) for identity in stored["known_clip_ids"])
        except (ValueError,TypeError,KeyError,AttributeError,OSError) as exc:
            raise MaterialResolutionError("material_analysis_scope_changed") from exc

    def prepare(self,project,topic,job_id,*,attempt_id=None):
        """先补齐真实素材目录，再写脚本。复用既有核验，不按脚本臆测素材。"""
        path=self.root/"render-diagnostics"/str(job_id)/"material-catalogue-analysis-scope.json"
        known,windows=(),{}
        if not path.exists():
            known,windows=_catalogue_candidates(self.db,project,topic)
            recovered=self._recovered(project,job_id,"catalogue-analysis")
            if recovered:
                first_asset,first_clip=next(iter(recovered.values()))
                membership=first_asset.metadata["visual_analysis"][str(first_clip.id)].get("catalogue_clip_ids")
                if not isinstance(membership,list):raise MaterialResolutionError("material_catalogue_changed")
                windows={}
                known=tuple(UUID(identity) for identity in membership)
        windows,known=self._bound_scope(project,windows,[],job_id,"catalogue-analysis",known=known)
        membership=tuple(sorted({*known,*(clip.id for _,clip in windows.values())},key=str))
        observations=self._analysis_facts(project,windows)
        pending={identity:(asset,clip) for identity,(asset,clip) in windows.items()
            if _analysis_pending(observations[identity],observations[identity].get("coverage")=="video_interval_analysis",
                job_id,attempt_id)}
        for index,(batch,videos) in enumerate(self._batches(pending)):
            if index>=_MAX_BATCHES:raise MaterialResolutionError("material_catalogue_batch_limit")
            self._analyze(project,batch,[],job_id,stage="catalogue-analysis",catalogue_clip_ids=membership,
                videos=videos,attempt_id=attempt_id)
        all_clips=[self.clips.get(clip_id) for clip_id in membership]
        if any(clip is None or not _allowed(self.assets.get(clip.asset_id),project) for clip in all_clips):
            raise MaterialResolutionError("material_catalogue_changed")
        source_assets={clip.asset_id:self.assets.get(clip.asset_id) for clip in all_clips}
        self._require_bytes(source_assets.values())
        ready=tuple(sorted({clip.id for clip in all_clips
            if planning_visual_evidence(self.db,source_assets[clip.asset_id],clip,project=project).get("usable_interval")},key=str))
        return ready

    def _windows(self, project, scenes, acquired_assets=None):
        """粗描述只负责召回；实际上传的是有边界的原片视频。"""
        found, requests, pools = {}, {}, []
        search = ClipTextSearchService(self.db)
        for scene in scenes:
            if not _needs_footage(scene):
                continue
            key = scene_visual_key(scene)
            from app.media.semantic_rejections import negative_source_evidence,rejected_window
            from app.assembly.edit_plan import source_frame_interval_ms
            from app.assembly.video_spec import milliseconds_to_frames
            negative = negative_source_evidence(self.db,key)
            requests[key] = {"scene_key":key, "voice_text":scene.voice_text, "purpose":scene.purpose,
                "visual_intent":scene.visual_intent.model_dump(mode="json"), "visual_requirement":scene.visual_requirement,
                "visual_requirement_reason":scene.visual_requirement_reason}
            if scene.viewer_takeaway:requests[key]["viewer_takeaway"] = scene.viewer_takeaway
            if scene.caption_emphasis:requests[key]["caption_emphasis"] = scene.caption_emphasis
            if scene.graphic_plan is not None and scene.graphic_plan.kind != "none":
                requests[key]["graphic_plan"] = scene.graphic_plan.model_dump(mode="json")
            preferred = self.clips.get(scene.preferred_clip_id) if scene.preferred_clip_id else None
            query = " ".join((scene.voice_text, scene.visual_intent.subject, scene.visual_intent.action or ""))
            hits = search.search(query, top_k=12)
            external = {}
            if scene.material_request and acquired_assets:
                for asset_id in acquired_assets.get(" ".join(scene.material_request.query.split()), []):
                    external.setdefault(asset_id,self.clips.list_by_asset(asset_id))
            local = [hit.clip for hit in hits if hit.clip != preferred]
            if external and scene.visual_intent.subject:
                # 有明确外取需求时，通用动作词不能挤掉具体对象候选；这只是粗召回，仍须视频核验。
                subject_hits = search.search(scene.visual_intent.subject, top_k=12)
                subject_ids = {hit.clip.id for hit in subject_hits if hit.score >= .5}
                local = [clip for clip in local if clip.id in subject_ids]
            scene_windows = []
            represented = set()
            hint_cache = {}
            def hint_windows(hint):
                if hint.id in hint_cache:return hint_cache[hint.id]
                choices=[]
                hint_cache[hint.id]=choices
                asset = self.assets.get(hint.asset_id)
                if (not _allowed(asset, project)
                    or asset.source_kind is SourceKind.AI_BROLL and scene.visual_requirement=="action_evidence"):
                    return choices
                evidence = planning_visual_evidence(self.db, asset, hint,project=project)
                usable=evidence.get("usable_interval")
                if (usable and full_interval_geometry(asset,hint)
                    and hint.end_ms-hint.start_ms>=scene.duration_target_ms):
                    choices.append((asset,hint))
                    return choices
                if evidence.get("output_presentation",{}).get("available") is False:
                    return choices
                anchors = [int(o["timestamp_ms"]) for o in evidence.get("observations", [])
                    if isinstance(o.get("timestamp_ms"), (int,float)) and hint.start_ms <= o["timestamp_ms"] < hint.end_ms]
                anchors = anchors or [(hint.start_ms+hint.end_ms)//2]
                fine = self.clips.list_by_asset(asset.id)
                for anchor in anchors[:2]:
                    containing = [c for c in fine if hint.start_ms <= c.start_ms <= anchor < c.end_ms <= hint.end_ms]
                    nearest = min(containing, key=lambda c:c.end_ms-c.start_ms) if containing else hint
                    # 够用时优先限制在细分镜头内，不因语速余量跨镜头。
                    # 几何切点并不代表内容改变；较长候选仍须看完整视频决定，
                    # 避免机械切点把连贯的对比画面也误删。
                    bounds = nearest if nearest.end_ms-nearest.start_ms >= scene.duration_target_ms else hint
                    span = min(24000, bounds.end_ms-bounds.start_ms, math.ceil(scene.duration_target_ms*1.35)+800)
                    if span < scene.duration_target_ms: continue
                    for proposed in (nearest.start_ms, anchor):
                        start = min(bounds.end_ms-span, max(bounds.start_ms, proposed))
                        end = start+span
                        identity = uuid5(_WINDOWS, f"{asset.id}:{asset.content_hash}:{start}:{end}")
                        if any(str(clip.id)==str(identity) for _,clip in choices):continue
                        choices.append((asset,Clip(id=identity,asset_id=asset.id,start_ms=start,
                            end_ms=end,asset_duration_ms=asset.duration_ms)))
                return choices
            def add_hint(hint,limit=1):
                added=0
                for asset,clip in hint_windows(hint):
                    first,last=source_frame_interval_ms(clip.start_ms,milliseconds_to_frames(scene.duration_target_ms,project.fps),project.fps,clip.end_ms)
                    if rejected_window(self.db,key,asset.id,first,last,evidence=negative):continue
                    identity=str(clip.id)
                    if identity in scene_windows:continue
                    if len(scene_windows)>=3 or added>=limit:break
                    found.setdefault(identity,(asset,clip))
                    scene_windows.append(identity)
                    represented.add(asset.id)
                    added+=1
                return added
            if preferred:add_hint(preferred,1 if external else 2)
            # 外取池按文件分配首个有效窗口；一份素材的多个Clip不能挤掉另一份已取得的候选。
            # 无效或重复hint不占槽；已有preferred仍保留，后续统一核验决定是否可用。
            for asset_id,hints in external.items():
                if len(scene_windows)>=3:break
                if asset_id in represented:continue
                for hint in hints:
                    if add_hint(hint):break
            for hint in local:
                if len(scene_windows)>=3:break
                add_hint(hint)
            if external and preferred and len(scene_windows)<3:add_hint(preferred)
            for hints in external.values():
                for hint in hints:
                    if len(scene_windows)>=3:break
                    add_hint(hint)
            pools.append(scene_windows)
        ordered = []
        for index in range(3):
            for pool in pools:
                if index < len(pool) and pool[index] not in ordered:
                    ordered.append(pool[index])
        distinct=_distinct_observed_windows(project,{key:found[key] for key in ordered})
        return dict(list(distinct.items())[:8]), list(requests.values())

    def resolve(self, project, scenes, job_id, *, work_id=None, acquired_assets=None, attempt_id=None,revision_reserve_calls=0,
                reserved_source_uses=(),after_master=False,remaining_talking_calls=1,supplement_reserve_calls=0):
        scenes = split_footage_clauses(scenes)
        windows, requests = self._windows(project, scenes, acquired_assets)
        if not requests: return scenes
        scope=self.root/"render-diagnostics"/str(job_id)/"material-source-analysis-scope.json"
        if not scope.exists():
            recovered=self._recovered(project,job_id,"video-analysis")
            if recovered: windows = recovered
        if len(requests) > 24:
            raise MaterialResolutionError("material_analysis_scope_too_large")
        if not windows and not scope.exists():
            missing=[(f"material_source_unavailable:{s.scene_id}:{s.material_request.source_kind}"
                if s.material_request else f"material_scene_unresolved:{s.scene_id}") for s in scenes if _needs_footage(s)]
            raise MaterialResolutionError(*missing)
        windows,_=self._bound_scope(project,windows,requests,job_id,"source-analysis")
        source_assets = list({asset.id:asset for asset, _ in windows.values()}.values())
        self._require_bytes(source_assets)
        def missing_requests(facts, needed):
            matches = facts.get("scene_matches", {})
            missing = []
            for request in needed:
                match = matches.get(request["scene_key"], {})
                if match.get("analysis_valid") is False:
                    if attempt_id is not None and match.get("analysis_attempt_key") != _analysis_attempt_key(job_id, attempt_id):
                        missing.append(request)
                    continue  # An invalid answer is unknown; never silently repeat it.
                if (facts.get("coverage") != "video_interval_analysis" or not match
                    or request.get("visual_requirement") == "action_evidence"
                    and match.get("supported") and match.get("subject_verified") is None):
                    missing.append(request)
            return missing
        try:
            return self._select(project,scenes,windows,work_id,reserved_source_uses=reserved_source_uses)
        except MaterialResolutionError as initial:
            failure=initial
        index = 0
        while True:
            # Only unresolved uses need new observations. Unchanged admitted
            # scene matches remain valid after local timing/occupancy checks.
            unresolved = {scene.scene_id for scene in scenes if any(
                reason in {f"material_scene_unresolved:{scene.scene_id}", f"material_combination_unresolved:{scene.scene_id}"}
                or reason.startswith(f"material_source_unavailable:{scene.scene_id}:") for reason in failure.reasons)}
            for scene_index, scene in enumerate(scenes):
                if f"material_combination_unresolved:{scene.scene_id}" in failure.reasons:
                    # A prior choice may consume the only option for this
                    # scene. Observe missing alternatives in that prefix too.
                    unresolved.update(value.scene_id for value in scenes[:scene_index + 1] if _needs_footage(value))
            needed_keys = {scene_visual_key(scene) for scene in scenes if scene.scene_id in unresolved}
            needed = [request for request in requests if not needed_keys or request["scene_key"] in needed_keys]
            reusable = self._analysis_facts(project, windows)
            pending, request_sets = {}, {}
            for identity, window in windows.items():
                missing = missing_requests(reusable[identity], needed)
                if missing and _analysis_pending(reusable[identity], False, job_id, attempt_id):
                    pending[identity], request_sets[identity] = window, missing
            if not pending:
                raise failure
            if index>=_MAX_BATCHES:
                raise MaterialResolutionError(*failure.reasons,"material_analysis_batch_limit",
                    candidate_rejections=failure.candidate_rejections)
            # Do not ask a candidate to repeat a known answer just because
            # another candidate is missing a different use in the same batch.
            batch_requests = request_sets[next(iter(pending))]
            keys = {request["scene_key"] for request in batch_requests}
            compatible = {identity: window for identity, window in pending.items()
                if {request["scene_key"] for request in request_sets[identity]} == keys}
            batch, videos = next(self._batches(compatible))
            try:
                self._analyze(project,batch,batch_requests,job_id,selection_clip_ids=tuple(windows),
                    videos=videos,attempt_id=attempt_id,revision_reserve_calls=revision_reserve_calls,after_master=after_master,
                    remaining_talking_calls=remaining_talking_calls,supplement_reserve_calls=supplement_reserve_calls)
            except MaterialResolutionError as resource:
                if resource.reasons not in {("material_analysis_revision_due",), ("material_acquisition_due",)}:raise
                raise MaterialResolutionError(*failure.reasons,*resource.reasons,
                    candidate_rejections=failure.candidate_rejections) from resource
            index += 1
            try:
                return self._select(project,scenes,windows,work_id,reserved_source_uses=reserved_source_uses)
            except MaterialResolutionError as current:
                failure=current

    def _video_input(self,identity,asset,clip):
        path = self.extractor.extract_video_interval(asset, clip.start_ms, clip.end_ms)
        info = self.probe.probe(path)
        tolerance = math.ceil(1000*asset.fps.denominator/asset.fps.numerator)+2
        if (abs(info.duration_ms-(clip.end_ms-clip.start_ms)) > tolerance
            or info.width <= 0 or info.height <= 0 or info.has_audio
            or abs(info.width*asset.height-info.height*asset.width) > 2*asset.width):
            raise MaterialResolutionError("material_analysis_proxy_invalid")
        if info.duration_ms>_BATCH_DURATION_MS or path.stat().st_size>_BATCH_BYTES:
            raise MaterialResolutionError(f"material_analysis_proxy_too_large:{identity}")
        return {"clip_id":identity, "path":str(path), "source_asset_id":str(asset.id),
            "source_content_hash":asset.content_hash, "start_ms":clip.start_ms, "end_ms":clip.end_ms,
            "video_sha256":hashlib.sha256(path.read_bytes()).hexdigest(), "proxy_duration_ms":info.duration_ms,
            "source_kind":asset.source_kind.value}

    def revision_candidates(self,project,failure):
        """复用本次缺口中的实际候选，不扩检索、不重新核验已知几何。"""
        identities={identity for rows in failure.candidate_rejections.values() for identity in rows}
        ready=[]
        for identity in sorted(identities):
            clip=self.clips.get(UUID(identity))
            asset=self.assets.get(clip.asset_id) if clip else None
            if not _allowed(asset,project):continue
            if planning_visual_evidence(self.db,asset,clip,project=project).get("usable_interval"):
                ready.append(clip.id)
        return tuple(ready[:8])

    def _batches(self,windows):
        """保留候选顺序与完整区间，缺口仍在时才准备下一批视频。"""
        batch,videos,duration,size={},[],0,0
        for identity,(asset,clip) in windows.items():
            if batch and duration+clip.end_ms-clip.start_ms>_BATCH_DURATION_MS:
                yield batch,videos
                batch,videos,duration,size={},[],0,0
            video=self._video_input(identity,asset,clip)
            count=Path(video["path"]).stat().st_size
            if batch and size+count>_BATCH_BYTES:
                yield batch,videos
                batch,videos,duration,size={},[],0,0
            batch[identity]=(asset,clip);videos.append(video)
            duration+=video["proxy_duration_ms"];size+=count
            if len(batch)>=_BATCH_CLIPS or duration>=_BATCH_DURATION_MS or size>=_BATCH_BYTES:
                yield batch,videos
                batch,videos,duration,size={},[],0,0
        if batch:yield batch,videos

    def _reserve_tail(self,project,job_id,stage,key,revision_reserve_calls=0,after_master=False,remaining_talking_calls=1,supplement_reserve_calls=0):
        from app.budget import BudgetLimitError,snapshot
        from app.db import BudgetPolicyRepository,ProviderCallRepository
        project_calls=ProviderCallRepository(self.db).list_for_project(project.id)
        prefix=f"video-production:{job_id}:{stage}:batch:"
        attempt=key.partition(":attempt:")[2]
        previous=[call for call in project_calls if call.idempotency_key.startswith(prefix)
            and call.idempotency_key.partition(":attempt:")[2]==attempt and call.status!="cancelled"]
        if len(previous)>=_MAX_BATCHES:
            raise MaterialResolutionError("material_analysis_batch_limit")
        tail=4 if project.presentation_style=="creator_led" else 3
        if after_master:tail=remaining_talking_calls+1
        if stage in {"catalogue-analysis", "talking-analysis"}:tail+=2  # 首次规划及至少一批场景用途核验。
        for policy in (BudgetPolicyRepository(self.db).get_by_project(None),BudgetPolicyRepository(self.db).get_by_project(project.id)):
            if policy is None:continue
            calls=ProviderCallRepository(self.db).list() if policy.project_id is None else project_calls
            # 第一批仍可直接完成；已核验却不足时，优先保留一次调整及复核，而非耗尽候选。
            reserve_calls = supplement_reserve_calls or revision_reserve_calls
            if (stage=="source-analysis" and reserve_calls and any(call.status=="completed" for call in previous)
                and policy.max_calls is not None):
                remaining=policy.max_calls-snapshot(calls,policy.currency).calls
                # A completed supplement may already own the remaining
                # budget. Yield to its durable ledger even when only the
                # production tail remains; replay must not recheck old clips.
                if remaining<tail+reserve_calls+1:
                    raise MaterialResolutionError("material_acquisition_due" if supplement_reserve_calls else "material_analysis_revision_due")
            if policy.max_calls is not None and snapshot(calls,policy.currency).calls+1+tail>policy.max_calls:
                raise BudgetLimitError("material_analysis_tail_budget","素材核验不能耗尽后续语音、口型与渲染额度")

    def _analyze(self,project,windows,requests,job_id,*,stage="source-analysis",catalogue_clip_ids=None,selection_clip_ids=None,
                 videos=None,attempt_id=None,revision_reserve_calls=0,reservation_guard=None,after_master=False,remaining_talking_calls=1,supplement_reserve_calls=0):
        source_assets=list({asset.id:asset for asset,_ in windows.values()}.values())
        self._require_bytes(source_assets)
        keys={r["scene_key"] for r in requests}
        if not getattr(self.provider, "supports_video_input", False):
            raise MaterialResolutionError("material_analysis_not_configured")
        if videos is None:
            videos=[self._video_input(identity,asset,clip) for identity,(asset,clip) in windows.items()]
        if (len(videos)>_BATCH_CLIPS or sum(v["proxy_duration_ms"] for v in videos)>_BATCH_DURATION_MS
                or sum(Path(v["path"]).stat().st_size for v in videos)>_BATCH_BYTES):
            raise MaterialResolutionError("material_analysis_scope_too_large")
        document={"videos":[{k:v for k,v in video.items() if k!="path"} for video in videos],"scenes":requests}
        digest=hashlib.sha256(json.dumps(document,sort_keys=True,ensure_ascii=False).encode()).hexdigest()[:24]
        key=f"video-production:{job_id}:{stage}:batch:{digest}"
        if attempt_id is not None:
            key+=":attempt:"+_analysis_attempt_key(job_id,attempt_id)
        evidence=self.root/"render-diagnostics"/str(job_id)/f"material-{stage}-{digest}-{hashlib.sha256(key.encode()).hexdigest()[:12]}.json"
        evidence.parent.mkdir(parents=True,exist_ok=True)
        evidence.write_text(json.dumps({"job_id":str(job_id),"attempt_id":str(attempt_id) if attempt_id is not None else None,
            "idempotency_key":key,"input":document},ensure_ascii=False,sort_keys=True),encoding="utf-8")
        identity = runtime_provider_identity(self.provider)
        if identity is not None and attempt_id is None:
            legacy=ProviderCallRepository(self.db).get_by_idempotency_key(f"video-production:{job_id}:{stage}")
            if legacy and legacy.status=="failed":
                raise ProviderExecutionRecordedFailure("原核验已失败，请通过同稿恢复创建新的尝试")
            if legacy and legacy.status in {"reserved","running"}:
                raise ProviderExecutionInProgress("原核验调用仍在执行，不能重复发送")
        def unchanged():
            self._require_bytes(source_assets)
            for video in videos:
                if hashlib.sha256(Path(video["path"]).read_bytes()).hexdigest() != video["video_sha256"]:
                    raise MaterialResolutionError("material_analysis_proxy_changed")
        def analyze():
            unchanged()
            return self.provider.analyze_intervals(videos, requests)
        def reserve():
            self._reserve_tail(project,job_id,stage,key,revision_reserve_calls,after_master=after_master,
                remaining_talking_calls=remaining_talking_calls,supplement_reserve_calls=supplement_reserve_calls)
            if reservation_guard is not None:
                reservation_guard()
        call=None
        if identity is None:
            result = analyze()  # 本地替身不冒充真实调用。
            validation_issues=getattr(self.provider,"last_validation_issues",[])
        else:
            result = ProviderExecutionService(self.db, self.cost_estimator).execute(project_id=project.id,
                idempotency_key=key, operation="vision", provider=identity,
                input_source=f"project:{project.id}:source-intervals", category=CostCategory.LLM,
                input_document=document,
                action=analyze, encode_result=lambda r:{"batch":r.model_dump(mode="json"),
                    "provider_usage":getattr(self.provider,"last_usage",{}),
                    "validation_issues":getattr(self.provider,"last_validation_issues",[])},
                decode_result=lambda r:VideoIntervalBatch.model_validate(r.get("batch",r)),
                error_code=lambda exc:"material_video_analysis_timeout" if isinstance(exc,VisionTimeout) else "material_video_analysis_failed", before_replay=unchanged,
                usage_callback=getattr(self.provider,"usage_accounting",None),
                reservation_guard=reserve)
            call=ProviderCallRepository(self.db).get_by_idempotency_key(key)
            # 缓存重放必须读取本调用的校验问题，不能沿用provider上一请求的临时字段。
            recorded=(call.result_payload or {}).get("result",{}) if call is not None else {}
            validation_issues=recorded.get("validation_issues",[]) if isinstance(recorded,dict) else []
        if (not isinstance(result, VideoIntervalBatch) or len(result.clips) != len(windows)
            or set(o.clip_id for o in result.clips) != set(windows)
            or any(m.scene_key not in keys for o in result.clips for m in o.matches)):
            raise MaterialResolutionError("material_analysis_scope_invalid")
        unchanged()
        by_video = {video["clip_id"]:video for video in videos}
        with self.db.transaction(immediate=True):
            for observed in result.clips:
                asset, proposed = windows[observed.clip_id]
                current = self.assets.get(asset.id)
                if not _allowed(current, project) or current.content_hash != asset.content_hash:
                    raise MaterialResolutionError("source_content_hash_mismatch")
                previous = self.clips.get(proposed.id)
                if previous is not None and (previous.asset_id,previous.start_ms,previous.end_ms)!=(proposed.asset_id,proposed.start_ms,proposed.end_ms):
                    raise MaterialResolutionError("material_interval_changed")
                clip = self.clips.upsert((previous or proposed).model_copy(update={"visual_description":observed.description}))
                metadata = dict(current.metadata); facts = dict(metadata.get("visual_analysis", {}))
                old = facts.get(str(clip.id), {})
                invalid_fields=_invalid_analysis_fields(validation_issues,observed.clip_id)
                valid=invalid_fields is None
                if (not valid and old.get("coverage") == "video_interval_analysis"
                    and old.get("analysis_valid") is not False and old.get("source_content_hash") == current.content_hash
                    and old.get("clip_interval") == {"start_ms": clip.start_ms, "end_ms": clip.end_ms}):
                    # A malformed new use does not erase independent, valid
                    # observations for other uses of the exact same bytes.
                    matches = dict(old.get("scene_matches", {}))
                    for scene_key in keys:
                        matches[scene_key] = {**matches.get(scene_key, {}), "analysis_valid": False,
                            "analysis_attempt_key": _analysis_attempt_key(job_id, attempt_id),
                            "analysis_invalid_fields": invalid_fields}
                    facts[str(clip.id)] = {**old, "scene_matches": matches}
                    metadata["visual_analysis"] = facts
                    if previous is not None:
                        self.clips.upsert(previous)
                    self.assets.update(current.model_copy(update={"metadata": metadata}))
                    continue
                matches={key:match for key,match in old.get("scene_matches",{}).items() if key not in keys}
                if valid:
                    matches.update({key:{"scene_key":key,"supported":False,"score":0.0,"reason":"本次核验未认可该用途"}
                        for key in keys})
                    matches.update({m.scene_key:m.model_dump(mode="json") for m in observed.matches})
                proxy = by_video[observed.clip_id]
                subtitles=observed.regions_xywh("subtitle_regions")
                protected=observed.regions_xywh("protected_regions")
                facts[str(clip.id)] = dict(source_content_hash=current.content_hash,
                    clip_interval=dict(start_ms=clip.start_ms,end_ms=clip.end_ms),
                    covered_start_ms=clip.start_ms, covered_end_ms=clip.end_ms,
                    coverage="video_interval_analysis" if valid else "invalid_video_interval_analysis",
                    method="provider_video_interval_analysis",analysis_valid=valid,
                    analysis_attempt_key=_analysis_attempt_key(job_id,attempt_id),
                    analysis_video_sha256=proxy["video_sha256"], analysis_duration_ms=clip.end_ms-clip.start_ms,
                    proxy_duration_ms=proxy["proxy_duration_ms"], visual_description=observed.description,
                    content_consistent=observed.content_consistent, comparison_layout=observed.comparison_layout,
                    subtitle_regions=subtitles, protected_regions=protected,
                    source_subtitle_clearance_verified=subtitle_clearance(subtitles, protected, asset.height),
                    scene_matches=matches, reason=observed.reason,
                    evidence_reference=f"{'catalogue-analysis' if stage=='catalogue-analysis' else 'talking-analysis' if stage=='talking-analysis' else 'video-analysis'}:{job_id}:{clip.id}:{proxy['video_sha256']}")
                if call is not None:facts[str(clip.id)]["analysis_call_id"]=str(call.id)
                if not valid:
                    facts[str(clip.id)]=_invalid_analysis_facts(facts[str(clip.id)],invalid_fields,
                        _analysis_attempt_key(job_id,attempt_id),call.id if call is not None else None)
                if catalogue_clip_ids is not None:
                    facts[str(clip.id)]["catalogue_clip_ids"]=[str(identity) for identity in catalogue_clip_ids]
                if selection_clip_ids is not None:
                    facts[str(clip.id)]["selection_clip_ids"] = list(selection_clip_ids)
                metadata["visual_analysis"] = facts
                updated_asset = self.assets.update(current.model_copy(update={"metadata":metadata}))
                if proxy.get("talking_motion_reference") is True:
                    from app.talking.source_suitability import admit_observed_motion_intervals
                    admit_observed_motion_intervals(self.db, updated_asset, clip, observed.talking_intervals,
                        evidence_reference=facts[str(clip.id)]["evidence_reference"],
                        video_sha256=proxy["video_sha256"], analysis_call_id=call.id if call is not None else None,
                        analysis_batch_key=key)

    def _select(self, project, scenes, windows, work_id, *, reserved_source_uses=()):
        usage = SourceUsageService(self.db)
        occupied = usage.occupied(project.id, work_id=work_id)
        occupied.extend(reserved_source_uses)
        pools, failures, rejected, negative_by_scene = {}, [], {}, {}
        for scene in scenes:
            key = scene_visual_key(scene)
            if not _needs_footage(scene):
                continue
            from app.media.semantic_rejections import negative_source_evidence,candidate_rejections
            negative = negative_source_evidence(self.db,key)
            negative_by_scene[scene.id] = negative
            options = []
            rejected[scene.scene_id] = {}
            for _, clip in windows.values():
                asset = self.assets.get(clip.asset_id)
                facts = full_interval_geometry(asset, clip)
                match = facts.get("scene_matches", {}).get(key, {})
                reasons=[]
                if not _allowed(asset,project):reasons.append("source_unavailable")
                if asset is not None and asset.source_kind is SourceKind.AI_BROLL and scene.visual_requirement=="action_evidence":
                    reasons.append("generated_not_factual_evidence")
                if not facts:reasons.append("interval_analysis_unavailable")
                elif match.get("analysis_valid") is False:reasons.append("visual_purpose_unknown")
                elif not match.get("supported") or match.get("score",0)<_MIN_MATCH or not matches_scene(facts,scene):
                    reasons.append("visual_purpose_mismatch")
                if facts and not facts.get("source_subtitle_clearance_verified"):reasons.append("subtitle_clearance_unverified")
                if clip.end_ms-clip.start_ms<scene.duration_target_ms:reasons.append("duration_insufficient")
                if reasons:
                    rejected[scene.scene_id][str(clip.id)]=reasons
                    continue
                candidate = CandidateAsset(scene_plan_id=scene.id,source_kind=asset.source_kind,asset_id=asset.id,
                    clip_id=clip.id,playback_start_ms=clip.start_ms,playback_end_ms=clip.end_ms, match_score=match["score"],
                    why=["真实视频区间与当前表达任务核验匹配"],estimated_cost=UsageCost(category={
                        SourceKind.USER_ASSET:CostCategory.USER_ASSET,SourceKind.HISTORICAL_ASSET:CostCategory.HISTORICAL_ASSET,
                        SourceKind.STOCK:CostCategory.STOCK,SourceKind.AI_BROLL:CostCategory.AI_VIDEO}[asset.source_kind],amount=None))
                if candidate_rejections(self.db,scene,candidate,fps=project.fps,evidence=negative):
                    rejected[scene.scene_id][str(clip.id)]=['source_semantic_use_rejected']
                    continue
                try:
                    from app.assembly.edit_plan import _default_edit_plan, presentation_for_style, EditPlanPreflightError
                    from app.domain.models import VisualStyleTokens, ProjectFormat
                    entry = _default_edit_plan(project,[scene],{scene.id:candidate}).scenes[0]
                    presentation_for_style(entry,VisualStyleTokens(),asset,scene_plan=scene,clip=clip,
                        playback_interval_ms=(clip.start_ms,clip.start_ms+scene.duration_target_ms),
                        portrait_output=project.format is ProjectFormat.VERTICAL)
                    uses = usage.candidate_uses(candidate, scene.scene_id, scene.duration_target_ms)
                    usage.require_available(project.id, uses, work_id=work_id)
                    from app.source_usage import conflicts
                    if any(conflicts(reserved,use) for use in uses for reserved in reserved_source_uses):
                        rejected[scene.scene_id][str(clip.id)]=["talking_motion_reserved"]
                        continue
                    degree = usage.clip_occupancy_degree(clip.model_copy(update={"end_ms":clip.start_ms+scene.duration_target_ms}), occupied)
                except SourceUsageConflict:
                    rejected[scene.scene_id][str(clip.id)]=["source_usage_conflict"]
                    continue
                except EditPlanPreflightError:
                    rejected[scene.scene_id][str(clip.id)]=["presentation_incompatible"]
                    continue
                fit={"direct":3,"illustrative":2,"unknown":1,"context":0}.get(match.get("fit","unknown"),1)
                options.append((candidate, asset, degree, uses, fit, clip.quality_score))
            if not options:
                source=scene.material_request.source_kind if scene.material_request else None
                failures.append(f"material_source_unavailable:{scene.scene_id}:{source}" if source else f"material_scene_unresolved:{scene.scene_id}")
                continue
            # 表达任务优先；仅在同等作用、匹配相近的合格素材之间平衡整片。
            best_fit=max(item[4] for item in options)
            best_score=max(item[0].match_score for item in options if item[4]==best_fit)
            pools[scene.id]=sorted((item for item in options if item[4]==best_fit and item[0].match_score>=best_score-.08),
                key=lambda item:str(item[0].clip_id))
        if failures:
            raise MaterialResolutionError(*failures,candidate_rejections=rejected)
        # 有界联合搜索取代逐段贪心。最多 8 候选，每步保留 64 个组合，不新增模型调用。
        # 画面重复和横屏次选过度使用先于来源偏好；不设三类来源比例。
        total=sum(scene.duration_target_ms for scene in scenes)
        states=[({},[],{},(0,0,0,0,0,0,0))]
        from app.domain.models import ProjectFormat
        from app.source_usage import conflicts
        for scene in scenes:
            if scene.id not in pools:continue
            extended=[]
            for selected,uses,landscape,rank in states:
                for item in pools[scene.id]:
                    candidate,asset,degree,current,fit,quality=item
                    intentional=intentional_reuse(scene,asset)
                    # 普通画面优先在已核验 Clip 内寻找未使用的区间。
                    if not intentional:
                        movable=candidate.model_copy(update={"playback_start_ms":None,"playback_end_ms":None})
                        try:
                            available=usage.allocate_candidate(movable,scene.scene_id,scene.duration_target_ms,
                                uses,prefer_unused=True)
                            if available is not None:
                                candidate=available
                                if candidate_rejections(self.db,scene,candidate,fps=project.fps,evidence=negative_by_scene[scene.id]):continue
                                current=usage.candidate_uses(candidate,scene.scene_id,scene.duration_target_ms)
                                usage.require_available(project.id,current,work_id=work_id)
                                if any(conflicts(reserved,use) for use in current for reserved in reserved_source_uses):continue
                        except SourceUsageConflict:
                            continue
                        item=(candidate,asset,degree,current,fit,quality)
                    # 各候选已查历史；组合展开只比较已解析来源，避免反复扫描整个数据库。
                    if any(conflicts(use,other) for use in current for other in uses):continue
                    repeated=0 if intentional else source_overlap_ms(current,uses)
                    wide=dict(landscape)
                    if project.format is ProjectFormat.VERTICAL and asset.width>asset.height:
                        for use in current:
                            wide[use["source_hash"]]=wide.get(use["source_hash"],0)+use["end_ms"]-use["start_ms"]
                    excess=sum(max(0,duration-total/2) for duration in wide.values())
                    creator=material_origin(asset)=="creator_provided"
                    duration=scene.duration_target_ms
                    previous_scene = next((value for value in scenes if value.order == scene.order - 1), None)
                    previous_choice = selected.get(previous_scene.id) if previous_scene is not None else None
                    stale_change = bool(previous_choice and same_source_information_change(scene,current,previous_scene,
                        previous_choice[3],intentional=intentional,
                        previous_intentional=intentional_reuse(previous_scene,previous_choice[1])))
                    next_rank=(rank[0]+repeated,excess,rank[2]+(duration if stale_change else 0),rank[3]+degree*duration,
                        rank[4]+(0 if creator else duration),rank[5]+(1-(quality if quality is not None else .5))*duration,
                        rank[6]+(1-candidate.match_score)*duration)
                    extended.append(({**selected,scene.id:item},[*uses,*current],wide,next_rank))
            if not extended:raise MaterialResolutionError(f"material_combination_unresolved:{scene.scene_id}",candidate_rejections=rejected)
            states=sorted(extended,key=lambda value:(value[3],tuple(str(item[0].clip_id) for item in value[0].values())))[:64]
        selected=states[0][0]
        revised=[]
        with self.db.transaction():
            usage.require_available(project.id,states[0][1],work_id=work_id)
            for scene in scenes:
                if scene.id not in selected:
                    revised.append(scene);continue
                candidate,asset,_,_,_,_=selected[scene.id]
                reason="按表达作用筛选合格画面，再联合比较整片重复、横屏占比、信息意图变化时连续同源、历史占用与本人素材价值；未设来源配额。"
                current=self.assets.get(asset.id)
                metadata=dict(current.metadata);facts=dict(metadata["visual_analysis"])
                observed=dict(facts[str(candidate.clip_id)])
                choices=dict(observed.get("editorial_selections",{}))
                match=observed.get("scene_matches",{}).get(scene_visual_key(scene),{})
                choice={"scene_id":scene.scene_id,"scene_plan_id":str(scene.id),"viewer_takeaway":scene.viewer_takeaway,
                    "source_kind":asset.source_kind.value,"reason":reason,
                    "playback_start_ms":candidate.playback_start_ms if candidate.playback_start_ms is not None else self.clips.get(candidate.clip_id).start_ms,
                    "playback_end_ms":candidate.playback_end_ms if candidate.playback_end_ms is not None else self.clips.get(candidate.clip_id).end_ms,
                    "origin":material_origin(asset),
                    "match_reason":match.get("reason"),"fit":match.get("fit","unknown"),
                    "quality_adequate":match.get("quality_adequate"),
                    "whole_video_unintentional_repeat_ms":states[0][3][0],
                    "whole_video_landscape_excess_ms":states[0][3][1],
                    "whole_video_same_source_on_information_change_ms":states[0][3][2]}
                choices[f"{scene_visual_key(scene)}:{scene.id}"]=choice
                choices[scene_visual_key(scene)]=choice
                observed["editorial_selections"]=choices;facts[str(candidate.clip_id)]=observed;metadata["visual_analysis"]=facts
                self.assets.update(current.model_copy(update={"metadata":metadata}))
                fallback=list(scene.fallback_sources)
                if asset.source_kind not in (*scene.preferred_sources,*fallback):fallback.append(asset.source_kind)
                revised.append(scene.model_copy(update={"preferred_clip_id":candidate.clip_id,"fallback_sources":fallback,"material_request":None}))
        return tuple(revised)
