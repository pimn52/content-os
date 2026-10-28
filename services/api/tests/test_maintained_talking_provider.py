"""普通 Provider 接线测试；虚拟运行时，不启动模型或子进程。"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.providers import maintained_talking as maintained
from app.domain.models import RationalFps
from app.providers.talking import (
    TalkingConfigurationError, TalkingExecutionOptions, TalkingProviderResponseError,
    TalkingReference, TalkingSynthesisResult,
)
from test_maintained_runtime import candidate
from test_maintained_talking_execution import talking_case
from test_maintained_talking_dual import dual_case


def provider(c):
    options = {key: value for key, value in c.options.items() if key != "operator_key"}
    return maintained.MaintainedLatentSyncProvider(c.root, options)


def reference(c, *, end_ms=None):
    request = c.request
    return TalkingReference(request.reference_clip_id, Path(request.reference_path),
        request.reference_start_ms, request.reference_end_ms if end_ms is None else end_ms)


def test_normal_provider_reuses_owned_engine_without_evaluation_admission(dual_case, monkeypatch):
    c = dual_case
    c.origin_path.unlink()
    monkeypatch.delenv("CONTENT_OS_EXECUTION_ADMISSION_KEY_HASHES")
    monkeypatch.setattr("app.providers.maintained_talking_dual.configured_admission_actor",
        lambda *_: pytest.fail("ordinary provider must not request evaluation admission"))
    received = []

    def synthesize(owner, request, **kwargs):
        received.append((owner, request, kwargs))
        assert (owner.private / "engine.pyz").is_file()
        assert owner.options["parameters"]["attention_policy"] == "latentsync-retained-explicit-math-v1"
        assert any(row["source_name"] == "vendor/checkpoints/whisper/tiny.pt"
                   for row in owner.options["resource_copies"])
        output = owner.private / "output.mp4"
        output.write_bytes(b"synthetic model output")
        return TalkingSynthesisResult(output, "1.5", expected_video_fps=RationalFps(numerator=25, denominator=1))

    monkeypatch.setattr(maintained.OwnedDualLatentSync, "synthesize", synthesize)
    p = provider(c)
    options = TalkingExecutionOptions(terminal_face_closeout=True, terminal_delivery_end_ms=1000,
        provider_parameters={"trailing_silence_lookahead_ms": 600})
    target = c.root / "generated" / "talking" / "ordinary-job.mp4"
    result = p.synthesize(c.request.profile, c.request.narration, reference(c), target, options)

    assert result.video_path == target and result.provider_version == "1.5"
    assert result.expected_video_fps == RationalFps(numerator=25, denominator=1)
    assert received[0][1].reference_start_ms == c.request.reference_start_ms
    assert received[0][1].reference_end_ms == c.request.reference_end_ms
    assert received[0][2]["execution_options"] is options
    evidence = json.loads(target.with_suffix(".execution.json").read_text(encoding="utf-8"))
    assert evidence["qa_state"] == "pending"
    assert evidence["narration_sha256"] == c.request.narration.content_hash
    assert evidence["reference_start_ms"] == c.request.reference_start_ms
    assert p.execution_runtime == c.options["runtime_label"]
    assert p.runtime_metadata.estimated_cost.amount == 0
    assert not list((c.root / "execution-staging").iterdir())


@pytest.mark.parametrize("invalid", ["narration_hash", "reference_short", "reference_drift", "runtime_drift"])
def test_input_and_runtime_changes_never_admit_output(dual_case, monkeypatch, invalid):
    c = dual_case
    audio = c.request.narration
    ref = reference(c)
    calls = []
    if invalid == "narration_hash":
        Path(audio.source_file).write_bytes(b"changed narration")
    if invalid == "reference_short":
        ref = reference(c, end_ms=100)

    def synthesize(owner, request, **kwargs):
        calls.append(request)
        changed = Path(request.reference_path) if invalid == "reference_drift" else c.options["checkpoint"]
        changed.write_bytes(b"changed during model execution")
        output = owner.private / "output.mp4"
        output.write_bytes(b"unadmitted output")
        return TalkingSynthesisResult(output, "1.5")

    monkeypatch.setattr(maintained.OwnedDualLatentSync, "synthesize", synthesize)
    target = c.root / "generated" / "talking" / "bad.mp4"
    with pytest.raises(TalkingProviderResponseError):
        provider(c).synthesize(c.request.profile, audio, ref, target)
    assert len(calls) == (0 if invalid in ("narration_hash", "reference_short") else 1)
    assert not target.exists()
    failure = next((c.root / "execution-staging").glob("*/failure.json"))
    assert json.loads(failure.read_text())["retry_automatically"] is False


def test_configured_provider_uses_path_config_without_operator_key(dual_case):
    c = dual_case
    options = {key: value for key, value in c.options.items()
               if key not in {"operator_key", "data_root", "staging_parent", "origin_reference", "installation_id"}}
    raw = {key: str(value) if isinstance(value, Path) else value for key, value in options.items()}
    config_file = c.root / "ordinary-talking.json"
    config_file.write_text(json.dumps(dict(config_version=2, runtime_options=raw)))
    p = maintained.configured_maintained_talking_provider(SimpleNamespace(data_root=c.root),
        {"CONTENT_OS_MAINTAINED_TALKING_CONFIG": str(config_file)})
    assert p.provider_name == "latentsync" and p.machine_id == c.options["machine_id"]
    document = json.loads(config_file.read_text())
    document["runtime_options"]["operator_key"] = "unexpected"
    config_file.write_text(json.dumps(document))
    with pytest.raises(TalkingConfigurationError):
        maintained.configured_maintained_talking_provider(SimpleNamespace(data_root=c.root),
            {"CONTENT_OS_MAINTAINED_TALKING_CONFIG": str(config_file)})
