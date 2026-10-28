"""普通素材用途登记到连续动作池的产品链路；替身事实不等于真实视觉通过。"""
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.db import (AssetRepository, ClipRepository, Database, TalkingProfileRepository,
    ProviderMachineCapabilityProfileRepository, ProviderCallRepository, BudgetPolicyRepository)
from app.domain.models import TalkingProfile, ProviderMachineCapabilityProfile, BudgetPolicy, Clip
from app.main import create_app
from app.providers.vision import TalkingMotionInterval, VideoIntervalBatch, VideoIntervalAnalysis
from app.talking.pool import build_talking_pool
from app.talking.source_suitability import register_motion_source
from test_material_resolution import material_library
from test_planning_feasibility import _consent


def motion_row(start=0, end=24000, **updates):
    return TalkingMotionInterval(start_offset_ms=start, end_offset_ms=end,
        single_person_continuous=True, motion_continuous=True, head_clearance=True,
        face_visibility=.9, mouth_visibility=.9, quality_score=.9, burned_in_subtitles=False,
        start_gaze="camera", end_gaze="camera", expression_labels=[], reason="整段观察替身",
        **updates)


def seed_motion(tmp_path):
    db, project, asset, coarse, _, provider, resolver = material_library(tmp_path)
    profile = TalkingProfileRepository(db).create(TalkingProfile(ip_profile_id=project.ip_profile_id,
        name="创作者动作", provider="local", reference_clip_ids=[], consent=_consent(),
        created_at=datetime.now(timezone.utc)))
    asset = register_motion_source(db, asset, identity="creator", ip_profile_id=project.ip_profile_id,
        talking_profile_id=profile.id, motion_reference=True)
    ProviderMachineCapabilityProfileRepository(db).save(ProviderMachineCapabilityProfile(
        capability="talking", mode="local", provider="local", model="model", runtime="runtime",
        machine_id="machine", readiness="verified", quality_status="verified", evidence_reference="fixture",
        provenance_source="fixture", commercial_status="commercial_safe", license_evidence_reference="fixture",
        updated_at=datetime.now(timezone.utc)))
    return db, project, asset, coarse, profile, provider, resolver


def set_observations(provider, make_rows):
    def analyze(videos, scenes):
        provider.calls.append((videos, scenes))
        return VideoIntervalBatch(clips=[VideoIntervalAnalysis(clip_id=video["clip_id"],
            description="创作者连续人物画面", content_consistent=True, comparison_layout="none",
            subtitle_regions=[], protected_regions=[], matches=[], reason="仅离线替身",
            talking_intervals=make_rows(video)) for video in videos])
    provider.analyze_intervals = analyze


def test_long_silent_creator_source_enters_pool_from_exact_video_intervals_and_replays(tmp_path):
    db, project, asset, coarse, profile, provider, resolver = seed_motion(tmp_path)
    set_observations(provider, lambda video: [motion_row(end=video["end_ms"]-video["start_ms"])])
    try:
        assert build_talking_pool(db, project.id, occupied=[]).usable_total_ms == 0
        job = uuid4()
        result = resolver.prepare_talking_sources(project, job)
        pool = build_talking_pool(db, project.id, occupied=[], data_root=tmp_path)
        assert result["analysed_intervals"] == 2
        assert pool.usable_total_ms == 30000 and pool.longest_continuous_ms == 24000
        assert all(video["talking_motion_reference"] is True for call, _ in provider.calls for video in call)
        assert AssetRepository(db).get(asset.id).has_audio is False
        assert ClipRepository(db).get(coarse.id) == coarse
        refs = TalkingProfileRepository(db).get(profile.id).reference_clip_ids
        assert len(refs) == 2
        calls = len(provider.calls)
        assert resolver.prepare_talking_sources(project, job)["analysed_intervals"] == 0
        assert resolver.prepare_talking_sources(project, uuid4())["analysed_intervals"] == 0
        assert len(provider.calls) == calls
        assert len(TalkingProfileRepository(db).get(profile.id).reference_clip_ids) == 2
        assert ProviderCallRepository(db).list() == []  # 替身调用不写成真实计费。
    finally:
        db.close()


@pytest.mark.parametrize("observations", [None, [], [motion_row(end=31000)],
    [motion_row(end=24000).model_copy(update={"motion_continuous": False})],
    [motion_row(end=24000).model_copy(update={"burned_in_subtitles": None})]])
def test_unknown_out_of_scope_or_noncontinuous_observations_never_admit_source(tmp_path, observations):
    db, project, _, _, profile, provider, resolver = seed_motion(tmp_path)
    set_observations(provider, lambda _: observations)
    try:
        job = uuid4()
        resolver.prepare_talking_sources(project, job)
        assert build_talking_pool(db, project.id, occupied=[]).usable_total_ms == 0
        assert TalkingProfileRepository(db).get(profile.id).reference_clip_ids == []
        calls = len(provider.calls)
        resolver.prepare_talking_sources(project, job)
        assert len(provider.calls) == calls
    finally:
        db.close()


def test_gaps_stay_gaps_and_withdrawing_motion_use_removes_only_machine_references(tmp_path):
    db, project, asset, _, profile, provider, resolver = seed_motion(tmp_path)
    set_observations(provider, lambda video: [motion_row(0, min(4000,video["end_ms"]-video["start_ms"]))]
        + ([motion_row(12000,24000)] if video["end_ms"]-video["start_ms"] >= 24000 else []))
    try:
        resolver.prepare_talking_sources(project, uuid4())
        pool = build_talking_pool(db, project.id, occupied=[])
        assert pool.usable_total_ms == 20000 and pool.longest_continuous_ms == 12000
        current = AssetRepository(db).get(asset.id)
        history = dict(current.metadata["talking_motion_analysis"])
        register_motion_source(db, current, identity="creator", motion_reference=False)
        assert build_talking_pool(db, project.id, occupied=[]).usable_total_ms == 0
        assert TalkingProfileRepository(db).get(profile.id).reference_clip_ids == []
        assert AssetRepository(db).get(asset.id).metadata["talking_motion_analysis"] == history
        register_motion_source(db, AssetRepository(db).get(asset.id), identity="creator",
            ip_profile_id=project.ip_profile_id, talking_profile_id=profile.id, motion_reference=True)
        calls = len(provider.calls)
        resolver.prepare_talking_sources(project, uuid4())
        assert len(provider.calls) == calls
        assert build_talking_pool(db, project.id, occupied=[]).usable_total_ms == 20000
    finally:
        db.close()


def test_ordinary_api_registers_creator_motion_without_duplicate_rights_or_preexisting_clip(tmp_path):
    db, project, asset, _, _, _, _ = material_library(tmp_path)
    path = db.path
    db.close()
    with TestClient(create_app(path)) as client:
        payload = dict(name="动作", provider="local", consent=_consent().model_dump(mode="json"),
            created_at=datetime.now(timezone.utc).isoformat(), ip_profile_id=str(project.ip_profile_id))
        response = client.post("/talking-profiles", json=payload)
        assert response.status_code == 201, response.text
        profile = response.json()
        assert profile["reference_clip_ids"] == []
        empty = client.post(f'/talking-profiles/{profile["id"]}/reference-selection',json={"brief":{}})
        assert empty.status_code == 422 and empty.json()["detail"] == "talking_profile_motion_reference_unavailable"
        value = client.patch(f"/assets/{asset.id}/identity", json={"identity":"creator",
            "ip_profile_id":str(project.ip_profile_id), "talking_profile_id":profile["id"],
            "motion_reference":True})
        assert value.status_code == 200, value.text
        assert value.json()["authorization_reference"] == asset.authorization_reference
        assert value.json()["metadata"]["talking_motion_source"]["purpose"] == "lip_sync_motion"
        wrong = client.patch(f"/assets/{asset.id}/identity", json={"identity":"other",
            "ip_profile_id":str(project.ip_profile_id), "talking_profile_id":profile["id"],
            "motion_reference":True})
        assert wrong.status_code == 422
        assert client.get(f"/assets/{asset.id}").json()["metadata"]["r1_identity"] == "creator"


def test_landscape_source_is_not_automatically_cropped_or_admitted(tmp_path):
    db, project, asset, _, profile, provider, resolver = seed_motion(tmp_path)
    AssetRepository(db).update(asset.model_copy(update={"width":1920,"height":1080}))
    try:
        resolver.prepare_talking_sources(project, uuid4())
        assert provider.calls == []
        assert TalkingProfileRepository(db).get(profile.id).reference_clip_ids == []
    finally:
        db.close()


def test_same_job_cannot_refresh_motion_analysis_batch_limit_on_advance(tmp_path):
    db, project, asset, _, _, provider, resolver = seed_motion(tmp_path)
    try:
        for clip in ClipRepository(db).list_by_asset(asset.id):
            ClipRepository(db).delete(clip.id)
        AssetRepository(db).update(asset.model_copy(update={"duration_ms":120000}))
        ClipRepository(db).create(Clip(asset_id=asset.id,start_ms=0,end_ms=120000,asset_duration_ms=120000))
        set_observations(provider,lambda video:[motion_row(end=video["end_ms"]-video["start_ms"])])
        job = uuid4()
        result = resolver.prepare_talking_sources(project,job)
        assert result["analysed_intervals"] == 3 and result["bounded_pending_intervals"] == 2
        assert len(provider.calls) == 3
        pool = build_talking_pool(db,project.id,occupied=[])
        assert pool.usable_total_ms == 72000 and pool.longest_continuous_ms == 24000
        assert resolver.prepare_talking_sources(project,job)["analysed_intervals"] == 0
        assert len(provider.calls) == 3
    finally:
        db.close()


def test_metered_motion_analysis_uses_budget_ledger_and_caller_guard_without_recharging_replay(tmp_path):
    db, project, _, _, _, provider, resolver = seed_motion(tmp_path)
    provider.provider_name,provider.model = "contract-fixture","fixture-video"
    BudgetPolicyRepository(db).save(BudgetPolicy(project_id=project.id,max_calls=12,
        allow_unknown_cost=True,updated_at=datetime.now(timezone.utc)))
    set_observations(provider,lambda video:[motion_row(end=video["end_ms"]-video["start_ms"])])
    guards = []
    try:
        job = uuid4()
        resolver.prepare_talking_sources(project,job,reservation_guard=lambda:guards.append("before_call"))
        calls = ProviderCallRepository(db).list_for_project(project.id)
        assert len(calls) == len(provider.calls) == len(guards) == 2
        assert all(call.operation == "vision" and call.status == "completed" for call in calls)
        resolver.prepare_talking_sources(project,job,reservation_guard=lambda:guards.append("before_call"))
        assert len(ProviderCallRepository(db).list_for_project(project.id)) == len(guards) == 2
    finally:
        db.close()


def test_motion_analysis_reserves_production_tail_before_any_provider_work(tmp_path):
    from app.budget import BudgetLimitError
    db, project, _, _, profile, provider, resolver = seed_motion(tmp_path)
    provider.provider_name,provider.model = "contract-fixture","fixture-video"
    BudgetPolicyRepository(db).save(BudgetPolicy(project_id=project.id,max_calls=5,
        allow_unknown_cost=True,updated_at=datetime.now(timezone.utc)))
    try:
        with pytest.raises(BudgetLimitError):
            resolver.prepare_talking_sources(project,uuid4())
        assert provider.calls == []
        assert TalkingProfileRepository(db).get(profile.id).reference_clip_ids == []
    finally:
        db.close()


def test_provider_failure_is_recorded_and_never_automatically_retried(tmp_path):
    from app.providers.vision import VisionTimeout
    from app.provider_execution import ProviderExecutionRecordedFailure
    db, project, _, _, _, provider, resolver = seed_motion(tmp_path)
    provider.provider_name,provider.model = "contract-fixture","fixture-video"
    BudgetPolicyRepository(db).save(BudgetPolicy(project_id=project.id,max_calls=12,
        allow_unknown_cost=True,updated_at=datetime.now(timezone.utc)))
    def timeout(videos, scenes):
        provider.calls.append((videos,scenes))
        raise VisionTimeout("fixture timeout")
    provider.analyze_intervals = timeout
    try:
        job = uuid4()
        with pytest.raises(VisionTimeout):resolver.prepare_talking_sources(project,job)
        with pytest.raises(ProviderExecutionRecordedFailure):resolver.prepare_talking_sources(project,job)
        assert len(provider.calls) == 1
        assert ProviderCallRepository(db).list_for_project(project.id)[0].status == "failed"
    finally:
        db.close()


def test_motion_call_estimate_is_read_only_and_uses_the_same_pending_candidates(tmp_path):
    from app.media.planning import estimated_talking_calls
    db, project, asset, _, _, provider, resolver = seed_motion(tmp_path)
    set_observations(provider,lambda video:[motion_row(end=video["end_ms"]-video["start_ms"])])
    try:
        changes = db.connection.total_changes
        assert estimated_talking_calls(db,project) == 2
        assert db.connection.total_changes == changes and provider.calls == []
        resolver.prepare_talking_sources(project,uuid4())
        changes = db.connection.total_changes
        assert estimated_talking_calls(db,project) == 0
        assert db.connection.total_changes == changes
        register_motion_source(db,AssetRepository(db).get(asset.id),identity="creator",motion_reference=False)
        assert estimated_talking_calls(db,project) == 0
    finally:
        db.close()


def test_changed_source_is_reported_without_being_admitted_or_charged(tmp_path):
    from pathlib import Path
    db, project, asset, _, _, provider, resolver = seed_motion(tmp_path)
    try:
        Path(asset.source_file).write_bytes(b'changed source bytes')
        result = resolver.prepare_talking_sources(project,uuid4())
        assert result["unavailable_sources"] == {str(asset.id):"source_content_hash_mismatch"}
        assert result["analysed_intervals"] == 0 and provider.calls == []
        assert build_talking_pool(db,project.id,data_root=tmp_path,occupied=[]).usable_total_ms == 0
    finally:
        db.close()
