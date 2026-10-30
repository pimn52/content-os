"""Read-only, already-qualified footage alternatives for one rendered scene."""
from __future__ import annotations

import hashlib
from pathlib import Path

from app.assembly.edit_plan import presentation_for_style, source_frame_interval_ms
from app.assembly.video_spec import source_interval_to_frames, _talking_visual_treatment
from app.db import AssetRepository, ClipRepository, ProjectRepository
from app.domain.models import (CandidateAsset, CostCategory, EditPlanScene, EditVisualRole,
    ProjectFormat, SourceKind, UsageCost, VideoScene, VideoVisual)
from app.media.local_paths import existing_local_media_file
from app.media.planning import _allowed
from app.media.visual_facts import full_interval_geometry, matches_scene, material_origin, scene_visual_key
from app.source_usage import SourceUsageService, overlap
from app.media.semantic_rejections import negative_source_evidence,rejected_window,source_view_uses


def replacement_options(db, root, spec, scene_plan, *, style, run_id, candidate_rejections=None):
    """No provider, proxy, draft or catalogue writes; admission remains in repair.plan."""
    target = next((scene for scene in spec.scenes if scene.scene_id == scene_plan.scene_id), None)
    entry = next((entry for entry in spec.edit_plan.scenes if entry.scene_plan_id == scene_plan.id), None) if spec.edit_plan else None
    footage = {SourceKind.USER_ASSET, SourceKind.HISTORICAL_ASSET, SourceKind.STOCK}
    if (target is None or entry is None or target.visual_role is not EditVisualRole.FOOTAGE
            or target.visual.source_kind not in footage or scene_plan.visual_requirement == 'creator_speaking'):
        return []
    assets, usage = AssetRepository(db), SourceUsageService(db)
    project = ProjectRepository(db).get(spec.project_id)
    if project is None:
        return []
    first,last = source_frame_interval_ms(target.visual.clip_start_ms,target.duration_frames,spec.fps,target.visual.clip_end_ms)
    rejected = source_view_uses(db,target.visual.asset_id,first,last)
    negative = negative_source_evidence(db,scene_visual_key(scene_plan))
    occupied = usage.occupied(project.id, run_id=run_id)
    options = []
    source_bytes_valid = {}
    rejected_candidates = candidate_rejections if candidate_rejections is not None else {}
    for clip in ClipRepository(db).list():
        asset = assets.get(clip.asset_id)
        if (not _allowed(asset, project) or asset.source_kind not in footage
                or clip.reference_source_timebase is not None or clip.asset_duration_ms != asset.duration_ms
                or any(asset.metadata.get(key) for key in ('talking_generation','talking_run','talking_run_preview'))):
            continue
        if source_interval_to_frames(clip.start_ms, clip.end_ms, spec.fps) < target.duration_frames:
            rejected_candidates[str(clip.id)] = ['duration_insufficient']
            continue
        interval = source_frame_interval_ms(clip.start_ms, target.duration_frames, spec.fps, clip.end_ms)
        facts = full_interval_geometry(asset, clip, interval)
        reasons = []
        if facts.get('coverage') != 'video_interval_analysis':reasons.append('interval_analysis_unavailable')
        elif not matches_scene(facts, scene_plan):reasons.append('visual_purpose_mismatch')
        if facts.get('source_subtitle_clearance_verified') is not True:reasons.append('subtitle_clearance_unverified')
        if reasons:
            rejected_candidates[str(clip.id)] = reasons
            continue
        match = facts['scene_matches'][scene_visual_key(scene_plan)]
        if match.get('fit') not in {'direct','illustrative','context'}:
            rejected_candidates[str(clip.id)] = ['visual_purpose_unknown']
            continue
        try:
            uses = source_view_uses(db,asset.id,interval[0],interval[1])
            if any(overlap(use, bad) for use in uses for bad in rejected):
                rejected_candidates[str(clip.id)] = ['rejected_source_interval']
                continue
            if rejected_window(db,scene_visual_key(scene_plan),asset.id,*interval,evidence=negative):
                rejected_candidates[str(clip.id)] = ['source_semantic_use_rejected']
                continue
            if asset.id not in source_bytes_valid:
                source = existing_local_media_file(asset.source_file, Path(root))
                with source.open('rb') as stream:
                    source_bytes_valid[asset.id] = hashlib.file_digest(stream, 'sha256').hexdigest() == asset.content_hash
            if not source_bytes_valid[asset.id]:
                rejected_candidates[str(clip.id)] = ['source_content_hash_mismatch']
                continue
            raw = entry.model_dump(mode='json')
            raw.update(selected_source_kind=asset.source_kind.value, selected_asset_id=str(asset.id),
                selected_clip_id=str(clip.id), framing_policy='face_safe_contain',
                portrait_presentation='full_canvas', crop_safety_evidence_reference=None,
                crop_safety_start_ms=None, crop_safety_end_ms=None, burned_in_subtitles='unknown',
                source_bottom_crop_ratio=0, source_subtitle_evidence_reference=None)
            treated = presentation_for_style(EditPlanScene.model_validate(raw), style, asset,
                scene_plan=scene_plan, clip=clip, playback_interval_ms=interval,
                portrait_output=project.format is ProjectFormat.VERTICAL)
            reframe, evidence, crop = _talking_visual_treatment(asset, treated)
            visual = VideoVisual(source_kind=asset.source_kind, asset_id=asset.id, clip_id=clip.id,
                authorization_reference=asset.authorization_reference, source_duration_ms=asset.duration_ms,
                clip_start_ms=clip.start_ms, clip_end_ms=clip.end_ms, vertical_reframe_mode=reframe,
                vertical_reframe_evidence_reference=evidence, source_bottom_crop_ratio=crop)
            # Only the visual dependency and its presentation change. Captions/audio/timing remain exact.
            revised = target.model_dump(mode='json')
            revised['visual'] = visual.model_dump(mode='json')
            for field in ('visual_role','framing_policy','portrait_presentation','subtitle_treatment','graphic_treatment','graphic_text'):
                revised[field] = treated.model_dump(mode='json')[field]
            candidate = CandidateAsset(scene_plan_id=scene_plan.id, source_kind=asset.source_kind,
                asset_id=asset.id, clip_id=clip.id, playback_start_ms=clip.start_ms, playback_end_ms=clip.end_ms,
                match_score=match['score'], why=['复用本场景已有的完整区间用途核验'], estimated_cost=UsageCost(category=CostCategory.RENDER))
            degree = usage.clip_occupancy_degree(clip.model_copy(update={'start_ms':interval[0],'end_ms':interval[1]}), occupied)
            options.append(dict(scene=VideoScene.model_validate(revised), entry=treated, candidate=candidate,
                evidence={'asset':asset.model_dump(mode='json'),'clip':clip.model_dump(mode='json'),
                    'used_interval_ms':list(interval),'purpose_evidence_reference':facts['evidence_reference'],
                    'scene_key':scene_visual_key(scene_plan)},
                fit_rank={'direct':3,'illustrative':2,'unknown':1,'context':0}.get(match.get('fit'),1),
                match_score=match['score'], quality_score=clip.quality_score if clip.quality_score is not None else .5,
                occupancy_degree=degree, creator_provided=material_origin(asset)=='creator_provided'))
        except ValueError:
            rejected_candidates[str(clip.id)] = ['presentation_or_lineage_incompatible']
            continue
        except OSError:
            rejected_candidates[str(clip.id)] = ['source_file_unavailable']
            continue
    if not options:
        return []
    best_fit = max(option['fit_rank'] for option in options)
    best_score = max(option['match_score'] for option in options if option['fit_rank'] == best_fit)
    options = [option for option in options if option['fit_rank'] == best_fit and option['match_score'] >= best_score-.08]
    return sorted(options, key=lambda option:(option['occupancy_degree'], not option['creator_provided'],
        -option['quality_score'], -option['match_score'], str(option['candidate'].clip_id)))[:16]
