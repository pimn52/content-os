"""Synthetic inventory and fake subprocess only; no installed model execution."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
import pytest
from pydantic import TypeAdapter, ValidationError
from app.domain.models import ExecutionSpecificationRecord, ExecutionSpecificationV9, ExecutionSpecificationV6
from app.domain.talking_recipe import REQUIRED_TALKING_ARTIFACTS, VAE_CACHE, VAE_REVISION
from app.domain.talking_child import TalkingChildRequest
from app.providers import maintained_talking_execution as runtime
from app.providers.prepared import ExecutionPreparationError
from test_maintained_runtime import candidate, reference
from test_latentsync_provider import _inputs


@pytest.fixture
def talking_case(candidate, monkeypatch):
    original = candidate["options"]
    root = original["data_root"]
    options = {k:v for k,v in original.items() if k not in ("model_root","parameters","device")}
    options.update(max_bytes=16*1024**2,gpu_uuid="GPU-11111111-1111-1111-1111-111111111111",timeout_seconds=30,
        parameters=dict(device="cuda:0",inference_steps=20,guidance_scale=1.5,seed=7,subtitle_crop_bottom_ratio=0,terminal_face_closeout=False))
    for label in ("vendor","hf","torch","tools"):
        options[label] = root/label
        options[label].mkdir()
    options["checkpoint"] = root/"checkpoint.pt"
    options["checkpoint"].write_bytes(b"synthetic checkpoint")
    for name in REQUIRED_TALKING_ARTIFACTS - {"checkpoint/model.pt"}:
        path = root/name
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(b"synthetic local dependency")
    (root/"vendor/configs/unet/stage2.yaml").write_text("model:\n  cross_attention_dim: 384\n")
    (root/"hf"/VAE_CACHE/"refs/main").write_text(VAE_REVISION)
    inventory = runtime.observe_talking_inventory(options["environment"],options["base"],max_bytes=options["max_bytes"])
    from app.runtime_inventory import RuntimeInventoryStore
    original_read=RuntimeInventoryStore.read_v2
    monkeypatch.setattr(RuntimeInventoryStore,"read_v2",lambda _self,descriptor,**kwargs:
        inventory if descriptor.sha256==inventory.descriptor.sha256 else original_read(_self,descriptor,**kwargs))
    origin = {k:v for k,v in candidate["origin"].items() if k != "diagnostic_scope"}
    origin.update(origin_version=4,generation_scope="evaluation_talking_run_only",accepts_talking_runtime_trust=True,
        runtime_inventory_sha256=inventory.descriptor.sha256)
    path = root/"talking-origin.json"
    path.write_text(json.dumps(origin))
    options["origin_reference"] = reference(path,root)
    from app.domain.maintained_runtime import MaintainedHostObservation
    host = MaintainedHostObservation(os_family="windows",architecture="amd64",os_version="fixture",
        host_sha256="1"*64,layout_sha256="2"*64,device="cuda:0",gpu_uuid=options["gpu_uuid"],driver_version="1.0",driver_probe_sha256="3"*64)
    monkeypatch.setattr(runtime,"observe_host",lambda *_a,**_kw:host)
    profile, narration, _reference = _inputs(root)
    audio_path = Path(narration.source_file)
    if not audio_path.exists(): audio_path.write_bytes(b"synthetic audio")
    narration = narration.model_copy(update={"source_file":str(audio_path.resolve()),"content_hash":hashlib.sha256(audio_path.read_bytes()).hexdigest()})
    source = root/"reference.mp4"
    source.write_bytes(b"synthetic reference")
    request = TalkingChildRequest(profile=profile,narration=narration,reference_clip_id=uuid4(),reference_path=str(source),
        reference_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),reference_start_ms=0,reference_end_ms=10000)
    return SimpleNamespace(options=options,request=request,origin=origin,origin_path=path,root=root)


def prepare(case):
    try:
        approved = runtime.observe_talking(case.options)
    except Exception as exc:
        if isinstance(exc, ExecutionPreparationError):
            raise
        raise ExecutionPreparationError("maintained_talking_preparation_invalid") from None
    return runtime.prepare_maintained_talking_child(request=case.request,approved_spec=approved,**case.options)


def test_complete_resource_manifest_retains_empty_package_and_cache_files(talking_case):
    c = talking_case
    names = ("vendor/latentsync/__init__.py", "hf/.no_exist/cache-marker")
    paths = []
    for name in names:
        label, relative = name.split("/", 1)
        path = c.options[label]/relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
        paths.append(path)
    before = {a.name:a for a in runtime.observe_talking_artifacts(c.options)}
    for name in names:
        assert before[name].size_bytes == 0
        assert before[name].sha256 == hashlib.sha256(b"").hexdigest()
    paths[0].write_bytes(b"changed")
    after = {a.name:a for a in runtime.observe_talking_artifacts(c.options)}
    assert before.keys() == after.keys()
    assert after[names[0]].sha256 != before[names[0]].sha256
    assert after[names[0]].size_bytes == 7
    assert after[names[1]] == before[names[1]]


def test_preparation_distinct_schema_and_no_subprocess(talking_case,monkeypatch):
    monkeypatch.setattr(runtime.subprocess,"Popen",lambda *_a,**_kw:pytest.fail("preparation cannot launch"))
    with prepare(talking_case) as prepared:
        spec = TypeAdapter(ExecutionSpecificationRecord).validate_json(prepared.specification.model_dump_json())
        assert type(spec) is ExecutionSpecificationV9
        assert spec.capability == "talking"
        with pytest.raises(ValidationError): ExecutionSpecificationV6.model_validate_json(spec.model_dump_json())
        monkeypatch.setattr(runtime,"observe_talking",lambda *_a,**_kw:pytest.fail("normal execution must reuse the admitted spec"))
        assert prepared.verify() == spec
        snapshot = prepared.execution_use_snapshot()
        assert snapshot["execution_sha256"] == spec.execution_sha256
        snapshot["execution_specification"]["parameters"]["seed"] = 100
        assert prepared.specification.parameters["seed"] == 7


@pytest.mark.parametrize("mutation",["vendor","hf","torch","tools","checkpoint","audio","source","bundle"])
def test_identity_changes_prevent_launch(talking_case,monkeypatch,mutation):
    with prepare(talking_case) as prepared:
        if mutation == "bundle":
            path = Path(prepared._temporary.name)/"application.pyz"
        elif mutation == "audio": path = Path(talking_case.request.narration.source_file)
        elif mutation == "source": path = Path(talking_case.request.reference_path)
        elif mutation == "checkpoint": path = talking_case.options["checkpoint"]
        else: path = next(p for p in talking_case.options[mutation].rglob("*") if p.is_file())
        path.write_bytes(path.read_bytes()+b"changed")
        monkeypatch.setattr(runtime.subprocess,"Popen",lambda *_a,**_kw:pytest.fail("changed identity must not launch"))
        with pytest.raises(ExecutionPreparationError,match="identity_changed"): prepared.execute()


@pytest.mark.parametrize("mutation",["missing_cache","old_voice_origin","old_asr_origin","unaccepted_trust","network_claim"])
def test_missing_local_dependencies_and_foreign_trust_fail(talking_case,mutation):
    c = talking_case
    if mutation == "missing_cache": (c.root/"torch/hub/checkpoints/2DFAN4-cd938726ad.zip").unlink()
    else:
        origin = dict(c.origin)
        if mutation == "old_voice_origin":
            origin.pop("accepts_talking_runtime_trust"); origin.update(origin_version=2,generation_scope="voice_clone_only",accepts_generation_runtime_trust=True)
        elif mutation == "old_asr_origin":
            origin.pop("accepts_talking_runtime_trust"); origin.pop("generation_scope"); origin.update(origin_version=3,transcription_scope="voice_evaluation_qa_only",accepts_transcription_runtime_trust=True)
        elif mutation == "unaccepted_trust": origin["accepts_talking_runtime_trust"] = False
        else: origin["network_isolation"] = "external_control"
        c.origin_path.write_text(json.dumps(origin)); c.options["origin_reference"] = reference(c.origin_path,c.root)
    with pytest.raises(ExecutionPreparationError): prepare(c)


def test_consumed_child_cannot_launch_twice_and_uses_explicit_cache(talking_case,monkeypatch):
    calls = []
    with prepare(talking_case) as prepared:
        private = Path(prepared._temporary.name)
        monkeypatch.setattr(runtime,"_system_root",lambda:talking_case.root)
        def fake(argv,**kw):
            calls.append((argv,kw))
            (private/"output.mp4").write_bytes(b"synthetic output")
            return SimpleNamespace(wait=lambda **_kw:0,poll=lambda:0)
        monkeypatch.setattr(runtime.subprocess,"Popen",fake)
        assert prepared.execute().read_bytes() == b"synthetic output"
        with pytest.raises(ExecutionPreparationError,match="consumed"): prepared.execute()
        assert len(calls) == 1 and calls[0][1]["shell"] is False
        env = calls[0][1]["env"]
        assert env["HF_HOME"] == str(talking_case.options["hf"]) and env["TORCH_HOME"] == str(talking_case.options["torch"])
        assert env["HF_HUB_OFFLINE"] == "1"


@pytest.mark.parametrize("duration",[1200,1280])
def test_reference_covers_existing_frame_alignment_without_loop(talking_case,monkeypatch,duration):
    c = talking_case
    c.request = c.request.model_copy(update={"reference_start_ms":0,"reference_end_ms":duration})
    monkeypatch.setattr(runtime.subprocess,"Popen",lambda *_a,**_kw:pytest.fail("preparation cannot launch"))
    if duration == 1200:
        # The Master is1200ms, but the adapter needs1280ms of forward source.
        with pytest.raises(ExecutionPreparationError,match="alignment_not_covered"): prepare(c)
    else:
        with prepare(c) as child:
            assert runtime.require_reference_coverage(child.request_snapshot()) == 1280
