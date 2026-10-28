"""Normal project presentation preferences persist without inference or real media."""

from starlette.requests import Request

from app.db import Database, ProjectRepository, ProjectDraftRepository
from app.domain.models import ScenePlan, SourceKind, VisualIntent
from app.main import create_app, ProjectCreateRequest, ProjectPresentationRequest
from app.project_drafts import save_project_draft


def invoke(coroutine):
    # These route handlers do local synchronous work and do not suspend.
    # Exercise them without Windows' socket-backed event loop initialization.
    try:
        coroutine.send(None)
    except StopIteration as completed:
        return completed.value
    raise AssertionError("project route unexpectedly suspended")


def test_project_create_and_style_change_preserve_copy_but_invalidate_old_visual_plan(tmp_path):
    app = create_app(tmp_path / "presentation.sqlite3")
    with Database(tmp_path / "presentation.sqlite3") as db:
        app.state.database = db
        request = Request({"type": "http", "app": app})
        create = next(route.endpoint for route in app.routes if getattr(route, "name", None) == "create_project")
        update = next(route.endpoint for route in app.routes if getattr(route, "name", None) == "update_project_presentation")
        project = invoke(create(ProjectCreateRequest(title="讲解", topic="视频码率"), request))
        assert project.presentation_style == "auto"
        scene = ScenePlan(project_id=project.id, scene_id="old", order=0, purpose="说明",
            voice_text="码率是数据量。", duration_target_ms=3000,
            visual_intent=VisualIntent(subject="语义文字"), preferred_sources=[SourceKind.TYPOGRAPHY])
        before = save_project_draft(db, project, script=scene.voice_text, topic=project.topic,
            scenes=[scene], routes=[], confirmed=[], video_spec=None)
        changed = invoke(update(project.id, ProjectPresentationRequest(presentation_style="creator_led"), request))
        assert changed.id == project.id and changed.ip_profile_id == project.ip_profile_id
        assert ProjectRepository(db).get(project.id).presentation_style == "creator_led"
        after = ProjectDraftRepository(db).get(project.id)
        assert after.script == before.script and after.topic == before.topic
        assert after.scenes == [] and after.routes == [] and after.confirmed == [] and after.video_spec is None
        assert after.version == before.version + 1 and after.script_revision == before.script_revision + 1
        invoke(update(project.id, ProjectPresentationRequest(presentation_style="creator_led"), request))
        assert ProjectDraftRepository(db).get(project.id) == after
        created = invoke(create(ProjectCreateRequest(title="本人", topic="解释", presentation_style="creator_led"), request))
        assert created.presentation_style == "creator_led"


def test_output_settings_require_explicit_horizontal_and_keep_copy(tmp_path):
    from app.domain.models import ProjectFormat
    app=create_app(tmp_path/'formats.sqlite')
    with Database(tmp_path/'formats.sqlite') as db:
        app.state.database=db
        request=Request({'type':'http','app':app})
        create=next(r.endpoint for r in app.routes if getattr(r,'name',None)=='create_project')
        update=next(r.endpoint for r in app.routes if getattr(r,'name',None)=='update_project_presentation')
        project=invoke(create(ProjectCreateRequest(title='讲解',topic='码率'),request))
        scene=ScenePlan(project_id=project.id,scene_id='intro',order=0,purpose='提出问题',voice_text='码率越高越好吗？',
            duration_target_ms=2000,visual_intent=VisualIntent(subject='码率'),preferred_sources=['user_asset'])
        save_project_draft(db,project,script=scene.voice_text,topic=project.topic,scenes=[scene],routes=[],confirmed=[],video_spec=None)
        horizontal=invoke(update(project.id,ProjectPresentationRequest(format='horizontal'),request))
        assert horizontal.format is ProjectFormat.HORIZONTAL and horizontal.format_policy=='fixed'
        assert (horizontal.resolution_width,horizontal.resolution_height)==(1920,1080)
        assert ProjectDraftRepository(db).get(project.id).script==scene.voice_text
        vertical=invoke(update(project.id,ProjectPresentationRequest(format_policy='auto'),request))
        assert vertical.format is ProjectFormat.VERTICAL and vertical.format_policy=='auto'
        assert (vertical.resolution_width,vertical.resolution_height)==(1080,1920)
        assert ProjectDraftRepository(db).get(project.id).scenes[0].voice_text==scene.voice_text
