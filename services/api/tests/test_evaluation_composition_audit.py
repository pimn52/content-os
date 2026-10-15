"""Narrow retained-render boundaries; no models, media tools or live database."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.db import AssetRepository, ClipRepository, Database, JobRepository, ProviderCallRepository
from app.domain.models import Job, JobStatus, JobType
from app.evaluation_composition import EvaluationCompositionService
from app.jobs.store import JobStore
from app.presentation_repair import PresentationRepairService
from app.production_runs import ProductionRunNotReady
from app.renderer.remotion import RemotionRenderer, UnauthorizedVisualError


@pytest.mark.parametrize('terminal', ['completed', 'failed', 'cancelled'])
def test_terminal_job_timestamp_remains_in_currentness_fence(tmp_path, terminal):
    with Database(tmp_path/'fence.sqlite3') as db:
        now = datetime.now(timezone.utc)
        job = Job(type=JobType.RENDER, created_at=now, updated_at=now, idempotency_key='audit-fence')
        JobRepository(db).create(job)
        store = JobStore(db)
        claimed = store.claim('fixture', timedelta(minutes=5), max_attempts=1, job_id=job.id)
        service = EvaluationCompositionService(db, tmp_path)
        before = service._token()
        assert store.heartbeat(claimed.id, 'fixture', timedelta(minutes=5))
        assert service._token() == before
        row = db.connection.execute('SELECT payload FROM jobs WHERE id=?', (str(job.id),)).fetchone()
        import json
        payload = json.loads(row['payload'])
        payload['status'] = terminal
        db.connection.execute('UPDATE jobs SET status=?,payload=? WHERE id=?', (terminal, json.dumps(payload), str(job.id)))
        terminal_token = service._token()
        db.connection.execute('UPDATE jobs SET updated_at=? WHERE id=?', ((now+timedelta(seconds=10)).isoformat(), str(job.id)))
        assert service._token() != terminal_token


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
        _fence=lambda _: pytest.fail('escaping output reached durable launch'))
    monkeypatch.setattr(module, 'EvaluationCompositionService', lambda *_: service)
    monkeypatch.setattr(JobRepository, 'get', lambda *_: job)
    monkeypatch.setattr(ProviderCallRepository, 'get_by_idempotency_key', lambda *_: SimpleNamespace(status='running'))
    with Database(root/'renderer.sqlite3') as db:
        renderer = RemotionRenderer(AssetRepository(db), ClipRepository(db), renderer_dir=root/'renderer')
        monkeypatch.setattr(renderer, '_render', lambda *_a, **_kw: pytest.fail('escaping output launched renderer'))
        with pytest.raises(UnauthorizedVisualError, match='composition_private_output_required'):
            renderer.render_composition(job.id, expected)
