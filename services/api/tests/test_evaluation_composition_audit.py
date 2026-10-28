"""Narrow retained-render boundaries; no models, media tools or live database."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
import hashlib

import pytest

from app.db import AssetRepository, ClipRepository, Database, JobRepository, ProviderCallRepository
from app.domain.models import Job, JobStatus, JobType
from app.domain.models import (ExecutionUseIdentity, ProviderUseAdmission, ProviderUseLicenseReview)
from app.evaluation_composition import EvaluationCompositionService
from app.execution_admission import UseAdmissionError
from app.execution_scope import ExecutionUseBinding, ExecutionUseScope
from app.jobs.lease_fence import owned_job_lease
from app.jobs.store import JobStore
from app.presentation_repair import PresentationRepairService
from app.production_runs import ProductionRunNotReady
from app.renderer.remotion import RemotionRenderer, UnauthorizedVisualError


def test_execution_fence_ignores_heartbeat_but_rejects_lease_owner_change(tmp_path, monkeypatch):
    with Database(tmp_path/'fence.sqlite3') as db:
        now = datetime.now(timezone.utc)
        job = Job(type=JobType.RENDER, created_at=now, updated_at=now, idempotency_key='audit-fence')
        JobRepository(db).create(job)
        store = JobStore(db)
        claimed = store.claim('fixture', timedelta(minutes=5), max_attempts=1, job_id=job.id)
        service = EvaluationCompositionService(db, tmp_path)
        monkeypatch.setattr(service, 'require_current_job', lambda current: current)
        with owned_job_lease(store,claimed,'fixture'):
            assert store.heartbeat(claimed.id, 'fixture', timedelta(minutes=5))
            assert service.require_current_execution(claimed).id == claimed.id
            db.connection.execute("UPDATE jobs SET lease_owner='other' WHERE id=?", (str(job.id),))
            with pytest.raises(UseAdmissionError, match='evaluation_talking_lease_lost'):
                service.require_current_execution(claimed)


def test_source_revocation_is_rechecked_without_database_wide_fence(tmp_path, monkeypatch):
    project=uuid4();capability=uuid4();admission_id=uuid4()
    identity=ExecutionUseIdentity(capability='talking',mode='local',provider='fixture',model='fixture',
        runtime='fixture',machine_id='fixture',model_artifact_sha256='a'*64)
    terms=b'synthetic terms'
    report=ProviderUseLicenseReview(evidence_class='operator_verified',project_id=project,purpose='internal_evaluation',capability_profile_id=capability,
        configuration_sha256='b'*64,identity=identity,intended_activity='isolated test',license_source='fixture',
        license_version='1',license_terms_reference='terms.txt',license_terms_sha256=hashlib.sha256(terms).hexdigest(),
        permits_intended_activity=True,permitted_operations=['review'],findings=['synthetic only'])
    raw=report.model_dump_json().encode()
    admission=ProviderUseAdmission(id=admission_id,project_id=project,idempotency_key='fixture',
        review_reference='review.json',review_sha256=hashlib.sha256(raw).hexdigest(),review=report,
        actor_id='fixture-operator',created_at=datetime.now(timezone.utc))
    binding=ExecutionUseBinding(admission_id=admission_id,capability_profile_id=capability,
        review_sha256=admission.review_sha256,configuration_sha256=report.configuration_sha256,identity=identity)
    scope=ExecutionUseScope(project_id=project,purpose='internal_evaluation',bindings=(binding,))
    with Database(tmp_path/'authority.sqlite3') as db:
        service=EvaluationCompositionService(db,tmp_path)
        monkeypatch.setattr(service.admissions,'get',lambda _project,_id: admission)
        monkeypatch.setattr(service.admissions,'_file',lambda reference,**_: raw if reference=='review.json' else terms)
        service.require_retained_source_authority(project,{'scope':scope})
        unrelated=db.connection.execute("INSERT INTO content_opportunities(id,dedupe_key,status,created_at,payload) VALUES ('unrelated','unrelated','new','now','{}')")
        assert unrelated.rowcount==1
        revoked=admission.model_copy(update=dict(revoked_at=datetime.now(timezone.utc),revoked_by='fixture-operator',
            revocation_reason='synthetic revocation'))
        monkeypatch.setattr(service.admissions,'get',lambda _project,_id: revoked)
        with pytest.raises(UseAdmissionError,match='provider_use_admission_revoked'):
            service.require_retained_source_authority(project,{'scope':scope})


@pytest.mark.parametrize('case', ['missing', 'escaping_resolved_target'])
def test_repair_refuses_invalid_output_before_measurement(tmp_path, monkeypatch, case):
    from app import presentation_repair
    job = SimpleNamespace(project_id=uuid4(), payload=SimpleNamespace(render_id=uuid4(), video_spec=None))
    root = tmp_path/'renders'
    expected = root/str(job.project_id)/f'{job.payload.render_id}.mp4'
    if case == 'escaping_resolved_target':
        expected.parent.mkdir(parents=True)
        expected.write_bytes(b'fixture')
        original = Path.resolve
        monkeypatch.setattr(Path, 'resolve', lambda self, *a, **kw: tmp_path/'outside.mp4' if self == expected else original(self, *a, **kw))
    monkeypatch.setattr(presentation_repair, 'measure_render_output', lambda *_: pytest.fail('invalid output reached media measurement'))
    with Database(tmp_path/'repair.sqlite3') as db:
        service = PresentationRepairService(db, tmp_path, root)
        with pytest.raises(ProductionRunNotReady, match='presentation_render_file_missing'):
            service.verify_output(job)


def test_composition_staged_target_cannot_resolve_outside_root(tmp_path, monkeypatch):
    import app.evaluation_composition as module
    root = tmp_path/'data'
    root.mkdir()
    expected = root/'private'/'staged.mp4'
    outside = tmp_path/'outside.mp4'
    original = Path.resolve
    monkeypatch.setattr(Path, 'resolve', lambda self, *a, **kw: outside if self == expected else original(self, *a, **kw))
    job = SimpleNamespace(id=uuid4(), project_id=uuid4(), status=JobStatus.RUNNING, attempt=1)
    owner = dict(batch_id=str(uuid4()), run_id=str(uuid4()), job_sha256='a'*64)
    service = SimpleNamespace(require_current_job=lambda _: dict(token='fixture', spec=None),
        require_job_identity=lambda _: (None, owner, None), output_path=lambda _: expected.with_name('verified.mp4'),
        provider_call_key=lambda _: 'fixture-call',
        _fence=lambda _: pytest.fail('escaping output reached durable launch'))
    monkeypatch.setattr(module, 'EvaluationCompositionService', lambda *_: service)
    monkeypatch.setattr(JobRepository, 'get', lambda *_: job)
    monkeypatch.setattr(ProviderCallRepository, 'get_by_idempotency_key', lambda *_: SimpleNamespace(status='running'))
    with Database(root/'renderer.sqlite3') as db:
        renderer = RemotionRenderer(AssetRepository(db), ClipRepository(db), renderer_dir=root/'renderer')
        monkeypatch.setattr(renderer, '_render', lambda *_a, **_kw: pytest.fail('escaping output launched renderer'))
        with pytest.raises(UnauthorizedVisualError, match='composition_private_output_required'):
            renderer.render_composition(job.id, expected)
