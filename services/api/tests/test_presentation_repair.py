"""Exact synthetic render observations; no creator/provider quality claims."""
import hashlib
import subprocess
import struct
import zlib
import wave
import os
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.db import Database, JobRepository, ProjectRepository, ProviderCallRepository, AudioAssetRepository, AssetRepository
from app.domain.models import JobStatus, VideoSpec
from app.jobs.handlers import RenderVideoJobHandler
from app.jobs.runner import JobExecutionError
from app.presentation_repair import PresentationRepairService, PresentationRepairRequest, PresentationObservationRequest, diagnosis_for_finding
from app.production_runs import ProductionRunNotReady, ProductionRunConflict
from app.runtime import resolve_local_executable
from test_production_runs import _ready_fixture
from test_production_runs import _fixture_reviewable_planned_preview


def synthetic_video(path, spec):
    path.parent.mkdir(parents=True, exist_ok=True)
    seconds = max(s.start_frame + s.duration_frames for s in spec.scenes) * spec.fps.denominator / spec.fps.numerator
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    still, audio = path.with_suffix(".png"), path.with_suffix(".wav")
    still.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", spec.width, spec.height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress((b"\0" + bytes((20, 30, 80)) * spec.width) * spec.height)) + chunk(b"IEND", b""))
    with wave.open(str(audio), "wb") as stream:
        stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(48000)
        stream.writeframes(b"\0\0" * int(seconds * 48000))
    result = subprocess.run([resolve_local_executable("ffmpeg"), "-y", "-v", "error", "-loop", "1", "-framerate",
        f"{spec.fps.numerator}/{spec.fps.denominator}", "-i", str(still), "-i", str(audio), "-t", str(seconds),
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)],
        capture_output=True, timeout=60, check=False)
    assert result.returncode == 0, result.stderr


def setup(client, path, finding="duplicate_text"):
    project, _, _ = _ready_fixture(client, path)
    draft = client.get(f"/projects/{project}/draft").json()
    draft["scenes"][0]["caption_emphasis"] = ["A fresh point"]
    response = client.put(f"/projects/{project}/draft", json={"script": draft["script"], "topic": "New", "scenes": draft["scenes"]})
    assert response.status_code == 200, response.text
    plan = client.post(f"/projects/{project}/production-preflight", json={}).json()
    response = client.post(f"/projects/{project}/production-runs", json={"idempotency_key": "synthetic-render", "expected_fingerprint": plan["fingerprint"]})
    assert response.status_code == 201, response.text
    run = response.json()
    with Database(path) as db:
        jobs = JobRepository(db)
        job = jobs.get(UUID(run["render_job_id"]))
        historical = job.payload.video_spec.model_dump(mode="json")
        # Model a persisted historical layout defect; normal new-plan preflight
        # already rejects full-copy duplication. This is not an automatic finding.
        historical["scenes"][0]["graphic_text"] = "A fresh creator point."
        historical["edit_plan"]["scenes"][0]["graphic_text"] = "A fresh creator point."
        job = job.model_copy(update={"status": JobStatus.COMPLETED,
            "payload": job.payload.model_copy(update={"video_spec": VideoSpec.model_validate(historical)})})
        jobs.update(job)
    output = path.parent / "renders" / project / f"{job.payload.render_id}.mp4"
    synthetic_video(output, job.payload.video_spec)
    root = f"/projects/{project}/production-runs/{run['id']}"
    request = {"idempotency_key": "observed", "render_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "scene_id": "hook", "finding": finding, "reason": "synthetic explicit layout observation", "evidence_reference": "fixture:exact-render"}
    observation = client.post(root + "/presentation-observations", json=request)
    assert observation.status_code == 201, observation.text
    assert client.post(root + "/presentation-observations", json=request).json() == observation.json()
    return project, run, root, job, output, observation.json()


def repair_body(client, root, observation):
    response = client.get(root + "/presentation-repair-plan", params={"observation_id": observation["id"]})
    assert response.status_code == 200, response.text
    plan = response.json()
    return plan, {"observation_id": observation["id"], "expected_fingerprint": plan["fingerprint"],
        "idempotency_key": "repair-once", "confirmed_action": "render_revision", "reason": "explicit synthetic correction"}


@pytest.mark.parametrize('corrupt',[False,True])
def test_ordinary_automatic_render_runs_whole_output_technical_qa_and_preserves_inputs(tmp_path,corrupt):
    path=tmp_path/'automatic-output.sqlite'
    with TestClient(create_app(path)) as client:
        pid,_,_=_ready_fixture(client,path)
        plan=client.post(f'/projects/{pid}/production-preflight',json={}).json()
        result=client.post(f'/projects/{pid}/production-runs',json={
            'idempotency_key':'ordinary-output','expected_fingerprint':plan['fingerprint']})
        assert result.status_code==201,result.text
        run=result.json()
    with Database(path) as db:
        db.data_root=tmp_path
        db.connection.execute('UPDATE production_runs SET automatic=1 WHERE id=?',(run['id'],))
        job=JobRepository(db).get(UUID(run['render_job_id']))
        job=job.model_copy(update={'status':JobStatus.RUNNING})
        JobRepository(db).update(job)
        master=AudioAssetRepository(db).get(job.payload.video_spec.master_narration.audio_asset_id)
        class Renderer:
            data_root=tmp_path
            def render(self,spec,output,**kwargs):
                output.parent.mkdir(parents=True,exist_ok=True)
                if corrupt:output.write_bytes(b'invalid rendered output')
                else:synthetic_video(output,spec)
        handler=RenderVideoJobHandler(ProjectRepository(db),Renderer(),tmp_path/'renders')
        if corrupt:
            with pytest.raises(JobExecutionError) as failure:handler(job)
            assert not failure.value.retryable
            assert (tmp_path/'renders'/pid/f'{job.payload.render_id}.mp4').read_bytes()==b'invalid rendered output'
            assert not (tmp_path/'render-diagnostics'/str(job.id)/'output-qa.json').exists()
        else:
            handler(job)
            evidence=json.loads((tmp_path/'render-diagnostics'/str(job.id)/'output-qa.json').read_text(encoding='utf-8'))
            assert evidence['decode']=='pass' and evidence['scope']=='technical_only_final_u_product_required'
        assert AudioAssetRepository(db).get(master.id)==master
        assert ProviderCallRepository(db).list_for_project(UUID(pid))==[]


def test_revision_replay_restart_preserves_master_and_predecessor(tmp_path):
    path = tmp_path / "presentation.sqlite"
    with TestClient(create_app(path)) as client:
        project, run, root, old, output, observation = setup(client, path)
        digest = hashlib.sha256(output.read_bytes()).hexdigest()
        plan, request = repair_body(client, root, observation)
        assert plan["action"] == "render_revision", plan
        assert plan["responsible_stage"] == "assembly" and plan["assessment_state"] == "known"
        assert plan["required_capability"] == []
        assert plan["preserves"]["master_audio_id"] == str(old.payload.video_spec.master_narration.audio_asset_id)
        assert plan["preserves"]["render_job_id"] == str(old.id)
        assert plan["preserves"]["render_sha256"] == digest
        assert plan["candidate_spec"]["scenes"][0]["graphic_text"] == "A fresh point"
        assert plan["candidate_spec"]["master_narration"] == old.payload.video_spec.master_narration.model_dump(mode="json")
        assert plan["candidate_spec"]["scenes"][0]["subtitle_treatment"] == "timed_captions"
        response = client.post(root + "/presentation-repair", json=request)
        assert response.status_code == 201, response.text
        record = response.json()
        assert client.post(root + "/presentation-repair", json=request).json() == record
        assert client.post(root + "/presentation-repair", json={**request, "reason": "different"}).status_code == 409
        assert client.get(root).json()["render_job_id"] == record["replacement_job_id"]
        assert hashlib.sha256(output.read_bytes()).hexdigest() == digest
        with Database(path) as db:
            assert JobRepository(db).get(old.id).model_dump(mode="json") == old.model_dump(mode="json")
            assert not ProviderCallRepository(db).list_for_project(UUID(project))
        history = client.get(root + "/presentation-repairs").json()
        assert history[0]["technical_qa"] is None and history[0]["final_review_state"] == "not_submitted"
    with TestClient(create_app(path)) as client:
        assert client.post(root + "/presentation-repair", json=request).json() == record
        assert client.get(root + "/presentation-repairs").json()[0]["plan"]["predecessor_job"]["id"] == str(old.id)


@pytest.mark.parametrize("finding,stage,capability", [
    ("unknown_subtitles", "material", "full_interval_source_subtitle_analysis"),
    ("face_out_of_frame", "unknown", "source_vs_render_framing_diagnosis"),
    ("source_change", "material", "scene_source_interval_selection_and_qa"),
    ("text_overlay", "assembly", "exact_scene_text_face_layout_diagnosis"),
    ("known_subtitle_conflict", "material", "full_interval_source_subtitle_analysis"),
    ("voice_naturalness", "voice", "exact_audio_naturalness_diagnosis"),
    ("talking_naturalness", "talking", "exact_talking_naturalness_diagnosis"),
    ("visible_sync", "unknown", "render_vs_talking_sync_diagnosis"),
    ("semantic_mismatch", "unknown", "exact_scene_narration_visual_semantics_diagnosis"),
    ("narrative_rhythm", "timing", "exact_scene_delivery_vs_edit_timing_diagnosis"),
])
def test_unsupported_or_unknown_does_not_generate(tmp_path, finding, stage, capability):
    path = tmp_path / "presentation.sqlite"
    with TestClient(create_app(path)) as client:
        project, run, root, old, output, observation = setup(client, path, finding)
        master_id = old.payload.video_spec.master_narration.audio_asset_id
        with Database(path) as db:
            before_master = AudioAssetRepository(db).get(master_id)
            before_jobs = JobRepository(db).list()
        plan, request = repair_body(client, root, observation)
        assert plan["action"] == "scoped_fix_required"
        assert plan["responsible_stage"] == stage and plan["assessment_state"] == "unknown"
        assert capability in plan["required_capability"]
        assert diagnosis_for_finding(finding)["responsible_stage"] == stage
        target = old.payload.video_spec.scenes[0]
        fps = old.payload.video_spec.fps
        assert plan["scope"] == {"scene_id": "hook", "start_ms": target.start_frame * 1000 * fps.denominator // fps.numerator,
            "end_ms": ((target.start_frame + target.duration_frames) * 1000 * fps.denominator + fps.numerator - 1) // fps.numerator}
        assert plan["preserves"]["master_audio_id"] == str(master_id)
        assert plan["preserves"]["master_audio_sha256"] == before_master.content_hash
        assert plan["preserves"]["talking_job_ids"] == plan["preserves"]["talking_output_asset_ids"] == []
        assert plan["preserves"]["scene_plan_ids"] == [str(entry.scene_plan_id) for entry in old.payload.video_spec.edit_plan.scenes]
        assert plan["candidate_spec"] is None and plan["voice_calls"] == plan["talking_calls"] == 0
        assert client.post(root + "/presentation-repair", json=request).status_code == 409
        assert client.get(root).json()["render_job_id"] == str(old.id)
        assert client.get(root).json()["talking_source_bindings"] == run["talking_source_bindings"]
        assert hashlib.sha256(output.read_bytes()).hexdigest() == plan["preserves"]["render_sha256"]
        with Database(path) as db:
            assert AudioAssetRepository(db).get(master_id) == before_master
            assert JobRepository(db).list() == before_jobs
            assert not ProviderCallRepository(db).list_for_project(UUID(project))
            assert db.connection.execute("SELECT COUNT(*) FROM presentation_repairs").fetchone()[0] == 0


def test_observation_source_is_explicit_idempotent_and_cannot_claim_subjective_qa(tmp_path):
    path = tmp_path / "presentation.sqlite"
    with TestClient(create_app(path)) as client:
        project, run, root, _, _, human = setup(client, path)
        assert human["source"] == "human_observation"
        with Database(path) as db:
            service = PresentationRepairService(db, tmp_path, tmp_path / "renders")
            request = PresentationObservationRequest.model_validate(human["request"])
            with pytest.raises(ProductionRunConflict, match="idempotency_conflict"):
                service.observe(UUID(project), UUID(run["id"]), request, source="deterministic_spec_qa")
            request = request.model_copy(update={"idempotency_key": "spec-qa-once"})
            observed = service.observe(UUID(project), UUID(run["id"]), request, source="deterministic_spec_qa")
            assert observed["source"] == "deterministic_spec_qa"
            assert service.observe(UUID(project), UUID(run["id"]), request, source="deterministic_spec_qa") == observed
            with pytest.raises(ProductionRunConflict, match="idempotency_conflict"):
                service.observe(UUID(project), UUID(run["id"]), request)
            with pytest.raises(ValueError, match="source_invalid"):
                service.observe(UUID(project), UUID(run["id"]), request, source="automatic_naturalness_pass")
            for finding in ("voice_naturalness", "talking_naturalness", "visible_sync", "semantic_mismatch", "narrative_rhythm"):
                with pytest.raises(ValueError, match="deterministic_finding_unsupported"):
                    service.observe(UUID(project), UUID(run["id"]), request.model_copy(update={"finding": finding}), source="deterministic_spec_qa")
            assert db.connection.execute("SELECT COUNT(*) FROM presentation_observations").fetchone()[0] == 2
            assert ProviderCallRepository(db).list_for_project(UUID(project)) == []
        assert client.post(root + "/presentation-observations", json={**human["request"], "source": "deterministic_spec_qa"}).status_code == 422


def test_scoped_quality_observation_remains_bound_to_exact_render_and_scene(tmp_path):
    path = tmp_path / "presentation.sqlite"
    with TestClient(create_app(path)) as client:
        project, _, root, old, output, observation = setup(client, path, "voice_naturalness")
        wrong_scene = {**observation["request"], "idempotency_key": "wrong-scene", "scene_id": "absent"}
        assert client.post(root + "/presentation-observations", json=wrong_scene).status_code == 409
        plan, request = repair_body(client, root, observation)
        assert plan["action"] == "scoped_fix_required" and plan["assessment_state"] == "unknown"
        output.write_bytes(output.read_bytes() + b"changed")
        response = client.get(root + "/presentation-repair-plan", params={"observation_id": observation["id"]})
        assert response.status_code == 409 and "observation_stale" in response.text
        assert client.post(root + "/presentation-repair", json=request).status_code == 409
        assert client.get(root).json()["render_job_id"] == str(old.id)
        with Database(path) as db:
            assert ProviderCallRepository(db).list_for_project(UUID(project)) == []
            assert db.connection.execute("SELECT COUNT(*) FROM presentation_repairs").fetchone()[0] == 0


@pytest.mark.parametrize("finding", ["source_change", "voice_naturalness"])
def test_independent_findings_require_one_joint_scoped_plan_without_discarding_observations(tmp_path, finding):
    path = tmp_path / "multiple-findings.sqlite"
    with TestClient(create_app(path)) as client:
        project, _, root, old, output, first = setup(client, path)
        second = client.post(root + "/presentation-observations", json={**first["request"],
            "idempotency_key": "independent-second-finding", "finding": finding,
            "reason": "A second unresolved defect on this same exact output."}).json()
        with Database(path) as db:
            before_jobs = JobRepository(db).list()
            before_master = AudioAssetRepository(db).get(old.payload.video_spec.master_narration.audio_asset_id)
        for selected in (first, second):
            plan, request = repair_body(client, root, selected)
            assert plan["action"] == "scoped_fix_required" and plan["assessment_state"] == "unknown"
            assert f"presentation_{finding}_diagnosis_required" in plan["stop_reasons"]
            assert plan["required_capability"] and "joint_scoped_repair_plan" not in plan["required_capability"]
            assert len(plan["diagnoses"]) == 2 and any(item["assessment_state"] == "unknown" for item in plan["diagnoses"])
            assert {observation["id"] for observation in plan["observations"]} == {first["id"], second["id"]}
            assert plan["candidate_spec"] is None and plan["changes"] == []
            assert client.post(root + "/presentation-repair", json=request).status_code == 409
            assert client.get(root).json()["render_job_id"] == str(old.id)
        assert hashlib.sha256(output.read_bytes()).hexdigest() == first["request"]["render_sha256"]
        with Database(path) as db:
            assert JobRepository(db).list() == before_jobs
            assert AudioAssetRepository(db).get(before_master.id) == before_master
            assert ProviderCallRepository(db).list_for_project(UUID(project)) == []
            assert db.connection.execute("SELECT COUNT(*) FROM presentation_observations").fetchone()[0] == 2


def test_repeated_observation_of_same_scene_and_finding_uses_latest_without_blocking_single_fix(tmp_path):
    path = tmp_path / "repeated-finding.sqlite"
    with TestClient(create_app(path)) as client:
        _, _, root, _, _, first = setup(client, path)
        latest = client.post(root + "/presentation-observations", json={**first["request"],
            "idempotency_key": "same-finding-with-more-evidence", "reason": "Additional evidence for the same defect."}).json()
        for selected in (first, latest):
            plan, _ = repair_body(client, root, selected)
            assert plan["action"] == "render_revision"
            assert [observation["id"] for observation in plan["observations"]] == [latest["id"]]
            assert "joint_scoped_repair_plan" not in plan["required_capability"]


def test_automatic_admission_guard_is_atomic_and_idempotent_replay_does_not_reenter_it(tmp_path):
    path = tmp_path / "guarded-fix.sqlite"
    with TestClient(create_app(path)) as client:
        project, run, root, old, _, observation = setup(client, path)
        _, request = repair_body(client, root, observation)
        with Database(path) as db:
            service = PresentationRepairService(db, tmp_path, tmp_path / "renders")
            before_jobs = JobRepository(db).list()
            calls = []
            def denied():
                assert db.connection.in_transaction
                calls.append("denied")
                raise ProductionRunNotReady(("automatic_admission_changed",))
            with pytest.raises(ProductionRunNotReady, match="automatic_admission_changed"):
                service.apply(UUID(project), UUID(run["id"]), PresentationRepairRequest.model_validate(request), admission_guard=denied)
            assert JobRepository(db).list() == before_jobs
            assert db.connection.execute("SELECT render_job_id FROM production_runs WHERE id=?", (run["id"],)).fetchone()[0] == str(old.id)
            assert db.connection.execute("SELECT COUNT(*) FROM presentation_repairs").fetchone()[0] == 0
            def admitted():
                assert db.connection.in_transaction
                calls.append("admitted")
            record = service.apply(UUID(project), UUID(run["id"]), PresentationRepairRequest.model_validate(request), admission_guard=admitted)
            def forbidden_reentry():
                pytest.fail("Exact idempotent replay must return before reentering automatic admission.")
            assert service.apply(UUID(project), UUID(run["id"]), PresentationRepairRequest.model_validate(request), admission_guard=forbidden_reentry) == record
            assert calls == ["denied", "admitted"]
            assert len(JobRepository(db).list()) == len(before_jobs) + 1
            assert ProviderCallRepository(db).list_for_project(UUID(project)) == []


def test_worker_measured_output_fresh_qa_and_one_render_limit(tmp_path):
    path = tmp_path / "presentation.sqlite"
    with TestClient(create_app(path)) as client:
        project, _, root, _, _, observation = setup(client, path)
        _, request = repair_body(client, root, observation)
        record = client.post(root + "/presentation-repair", json=request).json()
        with Database(path) as db:
            job = JobRepository(db).get(UUID(record["replacement_job_id"])).model_copy(update={"status": JobStatus.RUNNING})
            class SyntheticRenderer:
                data_root = tmp_path
                def render(self, spec, output, *, production_run_id=None):
                    assert spec.scenes[0].graphic_text == "A fresh point"
                    synthetic_video(output, spec)
            handler = RenderVideoJobHandler(ProjectRepository(db), SyntheticRenderer(), tmp_path / "renders")
            try:
                handler(job)
            except JobExecutionError:
                # Re-expose the exact technical cause, not sanitized Worker text.
                PresentationRepairService(db, tmp_path, tmp_path / "renders").verify_output(job)
                raise
            calls = ProviderCallRepository(db).list_for_project(UUID(project))
            assert len(calls) == 1 and calls[0].operation == "render" and calls[0].status == "completed"
            assert calls[0].actual_cost.amount == 0
            with pytest.raises(JobExecutionError, match="admission"):
                handler(job.model_copy(update={"attempt": job.attempt + 1}))
        history = client.get(root + "/presentation-repairs").json()[0]
        assert history["technical_qa"]["decode"] == "pass" and history["final_review_state"] == "not_submitted"
        with Database(path) as db:
            replacement = JobRepository(db).get(UUID(record["replacement_job_id"]))
        (tmp_path / "renders" / project / f"{replacement.payload.render_id}.mp4").write_bytes(b"changed after QA")
        assert client.get(root + "/presentation-repairs").json()[0]["technical_qa"]["state"] == "stale"


@pytest.mark.parametrize("change", ["master_bytes", "render_bytes", "allowance", "cancelled"])
def test_worker_rechecks_current_authority_and_bytes(tmp_path, change):
    path = tmp_path / "presentation.sqlite"
    with TestClient(create_app(path)) as client:
        project, run, root, _, output, observation = setup(client, path)
        _, request = repair_body(client, root, observation)
        record = client.post(root + "/presentation-repair", json=request).json()
        with Database(path) as db:
            job = JobRepository(db).get(UUID(record["replacement_job_id"])).model_copy(update={"status": JobStatus.RUNNING})
            if change == "master_bytes": (tmp_path / "approved-master.wav").write_bytes(b"changed")
            elif change == "render_bytes": output.write_bytes(b"changed")
            elif change == "cancelled": db.connection.execute("UPDATE production_runs SET stage='cancelled' WHERE id=?", (run["id"],))
            else:
                # A new explicit key cannot grant a second creation-bound repair.
                assert db.connection.execute("SELECT COUNT(*) FROM presentation_repairs WHERE run_id=?", (run["id"],)).fetchone()[0] == 1
                with pytest.raises(ProductionRunNotReady):
                    PresentationRepairService(db, tmp_path, tmp_path / "renders").plan(UUID(project), UUID(run["id"]), UUID(observation["id"]))
                return
            with pytest.raises((ProductionRunNotReady, ValueError)):
                PresentationRepairService(db, tmp_path, tmp_path / "renders").reserve_render(job)
            assert not ProviderCallRepository(db).list_for_project(UUID(project))


@pytest.mark.parametrize("finding", ["known_subtitle_conflict", "text_overlay"])
def test_unchanged_reviewed_talking_candidate_caption_or_panel(tmp_path, monkeypatch, finding):
    path = tmp_path / "talking-presentation.sqlite"
    with TestClient(create_app(path)) as client:
        project, waiting, bound, preview = _fixture_reviewable_planned_preview(client, path, monkeypatch)
        series_root = f"/projects/{project}/talking-slice-series/{bound['talking_series_id']}"
        assert client.post(series_root + "/continuity-review", json={"approved": True,
            "evidence_reference": "fixture:unchanged-preview", "findings": [], "preview_asset_id": str(preview.id),
            "preview_sha256": preview.content_hash}).status_code == 201
        assert client.post(series_root + "/talking-run").status_code == 201
        root = f"/projects/{project}/production-runs/{waiting['id']}"
        resumed = client.post(root + "/resume")
        assert resumed.status_code == 200, resumed.text
        client.put(f"/projects/{project}/budget", json={"max_calls": 10, "allow_unknown_cost": True})
        with Database(path) as db:
            jobs = JobRepository(db); old = jobs.get(UUID(resumed.json()["render_job_id"]))
            raw = old.payload.video_spec.model_dump(mode="json")
            # Previously persisted known subtitle/layout facts; the observation
            # does not turn unknown subtitles or an unapproved source into eligible.
            scene, entry = raw["scenes"][0], raw["edit_plan"]["scenes"][0]
            entry["burned_in_subtitles"] = "present" if finding == "known_subtitle_conflict" else "absent"
            if finding == "known_subtitle_conflict":
                scene["subtitle_treatment"] = entry["subtitle_treatment"] = "timed_captions"
            else:
                scene["graphic_text"] = entry["graphic_text"] = "A fresh point"
                scene["graphic_treatment"] = entry["graphic_treatment"] = "headline"
            old = old.model_copy(update={"status": JobStatus.COMPLETED,
                "payload": old.payload.model_copy(update={"video_spec": VideoSpec.model_validate(raw)})})
            jobs.update(old)
        output = tmp_path / "renders" / project / f"{old.payload.render_id}.mp4"
        synthetic_video(output, old.payload.video_spec)
        response = client.post(root + "/presentation-observations", json={"idempotency_key": "observed",
            "render_sha256": hashlib.sha256(output.read_bytes()).hexdigest(), "scene_id": "hook", "finding": finding,
            "reason": "fixture known presentation fact", "evidence_reference": "fixture:completed-version"})
        assert response.status_code == 201, response.text
        plan, request = repair_body(client, root, response.json())
        assert plan["action"] == "render_revision", plan
        assert plan["assessment_state"] == "known" and plan["responsible_stage"] == "assembly"
        assert plan["preserves"]["master_audio_id"] == bound["master_audio_id"]
        assert plan["preserves"]["talking_job_ids"] == [bound["talking_job_id"]]
        assert plan["preserves"]["talking_output_asset_ids"] == [bound["talking_output_asset_id"]]
        assert plan["preserves"]["reuse_admission"] == "unchanged_dependencies_only"
        candidate = plan["candidate_spec"]["scenes"][0]
        assert candidate["visual"] == scene["visual"]
        assert candidate["caption"] == scene["caption"] and candidate["captions"] == scene["captions"]
        assert plan["candidate_spec"]["master_narration"] == old.payload.video_spec.master_narration.model_dump(mode="json")
        if finding == "known_subtitle_conflict": assert candidate["subtitle_treatment"] == "none"
        else: assert candidate["portrait_presentation"] == "portrait_panel"
        with Database(path) as db:
            before_jobs = {job.id: job for job in JobRepository(db).list()}
            before_bindings = client.get(root).json()["talking_source_bindings"]
            before_master = AudioAssetRepository(db).get(UUID(bound["master_audio_id"]))
            before_talking = AssetRepository(db).get(UUID(bound["talking_output_asset_id"]))
        repaired = client.post(root + "/presentation-repair", json=request)
        assert repaired.status_code == 201, repaired.text
        assert client.get(root).json()["talking_source_bindings"] == before_bindings
        with Database(path) as db:
            after_jobs = {job.id: job for job in JobRepository(db).list()}
            assert set(after_jobs) - set(before_jobs) == {UUID(repaired.json()["replacement_job_id"])}
            assert all(after_jobs[key] == job for key, job in before_jobs.items())
            assert AudioAssetRepository(db).get(before_master.id) == before_master
            assert AssetRepository(db).get(before_talking.id) == before_talking
        assert plan["voice_calls"] == plan["talking_calls"] == 0


def test_atomic_allowance_concurrent_submit_and_budget_staleness(tmp_path):
    path = tmp_path / "presentation.sqlite"
    with TestClient(create_app(path)) as client:
        project, run, root, _, _, observation = setup(client, path)
        _, request = repair_body(client, root, observation)
        client.put(f"/projects/{project}/budget", json={"max_calls": 0, "allow_unknown_cost": True})
        assert client.post(root + "/presentation-repair", json=request).status_code == 409
        client.put(f"/projects/{project}/budget", json={"max_calls": 10, "allow_unknown_cost": True})
        _, request = repair_body(client, root, observation)
    def submit(key):
        with Database(path) as db:
            try:
                return PresentationRepairService(db, tmp_path, tmp_path / "renders").apply(UUID(project), UUID(run["id"]),
                    PresentationRepairRequest.model_validate({**request, "idempotency_key": key}))
            except ProductionRunNotReady:
                return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(submit, ["one", "two"]))
    assert sum(item is not None for item in outcomes) == 1
    with Database(path) as db:
        assert db.connection.execute("SELECT COUNT(*) FROM presentation_repairs").fetchone()[0] == 1


def test_migration_38_preserves_previous_jobs_and_shared_voice_usage(tmp_path):
    path = tmp_path / "presentation.sqlite"
    with TestClient(create_app(path)) as client:
        project, run, root, old, _, observation = setup(client, path)
        _, request = repair_body(client, root, observation)
        with Database(path) as db:
            # Existing lineage usage is historical evidence, not a fresh allowance.
            db.connection.execute("INSERT INTO voice_repairs(id,project_id,run_id,idempotency_key,request_fingerprint,replacement_job_id,payload) VALUES(?,?,?,?,?,?,?)",
                ("historical-repair", project, run["id"], "historical", "a" * 64, str(old.id), "{}"))
        plan, _ = repair_body(client, root, observation)
        assert plan["action"] == "stop" and plan["remaining_run_repairs"] == 0
        assert client.post(root + "/presentation-repair", json=request).status_code == 409
    with Database(path) as db:
        db.connection.execute("DROP TABLE presentation_repairs")
        db.connection.execute("DROP TABLE presentation_observations")
        db.connection.execute("DELETE FROM schema_migrations WHERE version=38")
    with Database(path) as db:
        assert JobRepository(db).get(old.id).model_dump(mode="json") == old.model_dump(mode="json")
        from app.repair_allowance import used_repairs
        assert used_repairs(db, UUID(run["id"])) == 1


def test_enqueue_failure_rolls_back_revision_and_allowance(tmp_path, monkeypatch):
    path = tmp_path / "presentation.sqlite"
    with TestClient(create_app(path)) as client:
        _, run, root, old, _, observation = setup(client, path)
        _, request = repair_body(client, root, observation)
        def fail_create(*args):
            raise RuntimeError("fixture enqueue failure")
        with monkeypatch.context() as patch:
            patch.setattr(JobRepository, "create", fail_create)
            with pytest.raises(RuntimeError, match="enqueue failure"):
                client.post(root + "/presentation-repair", json=request)
        assert client.get(root).json()["render_job_id"] == str(old.id)
        with Database(path) as db:
            from app.repair_allowance import used_repairs
            assert used_repairs(db, UUID(run["id"])) == 0


@pytest.mark.parametrize("change", ["dimensions", "frames", "corrupt"])
def test_failed_output_qa_keeps_old_render_and_never_retries(tmp_path, change):
    path = tmp_path / "presentation.sqlite"
    with TestClient(create_app(path)) as client:
        project, _, root, _, old_output, observation = setup(client, path)
        old_hash = hashlib.sha256(old_output.read_bytes()).hexdigest()
        _, request = repair_body(client, root, observation)
        record = client.post(root + "/presentation-repair", json=request).json()
        with Database(path) as db:
            job = JobRepository(db).get(UUID(record["replacement_job_id"])).model_copy(update={"status": JobStatus.RUNNING})
            class InvalidRenderer:
                data_root = tmp_path
                def render(self, spec, output, *, production_run_id=None):
                    if change == "corrupt":
                        output.parent.mkdir(parents=True, exist_ok=True)
                        output.write_bytes(b"invalid encoded output")
                    elif change == "dimensions":
                        synthetic_video(output, spec.model_copy(update={"width": 720}))
                    else:
                        synthetic_video(output, spec.model_copy(update={"scenes": [spec.scenes[0].model_copy(update={"duration_frames": 100})]}))
            handler = RenderVideoJobHandler(ProjectRepository(db), InvalidRenderer(), tmp_path / "renders")
            with pytest.raises(JobExecutionError): handler(job)
            with pytest.raises(JobExecutionError): handler(job.model_copy(update={"attempt": 2}))
            calls = ProviderCallRepository(db).list_for_project(UUID(project))
            assert len(calls) == 1 and calls[0].status == "failed"
        assert hashlib.sha256(old_output.read_bytes()).hexdigest() == old_hash
        assert client.get(root + "/presentation-repairs").json()[0]["technical_qa"] is None


@pytest.mark.skipif(os.environ.get("CONTENT_OS_PRESENTATION_RENDER_SMOKE") != "1", reason="explicit bounded local renderer smoke")
def test_real_local_remotion_short_text_and_timed_caption(tmp_path):
    """Synthetic typography only; an installed browser, never fixture-origin access."""
    from app.db import AssetRepository, ClipRepository, AudioAssetRepository
    from app.renderer import RemotionRenderer
    path = tmp_path / "presentation.sqlite"
    with TestClient(create_app(path)) as client:
        project, _, root, _, _, observation = setup(client, path)
        _, request = repair_body(client, root, observation)
        record = client.post(root + "/presentation-repair", json=request).json()
    renderer_root = Path(__file__).resolve().parents[3] / "apps" / "renderer"
    browser = renderer_root / "node_modules" / ".remotion" / "chrome-headless-shell" / "win64" / "chrome-headless-shell-win64" / "chrome-headless-shell.exe"
    assert browser.is_file()  # No downloads or browser fixture interaction.
    class LocalRunner:
        def run(self, argv, *, cwd, timeout_seconds):
            props = json.loads(Path(next(a.split("=", 1)[1] for a in argv if a.startswith("--props="))).read_text(encoding="utf-8"))
            assert props["videoSpec"]["scenes"][0]["graphic_text"] == "A fresh point"
            result = subprocess.run([*argv, f"--browser-executable={browser}", "--concurrency=1"], cwd=cwd,
                capture_output=True, timeout=timeout_seconds, check=False)
            from app.media.ffprobe import FFProbeAdapter
            if result.returncode == 0:
                print("render measured", FFProbeAdapter(resolve_local_executable("ffprobe")).probe(Path(argv[7])))
            return result
    with Database(path) as db:
        renderer = RemotionRenderer(AssetRepository(db), ClipRepository(db), audios=AudioAssetRepository(db),
            renderer_dir=renderer_root, runner=LocalRunner(), timeout_seconds=60)
        renderer.data_root = tmp_path
        job = JobRepository(db).get(UUID(record["replacement_job_id"])).model_copy(update={"status": JobStatus.RUNNING})
        RenderVideoJobHandler(ProjectRepository(db), renderer, tmp_path / "renders")(job)
        evidence = PresentationRepairService(db, tmp_path, tmp_path / "renders").history(UUID(project), UUID(record["run_id"]))[0]
        assert evidence["technical_qa"]["state"] == "verified"
        output = tmp_path / "renders" / project / f"{job.payload.render_id}.mp4"
        extracted = subprocess.run([resolve_local_executable("ffmpeg"), "-v", "error", "-ss", "1", "-i", str(output),
            "-frames:v", "1", str(tmp_path / "layout.png")], capture_output=True, timeout=30, check=False)
        assert extracted.returncode == 0, extracted.stderr
