"""Synthetic source trees/fake runtime only; no actual vendor staging/import."""
import ast
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import wave

import pytest
from pydantic import ValidationError

from app.providers import omnivoice_entry_v3 as entry
from app.providers import omnivoice_prepared as prepared
from app.providers.omnivoice_audio_pcm import PCMAudioDerivation, CONVERSION, TRANSFORM
from app.providers.omnivoice_local import OmniVoiceLocalParameters
from app.providers.omnivoice_recipe_v2 import parse_local_parameters
from app.providers.omnivoice_recipe_v3 import OmniVoiceLocalParametersV3, select_local_omnivoice_v3
from app.providers.prepared import ExecutionPreparationError, NativeExecutionUnsupported
from app.providers.runtime_tree import RuntimeTreeSelection, RuntimeFileSelection, prepare_runtime_tree
from test_omnivoice_components_v2 import parameters as v2_parameters, v2_tree, inputs, runtime
from test_omnivoice_local import model_tree
from test_omnivoice_prepared import ready, raw_spec
from test_component_policy2 import policy2


def parameters(**changes):
    values = v2_parameters().model_dump() | dict(parameter_version=3,
        audio_transform=TRANSFORM, pcm_conversion=CONVERSION,
        output_max_frames=100, output_max_bytes=1000)
    return OmniVoiceLocalParametersV3.model_validate(values | changes)


@pytest.fixture
def source_tree(tmp_path_factory, monkeypatch):
    root = tmp_path_factory.mktemp("synthetic-runtime")
    original = b"synthetic upstream, not vendor code\n"
    derived = b"synthetic stopped decoders, not executable vendor code\n"
    descriptor = b'{"fixture":true,"dispatch_authorized":false}\n'
    def derive(raw):
        if raw != original:
            raise ValueError()
        return PCMAudioDerivation(derived, descriptor)
    monkeypatch.setattr(entry.pcm, "derive_pcm_audio", derive)
    monkeypatch.setattr(entry, "SOURCE_HASHES", {"package/unchanged.py": hashlib.sha256(b"fixed source").hexdigest()})
    contents = {entry.UPSTREAM_SLOT: original, entry.DERIVED_SLOT: derived,
        entry.DESCRIPTOR_SLOT: descriptor, "Lib/site-packages/package/unchanged.py": b"fixed source",
        "python.exe": b"synthetic interpreter"}
    for name in entry.OWNED_MODULES:
        contents["Lib/site-packages/app/providers/" + name] = (Path(entry.__file__).parent / name).read_bytes()
    for name, payload in contents.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    return root


def test_explicit_version_and_legacy_serialization(v2_tree):
    params = parameters()
    assert type(parse_local_parameters(params.model_dump())) is OmniVoiceLocalParametersV3
    selected = select_local_omnivoice_v3(v2_tree, params)
    assert selected.recipe_version == 3 and selected.parameters == params
    assert params.generation_kwargs() == v2_parameters().generation_kwargs()
    assert all(params.generation_kwargs()[flag] is True for flag in
               ("denoise", "preprocess_prompt", "postprocess_output"))
    for old in (OmniVoiceLocalParameters(), v2_parameters()):
        assert parse_local_parameters(old.model_dump()).model_dump_json() == old.model_dump_json()
        with pytest.raises(ValidationError): OmniVoiceLocalParametersV3.model_validate(old.model_dump())
    with pytest.raises(ValidationError): type(v2_parameters()).model_validate(params.model_dump())
    with pytest.raises(NativeExecutionUnsupported): selected.require_native_preparation()


@pytest.mark.parametrize("change", [dict(parameter_version=3.0), dict(parameter_version=2),
    dict(audio_transform="other"), dict(pcm_conversion="other"), dict(output_max_frames=True),
    dict(output_max_frames=0), dict(output_max_bytes=44), dict(output_max_bytes=64*1024**2+1),
    dict(audio_tokenizer_precision="float16"), dict(denoise="false")])
def test_parameter_changes_reject(change):
    with pytest.raises(ValidationError): parameters(**change)


def test_source_identity_and_inventory(source_tree):
    names = [entry.UPSTREAM_SLOT, entry.DERIVED_SLOT, entry.DESCRIPTOR_SLOT,
        "Lib/site-packages/package/unchanged.py",
        *("Lib/site-packages/app/providers/" + n for n in entry.OWNED_MODULES)]
    rows = [SimpleNamespace(name=n, size_bytes=(source_tree/n).stat().st_size,
        sha256=hashlib.sha256((source_tree/n).read_bytes()).hexdigest()) for n in names]
    assert entry.require_pcm_recipe_sources(source_tree, inventory=SimpleNamespace(files=rows))
    with pytest.raises(ExecutionPreparationError, match="source_changed"):
        entry.require_pcm_recipe_sources(source_tree, inventory=SimpleNamespace(files=rows[1:]))
    rows[0].sha256 = "a"*64
    with pytest.raises(ExecutionPreparationError):
        entry.require_pcm_recipe_sources(source_tree, inventory=SimpleNamespace(files=rows))
    assert not (source_tree/entry.UPSTREAM_SLOT).is_relative_to(source_tree/"Lib/site-packages")


@pytest.mark.parametrize("slot", [entry.UPSTREAM_SLOT, entry.DERIVED_SLOT, entry.DESCRIPTOR_SLOT,
    "Lib/site-packages/package/unchanged.py",
    *("Lib/site-packages/app/providers/" + n for n in entry.OWNED_MODULES)])
def test_each_selected_source_change_rejects(source_tree, slot):
    (source_tree/slot).write_bytes(b"changed")
    with pytest.raises(ExecutionPreparationError, match="source_changed"):
        entry.require_pcm_recipe_sources(source_tree)


def test_invocation_has_independent_paths_and_no_spawn(source_tree, v2_tree, inputs):
    selected = select_local_omnivoice_v3(v2_tree, parameters())
    files = {("model", a.name): a.source for a in selected.artifacts}
    files.update({("runtime", str(p.relative_to(source_tree)).replace("\\", "/")): p
                  for p in source_tree.rglob("*") if p.is_file()})
    with pytest.raises(ExecutionPreparationError, match='invocation_invalid'):
        entry.build_local_clone_invocation_v3(files, parameters().model_dump(), inputs=inputs, timeout_seconds=10)
    for wrong in (float("nan"), 0, True, float("inf")):
        with pytest.raises(ExecutionPreparationError):
            entry.build_local_clone_invocation_v3(files, parameters().model_dump(), inputs=inputs, timeout_seconds=wrong)
    files[("runtime", entry.DERIVED_SLOT)] = source_tree / entry.UPSTREAM_SLOT
    with pytest.raises(ExecutionPreparationError):
        entry.build_local_clone_invocation_v3(files, parameters().model_dump(), inputs=inputs, timeout_seconds=10)


class FakeArray:
    def __init__(self, values, *, ndim=1, dtype="float32"):
        self.values, self.ndim, self.dtype, self.size = values, ndim, dtype, len(values)
    def __iter__(self): return iter(self.values)


@pytest.fixture
def fake_runtime(runtime, monkeypatch):
    runtime.numpy.ndarray = FakeArray
    runtime.numpy.dtype = lambda name: name
    def generate(self, **kwargs):
        runtime.events.append(("generate", kwargs))
        return [FakeArray([0.25, -0.5, 1.0])]
    monkeypatch.setattr(runtime.model_class, "generate", generate)
    monkeypatch.setattr(entry, "_load_runtime", lambda boundary=None: runtime)
    monkeypatch.setattr(entry, "require_pcm_recipe_sources", lambda *_a, **_kw: "fixture")
    # Output/reference unit harness only. Actual boundary integration is tested
    # with full synthetic PreparedRuntimeTree in test_omnivoice_pcm_import.py.
    monkeypatch.setattr(entry, 'require_pcm_boundary', lambda *_a, **_kw: None)
    return runtime


def test_output_reference_and_generation_binding(v2_tree, inputs, fake_runtime):
    target = entry.execute_local_clone_v3(v2_tree, parameters().model_dump_json(), inputs.model_dump_json())
    with wave.open(str(target), "rb") as output:
        assert (output.getnchannels(), output.getsampwidth(), output.getframerate(), output.getnframes()) == (1, 2, 24000, 3)
        assert output.readframes(3) == b"\x00\x20\x00\xc0\xff\x7f"
    generated = next(e[1] for e in fake_runtime.events if e[0] == "generate")
    assert generated["ref_text"] == inputs.reference_text
    assert generated["ref_audio"][1] == 24000 and len(generated["ref_audio"][0]) == 3
    assert all(generated[k] is True for k in ("denoise", "preprocess_prompt", "postprocess_output"))
    assert not any(e[0] == "write" for e in fake_runtime.events)  # SoundFile unused


@pytest.mark.parametrize("output", [[], [[0.1]], [FakeArray([])], [FakeArray([0.1], ndim=2)],
    [FakeArray([0.1], dtype="float64")], [FakeArray([float("nan")])],
    [FakeArray([float("inf")])], [FakeArray([0.1]*101)], [FakeArray([0.1]), FakeArray([0.2])]])
def test_invalid_outputs_create_no_file(v2_tree, inputs, fake_runtime, monkeypatch, output):
    monkeypatch.setattr(fake_runtime.model_class, "generate", lambda *a, **kw: output)
    with pytest.raises(ExecutionPreparationError, match="output_invalid"):
        entry.execute_local_clone_v3(v2_tree, parameters().model_dump_json(), inputs.model_dump_json())
    assert not Path(inputs.output_path).exists()


@pytest.mark.parametrize("stop", [entry.pcm.DECODE_STOP, "other runtime failure"])
def test_decoder_stop_has_no_retry(v2_tree, inputs, fake_runtime, monkeypatch, stop):
    attempts = []
    def generate(*a, **kw):
        attempts.append(1)
        raise RuntimeError(stop)
    monkeypatch.setattr(fake_runtime.model_class, "generate", generate)
    with pytest.raises(ExecutionPreparationError, match=stop if stop == entry.pcm.DECODE_STOP else "local_clone_failed"):
        entry.execute_local_clone_v3(v2_tree, parameters().model_dump_json(), inputs.model_dump_json())
    assert attempts == [1] and not Path(inputs.output_path).exists()


def test_source_failure_precedes_runtime(v2_tree, inputs, fake_runtime, monkeypatch):
    def reject(*a, **kw): raise ExecutionPreparationError("omnivoice_pcm_recipe_source_changed")
    monkeypatch.setattr(entry, "require_pcm_recipe_sources", reject)
    monkeypatch.setattr(entry, "_load_runtime", lambda: pytest.fail("must not load"))
    with pytest.raises(ExecutionPreparationError, match="source_changed"):
        entry.execute_local_clone_v3(v2_tree, parameters().model_dump_json(), inputs.model_dump_json())
    assert not fake_runtime.events and not Path(inputs.output_path).exists()


def test_existing_and_racing_outputs_preserved(v2_tree, inputs, fake_runtime, monkeypatch):
    path = Path(inputs.output_path)
    path.write_bytes(b"existing")
    with pytest.raises(ExecutionPreparationError, match="output_conflict"):
        entry.execute_local_clone_v3(v2_tree, parameters().model_dump_json(), inputs.model_dump_json())
    assert path.read_bytes() == b"existing" and not fake_runtime.events
    updated = inputs.model_copy(update={"output_path": str(path.with_name("race.wav"))})
    race = Path(updated.output_path)
    def generate(*a, **kw):
        race.write_bytes(b"concurrent")
        return [FakeArray([0.1])]
    monkeypatch.setattr(fake_runtime.model_class, "generate", generate)
    with pytest.raises(ExecutionPreparationError, match="conflict"):
        entry.execute_local_clone_v3(v2_tree, parameters().model_dump_json(), updated.model_dump_json())
    assert race.read_bytes() == b"concurrent"


def test_output_byte_budget_stops_before_creation(v2_tree, inputs, fake_runtime):
    with pytest.raises(ExecutionPreparationError, match="output_invalid"):
        entry.execute_local_clone_v3(v2_tree, parameters(output_max_bytes=45).model_dump_json(), inputs.model_dump_json())
    assert not Path(inputs.output_path).exists()


def test_current_owned_source_rechecked(source_tree, monkeypatch):
    original = entry._read_source
    owned = Path(entry.__file__).resolve().parent / "omnivoice_audio_pcm.py"
    def changed(path):
        raw = original(path)
        return raw+b"# changed implementation\n" if path == owned else raw
    monkeypatch.setattr(entry, "_read_source", changed)
    with pytest.raises(ExecutionPreparationError, match="source_changed"):
        entry.require_pcm_recipe_sources(source_tree)


def test_loader_checks_sources_before_vendor_import(monkeypatch):
    def reject(*a, **kw): raise ExecutionPreparationError("omnivoice_pcm_recipe_source_changed")
    monkeypatch.setattr(entry, "require_pcm_recipe_sources", reject)
    with pytest.raises(ExecutionPreparationError, match="source_changed"): entry._load_runtime()
    tree = ast.parse(Path(entry.__file__).read_bytes())
    assert not any(isinstance(n, ast.Import) and any(a.name == "soundfile" for a in n.names) for n in ast.walk(tree))


def test_partial_v3_tree_requires_import_boundary(ready, source_tree, monkeypatch):
    (ready.model/"config.json").write_text(json.dumps(dict(model_type="omnivoice",
        llm_config=dict(model_type="qwen3", rope_parameters=dict(rope_type="default")))))
    # Add actual owned policy bytes to the synthetic runtime; no vendor code.
    for slot in ("windows_component_policy.json", "windows_os_policy.json"):
        (source_tree/"Lib/site-packages/app/providers"/slot).write_bytes(
            (ready.runtime.root/"Lib/site-packages/app/providers"/slot).read_bytes())
    (source_tree/"entry.py").write_bytes(b"synthetic entry")
    with prepare_runtime_tree(trees=(RuntimeTreeSelection(source_tree/"Lib", "Lib"),
            RuntimeTreeSelection(source_tree/"evidence", "evidence")),
            files=(RuntimeFileSelection(source_tree/"python.exe", "python.exe", "interpreter"),
                RuntimeFileSelection(source_tree/"entry.py", "entry.py", "entrypoint")),
            staging_parent=ready.staging, max_bytes=2*1024**2) as rt:
        ready.host.tree = rt
        with pytest.raises(ExecutionPreparationError, match='boundary_required'):
            ready.prepare(parameters=parameters(), runtime=rt)


def test_missing_v3_sources_stop_before_model_staging(ready):
    before = set(ready.staging.iterdir())
    with pytest.raises(ExecutionPreparationError, match="source_changed"):
        ready.prepare(parameters=parameters())
    assert set(ready.staging.iterdir()) == before
