from datetime import datetime, timezone
import json
from uuid import UUID, uuid4

import pytest

from app.db import (
    AssetRepository, BudgetPolicyRepository, ClipRepository, Database,
    IPProfileRepository, ProjectRepository, ProviderMachineCapabilityProfileRepository,
    TalkingProfileRepository, VoiceProfileRepository,
)
from app.domain.models import (
    Asset, BudgetPolicy, Clip, ConsentRecord, IPProfile, Project,
    ProviderMachineCapabilityProfile, RationalFps, TalkingProfile, VoiceProfile,
)
from app.planning_feasibility import (
    planning_feasibility, planning_identity_context, required_talking_reference_duration_ms,
    talking_reference_capacity,
)


def test_empty_read_only_brief_preserves_unknowns(tmp_path):
    with Database(tmp_path / "brief.sqlite") as db:
        before = db.connection.total_changes
        brief = planning_feasibility(db, uuid4())
        assert db.connection.total_changes == before
    for capability in brief["capabilities"].values():
        assert capability["evidence_state"] == "unknown"
        assert capability["estimated_cost"] == "unknown"
        assert capability["dispatch_authorized"] is False
    assert brief["budget_policies"] == [
        {"scope": "global", "state": "unknown"}, {"scope": "project", "state": "unknown"},
    ]
    assert brief["live_usage_observation"] == []


@pytest.mark.parametrize("readiness,quality,evidence,expected", [
    ("configured", "unknown", None, "unverified"),
    ("available", "observed", "private-evidence", "unverified"),
    ("verified", "unknown", "private-evidence", "unverified"),
    ("verified", "verified", "private-evidence", "verified"),
])
def test_profile_evidence_never_grants_execution_or_exposes_free_text(tmp_path, readiness, quality, evidence, expected):
    with Database(tmp_path / "brief.sqlite") as db:
        ProviderMachineCapabilityProfileRepository(db).save(ProviderMachineCapabilityProfile(
            capability="talking", mode="local", provider="local", model="model", runtime="runtime",
            machine_id="machine", readiness=readiness, quality_status=quality,
            evidence_reference=evidence, provenance_source="private-provenance",
            commercial_status="commercial_safe", license_evidence_reference="private-license",
            updated_at=datetime.now(timezone.utc),
        ))
        brief = planning_feasibility(db, uuid4())
    talking = brief["capabilities"]["talking"]
    assert talking["evidence_state"] == expected
    assert talking["current_execution_license"] == "unverified"
    assert talking["dispatch_authorized"] is False
    assert "private-" not in json.dumps(brief)


def test_both_budget_scopes_and_live_counter_identity_exclusion(tmp_path):
    project_id = uuid4()
    with Database(tmp_path / "brief.sqlite") as db:
        for scope_id, max_calls in ((None, 0), (project_id, 10)):
            BudgetPolicyRepository(db).save(BudgetPolicy(
                project_id=scope_id, max_calls=max_calls,
                allow_unknown_cost=False, updated_at=datetime.now(timezone.utc),
            ))
        brief = planning_feasibility(db, project_id)
    assert [row["remaining_calls"] for row in brief["live_usage_observation"]] == [0, 10]
    assert all(row["max_amount"] is None for row in brief["budget_policies"])
    context = {"production_feasibility": brief, "evidence_refs": ["ip:v1"]}
    identity = planning_identity_context(context)
    assert "live_usage_observation" not in identity["production_feasibility"]
    assert "live_usage_observation" in context["production_feasibility"]  # never mutate prompt
    changed_usage = {**context, "production_feasibility": {**brief, "live_usage_observation": []}}
    assert planning_identity_context(changed_usage) == identity
    changed_policy = {**context, "production_feasibility": {**brief, "budget_policies": []}}
    assert planning_identity_context(changed_policy) != identity


def _creator_project(db):
    ip = IPProfileRepository(db).create(IPProfile(creator_name="private-person"))
    project = ProjectRepository(db).create(Project(
        ip_profile_id=ip.id, title="private-project", topic="private-topic",
        fps=RationalFps(numerator=30, denominator=1), created_at=datetime.now(timezone.utc),
    ))
    return ip, project


def _reference(db, ip_id, duration_ms=3200, clip_id=None):
    asset = AssetRepository(db).create(Asset(
        source_file="private-path/private-key.mp4", content_hash=uuid4().hex * 2,
        duration_ms=duration_ms, width=1080, height=1920,
        fps=RationalFps(numerator=25, denominator=1),
        authorization_reference="private-rights", imported_at=datetime.now(timezone.utc),
        metadata={} if ip_id is None else {"ip_profile_id": str(ip_id)},
    ))
    return ClipRepository(db).create(Clip(
        id=clip_id or uuid4(), asset_id=asset.id, start_ms=0, end_ms=duration_ms,
        asset_duration_ms=duration_ms, talking_candidate=True,
        transcript="private-transcript", visual_description="private-description",
    ))


def _consent():
    return ConsentRecord(subject_name="private-person", basis="self", confirmed=True,
                         confirmed_at=datetime.now(timezone.utc))


def test_project_source_choice_exposes_finite_motion_once_without_inferring_identity(tmp_path):
    with Database(tmp_path / "selected.sqlite") as db:
        ip, project = _creator_project(db)
        clip = _reference(db, None)
        source = AssetRepository(db).get(clip.asset_id)
        profile = TalkingProfileRepository(db).create(TalkingProfile(name="private-talking", provider="local",
            reference_clip_ids=[clip.id], consent=_consent(), created_at=datetime.now(timezone.utc)))
        capability=ProviderMachineCapabilityProfileRepository(db).save(ProviderMachineCapabilityProfile(
            capability="talking",mode="local",provider="local",model="model",runtime="runtime",machine_id="machine",
            readiness="verified",quality_status="verified",evidence_reference="evidence",
            provenance_source="fixture",
            commercial_status="commercial_safe",license_evidence_reference="license",updated_at=datetime.now(timezone.utc)))
        binding={"reference_clip_id":str(clip.id),"talking_profile_id":str(profile.id),"reference_asset_hash":source.content_hash,
            "capability_profile_id":str(capability.id)}
        run_id=str(uuid4())
        db.connection.execute("""INSERT INTO production_runs
            (id,project_id,idempotency_key,preflight_fingerprint,draft_version,script_revision,
             style_tokens,repair_allowance,stage,created_at,talking_source_bindings)
            VALUES (?,?,?,'hash',1,1,'{}',0,'awaiting_talking',?,?)""",
            (run_id,str(project.id),run_id,datetime.now(timezone.utc).isoformat(),json.dumps([binding]*6)))
        changes=db.connection.total_changes
        brief=planning_feasibility(db,project.id)
        assert db.connection.total_changes == changes
        assert brief["selected_talking_sources"] == [{"profile_id":str(profile.id),"asset_id":str(source.id),
            "clip_id":str(clip.id),"start_ms":0,"end_ms":3200,"duration_ms":3200,
            "alignment_state":"unknown","max_narration_duration_ms":None}]
        assert brief["creator_options"]["talking"]["state"] == "unknown" # No invented IP binding.
        assert planning_feasibility(db,uuid4())["selected_talking_sources"] == []
        assert "private-" not in json.dumps(brief)
        db.connection.execute("UPDATE production_runs SET talking_source_bindings=? WHERE id=?",
            (json.dumps([{**binding,"reference_asset_hash":"changed"}]),run_id))
        assert planning_feasibility(db,project.id)["selected_talking_sources"] == []


def test_creator_hints_match_reference_identity_without_exposing_private_fields(tmp_path):
    with Database(tmp_path / "creator.sqlite") as db:
        ip, project = _creator_project(db)
        own, foreign, unknown = (_reference(db, identity) for identity in (ip.id, uuid4(), None))
        voice = VoiceProfileRepository(db).create(VoiceProfile(
            ip_profile_id=ip.id, name="private-voice", provider="private-provider",
            provider_profile_id="private-key", reference_clip_ids=[own.id],
            consent=_consent(), created_at=datetime.now(timezone.utc),
        ))
        IPProfileRepository(db).update(ip.model_copy(update={"default_voice_profile_id": voice.id}))
        matching = TalkingProfileRepository(db).create(TalkingProfile(
            name="private-talking", provider="private-provider", provider_profile_id="private-key",
            reference_clip_ids=[unknown.id, foreign.id, own.id, uuid4()],
            consent=_consent(), created_at=datetime.now(timezone.utc),
        ))
        TalkingProfileRepository(db).create(TalkingProfile(
            name="private-other", provider="private-provider", reference_clip_ids=[foreign.id],
            consent=_consent(), created_at=datetime.now(timezone.utc),
        ))
        before = db.connection.total_changes
        brief = planning_feasibility(db, project.id)
        assert db.connection.total_changes == before
    options = brief["creator_options"]
    assert options["voice"] == {"default_binding": "matched", "profile_id": str(voice.id),
                                "reference_state": "present", "reference_clip_count": 1}
    assert options["talking"] == {"state": "matched_references", "truncated": False, "references": [{
        "profile_id": str(matching.id), "clip_id": str(own.id), "duration_ms": 3200,
        "talking_candidate": True, "reference_assessment_present": False,
    }]}
    assert options["dispatch_authorized"] is False
    assert options["quality_state"] == "unverified"
    assert "private-" not in json.dumps(brief)
    assert str(foreign.id) not in json.dumps(options)
    assert str(unknown.id) not in json.dumps(options)


def test_missing_references_and_cross_ip_default_never_become_available(tmp_path):
    with Database(tmp_path / "creator.sqlite") as db:
        ip, project = _creator_project(db)
        voice = VoiceProfileRepository(db).create(VoiceProfile(
            ip_profile_id=uuid4(), name="voice", provider="local", provider_profile_id="local",
            reference_clip_ids=[uuid4()], consent=_consent(), created_at=datetime.now(timezone.utc),
        ))
        IPProfileRepository(db).update(ip.model_copy(update={"default_voice_profile_id": voice.id}))
        TalkingProfileRepository(db).create(TalkingProfile(
            name="talking", provider="local", reference_clip_ids=[uuid4()],
            consent=_consent(), created_at=datetime.now(timezone.utc),
        ))
        before = planning_feasibility(db, project.id)
        assert before["creator_options"]["voice"]["default_binding"] == "unknown"
        assert before["creator_options"]["talking"]["state"] == "unknown"
        assert str(voice.id) not in json.dumps(before["creator_options"])
        VoiceProfileRepository(db).update(voice.model_copy(update={"ip_profile_id": ip.id}))
        after = planning_feasibility(db, project.id)
        assert after["creator_options"]["voice"]["default_binding"] == "matched"
        assert after["creator_options"]["voice"]["reference_state"] == "incomplete"
        assert after["creator_options"]["voice"]["reference_clip_count"] == 0
        assert planning_identity_context({"production_feasibility": before}) != planning_identity_context({"production_feasibility": after})


def test_talking_hints_are_bounded_and_stable(tmp_path):
    with Database(tmp_path / "creator.sqlite") as db:
        ip, project = _creator_project(db)
        references = [_reference(db, ip.id, 3000 + index, UUID(int=index + 1)) for index in range(10)]
        TalkingProfileRepository(db).create(TalkingProfile(
            name="talking", provider="local", reference_clip_ids=[value.id for value in reversed(references)],
            consent=_consent(), created_at=datetime.now(timezone.utc),
        ))
        first = planning_feasibility(db, project.id)
        assert planning_feasibility(db, project.id) == first
    options = first["creator_options"]["talking"]
    assert options["truncated"] is True
    assert [value["duration_ms"] for value in options["references"]] == list(range(3000, 3008))


@pytest.mark.parametrize("reference_ms,capacity_ms", [(3200,3200),(3199,2560),(3600,3200),(1600,1280),(500,0)])
def test_real_adapter_window_alignment_is_derived_without_runtime_or_model_call(reference_ms,capacity_ms):
    from app.providers.maintained_talking import MaintainedLatentSyncProvider
    assert talking_reference_capacity(MaintainedLatentSyncProvider,reference_ms)=={
        "alignment_state":"known","max_narration_duration_ms":capacity_ms}
    assert required_talking_reference_duration_ms(MaintainedLatentSyncProvider,1600,3200)==1920
    assert required_talking_reference_duration_ms(MaintainedLatentSyncProvider,3201,3200) is None


def test_capacity_contract_uses_only_local_validator_without_assuming_model_frame_rate():
    from app.providers.talking import TalkingInputError
    calls=[]
    class Provider:
        def validate_reference_duration(self,narration_ms,reference_ms):
            calls.append((narration_ms,reference_ms))
            if narration_ms+75>reference_ms:
                raise TalkingInputError("reference does not cover provider context")
        def synthesize(self,*args,**kwargs):
            pytest.fail("planning capacity cannot call a model")
    provider=Provider()
    assert talking_reference_capacity(provider,3200)=={
        "alignment_state":"known","max_narration_duration_ms":3125}
    assert required_talking_reference_duration_ms(provider,2000,3200)==2075
    assert len(calls)<=2*(3200).bit_length()+1
    assert talking_reference_capacity(None,3200)=={
        "alignment_state":"unknown","max_narration_duration_ms":None}
    assert required_talking_reference_duration_ms(object(),2000,3200) is None


def test_selected_reference_capacity_requires_matching_injected_provider_and_remains_read_only(tmp_path):
    calls=[]
    class Provider:
        provider_name="local"
        model="model"
        def validate_reference_duration(self,narration_ms,reference_ms):
            calls.append((narration_ms,reference_ms))
            if narration_ms+75>reference_ms:
                raise ValueError("not covered")
    with Database(tmp_path/"aligned.sqlite") as db:
        ip,project=_creator_project(db)
        clip=_reference(db,ip.id)
        TalkingProfileRepository(db).create(TalkingProfile(name="talking",provider="local",
            reference_clip_ids=[clip.id],consent=_consent(),created_at=datetime.now(timezone.utc)))
        ProviderMachineCapabilityProfileRepository(db).save(ProviderMachineCapabilityProfile(
            capability="talking",mode="local",provider="local",model="model",runtime="runtime",machine_id="machine",
            readiness="verified",quality_status="verified",evidence_reference="evidence",provenance_source="fixture",
            commercial_status="commercial_safe",license_evidence_reference="license",updated_at=datetime.now(timezone.utc)))
        changes=db.connection.total_changes
        known=planning_feasibility(db,project.id,talking_provider=Provider())
        assert db.connection.total_changes==changes
        source=known["selected_talking_sources"][0]
        assert source["duration_ms"]==3200 and source["max_narration_duration_ms"]==3125
        assert source["alignment_state"]=="known"
        previous_calls=len(calls)
        other=Provider();other.model="other-model"
        unknown=planning_feasibility(db,project.id,talking_provider=other)
        assert unknown["selected_talking_sources"][0]["alignment_state"]=="unknown"
        assert unknown["selected_talking_sources"][0]["max_narration_duration_ms"] is None
        assert len(calls)==previous_calls
        assert planning_identity_context({"production_feasibility":known})!=planning_identity_context({"production_feasibility":unknown})
