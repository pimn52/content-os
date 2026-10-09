"""Offline repair evidence and synthetic media; no provider/creator-quality claims."""
import hashlib
import json
import struct
import subprocess
import zlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.budget import ProviderCallLedger
from app.db import (
    AssetRepository, AudioAssetRepository, BudgetPolicyRepository, Database,
    JobRepository, ProviderCallRepository, TalkingSliceSeriesContinuityReviewRepository,
    TalkingSliceSeriesRepository, ProjectDraftRepository, TalkingProfileRepository,
    ProviderMachineCapabilityProfileRepository,
)
from app.domain.models import BudgetPolicy, CostCategory, JobStatus, RationalFps, TranscriptSegment
from app.jobs.handlers import _reserve_job_provider_call
from app.jobs.runner import JobExecutionError
from app.main import create_app
from app.media.ffprobe import FFProbeAdapter
from app.runtime import resolve_local_executable
from app.talking.planned_preview import TalkingRunPreviewJobHandler
from app.talking.repair import TalkingRepairRequest, TalkingRepairService
from app.production_runs import ProductionRunService
from app.talking.source_admission import TalkingSourceAdmissionRepository
from test_production_runs import _fixture_reviewable_planned_preview
from test_talking_review_policy import DIMENSIONS, answer, concern, judgment, root


def setup(client, path, monkeypatch, *, reject=True, dimension="artifacts"):
    project, run, binding, preview = _fixture_reviewable_planned_preview(client, path, monkeypatch, policy_version=2)
    if reject:
        response = client.post(f"{root(project, binding)}/continuity-review", json=judgment(
            preview, approved=False, dimensions={**DIMENSIONS, dimension: "fail"},
            scoped_findings=[{"dimension": dimension, "reason": "fixture: visible failure", "start_ms": 100, "end_ms": 500}],
        ))
        assert response.status_code == 201, response.text
    url = f"/projects/{project}/production-runs/{run['id']}"
    return project, run, binding, preview, url


def request(client, url, binding, key="repair-1"):
    response = client.get(f"{url}/talking-repair-plan", params={"scene_plan_id": binding["scene_plan_id"]})
    assert response.status_code == 200, response.text
    plan = response.json()
    return plan, {"scene_plan_id": binding["scene_plan_id"], "expected_fingerprint": plan["fingerprint"],
                  "idempotency_key": key, "confirmed_action": "regenerate_scene", "reason": "fixture: one bounded local replacement"}


def test_completed_rejected_repair_is_idempotent_preserves_evidence_and_restarts(tmp_path, monkeypatch):
    path = tmp_path / "repair.sqlite"
    with TestClient(create_app(path)) as client:
        project, run, binding, preview, url = setup(client, path, monkeypatch)
        with Database(path) as db:
            old_series = TalkingSliceSeriesRepository(db).get(UUID(binding["talking_series_id"]))
            old_review = TalkingSliceSeriesContinuityReviewRepository(db).get_by_series_id(old_series.id)
            old_master = AudioAssetRepository(db).get(UUID(binding["master_audio_id"]))
            jobs_before = len(JobRepository(db).list())
        plan, body = request(client, url, binding)
        assert plan["failure_kind"] == "quality" and plan["action"] == "regenerate_scene"
        assert plan["findings"][0]["start_ms"] == 100
        assert plan["max_provider_calls"] == 1 and plan["external_charge_ceiling"] == "0"
        assert plan["local_compute_cost"] is None
        repaired = client.post(f"{url}/talking-repair", json=body)
        assert repaired.status_code == 201, repaired.text
        record = repaired.json()
        assert record["predecessor_series_id"] == binding["talking_series_id"]
        assert record["reused_master_audio_id"] == binding["master_audio_id"]
        assert client.post(f"{url}/talking-repair", json=body).json() == record
        assert client.post(f"{url}/talking-repair", json={**body, "reason": "different"}).status_code == 409
        current = client.get(url).json()["talking_source_bindings"][0]
        assert current["talking_job_id"] == record["replacement_job_id"]
        assert current["talking_series_id"] is None and current["talking_qa_job_id"] is None
        assert client.post(f"{url}/talking-preview-prepare", json={"scene_plan_id": binding["scene_plan_id"]}).status_code == 409
        assert client.post(f"{root(project, binding)}/talking-run").status_code == 409
    with TestClient(create_app(path)) as client, Database(path) as db:
        assert client.get(f"{url}/talking-repairs").json()[0]["id"] == record["id"]
        assert TalkingSliceSeriesRepository(db).get(old_series.id) == old_series
        assert TalkingSliceSeriesContinuityReviewRepository(db).get_by_series_id(old_series.id) == old_review
        assert AudioAssetRepository(db).get(old_master.id) == old_master
        assert len(JobRepository(db).list()) == jobs_before + 1
        assert ProviderCallRepository(db).list_for_project(UUID(project)) == []


@pytest.mark.parametrize("dimension", ["identity", "source_performance", "continuity", "publishability"])
def test_unsupported_quality_stops_for_replan(tmp_path, monkeypatch, dimension):
    path = tmp_path / "unsupported.sqlite"
    with TestClient(create_app(path)) as client:
        _, _, binding, _, url = setup(client, path, monkeypatch, dimension=dimension)
        plan, body = request(client, url, binding)
        assert plan["failure_kind"] == "quality" and plan["action"] == "replan"
        assert client.post(f"{url}/talking-repair", json=body).status_code == 409
        assert client.get(f"{url}/talking-repairs").json() == []


@pytest.mark.parametrize("approved", [None, True])
def test_pending_or_positive_subject_is_not_quality_failure(tmp_path, monkeypatch, approved):
    path = tmp_path / "no-failure.sqlite"
    with TestClient(create_app(path)) as client:
        project, _, binding, preview, url = setup(client, path, monkeypatch, reject=False)
        if approved:
            assert client.post(f"{root(project, binding)}/continuity-review", json=judgment(preview)).status_code == 201
        plan, body = request(client, url, binding)
        assert plan["action"] == "stop" and plan["failure_kind"] == "unassessed"
        assert client.post(f"{url}/talking-repair", json=body).status_code == 409


def test_negative_local_answer_drives_repair_without_overwriting_full_review(tmp_path, monkeypatch):
    path = tmp_path / "concern.sqlite"
    with TestClient(create_app(path)) as client:
        project, _, binding, preview, url = setup(client, path, monkeypatch, reject=False)
        series_url = root(project, binding)
        assert client.post(f"{series_url}/continuity-review", json=judgment(preview)).status_code == 201
        item, _ = concern(client, series_url, preview)
        plan, _ = request(client, url, binding)
        assert plan["action"] == "stop"
        answered = client.post(f"{series_url}/review-concern-answers", json={
            "preview_asset_id": str(preview.id), "preview_sha256": preview.content_hash,
            "answers": [answer(item.json(), approved=False)],
        })
        assert answered.status_code == 200, answered.text
        plan, body = request(client, url, binding)
        assert plan["action"] == "regenerate_scene"
        assert client.post(f"{url}/talking-repair", json=body).status_code == 201
        assert client.get(f"{series_url}/continuity-review").json()["approved"] is True


@pytest.mark.parametrize("error,action,kind", [
    ("talking_temporarily_unavailable", "regenerate_scene", "technical"),
    ("talking_invalid_request", "stop", "invalid_request"),
    ("talking_provider_failed", "stop", "technical"),
    (None, "stop", "technical"),
])
def test_persisted_technical_error_classification(tmp_path, monkeypatch, error, action, kind):
    path = tmp_path / "technical.sqlite"
    with TestClient(create_app(path)) as client:
        _, _, binding, _, url = setup(client, path, monkeypatch, reject=False)
        with Database(path) as db:
            jobs = JobRepository(db)
            job = jobs.get(UUID(binding["talking_job_id"]))
            jobs.update(job.model_copy(update={"status": JobStatus.FAILED, "error_code": error}))
        plan, body = request(client, url, binding)
        assert plan["action"] == action and plan["failure_kind"] == kind
        assert client.post(f"{url}/talking-repair", json=body).status_code == (201 if action == "regenerate_scene" else 409)


@pytest.mark.parametrize("change", ["allowance", "budget", "bytes", "cancelled", "fingerprint"])
def test_stale_or_exhausted_repair_never_changes_binding(tmp_path, monkeypatch, change):
    path = tmp_path / "stale.sqlite"
    with TestClient(create_app(path)) as client:
        project, run, binding, _, url = setup(client, path, monkeypatch)
        _, body = request(client, url, binding)
        with Database(path) as db:
            before = len(JobRepository(db).list())
            if change == "allowance":
                db.connection.execute("UPDATE production_runs SET repair_allowance = 0 WHERE id = ?", (run["id"],))
            elif change == "budget":
                BudgetPolicyRepository(db).save(BudgetPolicy(project_id=UUID(project), max_calls=0, updated_at=datetime.now(timezone.utc)))
            elif change == "bytes":
                master = AudioAssetRepository(db).get(UUID(binding["master_audio_id"]))
                (tmp_path / master.source_file).write_bytes(b"corrupted")
            elif change == "cancelled":
                assert client.post(f"{url}/cancel").status_code == 200
            else:
                body["expected_fingerprint"] = "0" * 64
        response = client.post(f"{url}/talking-repair", json=body)
        assert response.status_code == 409, response.text
        with Database(path) as db:
            assert len(JobRepository(db).list()) == before
        assert client.get(f"{url}/talking-repairs").json() == []


def test_repair_reservation_limits_attempts_cost_and_current_budget(tmp_path, monkeypatch):
    path = tmp_path / "ledger.sqlite"
    with TestClient(create_app(path)) as client:
        project, _, binding, _, url = setup(client, path, monkeypatch)
        _, body = request(client, url, binding)
        record = client.post(f"{url}/talking-repair", json=body).json()
        with Database(path) as db:
            job = JobRepository(db).get(UUID(record["replacement_job_id"]))
            ledger = ProviderCallLedger(db)
            def reserve(local=True, attempt=1):
                return _reserve_job_provider_call(ledger, job.model_copy(update={"attempt": attempt}), operation="talking",
                    category=CostCategory.TALKING, provider=job.payload.planned_context.expected_provider,
                    model=job.payload.planned_context.expected_model, input_source="fixture:repair-call", known_local_cost=local)
            with pytest.raises(JobExecutionError) as rejected:
                reserve(False)
            assert rejected.value.code == "talking_repair_external_charge_not_authorized"
            BudgetPolicyRepository(db).save(BudgetPolicy(project_id=UUID(project), max_calls=0, updated_at=datetime.now(timezone.utc)))
            with pytest.raises(JobExecutionError) as rejected:
                reserve()
            assert rejected.value.code == "provider_budget_blocked"
            BudgetPolicyRepository(db).save(BudgetPolicy(project_id=UUID(project), max_calls=1, updated_at=datetime.now(timezone.utc)))
            call = reserve()
            assert call.estimated_cost.amount == Decimal("0")
            ledger.finish(project_id=UUID(project), call_id=call.id, status="failed", error_code="fixture")
            with pytest.raises(JobExecutionError) as rejected:
                reserve(attempt=2)
            assert rejected.value.code == "talking_repair_call_limit"
            assert len(ProviderCallRepository(db).list_for_project(UUID(project))) == 1
        listed = client.get(f"{url}/talking-repairs").json()[0]
        assert listed["provider_calls"][0]["actual_cost"] is None


def test_repair_rollback_and_concurrent_exact_replay(tmp_path, monkeypatch):
    path = tmp_path / "atomic.sqlite"
    with TestClient(create_app(path)) as client:
        project, run, binding, _, url = setup(client, path, monkeypatch)
        _, body = request(client, url, binding)
        with Database(path) as db:
            before = len(JobRepository(db).list())
            db.connection.execute("CREATE TRIGGER fail_repair BEFORE INSERT ON talking_repairs BEGIN SELECT RAISE(ABORT, 'fixture rollback'); END")
            with pytest.raises(Exception, match="fixture rollback"):
                TalkingRepairService(db, tmp_path).execute(UUID(project), UUID(run["id"]), TalkingRepairRequest.model_validate(body))
            assert len(JobRepository(db).list()) == before
            db.connection.execute("DROP TRIGGER fail_repair")
        def execute():
            with Database(path) as db:
                return TalkingRepairService(db, tmp_path).execute(UUID(project), UUID(run["id"]), TalkingRepairRequest.model_validate(body)).id
        with ThreadPoolExecutor(max_workers=2) as pool:
            ids = list(pool.map(lambda _: execute(), range(2)))
        assert ids[0] == ids[1]
        with Database(path) as db:
            assert len(JobRepository(db).list()) == before + 1


@pytest.mark.parametrize("change", ["consent", "source_receipt", "license", "source_bytes"])
def test_repair_worker_guard_reopens_authority_inside_reservation(tmp_path, monkeypatch, change):
    path = tmp_path / "worker-authority.sqlite"
    with TestClient(create_app(path)) as client:
        project, _, binding, _, url = setup(client, path, monkeypatch)
        _, body = request(client, url, binding)
        record = client.post(f"{url}/talking-repair", json=body).json()
        with Database(path) as db:
            job = JobRepository(db).get(UUID(record["replacement_job_id"]))
            context = job.payload.planned_context
            if change == "consent":
                repo = TalkingProfileRepository(db)
                profile = repo.get(UUID(binding["talking_profile_id"]))
                revoked = profile.model_copy(update={"consent": profile.consent.model_copy(update={"confirmed": False})})
                db.connection.execute("UPDATE talking_profiles SET payload = ? WHERE id = ?", (revoked.model_dump_json(), str(profile.id)))
            elif change == "source_receipt":
                TalkingSourceAdmissionRepository(db, tmp_path).revoke(context.source_admission_id, actor_id="reviewer", reason="fixture: revoked")
            elif change == "license":
                repo = ProviderMachineCapabilityProfileRepository(db)
                capability = repo.get(context.capability_profile_id)
                repo.save(capability.model_copy(update={"commercial_status": "non_commercial_only"}))
            else:
                source = AssetRepository(db).get(UUID(binding["reference_asset_id"]))
                (tmp_path / source.source_file).write_bytes(b"changed fixture source")
            def guard():
                ProductionRunService(db, tmp_path).require_talking_job_admission(job,
                    provider=context.expected_provider, model=context.expected_model,
                    runtime=context.expected_runtime, machine_id=context.expected_machine_id)
            with pytest.raises(JobExecutionError):
                _reserve_job_provider_call(ProviderCallLedger(db), job.model_copy(update={"attempt": 1}), operation="talking",
                    category=CostCategory.TALKING, provider=context.expected_provider, model=context.expected_model,
                    input_source="fixture:authority", known_local_cost=True, execution_guard=guard)
            assert ProviderCallRepository(db).list_for_project(UUID(project)) == []


def test_two_worker_attempts_share_atomic_repair_call_ceiling(tmp_path, monkeypatch):
    path = tmp_path / "call-race.sqlite"
    with TestClient(create_app(path)) as client:
        project, _, binding, _, url = setup(client, path, monkeypatch)
        _, body = request(client, url, binding)
        record = client.post(f"{url}/talking-repair", json=body).json()
        assert client.put(f"/projects/{project}/budget", json={"max_calls": 10, "allow_unknown_cost": True}).status_code == 200
        def reserve(attempt):
            with Database(path) as db:
                job = JobRepository(db).get(UUID(record["replacement_job_id"]))
                try:
                    return _reserve_job_provider_call(ProviderCallLedger(db), job.model_copy(update={"attempt": attempt}),
                        operation="talking", category=CostCategory.TALKING, provider="fixture-talking", model="fixture-model",
                        input_source="fixture:race", known_local_cost=True).id
                except JobExecutionError as exc:
                    return exc.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(reserve, (1, 2)))
        assert results.count("talking_repair_call_limit") == 1
        with Database(path) as db:
            assert len(ProviderCallRepository(db).list_for_project(UUID(project))) == 1


def test_replacement_new_collection_real_preview_and_fresh_review(tmp_path, monkeypatch):
    path = tmp_path / "successor.sqlite"
    ffmpeg, ffprobe = resolve_local_executable("ffmpeg"), resolve_local_executable("ffprobe")
    with TestClient(create_app(path)) as client:
        project, run, binding, old_preview, url = setup(client, path, monkeypatch)
        _, body = request(client, url, binding)
        record = client.post(f"{url}/talking-repair", json=body).json()
        # A synthetic portrait scene; the test supplies no creator likeness judgment.
        def chunk(kind, data):
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        still = tmp_path / "synthetic.png"
        still.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 64, 112, 8, 2, 0, 0, 0))
                          + chunk(b"IDAT", zlib.compress((b"\0" + bytes((0, 180, 90)) * 64) * 112)) + chunk(b"IEND", b""))
        output_path = tmp_path / "replacement.mp4"
        made = subprocess.run([ffmpeg, "-nostdin", "-y", "-loop", "1", "-framerate", "25", "-i", str(still),
                               "-t", "3", "-c:v", "libx264", str(output_path)], capture_output=True, text=True, timeout=60)
        assert made.returncode == 0, made.stderr[-1000:]
        with Database(path) as db:
            jobs, assets = JobRepository(db), AssetRepository(db)
            replacement = jobs.get(UUID(record["replacement_job_id"]))
            jobs.update(replacement.model_copy(update={"status": JobStatus.COMPLETED}))
            old = assets.get(UUID(binding["talking_output_asset_id"]))
            generation = {key: value for key, value in old.metadata["talking_generation"].items()
                          if key not in {"qa", "human_review", "human_review_state"}}
            generation.update(job_id=str(replacement.id), qa_state="pending")
            output = old.model_copy(update={"id": uuid4(), "source_file": output_path.name,
                "content_hash": hashlib.sha256(output_path.read_bytes()).hexdigest(), "width": 64, "height": 112,
                "fps": RationalFps(numerator=25, denominator=1), "has_audio": False,
                "metadata": {"talking_generation": generation}})
            assets.create(output)
        scene = {"scene_plan_id": binding["scene_plan_id"]}
        advanced = client.post(f"{url}/talking-qa-advance", json=scene)
        assert advanced.status_code == 200, advanced.text
        current = advanced.json()["talking_source_bindings"][0]
        assert current["talking_qa_job_id"] != binding["talking_qa_job_id"]
        with Database(path) as db:
            jobs, assets = JobRepository(db), AssetRepository(db)
            qa = jobs.get(UUID(current["talking_qa_job_id"]))
            jobs.update(qa.model_copy(update={"status": JobStatus.COMPLETED}))
            generation.update(qa_state="verified", qa={"job_id": str(qa.id), "automated_verified": True, "evidence_reference": "fixture:QA"})
            assets.update(output.model_copy(update={"metadata": {"talking_generation": generation}}))
        prepared = client.post(f"{url}/talking-preview-prepare", json=scene)
        assert prepared.status_code == 200, prepared.text
        current = prepared.json()["talking_source_bindings"][0]
        assert current["talking_series_id"] != binding["talking_series_id"]
        with Database(path) as db:
            jobs = JobRepository(db)
            job = jobs.get(UUID(current["talking_preview_job_id"]))
            running = job.model_copy(update={"status": JobStatus.RUNNING})
            jobs.update(running)
            TalkingRunPreviewJobHandler(db, tmp_path, ffmpeg_command=ffmpeg, probe=FFProbeAdapter(ffprobe))(running)
            jobs.update(running.model_copy(update={"status": JobStatus.COMPLETED}))
            preview = next(asset for asset in AssetRepository(db).list()
                           if asset.metadata.get("talking_run_preview", {}).get("job_id") == str(job.id))
            measured = FFProbeAdapter(ffprobe).probe(tmp_path / preview.source_file)
            assert measured.has_audio and abs(measured.duration_ms - 3000) <= 80
            assert preview.content_hash != old_preview.content_hash
        assert client.get(f"{url}/talking-repairs").json()[0]["successor_series_id"] == current["talking_series_id"]
        new_url = root(project, current)
        assert client.post(f"{new_url}/talking-run").status_code == 409
        assert client.post(f"{root(project, binding)}/talking-run").status_code == 409
        # Still one repair even with a new idempotency key / ample project budget.
        plan, again = request(client, url, current, key="repair-2")
        assert "talking_scene_repair_already_attempted" in plan["stop_reasons"]
        assert client.post(f"{url}/talking-repair", json=again).status_code == 409
        assert client.post(f"{new_url}/continuity-review", json=judgment(preview)).status_code == 201
        assert client.post(f"{new_url}/talking-run").status_code == 201


def test_schema_35_upgrade_keeps_existing_production_and_review_bytes(tmp_path, monkeypatch):
    path = tmp_path / "migration.sqlite"
    with TestClient(create_app(path)) as client:
        _, _, binding, _, _ = setup(client, path, monkeypatch)
    with Database(path) as db:
        before = db.connection.execute("SELECT payload FROM talking_slice_series_continuity_reviews").fetchall()[0][0]
        db.connection.execute("DROP TABLE talking_repairs")
        db.connection.execute("DELETE FROM schema_migrations WHERE version = 36")
    with Database(path) as db:
        assert db.connection.execute("SELECT payload FROM talking_slice_series_continuity_reviews").fetchall()[0][0] == before
        assert db.connection.execute("SELECT COUNT(*) FROM talking_repairs").fetchone()[0] == 0
        assert TalkingSliceSeriesRepository(db).get(UUID(binding["talking_series_id"])) is not None


def test_repair_preserves_other_current_scene_binding_and_master_review(tmp_path, monkeypatch):
    path = tmp_path / "siblings.sqlite"
    with TestClient(create_app(path)) as client:
        project, _, original, _, _ = setup(client, path, monkeypatch)
        with Database(path) as db:
            draft = ProjectDraftRepository(db).get(UUID(project))
            first = draft.scenes[0].model_copy(update={"id": uuid4(), "voice_text": "A fresh", "duration_target_ms": 1500})
            second = first.model_copy(update={"id": uuid4(), "scene_id": "continuation", "order": 1, "voice_text": "creator point."})
            master = AudioAssetRepository(db).get(UUID(original["master_audio_id"]))
            # Same approved bytes and exact spoken copy; fixture ASR resolves two scenes.
            master = master.model_copy(update={"transcript_segments": [
                TranscriptSegment(start_ms=0, end_ms=1500, text="A fresh"),
                TranscriptSegment(start_ms=1500, end_ms=3000, text="creator point."),
            ]})
            AudioAssetRepository(db).update(master)
            admission_id = JobRepository(db).get(UUID(original["talking_job_id"])).payload.planned_context.source_admission_id
        saved = client.put(f"/projects/{project}/draft", json={"script": "A fresh creator point.", "topic": "New",
            "scenes": [first.model_dump(mode="json"), second.model_dump(mode="json")]})
        assert saved.status_code == 200, saved.text
        assert client.put(f"/projects/{project}/budget", json={"max_calls": 10, "allow_unknown_cost": True}).status_code == 200
        plan = client.post(f"/projects/{project}/production-preflight", json={"repair_allowance": 2}).json()
        started = client.post(f"/projects/{project}/production-runs", json={"idempotency_key": "two-scenes",
            "repair_allowance": 2, "expected_fingerprint": plan["fingerprint"], "talking_review_policy_version": 2})
        assert started.status_code == 201, started.text
        url = f"/projects/{project}/production-runs/{started.json()['id']}"
        for index, scene in enumerate((first, second)):
            bound = client.post(f"{url}/talking-source-bind", json={
                "scene_plan_id": str(scene.id), "talking_profile_id": original["talking_profile_id"],
                "reference_clip_id": original["reference_clip_id"], "capability_profile_id": original["capability_profile_id"],
                "master_start_ms": index * 1500, "master_end_ms": (index + 1) * 1500,
                "authorization_reference": original["authorization_reference"], "brief": original["performance_brief"],
            })
            assert bound.status_code == 200, bound.text
            applied = client.post(f"{url}/talking-suitability-apply", json={
                "scene_plan_id": str(scene.id), "assessment_id": original["suitability_assessment_id"]})
            assert applied.status_code == 200, applied.text
            dispatched = client.post(f"{url}/talking-dispatch", json={"scene_plan_id": str(scene.id), "source_admission_id": str(admission_id)})
            assert dispatched.status_code == 200, dispatched.text
        bindings = client.get(url).json()["talking_source_bindings"]
        with Database(path) as db:
            jobs = JobRepository(db)
            failed = jobs.get(UUID(bindings[0]["talking_job_id"]))
            sibling = jobs.get(UUID(bindings[1]["talking_job_id"]))
            jobs.update(failed.model_copy(update={"status": JobStatus.FAILED, "error_code": "talking_temporarily_unavailable"}))
        _, body = request(client, url, bindings[0])
        repaired = client.post(f"{url}/talking-repair", json=body)
        assert repaired.status_code == 201, repaired.text
        assert repaired.json()["reused_scene_plan_ids"] == [str(second.id)]
        assert client.get(url).json()["talking_source_bindings"][1] == bindings[1]
        with Database(path) as db:
            assert JobRepository(db).get(sibling.id) == sibling
            assert AudioAssetRepository(db).get(master.id) == master
