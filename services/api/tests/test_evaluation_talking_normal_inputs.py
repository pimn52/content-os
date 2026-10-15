"""Normal saved Master/source resolution, fake Voice/ASR children only.

No _context/_receipt/_facts/input-rights stubs. This first bounded slice ends
at a deliberately absent Talking execution receipt, never dispatches Talking.
"""
from datetime import datetime, timezone
import hashlib
from types import SimpleNamespace
from uuid import uuid4
import pytest
from app.db import (AssetRepository, ClipRepository, ProjectDraftRepository,
    TalkingProfileRepository, ProviderMachineCapabilityProfileRepository, JobRepository)
from app.domain.models import (Asset, Clip, RationalFps, SourceKind, TalkingProfile,
    TalkingReferenceAssessment, ConsentRecord, ProviderMachineCapabilityProfile, VisualIntent)
from app.domain.evaluation_talking import TalkingSelection
from app.evaluation_talking import EvaluationTalkingService
from app.execution_admission import UseAdmissionError
from app.talking.source_suitability import TalkingSourceSuitabilityAssessment, TalkingSourceSuitabilityRepository
from app.talking.source_admission import TalkingSourceAdmissionRepository
from test_evaluation_master import prepare, admit, start, qa_case, bound_job, child_case, setup, candidate
from test_maintained_talking_execution import talking_case
from app.providers.maintained_talking_execution import observe_talking, observe_talking_inventory
from app.runtime_inventory import RuntimeInventoryStore


@pytest.fixture
def normal_inputs(qa_case,talking_case,request):
    q=qa_case
    master,selected,project=prepare(q)
    drafts=ProjectDraftRepository(q.c.db)
    old=drafts.get(project)
    scene=old.scenes[0].model_copy(update={"preferred_sources":[SourceKind.AI_VIDEO],"fallback_sources":[],
        "visual_intent":VisualIntent(subject="creator",action="speaking")})
    drafts.save(old.model_copy(update={"version":old.version+1,"scenes":[scene]}))
    root=q.c.root
    source=root/"normal-source.mp4"; source.write_bytes(b"synthetic portrait source")
    now=datetime.now(timezone.utc)
    asset=Asset(source_kind=SourceKind.USER_ASSET,source_file=source.name,content_hash=hashlib.sha256(source.read_bytes()).hexdigest(),
        duration_ms=10000,width=1080,height=1920,fps=RationalFps(numerator=25,denominator=1),has_audio=True,
        authorization_reference="fixture-only:person/source rights",imported_at=now)
    AssetRepository(q.c.db).create(asset)
    clip=Clip(asset_id=asset.id,start_ms=0,end_ms=10000,asset_duration_ms=10000,talking_candidate=True,
        face_visibility=.95,mouth_visibility=.95,talking_reference_assessment=TalkingReferenceAssessment(
            burned_in_subtitles=False,evidence_reference="fixture-only:source observation"))
    ClipRepository(q.c.db).create(clip)
    profile=TalkingProfile(name="Synthetic normal creator",provider="latentsync",reference_clip_ids=[clip.id],
        consent=ConsentRecord(subject_name="Creator",basis="self",confirmed=True,confirmed_at=now),created_at=now)
    TalkingProfileRepository(q.c.db).create(profile)
    dual = "engine" in talking_case.options
    if dual:
        import shutil
        from pathlib import Path
        from app.providers.maintained_talking_dual import observe_dual, observe_application_inventory, observe_engine_inventory
        options=dict(talking_case.options)
        for label in ("vendor","hf","torch","tools","engine"):
            target=root/label;shutil.copytree(options[label],target);options[label]=target
        checkpoint=root/"checkpoint.pt";checkpoint.write_bytes(Path(options["checkpoint"]).read_bytes());options["checkpoint"]=checkpoint
        options.update(data_root=root,staging_parent=root/"execution-staging")
        options["staging_parent"].mkdir(exist_ok=True)
        (root/talking_case.origin_path.name).write_bytes(talking_case.origin_path.read_bytes())
        talking_case.options=options
        spec=observe_dual(options)
        store=RuntimeInventoryStore(root)
        store.install_v2(observe_application_inventory(options["environment"],options["base"],max_bytes=options["max_bytes"]))
        store.install_v2(observe_engine_inventory(options["engine"],max_bytes=options["max_bytes"]))
    else:
        spec=observe_talking(talking_case.options)
    (root/talking_case.origin_path.name).write_bytes(talking_case.origin_path.read_bytes())
    if not dual:
        RuntimeInventoryStore(root).install_v2(observe_talking_inventory(talking_case.options["environment"],
            talking_case.options["base"],max_bytes=talking_case.options["max_bytes"]))
    cap=ProviderMachineCapabilityProfile(capability="talking",mode="local",provider=spec.provider,model=spec.model,
        runtime=spec.runtime,machine_id=spec.machine_id,readiness="verified",quality_status="verified",
        verified_parameters={"fresh_voice_talking_duration_ms":10000,**({"talking_execution_sha256":spec.execution_sha256 if getattr(talking_case,"quality_binding",True) else "0"*64} if dual else {})},provenance_source="synthetic-test-installation",
        evidence_reference="synthetic-only:capability",commercial_status="non_commercial_only",updated_at=now)
    if getattr(request,"param",None) == "calibration":
        cap=cap.model_copy(update={"readiness":"configured","quality_status":"unknown","verified_parameters":{}})
    ProviderMachineCapabilityProfileRepository(q.c.db).save(cap)
    evidence=root/"normal-source-review.txt"; evidence.write_bytes(b"synthetic exact source judgment")
    assessment=TalkingSourceSuitabilityAssessment(idempotency_key="normal-source",source_asset_id=asset.id,source_clip_id=clip.id,
        source_content_hash=asset.content_hash,start_ms=0,end_ms=10000,presentation="source_native_portrait",
        evidence_class="assisted_test",method="human_full_interval_review",method_version="fixture-only",
        coverage="full_interval_continuous",face_head_clearance="pass",motion_continuity="pass",subtitle_clearance="pass",
        effective_quality="pass",reviewer_reference="fixture-only:operator",evidence_reference=evidence.name)
    TalkingSourceSuitabilityRepository(q.c.db,root).create(assessment)
    sources=TalkingSourceAdmissionRepository(q.c.db,root)
    receipt=sources.admit(assessment.id,idempotency_key="normal-source-use",actor_id="fixture-operator",
        evidence_sha256=hashlib.sha256(evidence.read_bytes()).hexdigest())
    # Configure the separate synthetic Talking allowance before freezing the
    # Master plan. Changing budget policy afterward rightly invalidates it.
    from app.db import BudgetPolicyRepository, ProviderCallRepository
    from app.domain.models import BudgetPolicy
    calls=ProviderCallRepository(q.c.db)
    for budget_project,count in ((None,len(calls.list_all())),(project,len(calls.list_for_project(project)))):
        BudgetPolicyRepository(q.c.db).save(BudgetPolicy(project_id=budget_project,max_calls=count+1,
            allow_unknown_cost=True,updated_at=now))
    selected,_=admit(q,master,selected,project)
    parent=master.create(project,start(master,project,selected))
    assert parent.evaluation_master["current_eligible"], parent.evaluation_master
    assert master._context(project,selected)["plan"].scenes[0].production_need == "new_talking"
    selection=TalkingSelection(master_run_id=parent.id,scene_plan_id=scene.id,talking_profile_id=profile.id,
        reference_clip_id=clip.id,source_admission_id=receipt.id,capability_profile_id=cap.id,
        execution_admission_id=uuid4(),authorization_reference="fixture-only:exact transform",brief={})
    return SimpleNamespace(service=EvaluationTalkingService(q.c.db,root),db=q.c.db,root=root,project=project,
        parent=parent,selection=selection,profile=profile,clip=clip,asset=asset,cap=cap,sources=sources,receipt=receipt,spec=spec,
        talking_options=talking_case.options)


def test_normal_inputs_reach_separate_execution_gate_without_enqueue(normal_inputs):
    c=normal_inputs
    before=c.db.connection.total_changes
    with pytest.raises(UseAdmissionError,match="maintained_talking_execution_admission_required"):
        c.service._context(c.project,c.selection)
    binding=c.service.masters.records.binding(c.parent.id)
    old_ids=[binding.admission_id,*[b.admission_id for b in c.service.scopes.get("run",c.parent.id).bindings]]
    for identity in old_ids:
        with pytest.raises(UseAdmissionError,match="maintained_talking_execution_admission_required"):
            c.service._context(c.project,c.selection.model_copy(update={"execution_admission_id":identity}))
    assert c.db.connection.total_changes == before
    assert c.db.connection.execute("SELECT COUNT(*) FROM evaluation_talking_operations").fetchone()[0] == 0
    c.sources.revoke(c.receipt.id,actor_id="fixture-operator",reason="synthetic source withdrawal")
    before=c.db.connection.total_changes
    with pytest.raises(ValueError,match="revoked"):
        c.service._context(c.project,c.selection)
    assert c.db.connection.total_changes == before


@pytest.mark.parametrize("mutation,reason",[("consent","talking_profile_consent_required"),
    ("quality","evaluation_talking_master_not_eligible"),("duration","evaluation_talking_master_not_eligible"),
    ("short_source","talking_reference_alignment_not_covered"),("subtitles","talking_reference_fit_not_verified"),
    ("side_profile","talking_reference_fit_not_verified"),("rights","authorization_reference")])
def test_normal_input_negatives_stop_before_execution(normal_inputs,mutation,reason):
    c=normal_inputs
    if mutation == "consent":
        TalkingProfileRepository(c.db).delete(c.profile.id)
    elif mutation == "quality":
        ProviderMachineCapabilityProfileRepository(c.db).save(c.cap.model_copy(update={"quality_status":"unknown"}))
    elif mutation == "duration":
        ProviderMachineCapabilityProfileRepository(c.db).save(c.cap.model_copy(update={"verified_parameters":{}}))
    elif mutation == "short_source":
        ClipRepository(c.db).update(c.clip.model_copy(update={"end_ms":1}))
    elif mutation == "side_profile":
        ClipRepository(c.db).update(c.clip.model_copy(update={"face_visibility":0,"mouth_visibility":0}))
    elif mutation == "rights":
        AssetRepository(c.db).update(c.asset.model_copy(update={"authorization_reference":" "}))
    else:
        ClipRepository(c.db).update(c.clip.model_copy(update={"talking_reference_assessment":c.clip.talking_reference_assessment.model_copy(update={"burned_in_subtitles":True})}))
    # Missing rights is invalid already in the persisted Asset contract;
    # corruption must fail before the later Talking-specific rights check.
    with pytest.raises(ValueError if mutation == "rights" else UseAdmissionError,match=reason):
        c.service._context(c.project,c.selection)
