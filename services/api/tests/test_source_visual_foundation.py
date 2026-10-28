"""选段与画面证据的契约回归：不调用模型或渲染正片。"""
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.assembly.edit_plan import _default_edit_plan, presentation_for_style, full_interval_geometry
from app.db import AssetRepository, ClipRepository, Database, IPProfileRepository, ProjectRepository
from app.domain.models import (Asset, CandidateAsset, Clip, CostCategory, IPProfile, Project, RationalFps,
    ScenePlan, SourceKind, UsageCost, VisualIntent, VisualStyleTokens)
from app.production_preflight import _subtitle_state, build_production_preflight


def _project():
    return Project(title='码率', topic='码率', ip_profile_id=uuid4(), created_at=datetime.now(timezone.utc),
        fps=RationalFps(numerator=30, denominator=1))


def _scene(project):
    return ScenePlan(project_id=project.id, scene_id='evidence', order=0, purpose='解释码率',
        voice_text='码率影响画质。', duration_target_ms=3000, visual_intent=VisualIntent(subject='码率画质', action='展示'),
        preferred_sources=[SourceKind.USER_ASSET], fallback_sources=[SourceKind.CAPTURE])


def _facts():
    return dict(source_content_hash='a'*64, clip_interval=dict(start_ms=1000, end_ms=4000),
        coverage='full_interval_continuous', covered_start_ms=1000, covered_end_ms=4000,
        visual_description='实际选段的码率画质画面', comparison_layout='none', subtitle_regions=[[.2,.88,.6,.08]],
        source_subtitle_clearance_verified=True, evidence_reference='fixture:continuous-source-analysis')


@pytest.mark.parametrize('portrait', [True, False])
def test_source_subtitles_are_removed_before_either_output_layout(portrait):
    project = _project(); scene = _scene(project)
    clip = SimpleNamespace(id=uuid4(), start_ms=0, end_ms=10000)
    facts = _facts()
    asset = SimpleNamespace(id=uuid4(), width=1280, height=720, content_hash='a'*64,
        metadata={'visual_analysis':{str(clip.id):facts}})
    candidate = CandidateAsset(scene_plan_id=scene.id, source_kind=SourceKind.USER_ASSET, asset_id=asset.id,
        clip_id=clip.id, match_score=1, why=['fixture'], estimated_cost=UsageCost(category=CostCategory.USER_ASSET, amount=None))
    entry = _default_edit_plan(project, [scene], {scene.id:candidate}).scenes[0]
    resolved = presentation_for_style(entry, VisualStyleTokens(), asset, scene_plan=scene, clip=clip,
        playback_interval_ms=(1000,4000), portrait_output=portrait)
    assert .1 < resolved.source_bottom_crop_ratio < .2
    assert resolved.subtitle_treatment.value == 'timed_captions'
    assert resolved.source_subtitle_evidence_reference == facts['evidence_reference']
    # Same hash/Clip cannot widen this clearance to the whole original Clip.
    assert full_interval_geometry(asset, clip) == {}
    assert _subtitle_state(asset, clip, (0,3000))[0] == 'unknown'
    assert _subtitle_state(asset, clip, (1000,4000))[0] == 'present'
    facts['source_subtitle_clearance_verified'] = False
    with pytest.raises(ValueError, match='source_subtitle_clearance_required'):
        presentation_for_style(entry, VisualStyleTokens(), asset, clip=clip,
            playback_interval_ms=(1000,4000), portrait_output=portrait)


@pytest.mark.parametrize('mutation', ['sampled', 'stale_hash', 'outside_interval', 'malformed'])
def test_sampled_or_stale_geometry_never_becomes_full_interval_clearance(mutation):
    clip = SimpleNamespace(id=uuid4(), start_ms=0, end_ms=10000)
    facts = _facts()
    if mutation == 'sampled': facts['coverage'] = 'sampled_keyframe'
    if mutation == 'stale_hash': facts['source_content_hash'] = 'b'*64
    if mutation == 'outside_interval': facts['covered_end_ms'] = 3999
    if mutation == 'malformed': facts['clip_interval'] = []
    asset = SimpleNamespace(content_hash='a'*64, metadata={'visual_analysis':{str(clip.id):facts}})
    assert full_interval_geometry(asset, clip, (1000,4000)) == {}
    assert _subtitle_state(asset, clip, (1000,4000))[0] == 'unknown'


def test_machine_comparison_layout_cannot_bypass_missing_subtitle_clearance():
    project=_project();scene=_scene(project)
    clip=SimpleNamespace(id=uuid4(),start_ms=0,end_ms=10000)
    facts={**_facts(),'coverage':'video_interval_analysis','method':'provider_video_interval_analysis',
        'content_consistent':True,'analysis_video_sha256':'b'*64,'analysis_duration_ms':3000,
        'source_subtitle_clearance_verified':False,'comparison_layout':'side_by_side_equal_halves'}
    asset=SimpleNamespace(id=uuid4(),content_hash='a'*64,width=1280,height=720,metadata={'visual_analysis':{str(clip.id):facts}})
    candidate=CandidateAsset(scene_plan_id=scene.id,source_kind=SourceKind.USER_ASSET,asset_id=asset.id,
        clip_id=clip.id,match_score=1,why=['fixture'],estimated_cost=UsageCost(category=CostCategory.USER_ASSET,amount=None))
    entry=_default_edit_plan(project,[scene],{scene.id:candidate}).scenes[0]
    with pytest.raises(ValueError,match='source_subtitle_clearance_required'):
        presentation_for_style(entry,VisualStyleTokens(),asset,clip=clip,playback_interval_ms=(1000,4000))


def test_preflight_uses_actual_playback_scope_and_invalidates_changed_visual_facts(tmp_path):
    import hashlib
    from app.project_drafts import save_project_draft
    with Database(tmp_path/'exact-source.sqlite3') as db:
        db.data_root = tmp_path
        source = tmp_path/'source.mp4'; source.write_bytes(b'fixture video bytes')
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        profile = IPProfileRepository(db).create(IPProfile(creator_name='创作者'))
        project = ProjectRepository(db).create(_project().model_copy(update={'ip_profile_id':profile.id}))
        scene = _scene(project)
        asset = AssetRepository(db).create(Asset(source_file=str(source), content_hash=source_hash,
            width=1080, height=1920, duration_ms=10000, fps=project.fps, authorization_reference='已入库', imported_at=project.created_at))
        clip = ClipRepository(db).create(Clip(asset_id=asset.id, start_ms=0, end_ms=10000,
            asset_duration_ms=10000, visual_description='码率画质展示', quality_score=1))
        facts = {**_facts(), 'source_content_hash':source_hash, 'subtitle_regions':[]}
        AssetRepository(db).update(asset.model_copy(update={'metadata':{'visual_analysis':{str(clip.id):facts}}}))
        scene = scene.model_copy(update={'preferred_clip_id':clip.id})
        draft = save_project_draft(db, project, topic=project.topic, script=scene.voice_text,
            scenes=[scene], routes=[], confirmed=[], video_spec=None)
        preflight = build_production_preflight(db, project, draft, style_tokens=VisualStyleTokens())
        assert preflight.scenes[0].suitability == 'reviewed_source_interval'
        selected = preflight.scenes[0].selected_candidate
        assert (selected.playback_start_ms, selected.playback_end_ms) == (1000,4000)
        assert not any('source_suitability_unknown' in reason or 'source_subtitle_handling_unresolved' in reason for reason in preflight.stop_reasons)
        assert preflight.scenes[0].burned_in_subtitles == 'absent'
        assert preflight.preliminary_edit_plan.scenes[0].subtitle_treatment.value == 'timed_captions'
        first_identity = preflight.scenes[0].visual_dependency_fingerprint
        facts['coverage'] = 'sampled_keyframe'
        AssetRepository(db).update(asset.model_copy(update={'metadata':{'visual_analysis':{str(clip.id):facts}}}))
        changed = build_production_preflight(db, project, draft, style_tokens=VisualStyleTokens())
        assert f'source_suitability_unknown:{scene.scene_id}' in changed.stop_reasons
        assert f'source_subtitle_handling_unresolved:{scene.scene_id}' in changed.stop_reasons
        assert changed.scenes[0].visual_dependency_fingerprint != first_identity
        facts['coverage'] = 'full_interval_continuous'
        AssetRepository(db).update(asset.model_copy(update={'metadata':{'visual_analysis':{str(clip.id):facts}}}))
        source.write_bytes(b'replaced file')
        replaced = build_production_preflight(db, project, draft, style_tokens=VisualStyleTokens())
        assert f'source_content_hash_mismatch:{scene.scene_id}' in replaced.stop_reasons


def test_legacy_bundle_exposes_real_frame_time_without_backfilling_source_approval(tmp_path):
    from app.db import AnalysisResultRepository
    from app.domain.models import AnalysisResultBundle, AnalysisClipResult, AnalysisKeyframe
    from app.media.vision import planning_visual_evidence
    with Database(tmp_path/'legacy.sqlite3') as db:
        project = _project()
        asset = AssetRepository(db).create(Asset(source_file='source.mp4', content_hash='a'*64,
            width=1280, height=720, duration_ms=100000, fps=project.fps, authorization_reference='已入库', imported_at=project.created_at))
        clip = ClipRepository(db).create(Clip(asset_id=asset.id,start_ms=0,end_ms=100000,asset_duration_ms=100000,
            visual_description='飞盘画面'))
        ClipRepository(db).create(Clip(asset_id=asset.id,start_ms=81000,end_ms=84000,asset_duration_ms=100000))
        result = AnalysisClipResult(asset_id=asset.id,clip_id=clip.id,start_ms=0,end_ms=100000,
            visual_description='飞盘画面',keyframes=[AnalysisKeyframe(timestamp_ms=83000,reference='frame.jpg',description='狗接飞盘')])
        bundle = AnalysisResultRepository(db).create(AnalysisResultBundle(input_hash='b'*64,mode='assisted_test',
            source='fixture',model='fixture',tool='fixture',analyzed_at=project.created_at,results=[result]))
        asset = AssetRepository(db).update(asset.model_copy(update={'metadata':{'r1_analysis':{'bundle_id':str(bundle.id)}}}))
        evidence = planning_visual_evidence(db, asset, clip)
        assert evidence['observations'] == [{'timestamp_ms':83000,'description':'狗接飞盘'}]
        assert evidence['coverage'] == 'sampled_keyframes'
        assert evidence['source_hash_bound'] is False
        assert evidence['local_interval_hints'] == [dict(timestamp_ms=83000,start_ms=81000,end_ms=84000,duration_ms=3000)]
        assert 'visual_analysis' not in AssetRepository(db).get(asset.id).metadata
