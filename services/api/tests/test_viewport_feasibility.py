"""Read-only viewport facts preserve unknown quality and exact negative scope."""
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import pytest
from uuid import uuid4
from fastapi.testclient import TestClient
from app.db import AssetRepository, ClipRepository, Database
from app.domain.models import Asset, Clip, RationalFps, SourceCropWindow
from app.main import create_app
from app.media.crop_assessment import CropAssessment, CropAssessmentRepository, CropProposal, FaceBoxObservation


def seed(tmp_path, monkeypatch):
    root = tmp_path / 'data-root'; root.mkdir()
    source = root / 'source.media'; source.write_bytes(b'fixture media bytes, no actual video quality')
    path = root / 'test.sqlite'
    monkeypatch.setenv('CONTENT_OS_DATA_ROOT', str(root))
    asset = Asset(source_file='source.media', content_hash=hashlib.sha256(source.read_bytes()).hexdigest(), duration_ms=2000,
        width=1280, height=720, fps=RationalFps(numerator=30, denominator=1), authorization_reference='fixture-rights', imported_at=datetime.now(timezone.utc))
    clip = Clip(asset_id=asset.id, start_ms=0, end_ms=2000, asset_duration_ms=2000)
    with Database(path) as db:
        AssetRepository(db).create(asset); ClipRepository(db).create(clip)
    payload = dict(proposal=CropProposal(source_asset_id=asset.id, source_clip_id=clip.id, start_ms=0, end_ms=2000,
        crop_window=SourceCropWindow(x=100, y=0, width=450, height=600)).model_dump(mode='json'),
        source_content_hash=asset.content_hash, output_height=1440, source_subtitle_state='not_observed')
    return root, source, path, asset, clip, payload


def test_read_only_portable_source_and_geometric_metrics_do_not_admit(tmp_path, monkeypatch):
    root, source, path, asset, clip, payload = seed(tmp_path, monkeypatch)
    other = tmp_path / 'different-cwd'; other.mkdir(); monkeypatch.chdir(other)
    with TestClient(create_app(path)) as client:
        before = client.get('/assets').json()
        result = client.post(f'/assets/{asset.id}/viewport-feasibility', json=payload)
        assert result.status_code == 200, result.text
        value = result.json()
        assert value['status'] == 'needs_review' and value['stop_reasons'] == []
        assert value['source_hash_verified'] is True and value['crop_assessment'] == 'unknown'
        assert value['transform_admission'] == 'not_evaluated' and value['external_calls'] == 0
        assert value['retained_source_fraction'] == pytest.approx(450 * 600 / (1280 * 720))
        assert value['upscale_factor'] == pytest.approx(2.4)
        assert 'full_interval_face_head_clearance' in value['quality_unknowns']
        assert client.get(f'/assets/{asset.id}/media').content == source.read_bytes()
        assert client.get(f'/clips/{clip.id}/media').content == source.read_bytes()
        assert client.get('/assets').json() == before
        assert client.get(f'/assets/{asset.id}/clips').json()[0]['id'] == str(clip.id)


@pytest.mark.parametrize('change,expected', [
    ({'source_subtitle_state':'unknown'}, 'source_subtitles_unknown'),
    ({'output_height':1680}, 'crop_aspect_mismatch'),
    ({'source_content_hash':'b'*64}, 'source_identity_changed'),
    ({'proposal.end_ms':2001}, 'interval_outside_source_clip'),
    ({'proposal.crop_window.x':1000}, 'crop_outside_source'),
    ({'source_subtitle_state':'known_burned_in', 'known_subtitle_regions':[dict(x=0,y=500,width=1280,height=100)]}, 'known_subtitle_overlap'),
])
def test_specific_infeasibility_before_human_review(tmp_path, monkeypatch, change, expected):
    _, _, path, asset, _, payload = seed(tmp_path, monkeypatch)
    for key, value in change.items():
        parts = key.split('.'); target = payload
        for part in parts[:-1]: target = target[part]
        target[parts[-1]] = value
    with TestClient(create_app(path)) as client:
        result = client.post(f'/assets/{asset.id}/viewport-feasibility', json=payload).json()
        assert result['status'] == 'blocked' and expected in result['stop_reasons']
        assert result['transform_admission'] == 'not_evaluated'


def test_negative_is_exact_fixture_cannot_reject_and_actual_drift_is_reported(tmp_path, monkeypatch):
    _, source, path, asset, clip, payload = seed(tmp_path, monkeypatch)
    assessment = CropAssessment(proposal=CropProposal.model_validate(payload['proposal']), source_content_hash=asset.content_hash,
        evidence_class='fixture', method='supplied-face-box', method_version='1', coverage='sampled_frames',
        observations=(FaceBoxObservation(time_ms=100, face_box=SourceCropWindow(x=98,y=50,width=100,height=100), confidence=.9),), evidence_reference='fixture-only')
    with Database(path) as db: CropAssessmentRepository(db).create(assessment)
    with TestClient(create_app(path)) as client:
        endpoint = f'/assets/{asset.id}/viewport-feasibility'
        assert client.post(endpoint,json=payload).json()['crop_assessment'] == 'unknown'
        with Database(path) as db: CropAssessmentRepository(db).create(assessment.model_copy(update={'id':uuid4(),'evidence_class':'assisted_test'}))
        assert 'persisted_face_boundary_failure' in client.post(endpoint,json=payload).json()['stop_reasons']
        payload['proposal']['crop_window']['x']=90
        assert client.post(endpoint,json=payload).json()['crop_assessment']=='unknown'
        source.write_bytes(b'changed')
        result=client.post(endpoint,json=payload).json()
        assert result['source_hash_verified'] is False and 'source_bytes_changed' in result['stop_reasons']
        assert result['crop_assessment']=='unknown'


@pytest.mark.parametrize('portable', ['data-root/source.media','../outside.media','https://example.invalid/video.mp4'])
def test_portable_prefix_and_escapes_at_preview_boundary(tmp_path, monkeypatch, portable):
    _, source, path, asset, clip, _ = seed(tmp_path, monkeypatch)
    with Database(path) as db: AssetRepository(db).update(asset.model_copy(update={'source_file':portable}))
    with TestClient(create_app(path)) as client:
        for endpoint in [f'/assets/{asset.id}/media',f'/clips/{clip.id}/media']:
            response=client.get(endpoint)
            assert response.status_code == (200 if portable.startswith('data-root/') else 422)
            if response.status_code==200: assert response.content==source.read_bytes()
