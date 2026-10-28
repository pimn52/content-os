"""A dispatched source-bound duration stays valid at the worker admission gate."""
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.db import Database
from app.domain.models import (
    ConsentRecord, Job, JobType, PlannedTalkingContext,
    TalkingGenerationJobPayload, TalkingPerformanceBrief,
)
from app.production_runs import (
    ProductionRunNotReady, ProductionRunService, TalkingSourceBinding,
    _snapshot_sha256, _talking_binding_sha256,
)


@pytest.mark.parametrize("verified_limit", [None, 3000, 6000])
@pytest.mark.parametrize("duration_changed", [False, True])
def test_worker_reopens_the_same_source_bounded_duration(monkeypatch, verified_limit, duration_changed):
    project_id, run_id, scene_id, job_id = (uuid4() for _ in range(4))
    capability = SimpleNamespace(
        id=uuid4(), provider="latentsync", model="LatentSync-1.5",
        runtime="local-runtime", machine_id="local-machine", scope_key="local-test",
        verified_parameters={} if verified_limit is None else {"fresh_voice_talking_duration_ms": verified_limit},
    )
    profile = SimpleNamespace(id=uuid4(), consent=ConsentRecord(
        subject_name="Creator", basis="explicit_authorization",
        confirmed=True, confirmed_at=datetime.now(timezone.utc),
        authorization_reference="profile-consent",
    ))
    master = SimpleNamespace(id=uuid4())
    binding = TalkingSourceBinding(
        scene_plan_id=scene_id, talking_profile_id=profile.id,
        reference_clip_id=uuid4(), reference_asset_id=uuid4(),
        reference_asset_hash="a" * 64, reference_start_ms=0, reference_end_ms=3200,
        source_authorization_reference="registered-source-rights",
        reference_assessment_reference="full-interval-review",
        performance_brief=TalkingPerformanceBrief(), capability_profile_id=capability.id,
        capability_evidence_reference="runtime-evidence", license_evidence_reference="model-license",
        master_audio_id=master.id, master_start_ms=0, master_end_ms=2760,
        authorization_reference="registered-source-rights", talking_job_id=job_id,
    )
    slice_plan = SimpleNamespace(start_segment_index=0, end_segment_index=1)
    run = SimpleNamespace(status="awaiting_talking_dependencies", talking_source_bindings=(binding,))

    with Database() as db:
        service = ProductionRunService(db, Path("."))
        monkeypatch.setattr(service, "get", lambda *_: run)
        monkeypatch.setattr(service, "_validated_talking_dispatch", lambda *_, **__: (
            binding, profile, master, capability, slice_plan,
        ))
        monkeypatch.setattr(service, "_talking_reference_window", lambda *args: (0, 3200))
        # This is the duration chosen by dispatch_talking: the source is also a ceiling.
        dispatched_limit = service._talking_duration_limit(capability, 3200)
        assert dispatched_limit == (3000 if verified_limit == 3000 else 3200)
        payload = TalkingGenerationJobPayload(
            project_id=project_id, talking_profile_id=profile.id,
            reference_clip_id=binding.reference_clip_id, narration_audio_id=master.id,
            authorization_reference=binding.authorization_reference,
            planned_context=PlannedTalkingContext(
                run_id=run_id, scene_plan_id=scene_id, capability_profile_id=capability.id,
                expected_provider=capability.provider, expected_model=capability.model,
                expected_runtime=capability.runtime, expected_machine_id=capability.machine_id,
                binding_sha256=_talking_binding_sha256(binding),
                consent_sha256=_snapshot_sha256(profile.consent.model_dump(mode="json")),
            ),
            slice_start_segment_index=0, slice_end_segment_index=1,
            slice_max_duration_ms=dispatched_limit + (1 if duration_changed else 0),
            slice_limit_source="reference-duration-attempt-limit",
            reference_window_start_ms=0, reference_window_end_ms=3200,
        )
        now = datetime.now(timezone.utc)
        job = Job(id=job_id, project_id=project_id, type=JobType.GENERATE_TALKING,
                  idempotency_key="source-duration", created_at=now, updated_at=now, payload=payload)
        identity = dict(provider=capability.provider, model=capability.model,
                        runtime=capability.runtime, machine_id=capability.machine_id)
        if duration_changed:
            with pytest.raises(ProductionRunNotReady) as exc:
                service.require_talking_job_admission(job, **identity)
            assert exc.value.reasons == ("planned_talking_execution_snapshot_changed",)
        else:
            service.require_talking_job_admission(job, **identity)
