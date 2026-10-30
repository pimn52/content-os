"""Real ledger/local fixture DB; media, QA admission and model perception are substitutes."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.costs import ProviderUsage
from app.db import (AssetRepository, AudioAssetRepository, BudgetPolicyRepository, ClipRepository, Database,
                    IPProfileRepository, JobRepository, ProjectDraftRepository, ProjectRepository, ProviderCallRepository)
from app.domain.models import (AudioAsset, BudgetPolicy, ContentPlanJobPayload, IPProfile, Job, JobStatus, JobType, Project,
                              ProjectDraft, RenderVideoJobPayload)
from app.media.ffprobe import ProbeMetadata
from app.presentation_repair import PresentationObservationRequest, PresentationRepairService
from app.presentation_semantic import PresentationSemanticDiagnosis, semantic_diagnosis_is_current
from app.production_runs import ProductionRunNotReady
from app.providers.vision import VisionTimeout
from test_presentation_diagnosis import comparison_fixture, batch


class FixtureProvider:
    provider_name, model = "fixture-video", "fixture-perception"
    supports_video_input, protocol = True, "chat_completions"

    def __init__(self, *, source=True, render=False, change=None, fail=False, on_call=None):
        self.calls = 0
        self.source, self.render, self.change, self.fail, self.on_call = source, render, change, fail, on_call
        self.last_validation_issues = []

    def analyze_intervals(self, videos, requests):
        self.calls += 1
        if self.on_call:
            self.on_call()
        if self.fail:
            raise VisionTimeout("fixture transport timeout")
        assert len(videos) == 2 and len(requests) == 1
        scope = {"scene_key": requests[0]["scene_key"], "render": videos[0], "source": videos[1]}
        result = batch(scope, source=self.source, render=self.render)
        if self.change:
            rows = result.model_dump(mode="json")
            rows["clips"][0]["matches"][0][self.change] = None
            result = type(result).model_validate(rows)
        return result

    def usage_accounting(self, error=None):
        return ProviderUsage(input_tokens=None if error else 100, output_tokens=None if error else 20,
                             usage_observable=error is None)


class FixtureMedia:
    def __init__(self, root):
        self.root, self.infos, self.calls = root, {}, []

    def _extract(self, asset, start, end, frames):
        self.calls.append((frames, start, end, asset.id))
        path = self.root / ("render-proxy.mp4" if frames else "source-proxy.mp4")
        path.write_bytes(b"fixture silent complete-interval proxy " + str((asset.id, start, end)).encode())
        duration = (end-start)*1000*asset.fps.denominator//asset.fps.numerator if frames else end-start
        self.infos[str(path)] = ProbeMetadata(duration, asset.width, asset.height, asset.fps, False, {})
        return path

    def extract_video_frames(self, asset, start, end):
        return self._extract(asset, start, end, True)

    def extract_video_interval(self, asset, start, end):
        return self._extract(asset, start, end, False)

    def probe(self, path):
        return self.infos[str(path)]

    def probe_video_frames(self,path):
        info=self.probe(path)
        return {"frame_count":round(info.duration_ms*info.fps.numerator/(1000*info.fps.denominator)),
                "duration_ms":info.duration_ms,"fps":info.fps}


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    db = Database(tmp_path / "semantic-fixture.sqlite")
    db.data_root = tmp_path
    spec, plan, asset, clip = comparison_fixture()
    now, run_id = datetime.now(timezone.utc), uuid4()
    profile = IPProfileRepository(db).create(IPProfile(creator_name="Fixture"))
    ProjectRepository(db).create(Project(id=spec.project_id, ip_profile_id=profile.id, title="Fixture",
        topic="semantic comparison fixture", fps=spec.fps, created_at=now))
    source = tmp_path / asset.source_file
    source.parent.mkdir(parents=True)
    source.write_bytes(b"fixture source bytes, not a quality verdict")
    asset = AssetRepository(db).create(asset.model_copy(update={"content_hash":hashlib.sha256(source.read_bytes()).hexdigest()}))
    ClipRepository(db).create(clip)
    master_path = tmp_path / "master.wav"
    master_path.write_bytes(b"fixture independently admitted master")
    master = AudioAssetRepository(db).create(AudioAsset(id=spec.master_narration.audio_asset_id,
        source_file=master_path.name, content_hash=hashlib.sha256(master_path.read_bytes()).hexdigest(),
        duration_ms=spec.master_narration.end_ms, sample_rate=16000, channels=1,
        authorization_reference="fixture registered rights", imported_at=now,
        transcript_source=spec.master_narration.transcript_source, transcript_segments=spec.master_narration.transcript_segments))
    intro = plan.model_copy(update={"id":uuid4(),"scene_id":"intro","order":0,
        "voice_text":spec.master_narration.transcript_segments[0].text})
    ProjectDraftRepository(db).save(ProjectDraft(project_id=spec.project_id, version=1, script_revision=1,
        script=intro.voice_text+plan.voice_text, scenes=[intro,plan], updated_at=now))
    job = JobRepository(db).create(Job(project_id=spec.project_id, type=JobType.RENDER, status=JobStatus.COMPLETED,
        idempotency_key="fixture-render", created_at=now, updated_at=now,
        payload=RenderVideoJobPayload(project_id=spec.project_id, render_id=uuid4(), video_spec=spec)))
    output = tmp_path / "renders" / str(spec.project_id) / f"{job.payload.render_id}.mp4"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"fixture existing rendered output")
    db.connection.execute("""INSERT INTO production_runs
        (id,project_id,idempotency_key,preflight_fingerprint,draft_version,script_revision,
         style_tokens,repair_allowance,stage,render_job_id,created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (str(run_id), str(spec.project_id), "fixture-run", "a"*64,
        1, 1, "{}", 1, "render", str(job.id), now.isoformat()))
    BudgetPolicyRepository(db).save(BudgetPolicy(project_id=spec.project_id, max_calls=4,
        allow_unknown_cost=True, updated_at=now))
    # Existing PresentationRepairService tests cover full renderer/QA admission.
    # Here retain actual byte and draft checks while isolating this call boundary.
    def validate(service, row, current_spec):
        draft = ProjectDraftRepository(db).get(spec.project_id)
        if draft.version != row["draft_version"] or draft.script_revision != row["script_revision"]:
            raise ProductionRunNotReady(("presentation_draft_changed",))
        for value in (AssetRepository(db).get(asset.id), AudioAssetRepository(db).get(master.id)):
            actual = tmp_path / value.source_file
            if hashlib.sha256(actual.read_bytes()).hexdigest() != value.content_hash:
                raise ProductionRunNotReady(("fixture_source_bytes_changed",))
        return []
    monkeypatch.setattr(PresentationRepairService, "_validate", validate)
    media = FixtureMedia(tmp_path)
    service = PresentationRepairService(db, tmp_path, tmp_path / "renders")
    observation = service.observe(spec.project_id, run_id, PresentationObservationRequest(
        idempotency_key="semantic-observed", render_sha256=hashlib.sha256(output.read_bytes()).hexdigest(),
        scene_id=plan.scene_id, finding="semantic_mismatch", reason="fixture reported mismatch",
        evidence_reference="fixture:human-observation"))
    value = SimpleNamespace(db=db, project=spec.project_id, run=run_id, observation=observation, job=job,
        media=media, root=tmp_path, asset=asset, source=source, master=master, output=output)
    yield value
    db.close()


def diagnose(value, provider=None):
    return PresentationSemanticDiagnosis(value.db, value.root, provider, value.media, value.media).diagnose(
        value.project, value.run, value.observation["id"])


def current(value, result):
    row = value.db.connection.execute("SELECT * FROM production_runs WHERE id=?", (str(value.run),)).fetchone()
    return semantic_diagnosis_is_current(value.db, result, row)


@pytest.mark.parametrize('change',['wrong_scene_copy','partial_scene_copy','extra_copy_in_segment','ambiguous_segment','shifted_scene_audio'])
def test_full_master_qa_cannot_substitute_exact_scene_text_interval_before_call(prepared,change):
    value=prepared
    if change in {'wrong_scene_copy','partial_scene_copy'}:
        draft=ProjectDraftRepository(value.db).get(value.project)
        detail=draft.scenes[1].model_copy(update={'voice_text':'另一个场景的不同内容。' if change=='wrong_scene_copy' else '按实际需求选择参数'})
        ProjectDraftRepository(value.db).save(draft.model_copy(update={'version':2,'scenes':[draft.scenes[0],detail]}))
        value.db.connection.execute('UPDATE production_runs SET draft_version=2 WHERE id=?',(str(value.run),))
    elif change=='extra_copy_in_segment':
        segments=value.master.transcript_segments
        extra=segments[-1].model_copy(update={'text':segments[-1].text+'还有另外一项未声明的事实。'})
        AudioAssetRepository(value.db).update(value.master.model_copy(update={'transcript_segments':[segments[0],extra]}))
    elif change=='ambiguous_segment':
        segments=value.master.transcript_segments
        merged=segments[0].model_copy(update={'end_ms':segments[-1].end_ms,'text':''.join(segment.text for segment in segments)})
        AudioAssetRepository(value.db).update(value.master.model_copy(update={'transcript_segments':[merged]}))
    else:
        spec=value.job.payload.video_spec
        first,last=spec.scenes
        moved=first.narration_end_ms+40
        spec=spec.model_copy(update={'scenes':[first.model_copy(update={'narration_end_ms':moved}),
            last.model_copy(update={'narration_start_ms':moved})]})
        JobRepository(value.db).update(value.job.model_copy(update={'payload':value.job.payload.model_copy(update={'video_spec':spec})}))
    provider=FixtureProvider()
    result=diagnose(value,provider)
    assert result['assessment_state']=='unknown'
    assert provider.calls==0 and value.media.calls==[]
    assert ProviderCallRepository(value.db).list_for_project(value.project)==[]


@pytest.mark.parametrize("source,render,stage", [(True,False,"assembly"), (False,False,"material"),
                                                (False,True,"unknown"), (True,True,"unknown")])
def test_same_task_comparison_changes_actual_responsibility_and_preserves_evidence(prepared, source, render, stage):
    value = prepared
    provider = FixtureProvider(source=source, render=render)
    result = diagnose(value, provider)
    assert result["responsible_stage"] == stage, result
    assert result["assessment_state"] == ("unknown" if stage == "unknown" else "known")
    assert current(value, result)
    assert provider.calls == 1 and len(value.media.calls) == 2
    assert value.media.calls[0][:3] == (True, 30, 60)
    assert value.media.calls[1][:3] == (False, 1000, 2000)
    assert result["scope"]["scene_request"]["voice_text"] == "完整旁白：按实际需求选择参数，不承诺实测提升。"
    assert "audio_not_observed" in result["limitations"] and result["required_capability"]
    assert result["preserves"]["master_audio_id"] == str(value.master.id)
    assert result["input"]["videos"][0]["video_sha256"]
    assert "path" not in result["input"]["videos"][0]
    assert diagnose(value, provider) == result and provider.calls == 1
    calls = ProviderCallRepository(value.db).list_for_project(value.project)
    assert len(calls) == 1 and calls[0].status == "completed"
    assert calls[0].estimated_cost.amount is None and calls[0].actual_cost is None
    assert calls[0].input_tokens == 100 and calls[0].output_tokens == 20
    assert calls[0].result_payload["result"] == result["raw_result"]
    assert JobRepository(value.db).list() == [value.job]
    assert AudioAssetRepository(value.db).get(value.master.id) == value.master
    saved = json.loads(value.db.connection.execute("SELECT payload FROM presentation_observations").fetchone()[0])
    assert saved["request"] == value.observation["request"] and saved["source"] == "human_observation"


@pytest.mark.parametrize("change", ["quality_adequate", "subject_verified"])
def test_unknown_fields_do_not_become_semantic_failure_or_trigger_another_call(prepared, change):
    provider = FixtureProvider(change=change)
    result = diagnose(prepared, provider)
    assert result["state"] == "unknown" and result["responsible_stage"] == "unknown"
    assert diagnose(prepared, provider) == result and provider.calls == 1


def test_missing_video_provider_is_a_bound_terminal_unknown_without_any_call(prepared):
    result = diagnose(prepared)
    assert result["state"] == "unknown" and result["call_id"] is None
    assert result["required_capability"] == ["configured_video_comparison_provider"]
    assert current(prepared, result) and prepared.media.calls == []
    configured = FixtureProvider()
    assert diagnose(prepared, configured) == result and configured.calls == 0
    assert ProviderCallRepository(prepared.db).list() == []


@pytest.mark.parametrize("calls,unknown,reason", [(0,True,"call_limit"), (4,False,"unknown_cost")])
def test_budget_uses_existing_policy_and_unknown_estimate_without_claiming_zero(prepared,calls,unknown,reason):
    policy = BudgetPolicyRepository(prepared.db).get_by_project(prepared.project)
    BudgetPolicyRepository(prepared.db).save(policy.model_copy(update={"max_calls":calls, "allow_unknown_cost":unknown}))
    provider = FixtureProvider()
    result = diagnose(prepared, provider)
    assert result["stop_reasons"] == ["semantic_budget_"+reason], result
    assert provider.calls == 0 and ProviderCallRepository(prepared.db).list() == []
    assert prepared.media.calls == []
    assert diagnose(prepared, provider) == result and provider.calls == 0


def test_provider_failure_is_persisted_accounted_and_never_resent(prepared):
    provider = FixtureProvider(fail=True)
    result = diagnose(prepared, provider)
    assert result["state"] == "failed" and result["assessment_state"] == "unknown"
    assert result["call_status"] == "failed" and result["call_id"]
    assert diagnose(prepared, provider) == result and provider.calls == 1
    calls = ProviderCallRepository(prepared.db).list()
    assert len(calls) == 1 and calls[0].error_code == "semantic_video_timeout"
    assert calls[0].usage_observable is False and calls[0].actual_cost is None


def test_stopping_a_repair_retains_exact_diagnosis_but_cannot_start_new_inference(prepared):
    provider=FixtureProvider(source=False,render=False)
    first=diagnose(prepared,provider)
    prepared.db.connection.execute('UPDATE production_runs SET waiting_stop_reasons=? WHERE id=?',
        (json.dumps(['automatic_stop:presentation_joint_material_repair_not_supported']),str(prepared.run)))
    assert current(prepared,first)
    assert diagnose(prepared,provider)==first and provider.calls==1
    request=PresentationObservationRequest.model_validate(prepared.observation['request']).model_copy(update={
        'idempotency_key':'new-observation-while-stopped','reason':'fixture follow-up evidence'})
    service=PresentationRepairService(prepared.db,prepared.root,prepared.root/'renders')
    prepared.observation=service.observe(prepared.project,prepared.run,request)
    before=list(prepared.media.calls)
    result=diagnose(prepared,provider)
    assert result['stop_reasons']==['semantic_production_stopped']
    assert provider.calls==1 and prepared.media.calls==before


def test_second_observation_on_same_render_cannot_buy_another_diagnosis(prepared):
    provider = FixtureProvider()
    first = diagnose(prepared, provider)
    service = PresentationRepairService(prepared.db,prepared.root,prepared.root/"renders")
    request = PresentationObservationRequest.model_validate(prepared.observation["request"]).model_copy(update={
        "idempotency_key":"semantic-second", "reason":"additional observation"})
    second = service.observe(prepared.project,prepared.run,request)
    prepared.observation = second
    assert not current(prepared, first)
    extracted = list(prepared.media.calls)
    result = diagnose(prepared, provider)
    assert result["stop_reasons"] == ["semantic_render_call_limit"]
    assert provider.calls == 1 and len(ProviderCallRepository(prepared.db).list()) == 1
    assert prepared.media.calls == extracted


def test_version_change_during_success_keeps_consumed_usage_and_old_observation_stale(prepared):
    def changed():
        draft = ProjectDraftRepository(prepared.db).get(prepared.project)
        ProjectDraftRepository(prepared.db).save(draft.model_copy(update={"version":2, "script_revision":2, "script":"changed"}))
    provider = FixtureProvider(on_call=changed)
    result = diagnose(prepared, provider)
    assert result["stale"] and result["assessment_state"] == "unknown" and not current(prepared,result)
    assert result["raw_result"]["batch"]["clips"]
    call = ProviderCallRepository(prepared.db).list()[0]
    assert call.status == "completed" and call.input_tokens == 100
    assert diagnose(prepared,provider) == result and provider.calls == 1


@pytest.mark.parametrize("changed", ["render", "source", "master", "spec", "observation"])
def test_changed_bound_inputs_cannot_reactivate_previous_known_diagnosis(prepared,changed):
    provider = FixtureProvider()
    result = diagnose(prepared,provider)
    if changed in {"render","source"}:
        getattr(prepared,"output" if changed=="render" else "source").write_bytes(b"changed bytes")
    elif changed=="master":
        AudioAssetRepository(prepared.db).update(prepared.master.model_copy(update={"metadata":{"changed":True}}))
    elif changed=="spec":
        job = prepared.job
        spec = job.payload.video_spec.model_copy(update={"width":720})
        JobRepository(prepared.db).update(job.model_copy(update={"payload":job.payload.model_copy(update={"video_spec":spec})}))
    else:
        saved = json.loads(prepared.db.connection.execute("SELECT payload FROM presentation_observations").fetchone()[0])
        saved["request"]["reason"] = "changed"
        prepared.db.connection.execute("UPDATE presentation_observations SET payload=?",(json.dumps(saved),))
    if changed not in {"render","source"}:
        assert not current(prepared,result)
    stale = diagnose(prepared,provider)
    assert stale["stale"] and stale["responsible_stage"] == "unknown"
    assert provider.calls == 1 and len(ProviderCallRepository(prepared.db).list()) == 1


def test_lightweight_projection_never_reads_media(prepared,monkeypatch):
    result=diagnose(prepared,FixtureProvider())
    def forbidden(*args,**kwargs):
        raise AssertionError("status projection must not read bytes")
    monkeypatch.setattr(Path,"read_bytes",forbidden)
    assert current(prepared,result)


def test_interrupted_running_call_is_not_sent_again(prepared):
    def crash():
        raise KeyboardInterrupt("fixture process stop after claiming execution")
    provider=FixtureProvider(on_call=crash)
    with pytest.raises(KeyboardInterrupt):
        diagnose(prepared,provider)
    assert ProviderCallRepository(prepared.db).list()[0].status=="running"
    provider.on_call=None
    extracted=list(prepared.media.calls)
    result=diagnose(prepared,provider)
    assert result["call_status"]=="running" and result["assessment_state"]=="unknown"
    assert result["stop_reasons"]==["provider_execution_in_progress"]
    assert diagnose(prepared,provider)==result and provider.calls==1
    assert prepared.media.calls==extracted


def test_completed_ledger_replay_restores_projection_without_repeating_perception(prepared):
    provider=FixtureProvider()
    first=diagnose(prepared,provider)
    saved=json.loads(prepared.db.connection.execute("SELECT payload FROM presentation_observations").fetchone()[0])
    saved.pop("machine_diagnosis")
    prepared.db.connection.execute("UPDATE presentation_observations SET payload=?",(json.dumps(saved),))
    policy=BudgetPolicyRepository(prepared.db).get_by_project(prepared.project)
    BudgetPolicyRepository(prepared.db).save(policy.model_copy(update={"max_calls":0,"allow_unknown_cost":False}))
    restored=diagnose(prepared,provider)
    assert restored["call_id"]==first["call_id"] and restored["raw_result"]==first["raw_result"]
    assert restored["responsible_stage"]=="assembly" and provider.calls==1
    assert len(ProviderCallRepository(prepared.db).list())==1


def test_global_budget_is_not_bypassed_by_project_allowance(prepared):
    BudgetPolicyRepository(prepared.db).save(BudgetPolicy(max_calls=0,allow_unknown_cost=True,
        updated_at=datetime.now(timezone.utc)))
    provider=FixtureProvider()
    result=diagnose(prepared,provider)
    assert result["stop_reasons"]==["semantic_budget_call_limit"]
    assert provider.calls==0 and ProviderCallRepository(prepared.db).list()==[]
    assert prepared.media.calls==[]


def test_failed_ledger_without_projection_does_not_extract_or_retry(prepared):
    provider=FixtureProvider(fail=True)
    first=diagnose(prepared,provider)
    saved=json.loads(prepared.db.connection.execute("SELECT payload FROM presentation_observations").fetchone()[0])
    saved.pop("machine_diagnosis")
    prepared.db.connection.execute("UPDATE presentation_observations SET payload=?",(json.dumps(saved),))
    extracted=list(prepared.media.calls)
    result=diagnose(prepared,provider)
    assert result["call_id"]==first["call_id"] and result["call_status"]=="failed"
    assert result["stop_reasons"]==["provider_execution_previously_failed"]
    assert prepared.media.calls==extracted and provider.calls==1


def _automatic_parent(value):
    now=datetime.now(timezone.utc)
    parent=JobRepository(value.db).create(Job(project_id=value.project,type=JobType.PLAN_CONTENT,
        status=JobStatus.COMPLETED,idempotency_key="fixture-plan-parent",created_at=now,updated_at=now,
        payload=ContentPlanJobPayload(project_id=value.project,topic="fixture")))
    value.db.connection.execute("UPDATE production_runs SET automatic=1,start_job_id=? WHERE id=?",
        (str(parent.id),str(value.run)))
    return parent


@pytest.mark.parametrize("blocked",["failed_parent","missing_parent","render_not_completed","waiting_stop"])
def test_current_admission_stops_old_or_sealed_run_before_provider(prepared,blocked):
    parent=_automatic_parent(prepared)
    if blocked=="failed_parent":
        JobRepository(prepared.db).update(parent.model_copy(update={"status":JobStatus.FAILED}))
    elif blocked=="missing_parent":
        prepared.db.connection.execute("UPDATE production_runs SET start_job_id=NULL WHERE id=?",(str(prepared.run),))
    elif blocked=="render_not_completed":
        JobRepository(prepared.db).update(prepared.job.model_copy(update={"status":JobStatus.CANCELLED}))
    else:
        prepared.db.connection.execute("UPDATE production_runs SET waiting_stop_reasons=? WHERE id=?",
            (json.dumps(["automatic_stop:archived_fixture"]),str(prepared.run)))
    provider=FixtureProvider()
    result=diagnose(prepared,provider)
    assert result["assessment_state"]=="unknown" and provider.calls==0
    assert ProviderCallRepository(prepared.db).list()==[]


def test_parent_failure_during_success_marks_stale_without_rolling_back_usage(prepared):
    parent=_automatic_parent(prepared)
    provider=FixtureProvider(on_call=lambda:JobRepository(prepared.db).update(parent.model_copy(update={"status":JobStatus.FAILED})))
    result=diagnose(prepared,provider)
    assert result["stale"] and not current(prepared,result)
    assert ProviderCallRepository(prepared.db).list()[0].status=="completed"
    assert ProviderCallRepository(prepared.db).list()[0].input_tokens==100
    assert diagnose(prepared,provider)==result and provider.calls==1


def test_frame_short_proxy_does_not_reach_provider(prepared,monkeypatch):
    monkeypatch.setattr(prepared.media,"probe_video_frames",lambda path:{
        "frame_count":29,"fps":prepared.job.payload.video_spec.fps,"duration_ms":1000})
    provider=FixtureProvider()
    result=diagnose(prepared,provider)
    assert result["stop_reasons"]==["semantic_render_frame_evidence_invalid"]
    assert provider.calls==0 and ProviderCallRepository(prepared.db).list()==[]
