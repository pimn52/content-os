"""Historical repeated motion shape; dispatch context and admission gates are fixture stubs."""
import json
import sqlite3
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.db import AssetRepository, AudioAssetRepository, ClipRepository, Database, JobRepository, ProviderCallRepository
from app.budget import ProviderCallLedger
from app.db import TalkingProfileRepository
from app.domain.models import JobStatus, JobType
from app.jobs import JobRunner, JobStore
from app.jobs.handlers import TalkingGenerationJobHandler
from app.main import create_app
from app.production_runs import ProductionRunNotReady, ProductionRunService, TalkingPlanDependency
from fastapi.testclient import TestClient
from test_talking_batch_dispatch import batch  # Reuses the isolated in-memory fixture.
from test_talking_delivery_intervals import _delivery_fixture


def _six_scenes(batch):
    # The historical durations are representative; no real media or model is used.
    durations = [2760, 2680, 1560, 2240, 1700, 2460]
    for index in range(2, 6):
        batch.dependencies.append(TalkingPlanDependency(scene_plan_id=uuid4(), scene_id=f"speech-{index}",
            visual_dependency_fingerprint=str(index + 1) * 64))
    cursor = 0
    for item, duration in zip(batch.dependencies, durations):
        batch.times[item.scene_plan_id] = (cursor, cursor + duration)
        cursor += duration
    batch.db.connection.execute("UPDATE production_runs SET talking_dependencies=? WHERE id=?",
        (json.dumps([item.model_dump(mode="json") for item in batch.dependencies]), str(batch.run_id)))


def test_six_scenes_cannot_reuse_one_short_reference_before_dispatch(batch):
    _six_scenes(batch)
    clips = ClipRepository(batch.db)
    clip = clips.get(batch.choice["reference_clip_id"])
    clips.update(clip.model_copy(update={"end_ms": 3200}))
    with pytest.raises(ProductionRunNotReady, match="talking_source_coverage_insufficient_replan"):
        batch.service.dispatch_all_talking(batch.project.id, batch.run_id, **batch.choice)
    assert JobRepository(batch.db).list() == []
    assert ProviderCallRepository(batch.db).list() == []
    assert batch.service.get(batch.project.id, batch.run_id).talking_source_bindings == ()


def test_worker_rejects_restarted_motion_before_call(tmp_path, monkeypatch):
    """Use real ordinary dispatch and admission; only media/provider are synthetic."""
    monkeypatch.setenv("CONTENT_OS_DATA_ROOT", str(tmp_path))
    path = tmp_path / "repeat-worker.sqlite"
    class Provider:
        provider_name = "fixture-talking"
        model = "fixture-model"
        is_local = True
        calls = 0
        def synthesize(self, *args):
            self.calls += 1
            raise AssertionError("repeated motion must not reach the provider")
    provider = Provider()
    with TestClient(create_app(path)) as client:
        project_id, run, scenes, master, profile, clip, capability = _delivery_fixture(client, path, 140)
        with Database(path) as db:
            service = ProductionRunService(db, tmp_path)
            queued = service.dispatch_all_talking(project_id, UUID(run["id"]),
                talking_profile_id=profile.id, reference_clip_id=clip.id, capability_profile_id=capability.id)
            jobs = JobRepository(db)
            second = jobs.get(queued.talking_source_bindings[1].talking_job_id)
            jobs.update(second.model_copy(update={"payload": second.payload.model_copy(update={
                "reference_window_start_ms": 0, "reference_window_end_ms": 1400})}))
            handler = TalkingGenerationJobHandler(TalkingProfileRepository(db), AudioAssetRepository(db),
                AssetRepository(db), SimpleNamespace(data_root=tmp_path), provider, tmp_path / "generated",
                ledger=ProviderCallLedger(db), execution_runtime="fixture", execution_machine_id="fixture-host")
            runner = JobRunner(JobStore(db), {JobType.GENERATE_TALKING: handler},
                worker_id="repeat-regression", lease_duration=timedelta(minutes=1), max_attempts=1)
            finished = runner.run_once(allowed_types={JobType.GENERATE_TALKING})
            assert finished is not None and finished.status is JobStatus.FAILED
            assert finished.error_code == "talking_motion_restart_requires_replan"
            assert provider.calls == 0
            assert ProviderCallRepository(db).list_for_project(project_id) == []


@pytest.mark.parametrize("alias_kind", ["same_hash", "derived"])
def test_six_old_restarts_are_rejected_even_through_asset_aliases(batch, alias_kind):
    _six_scenes(batch)
    clips, assets, jobs = ClipRepository(batch.db), AssetRepository(batch.db), JobRepository(batch.db)
    original_clip = clips.get(batch.choice["reference_clip_id"])
    original = assets.create(assets.get(original_clip.asset_id).model_copy(update={
        "id": uuid4(), "content_hash": "d" * 64, "duration_ms": 16000}))
    original_clip = clips.create(original_clip.model_copy(update={"id": uuid4(), "asset_id": original.id,
        "end_ms": 16000, "asset_duration_ms": 16000}))
    run = batch.service.dispatch_all_talking(batch.project.id, batch.run_id,
        **{**batch.choice, "reference_clip_id": original_clip.id})
    assert len(jobs.list()) == 6
    original = assets.get(original.id)
    if alias_kind == "same_hash":
        # The asset table prevents a second identity for identical content.
        with pytest.raises(sqlite3.IntegrityError):
            assets.create(original.model_copy(update={"id": uuid4(), "source_file": "alias.mp4"}))
        alias = original
    else:
        alias = assets.create(original.model_copy(update={"id": uuid4(), "source_file": "derived.mp4",
            "content_hash": "c" * 64, "metadata": {"source_derivation": {
                "source_asset_id": str(original.id), "source_content_hash": original.content_hash,
                "source_interval": {"start_ms": 0, "end_ms": 16000}}}}))
    alias_clip = clips.create(original_clip.model_copy(update={"id": uuid4(), "asset_id": alias.id,
        "end_ms": 16000, "asset_duration_ms": 16000}))
    bindings = list(run.talking_source_bindings)
    for index, binding in enumerate(bindings):
        job = jobs.get(binding.talking_job_id)
        jobs.update(job.model_copy(update={"payload": job.payload.model_copy(update={
            "reference_window_start_ms": 0, "reference_window_end_ms": 3200})}))
        if index:
            bindings[index] = binding.model_copy(update={"reference_clip_id": alias_clip.id,
                "reference_asset_id": alias.id, "reference_asset_hash": alias.content_hash})
    replay = run.model_copy(update={"talking_source_bindings": tuple(bindings)})
    master = AudioAssetRepository(batch.db).get(replay.talking_master_audio_id)
    with pytest.raises(ProductionRunNotReady, match="talking_motion_restart_requires_replan"):
        batch.service._talking_reference_windows(replay, master)
    with pytest.raises(ProductionRunNotReady, match="talking_motion_restart_requires_replan"):
        batch.service._require_talking_source_continuity(replay)
    assert ProviderCallRepository(batch.db).list() == []
