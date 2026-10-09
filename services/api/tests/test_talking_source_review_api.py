"""V75b2b: ordinary source-review operations retain D024 authority."""
from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
import pytest

from app.db import Database, JobRepository, ProviderCallRepository
from app.main import create_app
from app.production_runs import _unique_scene_speech_span
from app.domain.models import TranscriptSegment
from app.talking.source_suitability import TalkingSourceSuitabilityRepository
from test_production_runs import _talking_assessment_payload, _talking_source_fixture


def _payload(source: Path, *, key: str = "managed-one", **changes) -> dict:
    return {
        "idempotency_key": key,
        "expected_source_content_hash": hashlib.sha256(source.read_bytes()).hexdigest(),
        "expected_start_ms": 0, "expected_end_ms": 5_000,
        "presentation": "source_native_portrait", "coverage": "full_interval_continuous",
        "face_head_clearance": "pass", "motion_continuity": "pass",
        "subtitle_clearance": "pass", "effective_quality": "pass",
        "findings": ["Reviewed the exact continuous portrait Clip; head and subtitles clear."],
        "confirmed_inspection": True, "adopt_for_reuse": True,
        **changes,
    }


def _reviewer(monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, str], dict[str, str]]:
    first = "review-key-with-more-than-thirty-two-characters-0001"
    second = "review-key-with-more-than-thirty-two-characters-0002"
    monkeypatch.setenv("CONTENT_OS_TALKING_REVIEW_KEY_HASHES", json.dumps({
        "reviewer-one": hashlib.sha256(first.encode()).hexdigest(),
        "reviewer-two": hashlib.sha256(second.encode()).hexdigest(),
    }))
    return {"x-content-os-talking-review-key": first}, {"x-content-os-talking-review-key": second}


def test_review_record_reuse_context_and_dispatch_without_copying_ids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    headers, _ = _reviewer(monkeypatch)
    path = tmp_path / "source-review.sqlite"
    with TestClient(create_app(path)) as client:
        project_id, waiting, binding, source = _talking_source_fixture(client, path)
        clip_id = binding["reference_clip_id"]
        context_url = f"/projects/{project_id}/production-runs/{waiting['id']}/talking-source-context"
        setup = client.get(context_url, params={"scene_plan_id": binding["scene_plan_id"]})
        assert setup.status_code == 200, setup.text
        assert (setup.json()["speech_start_ms"], setup.json()["speech_end_ms"]) == (0, 3000)
        assert setup.json()["master_audio_id"] == waiting["talking_master_audio_id"]
        assert client.get(f"/clips/{clip_id}/talking-source-reviews").json() == []
        review_url = f"/clips/{clip_id}/talking-source-reviews"
        assert client.post(review_url, json=_payload(source)).status_code == 403
        assert client.post(review_url, json=_payload(source), headers={"x-content-os-talking-review-key": "wrong"}).status_code == 403
        saved = client.post(review_url, json=_payload(source), headers=headers)
        assert saved.status_code == 201, saved.text
        result = saved.json()
        assert result["decision"] == "review_claimed_suitable" and result["admission"]["actor_id"] == "reviewer-one"
        assert result["assessment"]["evidence_class"] == "assisted_test"
        assert client.post(review_url, json=_payload(source), headers=headers).json() == result
        choices = client.get(review_url).json()
        assert len(choices) == 1 and choices[0]["state"] == "current"
        assert choices[0]["admission"]["id"] == result["admission"]["id"]
        evidence = client.get(f"/talking-reference-suitability-assessments/{result['assessment']['id']}/evidence")
        assert evidence.status_code == 200
        assert evidence.headers["content-type"].startswith("application/octet-stream")
        assert evidence.headers["x-content-sha256"] == result["evidence_sha256"]
        assert b"review-key" not in evidence.content and json.loads(evidence.content)["actor_id"] == "reviewer-one"
        bind = client.post(f"/projects/{project_id}/production-runs/{waiting['id']}/talking-source-bind", json=binding)
        assert bind.status_code == 200, bind.text
        applied = client.post(f"/projects/{project_id}/production-runs/{waiting['id']}/talking-suitability-apply", json={
            "scene_plan_id": binding["scene_plan_id"], "assessment_id": result["assessment"]["id"],
        })
        assert applied.status_code == 200, applied.text
        dispatched = client.post(f"/projects/{project_id}/production-runs/{waiting['id']}/talking-dispatch", json={
            "scene_plan_id": binding["scene_plan_id"], "source_admission_id": result["admission"]["id"],
        })
        assert dispatched.status_code == 200, dispatched.text
        with Database(path) as db:
            assert len(JobRepository(db).list()) == 1
            assert ProviderCallRepository(db).list_for_project(UUID(project_id)) == []


def test_negative_unknown_and_unadopted_reviews_never_create_receipt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    headers, _ = _reviewer(monkeypatch)
    path = tmp_path / "negative.sqlite"
    with TestClient(create_app(path)) as client:
        project_id, _, binding, source = _talking_source_fixture(client, path)
        url = f"/clips/{binding['reference_clip_id']}/talking-source-reviews"
        for key, changes, decision in (
            ("negative", {"face_head_clearance": "fail"}, "unusable"),
            ("unknown", {"coverage": "partial", "subtitle_clearance": "unknown"}, "unknown"),
            ("unadopted", {"adopt_for_reuse": False}, "review_claimed_suitable"),
        ):
            result = client.post(url, json=_payload(source, key=key, **changes), headers=headers)
            assert result.status_code == 201, result.text
            assert result.json()["decision"] == decision and result.json()["admission"] is None
        assert client.post(url, json={key: val for key, val in _payload(source, key="missing-adopt").items()
                                      if key != "adopt_for_reuse"}, headers=headers).status_code == 422
        assert client.post(url, json=_payload(source, key="missing-inspection", confirmed_inspection=False), headers=headers).status_code == 422
        with Database(path) as db:
            assert JobRepository(db).list() == []
            assert ProviderCallRepository(db).list_for_project(UUID(project_id)) == []


def test_review_replay_actor_conflict_stale_evidence_and_revocation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first, second = _reviewer(monkeypatch)
    path = tmp_path / "stale.sqlite"
    with TestClient(create_app(path)) as client:
        _, _, binding, source = _talking_source_fixture(client, path)
        url = f"/clips/{binding['reference_clip_id']}/talking-source-reviews"
        saved = client.post(url, json=_payload(source), headers=first).json()
        assert client.post(url, json=_payload(source), headers=second).status_code == 409
        assert client.post(url, json=_payload(source, findings=["different"]), headers=first).status_code == 409
        report = tmp_path / saved["assessment"]["evidence_reference"]
        original_report = report.read_bytes()
        report.write_bytes(report.read_bytes() + b"changed")
        listing = client.get(url).json()
        assert listing[0]["state"] == "evidence_changed"
        assert client.post(url, json=_payload(source), headers=first).status_code == 409
        assert client.post(f"/talking-source-admissions/{saved['admission']['id']}/revoke", json={
            "reason": "withdrawn",
        }, headers=first).status_code == 200
        report.write_bytes(original_report)
        assert client.get(url).json()[0]["state"] == "revoked"


def test_list_keeps_corrupt_entry_visible_without_hiding_other_reviews(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    headers, _ = _reviewer(monkeypatch)
    path = tmp_path / "corrupt.sqlite"
    with TestClient(create_app(path)) as client:
        _, _, binding, source = _talking_source_fixture(client, path)
        url = f"/clips/{binding['reference_clip_id']}/talking-source-reviews"
        good = client.post(url, json=_payload(source), headers=headers).json()
        with Database(path) as db:
            db.connection.execute(
                "INSERT INTO talking_source_suitability(id,idempotency_key,source_asset_id,source_clip_id,payload) VALUES(?,?,?,?,?)",
                (str(uuid4()), "corrupt", good["assessment"]["source_asset_id"],
                 binding["reference_clip_id"], "not json"),
            )
        choices = client.get(url)
        assert choices.status_code == 200, choices.text
        assert sorted(item["state"] for item in choices.json()) == ["corrupt_record", "current"]


def test_context_rejects_wrong_scope_and_does_not_guess_ambiguous_speech(tmp_path: Path) -> None:
    path = tmp_path / "context.sqlite"
    with TestClient(create_app(path)) as client:
        project_id, waiting, binding, _ = _talking_source_fixture(client, path)
        url = f"/projects/{project_id}/production-runs/{waiting['id']}/talking-source-context"
        assert client.get(url, params={"scene_plan_id": str(uuid4())}).status_code == 409
        assert client.get(f"/projects/{uuid4()}/production-runs/{waiting['id']}/talking-source-context",
                          params={"scene_plan_id": binding["scene_plan_id"]}).status_code == 404
        draft = client.get(f"/projects/{project_id}/draft").json()
        changed = client.put(f"/projects/{project_id}/draft", json={
            "script": draft["script"], "topic": "changed draft topic", "scenes": draft["scenes"],
        })
        assert changed.status_code == 200, changed.text
        assert client.get(url, params={"scene_plan_id": binding["scene_plan_id"]}).status_code == 409
        assert client.post(f"/projects/{project_id}/production-runs/{waiting['id']}/cancel").status_code == 200
        assert client.get(url, params={"scene_plan_id": binding["scene_plan_id"]}).status_code == 409
    scene = SimpleNamespace(id=uuid4(), voice_text="same words")
    aligned = SimpleNamespace(duration_ms=5_000, transcript_source="observed", transcript_segments=[
        TranscriptSegment(start_ms=200, end_ms=1200, text="same words"),
    ])
    assert _unique_scene_speech_span(aligned, [scene], scene.id) == (200, 1200)
    aligned.transcript_segments.append(TranscriptSegment(start_ms=1700, end_ms=2700, text="same words"))
    assert _unique_scene_speech_span(aligned, [scene], scene.id) is None
    second = SimpleNamespace(id=uuid4(), voice_text="next words")
    aligned.transcript_segments = [TranscriptSegment(start_ms=0, end_ms=3000, text="same words next words")]
    assert _unique_scene_speech_span(aligned, [scene, second], scene.id) is None


def test_unadopted_report_tampering_and_changed_source_stay_visible(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    headers, _ = _reviewer(monkeypatch)
    path = tmp_path / "unadopted.sqlite"
    with TestClient(create_app(path)) as client:
        _, _, binding, source = _talking_source_fixture(client, path)
        url = f"/clips/{binding['reference_clip_id']}/talking-source-reviews"
        body = _payload(source, adopt_for_reuse=False)
        saved = client.post(url, json=body, headers=headers).json()
        report = tmp_path / saved["assessment"]["evidence_reference"]
        report.write_bytes(report.read_bytes() + b"tampered")
        assert client.get(url).json()[0]["state"] == "evidence_changed"
        assert client.get(f"/talking-reference-suitability-assessments/{saved['assessment']['id']}/evidence").status_code == 409
        assert client.post(url, json=body, headers=headers).status_code == 409
        source.write_bytes(b"changed source")
        assert client.get(url).json()[0]["state"] == "source_stale"


def test_review_failure_rolls_back_record_and_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    headers, _ = _reviewer(monkeypatch)
    path = tmp_path / "atomic.sqlite"
    with TestClient(create_app(path)) as client:
        _, _, binding, source = _talking_source_fixture(client, path)
        url = f"/clips/{binding['reference_clip_id']}/talking-source-reviews"
        original = TalkingSourceSuitabilityRepository.create

        def fail_create(self, value):
            raise ValueError("injected persistence failure")

        monkeypatch.setattr(TalkingSourceSuitabilityRepository, "create", fail_create)
        failed = client.post(url, json=_payload(source), headers=headers)
        assert failed.status_code == 409
        assert client.get(url).json() == []
        directory = tmp_path / "talking-source-reviews"
        assert list(directory.iterdir()) == []
        monkeypatch.setattr(TalkingSourceSuitabilityRepository, "create", original)
        assert client.post(url, json=_payload(source), headers=headers).status_code == 201


def test_evidence_path_never_reads_outside_data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    headers, _ = _reviewer(monkeypatch)
    path = tmp_path / "path.sqlite"
    with TestClient(create_app(path)) as client:
        _, _, binding, source = _talking_source_fixture(client, path)
        url = f"/clips/{binding['reference_clip_id']}/talking-source-reviews"
        saved = client.post(url, json=_payload(source), headers=headers).json()
        report = tmp_path / saved["assessment"]["evidence_reference"]
        outside = tmp_path.parent / f"outside-review-{uuid4()}.json"
        outside.write_bytes(b"outside evidence")
        legacy = {**saved["assessment"], "id": str(uuid4()), "idempotency_key": "outside-report",
                  "evidence_reference": f"../{outside.name}"}
        assert client.post("/talking-reference-suitability-assessments", json=legacy).status_code == 201
        assert client.post("/talking-reference-suitability-assessments", json={
            **legacy, "idempotency_key": f"managed:{uuid4()}",
        }).status_code == 422
        assert client.post("/talking-reference-suitability-assessments", json={
            **legacy, "idempotency_key": f"other-{uuid4()}",
            "evidence_reference": saved["assessment"]["evidence_reference"],
        }).status_code == 422
        assert client.post("/talking-reference-suitability-assessments", json={
            **legacy, "idempotency_key": f"other-{uuid4()}",
            "evidence_reference": saved["assessment"]["evidence_reference"].replace("/", "\\"),
        }).status_code == 422
        assert any(item["state"] == "evidence_unavailable" for item in client.get(url).json())
        assert client.get(f"/talking-reference-suitability-assessments/{legacy['id']}/evidence").status_code == 409
        report.unlink()
        try:
            report.symlink_to(outside)
        except (OSError, NotImplementedError):
            return  # Portable path-escape coverage above still applies without symlink privileges.
        assert all(item["state"] == "evidence_unavailable" for item in client.get(url).json())
        assert client.get(f"/talking-reference-suitability-assessments/{saved['assessment']['id']}/evidence").status_code == 409


def test_existing_trusted_assisted_review_is_discoverable_without_new_review(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    headers, _ = _reviewer(monkeypatch)
    path = tmp_path / "legacy-adoption.sqlite"
    with TestClient(create_app(path)) as client:
        _, waiting, binding, _ = _talking_source_fixture(client, path)
        bound = client.post(f"/projects/{waiting['project_id']}/production-runs/{waiting['id']}/talking-source-bind",
                            json=binding).json()["talking_source_bindings"][0]
        evidence = tmp_path / "retained-review.json"
        evidence.write_bytes(b'{"review":"already answered"}')
        claim = client.post("/talking-reference-suitability-assessments", json={
            **_talking_assessment_payload(bound, key="retained-existing-review"),
            "evidence_reference": evidence.name,
        })
        assert claim.status_code == 201, claim.text
        adopted = client.post(f"/talking-reference-suitability-assessments/{claim.json()['id']}/admissions", json={
            "idempotency_key": "retained-adoption", "evidence_sha256": hashlib.sha256(evidence.read_bytes()).hexdigest(),
        }, headers=headers)
        assert adopted.status_code == 201, adopted.text
        choices = client.get(f"/clips/{bound['reference_clip_id']}/talking-source-reviews").json()
        assert len(choices) == 1 and choices[0]["state"] == "current"
        assert choices[0]["assessment"]["id"] == claim.json()["id"]
        assert choices[0]["admission"]["id"] == adopted.json()["id"]


def test_concurrent_review_requests_reuse_one_receipt_after_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    headers, _ = _reviewer(monkeypatch)
    path = tmp_path / "concurrent.sqlite"
    with TestClient(create_app(path)) as client:
        _, _, binding, source = _talking_source_fixture(client, path)
    url = f"/clips/{binding['reference_clip_id']}/talking-source-reviews"
    body = _payload(source)

    def submit() -> tuple[int, dict]:
        with TestClient(create_app(path)) as caller:
            result = caller.post(url, json=body, headers=headers)
            return result.status_code, result.json()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(lambda _: submit(), range(2)))
    assert first[0] == second[0] == 201
    assert first[1]["assessment"]["id"] == second[1]["assessment"]["id"]
    assert first[1]["admission"]["id"] == second[1]["admission"]["id"]
    with TestClient(create_app(path)) as reopened:
        choices = reopened.get(url).json()
        assert len(choices) == 1 and choices[0]["state"] == "current"
    assert len(list((tmp_path / "talking-source-reviews").glob("*.json"))) == 1
