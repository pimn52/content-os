"""Offline aggregate-policy fixtures; no real creator judgments or inference."""
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.db import (
    AssetRepository, Database, JobRepository,
    TalkingSliceSeriesRepository,
    ProviderMachineCapabilityProfileRepository, ClipRepository,
)
from app.domain.models import PlannedTalkingRunOrigin, JobStatus
from app.main import create_app
from app.production_runs import _snapshot_sha256
from app.talking.admission import talking_visual_blocker
from app.talking.planned_admission import PlannedTalkingAdmissionService
from test_production_runs import _fixture_reviewable_planned_preview, _talking_source_fixture


DIMENSIONS = dict.fromkeys(("visible_sync", "identity", "artifacts", "source_performance", "continuity", "publishability"), "pass")


def test_v1_origin_golden_canonical_digest():
    # Independent schema-34 fixture: adding default fields to v1 must fail this test.
    raw = {"schema_version": "0.1", "version": 1}
    raw.update(dict.fromkeys(("run_id", "scene_plan_id", "generation_job_id", "qa_job_id", "output_asset_id", "master_audio_id", "source_clip_id", "source_admission_id"), "00000000-0000-0000-0000-000000000001"))
    raw.update(dict.fromkeys(("binding_sha256", "plan_fingerprint", "qa_report_sha256", "output_sha256", "master_sha256", "master_review_sha256", "source_sha256", "consent_sha256", "child_review_sha256"), "0" * 64))
    raw.update(master_start_ms=0, master_end_ms=1000, source_start_ms=0, source_end_ms=1000,
               authorization_reference="rights", license_evidence_reference="license")
    origin = PlannedTalkingRunOrigin.model_validate(raw)
    assert origin.model_dump(mode="json") == raw
    assert _snapshot_sha256(origin.model_dump(mode="json")) == "3bc2afb76b31ecad724f8fed6d8292dd4eac3c23aed66139e911a14fe7ea3f60"


def fixture(client, path, monkeypatch, version=2):
    return _fixture_reviewable_planned_preview(client, path, monkeypatch, policy_version=version)


def root(project, binding):
    return f"/projects/{project}/talking-slice-series/{binding['talking_series_id']}"


def judgment(preview, **changes):
    return {"approved": True, "evidence_reference": "fixture:whole-preview", "findings": [],
            "preview_asset_id": str(preview.id), "preview_sha256": preview.content_hash,
            "review_policy_version": 2, "dimensions": dict(DIMENSIONS), **changes}


def blocker(path, project, admitted):
    with Database(path) as db:
        assets, clips = AssetRepository(db), ClipRepository(db)
        return talking_visual_blocker(assets.get(UUID(admitted["assembled_asset_id"])),
                                      clips.get(UUID(admitted["assembled_clip_id"])), assets,
                                      project_id=UUID(project), copy="A fresh creator point.")


def concern(client, url, preview, key="local-1", end=600):
    body = {"idempotency_key": key, "preview_asset_id": str(preview.id), "preview_sha256": preview.content_hash,
            "finding": {"dimension": "visible_sync", "reason": "fixture: inspect this interval", "start_ms": 100, "end_ms": end},
            "evidence_reference": "fixture:located-concern"}
    return client.post(f"{url}/review-concerns", json=body), body


def answer(item, approved=True):
    return {"concern_id": item["id"], "approved": approved,
            "evidence_reference": "fixture:focused-human-answer", "reason": "fixture: exact interval inspected"}


def test_v2_pending_child_exact_review_and_same_byte_admission(tmp_path, monkeypatch):
    path = tmp_path / "v2.sqlite"
    with TestClient(create_app(path)) as client:
        project, waiting, binding, preview = fixture(client, path, monkeypatch)
        url = root(project, binding)
        status = client.get(url).json()
        assert status["review_policy_version"] == 2 and len(status["required_dimensions"]) == 6
        assert status["children"][0]["human_review_state"] is None
        assert status["children"][0]["blockers"] == []
        assert client.post(f"{url}/talking-run").status_code == 409
        old = judgment(preview)
        old.pop("dimensions")
        old.pop("review_policy_version")
        assert client.post(f"{url}/continuity-review", json=old).status_code == 409
        reviewed = client.post(f"{url}/continuity-review", json=judgment(preview))
        assert reviewed.status_code == 201, reviewed.text
        assert client.get(f"{url}/continuity-review").json()["dimensions"] == {"schema_version": "0.1", **DIMENSIONS}
        assert client.post(f"{url}/continuity-review", json=judgment(preview)).json() == reviewed.json()
        admitted = client.post(f"{url}/talking-run")
        assert admitted.status_code == 201, admitted.text
        record = admitted.json()
        assert record["review_policy_version"] == 2 and record["assembled_asset_id"] == str(preview.id)
        assert record["reviewed_preview_sha256"] == preview.content_hash
        assert blocker(path, project, record) is None
        with Database(path) as db:
            output = AssetRepository(db).get(UUID(binding["talking_output_asset_id"]))
            assert output.metadata["talking_generation"].get("human_review") is None
            # An optional positive child judgment does not change the v2 origin.
            origin = TalkingSliceSeriesRepository(db).get(UUID(binding["talking_series_id"])).planned_origin
            generation = dict(output.metadata["talking_generation"])
            generation.update(human_review_state="approved", human_review={"approved": True, "evidence_reference": "fixture:optional"})
            AssetRepository(db).update(output.model_copy(update={"metadata": {"talking_generation": generation}}))
            assert PlannedTalkingAdmissionService(db, tmp_path).require_current_candidate(
                TalkingSliceSeriesRepository(db).get(UUID(binding["talking_series_id"])))[0].id == preview.id
            assert origin.child_review_sha256 is None
        assert client.post(f"{url}/talking-run").json() == record


@pytest.mark.parametrize("outcome", ["fail", "unknown"])
def test_v2_incomplete_dimension_cannot_write_approval(tmp_path, monkeypatch, outcome):
    path = tmp_path / "dimensions.sqlite"
    with TestClient(create_app(path)) as client:
        project, _, binding, preview = fixture(client, path, monkeypatch)
        body = judgment(preview, dimensions={**DIMENSIONS, "identity": outcome})
        assert client.post(f"{root(project, binding)}/continuity-review", json=body).status_code == 409
        assert client.get(f"{root(project, binding)}/continuity-review").status_code == 404


def test_v2_local_answers_and_late_concern_preserve_full_review(tmp_path, monkeypatch):
    path = tmp_path / "concerns.sqlite"
    with TestClient(create_app(path)) as client:
        project, _, binding, preview = fixture(client, path, monkeypatch)
        url = root(project, binding)
        item, body = concern(client, url, preview)
        assert item.status_code == 201, item.text
        assert client.post(f"{url}/review-concerns", json=body).json() == item.json()
        assert concern(client, url, preview, key="invalid", end=preview.duration_ms + 1)[0].status_code == 409
        assert client.post(f"{url}/continuity-review", json=judgment(preview)).status_code == 409
        reviewed = client.post(f"{url}/continuity-review", json=judgment(preview, concern_answers=[answer(item.json())]))
        assert reviewed.status_code == 201, reviewed.text
        admitted = client.post(f"{url}/talking-run").json()
        assert blocker(path, project, admitted) is None
        late, _ = concern(client, url, preview, key="late")
        assert late.status_code == 201
        assert client.post(f"{url}/talking-run").status_code == 409
        assert blocker(path, project, admitted) == "talking_run_planned_origin_stale"
        fixed = client.post(f"{url}/review-concern-answers", json={
            "preview_asset_id": str(preview.id), "preview_sha256": preview.content_hash,
            "answers": [answer(late.json())],
        })
        assert fixed.status_code == 200, fixed.text
        assert client.post(f"{url}/talking-run").json() == admitted
        assert client.get(f"{url}/continuity-review").json()["id"] == reviewed.json()["id"]
        assert blocker(path, project, admitted) is None


def test_negative_local_answer_and_run_rejection_are_immutable(tmp_path, monkeypatch):
    path = tmp_path / "negative.sqlite"
    with TestClient(create_app(path)) as client:
        project, _, binding, preview = fixture(client, path, monkeypatch)
        url = root(project, binding)
        item, _ = concern(client, url, preview)
        subject = {"preview_asset_id": str(preview.id), "preview_sha256": preview.content_hash}
        assert client.post(f"{url}/review-concern-answers", json={**subject, "answers": [answer(item.json(), False)]}).status_code == 200
        assert client.post(f"{url}/review-concern-answers", json={**subject, "answers": [answer(item.json())]}).status_code == 409
        assert client.post(f"{url}/continuity-review", json=judgment(preview)).status_code == 409
        rejected_body = judgment(preview, approved=False, dimensions={**DIMENSIONS, "visible_sync": "fail"},
                                 scoped_findings=[{"dimension": "visible_sync", "reason": "fixture: unpublishable", "start_ms": 100, "end_ms": 600}])
        rejected = client.post(f"{url}/continuity-review", json=rejected_body)
        assert rejected.status_code == 201, rejected.text
        assert client.post(f"{url}/continuity-review", json=rejected_body).json() == rejected.json()
        assert client.post(f"{url}/continuity-review", json=judgment(preview)).status_code == 409
        assert client.post(f"{url}/talking-run").status_code == 409
        with Database(path) as db:
            from app.talking.review_policy import TalkingReviewPolicyService
            assert "talking_exact_preview_previously_rejected" in TalkingReviewPolicyService(db).blockers(UUID(project), preview.content_hash)
            from app.talking.planned_preview import TalkingRunPreviewJobHandler
            from app.jobs.runner import JobExecutionError
            job = JobRepository(db).get(UUID(binding["talking_preview_job_id"]))
            handler = TalkingRunPreviewJobHandler(db, tmp_path, ffmpeg_command="unused", probe=None)
            with pytest.raises(JobExecutionError) as denied:
                handler(job.model_copy(update={"status": JobStatus.RUNNING}))
            assert denied.value.code == "talking_preview_invalid"


@pytest.mark.parametrize("failure", ["child_rejected", "child_unknown", "qa_changed", "capability_changed", "capability_unknown", "version_stripped"])
def test_v2_current_origin_failures_block_admission(tmp_path, monkeypatch, failure):
    path = tmp_path / "stale.sqlite"
    with TestClient(create_app(path)) as client:
        project, _, binding, preview = fixture(client, path, monkeypatch)
        url = root(project, binding)
        assert client.post(f"{url}/continuity-review", json=judgment(preview)).status_code == 201
        with Database(path) as db:
            if failure.startswith("child") or failure == "qa_changed":
                assets = AssetRepository(db)
                asset = assets.get(UUID(binding["talking_output_asset_id"]))
                generation = dict(asset.metadata["talking_generation"])
                if failure == "qa_changed":
                    generation["qa"] = {**generation["qa"], "evidence_reference": "changed"}
                else:
                    generation["human_review_state"] = "rejected" if failure == "child_rejected" else "uncertain"
                assets.update(asset.model_copy(update={"metadata": {"talking_generation": generation}}))
            elif failure.startswith("capability"):
                repo = ProviderMachineCapabilityProfileRepository(db)
                cap = repo.get(UUID(binding["capability_profile_id"]))
                repo.save(cap.model_copy(update={"quality_status": "unknown"} if failure == "capability_unknown" else {"verified_parameters": {**cap.verified_parameters, "new_parameter": 1}}))
            else:
                series = TalkingSliceSeriesRepository(db).get(UUID(binding["talking_series_id"]))
                raw = series.model_dump(mode="json")
                origin = raw["planned_origin"]
                origin.pop("review_policy_version")
                origin.pop("execution_sha256")
                origin.update(version=1, child_review_sha256="0" * 64)
                import json
                db.connection.execute("UPDATE talking_slice_series SET payload = ? WHERE id = ?", (json.dumps(raw), str(series.id)))
        assert client.post(f"{url}/talking-run").status_code == 409


def test_creation_policy_is_immutable_and_old_clients_stay_v1(tmp_path, monkeypatch):
    path = tmp_path / "create.sqlite"
    with TestClient(create_app(path)) as client:
        project, waiting, _, _ = _talking_source_fixture(client, path, policy_version=2)
        create = f"/projects/{project}/production-runs"
        body = {"idempotency_key": waiting["idempotency_key"], "expected_fingerprint": waiting["preflight_fingerprint"], "talking_review_policy_version": 2}
        assert client.post(create, json=body).status_code == 201
        assert client.post(create, json={**body, "talking_review_policy_version": 1}).status_code == 409
        assert client.post(create, json={**body, "idempotency_key": "alias", "talking_review_policy_version": 1}).status_code == 409
        assert client.post(create, json={**body, "talking_review_policy_version": 3}).status_code == 422


def test_v1_origin_frozen_serialization_and_legacy_review_survive_restart(tmp_path, monkeypatch):
    path = tmp_path / "legacy.sqlite"
    with TestClient(create_app(path)) as client:
        project, _, binding, preview = fixture(client, path, monkeypatch, version=1)
        url = root(project, binding)
        body = judgment(preview)
        body.pop("review_policy_version")
        body.pop("dimensions")
        review = client.post(f"{url}/continuity-review", json=body).json()
        assert review["review_policy_version"] == 1 and review["dimensions"] is None
        with Database(path) as db:
            import json
            raw = json.loads(db.connection.execute("SELECT payload FROM talking_slice_series WHERE id = ?", (binding["talking_series_id"],)).fetchone()["payload"])
            frozen = raw["planned_origin"]
            assert set(frozen) == set(PlannedTalkingRunOrigin.model_fields)
            fingerprint = _snapshot_sha256(frozen)
            legacy = db.connection.execute("SELECT payload FROM talking_slice_series_continuity_reviews WHERE series_id = ?", (binding["talking_series_id"],)).fetchone()["payload"]
            legacy = json.loads(legacy)
            for key in ("review_policy_version", "dimensions", "scoped_findings"):
                legacy.pop(key)
            legacy_payload = json.dumps(legacy)
            db.connection.execute("UPDATE talking_slice_series_continuity_reviews SET payload = ? WHERE series_id = ?", (legacy_payload, binding["talking_series_id"]))
            # Simulate schema-34 stored payloads and database, without changing their bytes.
            db.connection.execute("DROP TABLE talking_review_concern_answers")
            db.connection.execute("DROP TABLE talking_review_concerns")
            db.connection.execute("ALTER TABLE production_runs DROP COLUMN review_request_fingerprint")
            db.connection.execute("ALTER TABLE production_runs DROP COLUMN talking_review_policy_version")
            db.connection.execute("DELETE FROM schema_migrations WHERE version = 35")
    with TestClient(create_app(path)) as client:
        assert client.get(url).json()["planned_origin_sha256"] == fingerprint
        with Database(path) as db:
            assert db.connection.execute("SELECT payload FROM talking_slice_series_continuity_reviews WHERE series_id = ?", (binding["talking_series_id"],)).fetchone()["payload"] == legacy_payload
        assert client.get(f"{url}/continuity-review").json()["dimensions"] is None
        assert client.post(f"{url}/talking-run").status_code == 201
        with Database(path) as db:
            series = TalkingSliceSeriesRepository(db).get(UUID(binding["talking_series_id"]))
            assert series.planned_origin.model_dump(mode="json") == frozen
            assert _snapshot_sha256(series.planned_origin.model_dump(mode="json")) == fingerprint
