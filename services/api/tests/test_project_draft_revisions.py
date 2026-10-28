from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from fastapi.testclient import TestClient
import pytest

from app.db import Database, IPProfileRepository, ProjectRepository
from app.domain.models import CandidateAsset, CostCategory, DraftRoute, IPProfile, Project, RationalFps, ScenePlan, SourceKind, UsageCost, VisualIntent
from app.main import create_app
from app.project_drafts import save_project_draft
from app.providers.scene_planner import ScenePlanResult


def _scene(project_id: UUID, text: str = "A fresh creator point.") -> ScenePlan:
    return ScenePlan(
        project_id=project_id,
        scene_id="hook",
        order=0,
        purpose="hook",
        voice_text=text,
        duration_target_ms=1_000,
        visual_intent=VisualIntent(subject="creator", action="speaking"),
        preferred_sources=[SourceKind.USER_ASSET],
        fallback_sources=[SourceKind.TYPOGRAPHY],
    )


def _route(scene: ScenePlan) -> tuple[CandidateAsset, DraftRoute]:
    candidate = CandidateAsset(
        scene_plan_id=scene.id,
        source_kind=SourceKind.TYPOGRAPHY,
        match_score=0.8,
        why=["local fallback"],
        recommended=True,
        estimated_cost=UsageCost(category=CostCategory.TYPOGRAPHY),
    )
    return candidate, DraftRoute(scene_plan_id=scene.id, candidates=[candidate])


def test_script_edit_invalidates_old_scene_routes_and_records_history(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "draft-revisions.sqlite")) as client:
        project = client.post("/projects", json={"title": "Revision", "topic": "Original"}).json()
        project_id = UUID(project["id"])
        scene = _scene(project_id)
        candidate, route = _route(scene)
        initial = client.put(f"/projects/{project_id}/draft", json={
            "script": "Original script",
            "topic": "Original",
            "scenes": [scene.model_dump(mode="json")],
            "routes": [route.model_dump(mode="json")],
            "confirmed": [candidate.model_dump(mode="json")],
        })
        assert initial.status_code == 200, initial.text
        assert initial.json()["script_revision"] == 1

        # A stale tab may replay its prior plan and candidates, but changing
        # the script must make the server discard every derived artifact.
        changed = client.put(f"/projects/{project_id}/draft", json={
            "script": "A different new script",
            "topic": "Original",
            "scenes": [scene.model_dump(mode="json")],
            "routes": [route.model_dump(mode="json")],
            "confirmed": [candidate.model_dump(mode="json")],
        })
        assert changed.status_code == 200, changed.text
        body = changed.json()
        assert body["script"] == "A different new script"
        assert body["scenes"] == body["routes"] == body["confirmed"] == []
        assert body["video_spec"] is None
        assert body["script_revision"] == 2
        assert body["invalidation_reasons"] == ["script_changed"]

        revisions = client.get(f"/projects/{project_id}/draft/revisions")
        assert revisions.status_code == 200
        assert [item["version"] for item in revisions.json()] == [1, 2]
        assert [item["script"] for item in revisions.json()] == ["Original script", "A different new script"]


def test_matching_user_edited_scenes_survive_script_edit_but_routes_are_cleared(tmp_path: Path) -> None:
    with Database(tmp_path / "matched-scene-copy.sqlite") as db:
        profile = IPProfile(creator_name="Creator")
        IPProfileRepository(db).create(profile)
        project = Project(
            ip_profile_id=profile.id, title="Matched copy", topic="Original",
            fps=RationalFps(numerator=30, denominator=1), created_at=datetime.now(timezone.utc),
        )
        ProjectRepository(db).create(project)
        project_id = project.id
        first_scene = _scene(project_id, "Old spoken copy.")
        first_candidate, first_route = _route(first_scene)
        save_project_draft(
            db, project, script="Old spoken copy.", topic="Original",
            scenes=[first_scene], routes=[first_route], confirmed=[first_candidate], video_spec=None,
        )

        revised_scene = _scene(project_id, "A revised spoken copy.")
        revised_candidate, revised_route = _route(revised_scene)
        changed = save_project_draft(
            db, project, script="A revised spoken copy!", topic="Original",
            scenes=[revised_scene], routes=[revised_route], confirmed=[revised_candidate], video_spec=None,
        )
        assert changed.script == "A revised spoken copy!"
        assert [scene.voice_text for scene in changed.scenes] == ["A revised spoken copy."]
        assert changed.routes == changed.confirmed == []
        assert changed.video_spec is None
        assert changed.script_revision == 2
        assert changed.invalidation_reasons == ["script_changed", "scene_copy_changed"]

        stale = save_project_draft(
            db, project, script="A different script.", topic="Original",
            scenes=[revised_scene], routes=[revised_route], confirmed=[revised_candidate], video_spec=None,
        )
        assert stale.scenes == stale.routes == stale.confirmed == []
        assert stale.script_revision == 3


def test_ip_update_invalidates_existing_project_draft_server_side(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "profile-invalidates.sqlite")) as client:
        project = client.post("/projects", json={"title": "IP revision", "topic": "Topic"}).json()
        project_id = UUID(project["id"])
        scene = _scene(project_id)
        candidate, route = _route(scene)
        saved = client.put(f"/projects/{project_id}/draft", json={
            "script": "Keep this editable copy",
            "topic": "Topic",
            "scenes": [scene.model_dump(mode="json")],
            "routes": [route.model_dump(mode="json")],
            "confirmed": [candidate.model_dump(mode="json")],
        })
        assert saved.status_code == 200

        profile = client.get("/ip-profile").json()
        updated = client.put("/ip-profile", json={
            **{key: profile[key] for key in ("creator_name", "domains", "topics", "knowledge", "opinions", "vocabulary", "style_notes", "avoided_expressions", "boundaries", "common_hooks", "metadata")},
            "audience": "builders who want concrete workflows",
        })
        assert updated.status_code == 200

        draft = client.get(f"/projects/{project_id}/draft").json()
        assert draft["script"] == "Keep this editable copy"
        assert draft["scenes"] == draft["routes"] == draft["confirmed"] == []
        assert draft["invalidation_reasons"] == ["ip_profile_changed"]
        assert draft["script_revision"] == 2
        assert draft["ip_profile_version"] == 2


def test_persisted_scene_plan_becomes_editable_generated_copy(tmp_path: Path) -> None:
    class Planner:
        def plan(self, project, *, script=None, topic=None):
            scene = _scene(project.id, "This is model-produced spoken copy.")
            return ScenePlanResult(project.id, (scene,))

    with TestClient(create_app(tmp_path / "generated-copy.sqlite", scene_planner=Planner())) as client:
        project = client.post("/projects", json={"title": "Generated", "topic": "New topic"}).json()
        response = client.post(f"/projects/{project['id']}/scene-plan", json={"persist": True})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["generated_script"] is True
        assert body["script"] == "This is model-produced spoken copy."
        assert body["draft"] is not None
        assert body["draft"]["script"] == body["script"]
        assert body["draft"]["scenes"][0]["voice_text"] == body["script"]
        assert body["draft"]["evidence_refs"] == [f"ip_profile:{project['ip_profile_id']}:v1"]


def _clause_master(path, project_id, script, *, verified=True):
    """Synthetic admitted fixture with real WAV bytes; no actual voice-quality claim."""
    import hashlib
    import wave
    from app.db import AudioAssetRepository, VoiceProfileRepository
    from app.domain.models import AudioAsset, ConsentRecord, TranscriptSegment, VoiceProfile
    from app.providers.asr import TranscriptionResult, TranscriptionSegment
    from app.voice_qa import apply_voice_qa, verify_generated_voice

    source = path.parent / "master-clauses.wav"
    with wave.open(str(source), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(24_000)
        stream.writeframes(b"\x00\x20" * 120_000)
    with Database(path) as db:
        repository = AudioAssetRepository(db)
        project = ProjectRepository(db).get(project_id)
        now = datetime.now(timezone.utc)
        profile = VoiceProfileRepository(db).create(VoiceProfile(
            ip_profile_id=project.ip_profile_id, name="Synthetic creator", provider="fixture-voice", provider_profile_id="fixture",
            reference_clip_ids=[project_id], consent=ConsentRecord(subject_name="Creator", basis="self",
                confirmed=True, confirmed_at=now), created_at=now))
        ip = IPProfileRepository(db).get(project.ip_profile_id)
        IPProfileRepository(db).update(ip.model_copy(update={"default_voice_profile_id": profile.id}))
        master = repository.create(AudioAsset(
            source_file=source.name, duration_ms=5000, content_hash=hashlib.sha256(source.read_bytes()).hexdigest(),
            sample_rate=24000, channels=1, authorization_reference="fixture:voice-consent", imported_at=now,
            transcript_source="fixture-asr", transcript_segments=[
                TranscriptSegment(start_ms=0, end_ms=2320, text="马率是每秒分给画面的数据量"),
                TranscriptSegment(start_ms=2320, end_ms=3460, text="它只是预算"),
                TranscriptSegment(start_ms=3460, end_ms=5000, text="不是清晰度本身"),
            ], metadata={"voice_generation": {
                "project_id": str(project_id), "voice_profile_id": str(profile.id), "target_text": script,
                "qa_state": "verified" if verified else "pending", "human_review_state": "approved",
                "human_review": {"approved": True, "evidence_reference": "fixture:review",
                    "findings": ["Synthetic fixture only"], "likeness": "pass", "naturalness": "pass",
                    "emphasis": "pass", "pace": "pass", "pauses": "pass", "rhythm": "pass",
                    "reviewed_at": now.isoformat()},
            }},
        ))
        transcription = TranscriptionResult(" ".join(item.text for item in master.transcript_segments),
            tuple(TranscriptionSegment(item.start_ms, item.end_ms, item.text) for item in master.transcript_segments))
        report = verify_generated_voice(master.model_copy(update={"source_file": str(source)}), script, transcription)
        assert report.verified
        reviewed = apply_voice_qa(master, report, transcription, provider="fixture", model="fixture")
        reviewed.metadata["voice_generation"].update(
            qa_state="verified" if verified else "pending", human_review_state="approved")
        master = repository.update(reviewed)
        return master


def test_normal_draft_save_splits_talking_at_master_clauses_preserving_copy_and_idempotency(tmp_path):
    path = tmp_path / "master-clause-draft.sqlite"
    script = "码率是每秒分给画面的数据量， 它只是预算，不是清晰度本身。"
    with TestClient(create_app(path)) as client:
        project = client.post("/projects", json={"title": "短句规划", "topic": "码率"}).json()
        project_id = UUID(project["id"])
        master = _clause_master(path, project_id, script, verified=False)
        scene = _scene(project_id, script).model_copy(update={
            "preferred_sources": [SourceKind.TALKING_PROFILE], "fallback_sources": [SourceKind.AI_VIDEO],
            "visual_requirement": "creator_speaking", "visual_requirement_reason": "本人讲解新文案",
            "evidence_refs": ["fixture:original-editorial-basis"],
        })
        payload = {"script": script, "topic": "码率", "scenes": [scene.model_dump(mode="json")]}
        before = client.put(f"/projects/{project_id}/draft", json=payload).json()
        assert len(before["scenes"]) == 1  # 未认可 Master 不能提供自动切点。
        from app.db import AudioAssetRepository
        with Database(path) as db:
            AudioAssetRepository(db).update(master.model_copy(update={"metadata": {
                **master.metadata, "voice_generation": {**master.metadata["voice_generation"], "qa_state": "verified"}}}))
        saved = client.put(f"/projects/{project_id}/draft", json=payload)
        saved.raise_for_status()
        body = saved.json()
        assert [item["voice_text"] for item in body["scenes"]] == ["码率是每秒分给画面的数据量， ", "它只是预算，不是清晰度本身。"]
        assert "".join(item["voice_text"] for item in body["scenes"]) == script
        assert [item["duration_target_ms"] for item in body["scenes"]] == [2320, 2680]
        assert [item["order"] for item in body["scenes"]] == [0, 1]
        assert len({item["id"] for item in body["scenes"]}) == 2
        assert all(item["visual_intent"] == scene.visual_intent.model_dump(mode="json") for item in body["scenes"])
        assert all(item["evidence_refs"] == scene.evidence_refs for item in body["scenes"])
        assert body["evidence_refs"] == before["evidence_refs"]
        assert body["script_revision"] == before["script_revision"] + 1
        assert body["invalidation_reasons"] == ["scene_copy_changed"]
        # 重发原始长分镜或保存拆分后的分镜均不产生新版本和随机 ID。
        assert client.put(f"/projects/{project_id}/draft", json=payload).json() == body
        assert client.put(f"/projects/{project_id}/draft", json={
            **payload, "scenes": body["scenes"]}).json() == body
        assert len(client.get(f"/projects/{project_id}/draft/revisions").json()) == 2
        assert client.get(f"/projects/{project_id}/provider-calls").json() == []


@pytest.mark.parametrize("protection", ["selected", "capture_only", "copy_mismatch", "asr_insertion", "single_long_clause", "current_run"])
def test_master_clause_split_does_not_change_selected_unaligned_or_locked_inputs(tmp_path, protection):
    from app.db import AudioAssetRepository
    from app.domain.models import TranscriptSegment
    from uuid import uuid4

    path = tmp_path / "protected-clause-draft.sqlite"
    script = "码率是每秒分给画面的数据量， 它只是预算，不是清晰度本身。"
    with TestClient(create_app(path)) as client:
        project_id = UUID(client.post("/projects", json={"title": "保护", "topic": "码率"}).json()["id"])
        master = _clause_master(path, project_id, script, verified=protection != "current_run")
        scene = _scene(project_id, script).model_copy(update={
            "preferred_sources": [SourceKind.AI_VIDEO], "fallback_sources": [],
            "visual_requirement": "creator_speaking", "visual_requirement_reason": "本人讲解新文案"})
        if protection == "capture_only":
            scene = scene.model_copy(update={"preferred_sources": [SourceKind.CAPTURE]})
        payload = {"script": script + ("另有一句。" if protection == "copy_mismatch" else ""),
            "topic": "码率", "scenes": [scene.model_dump(mode="json")]}
        if protection == "selected":
            candidate, route = _route(scene)
            payload.update(routes=[route.model_dump(mode="json")], confirmed=[candidate.model_dump(mode="json")])
        if protection in {"asr_insertion", "single_long_clause"}:
            segments = [TranscriptSegment(start_ms=0, end_ms=5000,
                text=script + ("多说了一段内容" if protection == "asr_insertion" else ""))]
            with Database(path) as db:
                AudioAssetRepository(db).update(master.model_copy(update={"transcript_segments": segments}))
        before = client.put(f"/projects/{project_id}/draft", json=payload)
        before.raise_for_status()
        body = before.json()
        if protection == "current_run":
            with Database(path) as db:
                db.connection.execute("""INSERT INTO production_runs
                    (id,project_id,idempotency_key,preflight_fingerprint,draft_version,script_revision,
                     style_tokens,repair_allowance,stage,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (str(uuid4()), str(project_id), "fixture-locked", "a" * 64, body["version"],
                     body["script_revision"], "{}", 0, "awaiting_master", datetime.now(timezone.utc).isoformat()))
                row = dict(db.connection.execute("SELECT * FROM production_runs").fetchone())
                AudioAssetRepository(db).update(master.model_copy(update={"metadata": {
                    **master.metadata, "voice_generation": {**master.metadata["voice_generation"], "qa_state": "verified"}}}))
            assert client.put(f"/projects/{project_id}/draft", json=payload).json() == body
            with Database(path) as db:
                assert dict(db.connection.execute("SELECT * FROM production_runs").fetchone()) == row
        assert len(body["scenes"]) == 1 and body["scenes"][0]["id"] == str(scene.id)
        assert body["scenes"][0]["voice_text"] == script
        assert client.get(f"/projects/{project_id}/provider-calls").json() == []


@pytest.mark.parametrize("text,accepted", [("", False), (" ", False), ("\n\t", False), ("\t真实观点。 \n", True)])
def test_normal_draft_preserves_spoken_copy_whitespace_but_rejects_blank_speech(tmp_path, text, accepted):
    with TestClient(create_app(tmp_path / "spoken-whitespace.sqlite")) as client:
        project_id = UUID(client.post("/projects", json={"title": "原稿", "topic": "保留原稿"}).json()["id"])
        payload = _scene(project_id).model_dump(mode="json")
        payload["voice_text"] = text
        result = client.put(f"/projects/{project_id}/draft", json={"script": text, "scenes": [payload]})
        assert result.status_code == (200 if accepted else 422)
        if accepted:
            assert result.json()["scenes"][0]["voice_text"] == text
            assert client.get(f"/projects/{project_id}/draft").json()["scenes"][0]["voice_text"] == text
