from pathlib import Path

import pytest
from pydantic import ValidationError

from app.db import Database
from app.runtime_settings import RuntimeSettings, load_runtime_settings, runtime_environment, save_runtime_settings


def test_settings_store_only_paths_and_overlay_existing_configuration(tmp_path: Path):
    key = tmp_path / 'planner-key.txt'
    key.write_text('fixture-secret-not-for-storage', encoding='utf-8')
    pexels = tmp_path / 'pexels-key.txt'
    pexels.write_text('another-fixture-secret', encoding='utf-8')
    (tmp_path / 'voice.json').write_text('{}', encoding='utf-8')
    (tmp_path / 'asr').mkdir()
    saved = RuntimeSettings(planner_key_file=str(key), planner_base_url='https://example.test/v1',
        planner_model='fixture-model', voice_config_file=str(tmp_path / 'voice.json'),
        voice_qa_model_path=str(tmp_path / 'asr'), pexels_key_file=str(pexels), vision_thinking_type='disabled',
        planner_reasoning_effort='low', planner_max_output_tokens='8192', planner_timeout_seconds='120')
    with Database(tmp_path / 'runtime-settings.sqlite') as db:
        assert load_runtime_settings(db) == RuntimeSettings()
        assert save_runtime_settings(db, saved) == saved
        assert load_runtime_settings(db) == saved
        assert 'fixture-secret-not-for-storage' not in db.connection.execute(
            'SELECT payload FROM provider_machine_settings').fetchone()[0]
        assert 'another-fixture-secret' not in db.connection.execute(
            'SELECT payload FROM provider_machine_settings').fetchone()[0]
        effective = runtime_environment(saved, {'CONTENT_OS_LLM_MODEL': 'old', 'CONTENT_OS_ASR_PROVIDER': 'openai-compatible',
            'CONTENT_OS_ASR_LOCAL_MODEL': 'old', 'EXISTING': 'kept'})
        assert effective['CONTENT_OS_LLM_API_KEY_FILE'] == str(key)
        assert effective['CONTENT_OS_LLM_MODEL'] == 'fixture-model'
        assert effective['CONTENT_OS_VOICE_PROVIDER'] == 'omnivoice-maintained'
        assert effective['CONTENT_OS_ASR_PROVIDER'] == 'local'
        assert effective['CONTENT_OS_ASR_LOCAL_MODEL'] == str(tmp_path / 'asr')
        assert effective['CONTENT_OS_PEXELS_API_KEY_FILE'] == str(pexels)
        assert effective['CONTENT_OS_VISION_THINKING_TYPE'] == 'disabled'
        assert effective['CONTENT_OS_LLM_REASONING_EFFORT'] == 'low'
        assert effective['CONTENT_OS_LLM_MAX_OUTPUT_TOKENS'] == '8192'
        assert effective['CONTENT_OS_LLM_TIMEOUT_SECONDS'] == '120'
        assert effective['EXISTING'] == 'kept'


def test_settings_reject_credentials_and_unsafe_endpoint():
    with pytest.raises(ValidationError):
        RuntimeSettings(planner_key_file='sk-not-a-file-path')
    with pytest.raises(ValidationError):
        RuntimeSettings(planner_base_url='https://user:password@example.test/v1')
    with pytest.raises(ValidationError):
        RuntimeSettings(vision_video_input='yes')
    with pytest.raises(ValidationError):
        RuntimeSettings(vision_thinking_type='unknown')
    with pytest.raises(ValidationError):
        RuntimeSettings(planner_reasoning_effort='unknown')
    with pytest.raises(ValidationError):
        RuntimeSettings(planner_max_output_tokens='0')
    with pytest.raises(ValidationError):
        RuntimeSettings(planner_timeout_seconds='1801')


def test_settings_api_uses_saved_configuration_after_restart(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import create_app

    monkeypatch.setenv('CONTENT_OS_LLM_API_KEY', 'inherited-secret-not-for-display')
    monkeypatch.delenv('CONTENT_OS_LLM_API_KEY_FILE', raising=False)
    key = tmp_path / 'key.txt'
    key.write_text('fixture-key-not-for-display', encoding='utf-8')
    path = tmp_path / 'api.sqlite'
    initial = RuntimeSettings(planner_key_file=str(key), planner_model='configured').model_dump()
    with TestClient(create_app(path)) as client:
        assert client.get('/runtime/settings').json() == RuntimeSettings().model_dump()
        response = client.put('/runtime/settings', json=initial)
        assert response.status_code == 200 and response.json() == initial
        assert 'fixture-key-not-for-display' not in response.text
        assert 'inherited-secret-not-for-display' not in response.text
        assert client.app.state.runtime_environment.get('CONTENT_OS_LLM_MODEL') != 'configured'
        invalid = {**initial, 'planner_key_file': str(tmp_path / 'missing.txt')}
        assert client.put('/runtime/settings', json=invalid).status_code == 422
        invalid_pexels = {**initial, 'pexels_key_file': str(tmp_path / 'missing-pexels.txt')}
        assert client.put('/runtime/settings', json=invalid_pexels).status_code == 422
    with TestClient(create_app(path)) as client:
        assert client.get('/runtime/settings').json() == initial
        assert client.app.state.runtime_environment['CONTENT_OS_LLM_MODEL'] == 'configured'
        assert client.app.state.runtime_environment['CONTENT_OS_LLM_API_KEY_FILE'] == str(key)


def test_worker_uses_persisted_paths_and_registers_handlers(tmp_path, monkeypatch):
    from app.domain.models import JobType
    from app.worker_cli import parse_config, run
    from types import SimpleNamespace

    db_path = tmp_path / 'worker.sqlite'
    (tmp_path / 'voice.json').write_text('{}', encoding='utf-8')
    (tmp_path / 'talking.json').write_text('{}', encoding='utf-8')
    (tmp_path / 'asr').mkdir()
    settings = RuntimeSettings(voice_config_file=str(tmp_path / 'voice.json'),
        talking_config_file=str(tmp_path / 'talking.json'), voice_qa_model_path=str(tmp_path / 'asr'))
    with Database(db_path) as db:
        save_runtime_settings(db, settings)
    seen = {}
    def build(config, db):
        import os
        seen['types'] = set(config.job_types)
        seen['voice'] = os.environ['CONTENT_OS_MAINTAINED_VOICE_CONFIG']
        return SimpleNamespace()
    monkeypatch.setattr('app.worker_cli.build_runner', build)
    monkeypatch.setattr('app.worker_cli._run_product_queue', lambda config, db, runner, stop: 0)
    assert run(parse_config(['--once', '--production-only', '--db', str(db_path), '--job-type', 'render'])) == 0
    assert {JobType.GENERATE_VOICE, JobType.VERIFY_VOICE, JobType.GENERATE_TALKING, JobType.RENDER} <= seen['types']
    assert seen['voice'] == settings.voice_config_file


def test_product_worker_builds_registered_handlers_from_saved_settings(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from app.domain.models import JobType
    from app.worker_cli import parse_config, run

    db_path = tmp_path / 'registered.sqlite'
    voice, talking, asr = tmp_path / 'voice.json', tmp_path / 'talking.json', tmp_path / 'asr'
    voice.write_text('{}', encoding='utf-8'); talking.write_text('{}', encoding='utf-8'); asr.mkdir()
    with Database(db_path) as db:
        save_runtime_settings(db, RuntimeSettings(voice_config_file=str(voice), talking_config_file=str(talking),
            voice_qa_model_path=str(asr)))
    monkeypatch.setattr('app.worker_cli._build_voice_provider', lambda config, db: SimpleNamespace(model='voice'))
    monkeypatch.setattr('app.worker_cli._build_talking_provider', lambda config: SimpleNamespace(model='talking'))
    seen = {}
    def queue(config, db, runner, stop):
        seen['types'] = runner.handler_types
        return 0
    monkeypatch.setattr('app.worker_cli._run_product_queue', queue)
    config = parse_config(['--once', '--production-only', '--db', str(db_path), '--job-type', 'render'])
    assert run(config) == 0
    assert {JobType.GENERATE_VOICE, JobType.VERIFY_VOICE, JobType.GENERATE_TALKING,
        JobType.VERIFY_TALKING, JobType.TRANSCRIBE_AUDIO, JobType.RENDER} <= seen['types']
