"""Complete synthetic file selections only; no vendor import or native calls."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.providers import cpu_native_recipe as cpu
from app.providers import omnivoice_pcm_import as subject
from app.providers import omnivoice_entry_v3 as entry
from app.providers.prepared import ExecutionPreparationError, NativeExecutionUnsupported
from app.providers.runtime_tree import RuntimeTreeSelection, RuntimeFileSelection, prepare_runtime_tree
from app.providers.windows_component_policy import owned_component_policy_bytes
from app.providers.windows_os_policy import owned_os_policy_bytes
from app.providers.omnivoice_recipe_v3 import select_local_omnivoice_v3
from test_omnivoice_pcm_v3 import source_tree, parameters, FakeArray
from test_omnivoice_components_v2 import v2_tree, inputs, runtime
from test_omnivoice_local import model_tree
from test_omnivoice_prepared import ready, raw_spec
from test_component_policy2 import policy2


@pytest.fixture
def layout(source_tree, tmp_path_factory, monkeypatch):
    # Substitute only fixture source identities, not production/native authority.
    raw = b'synthetic CPU and optional-import source, not vendor code\n'
    digest = hashlib.sha256(raw).hexdigest()
    policy, policy_hash = cpu._owned()
    monkeypatch.setattr(cpu, '_owned', lambda: (policy | {'sources': {'torch/__init__.py': digest}}, policy_hash))
    monkeypatch.setattr(subject, 'SOURCE_PINS', {'transformers/optional.py': digest})
    for slot in ('torch/__init__.py', 'transformers/optional.py'):
        path = source_tree/'Lib/site-packages'/slot
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    providers = source_tree/'Lib/site-packages/app/providers'
    for name in subject.OWNED_FILES:
        (providers/name).write_bytes((Path(subject.__file__).parent/name).read_bytes())
    (providers/'windows_component_policy.json').write_bytes(owned_component_policy_bytes())
    (providers/'windows_os_policy.json').write_bytes(owned_os_policy_bytes())
    (source_tree/'DLLs').mkdir()
    (source_tree/subject.BOOTSTRAP_SLOT).write_bytes(subject.owned_pcm_bootstrap_bytes())
    for name in ('python._pth', 'python312._pth'):
        (source_tree/name).write_bytes(subject.PTH)
    (source_tree/subject.POLICY_SLOT).write_bytes(subject.owned_pcm_import_policy_bytes())
    context = tmp_path_factory.mktemp('pcm-context')
    staging, work, windows = (context/name for name in ('pcm-staging', 'pcm-work', 'pcm-windows'))
    for path in (staging, work, windows): path.mkdir()
    (windows/'System32').mkdir()
    def stage():
        return prepare_runtime_tree(
            trees=tuple(RuntimeTreeSelection(source_tree/name, name) for name in ('Lib', 'DLLs', 'evidence')),
            files=(RuntimeFileSelection(source_tree/'python.exe', 'python.exe', 'interpreter'),
                   RuntimeFileSelection(source_tree/subject.BOOTSTRAP_SLOT, subject.BOOTSTRAP_SLOT, 'entrypoint'),
                   *(RuntimeFileSelection(source_tree/name, name, 'dependency') for name in ('python._pth', 'python312._pth'))),
            staging_parent=staging, max_bytes=2*1024**2)
    return SimpleNamespace(source=source_tree, stage=stage, work=work, windows=windows)


@pytest.fixture
def boundary(layout):
    with layout.stage() as tree:
        value = subject.prepare_pcm_import_boundary(tree=tree, work_directory=layout.work, windows_root=layout.windows)
        yield value
        value.close()


def files_for(tree, model):
    selected = select_local_omnivoice_v3(model, parameters())
    return {('runtime', row.name): tree.root/row.name for row in tree.inventory.files} | {
        ('model', item.name): item.source for item in selected.artifacts}


def test_complete_selection_command_and_pending_authority(boundary, v2_tree, inputs):
    observation = boundary.verify()
    assert not observation.dispatch_authorized and not observation.loading_authorized
    assert 'actual_fresh_child_startup_and_import_finders' in observation.pending
    invocation = entry.build_local_clone_invocation_v3(files_for(boundary.tree, v2_tree),
        parameters().model_dump(), inputs=inputs, timeout_seconds=10, pcm_boundary=boundary)
    assert invocation.environment == boundary.environment.environment
    assert dict(invocation.environment)['TORIO_USE_FFMPEG_VERSION'] == ''
    assert dict(invocation.environment)['TORCHAUDIO_USE_SOX'] == '0'
    assert invocation.argv[1:4] == ('-I', '-B', str(boundary.tree.root/subject.BOOTSTRAP_SLOT))
    with pytest.raises(NativeExecutionUnsupported): boundary.require_native_preparation()
    files = files_for(boundary.tree, v2_tree)
    files.pop(('runtime', subject.POLICY_SLOT))
    with pytest.raises(ExecutionPreparationError, match='invocation_invalid'):
        entry.build_local_clone_invocation_v3(files, parameters().model_dump(), inputs=inputs,
            timeout_seconds=10, pcm_boundary=boundary)


@pytest.mark.parametrize('name', ['soundfile.py', 'librosa', 'soxr.cp312-win_amd64.pyd',
    'torchcodec', 'SoundFile-1.dist-info', 'soundfile.dist-info', '_soundfile.py', '_soundfile_data',
    'soxr.so', 'torchcodec.dll', 'librosa.egg-info'])
@pytest.mark.parametrize('root', subject.IMPORT_ROOTS)
def test_optional_import_forms_stop_selection(layout, name, root):
    path = layout.source/root/name
    if '.' not in name or name.endswith('info'): path.mkdir()
    else: path.write_bytes(b'synthetic optional member')
    with layout.stage() as tree:
        with pytest.raises(ExecutionPreparationError, match='optional_package_visible'):
            subject.prepare_pcm_import_boundary(tree=tree, work_directory=layout.work, windows_root=layout.windows)


@pytest.mark.parametrize('name', ['vendor.zip', 'vendor.egg', 'vendor.whl', 'anything.pyc',
    'extra.pth', 'extra._pth', 'sitecustomize.py', 'usercustomize.py', '__pycache__', 'pyvenv.cfg'])
def test_unreviewed_startup_layout_rejected(layout, name):
    path = layout.source/'Lib'/name
    if name == '__pycache__': path.mkdir()
    else: path.write_bytes(b'unsupported startup form')
    with pytest.raises(ExecutionPreparationError, match='startup_layout_unsupported|bytecode_unsupported'):
        with layout.stage() as tree:
            subject.prepare_pcm_import_boundary(tree=tree, work_directory=layout.work, windows_root=layout.windows)


@pytest.mark.parametrize('slot', ['python._pth', 'python312._pth', subject.POLICY_SLOT, subject.BOOTSTRAP_SLOT,
    'Lib/site-packages/transformers/optional.py', 'Lib/site-packages/torch/__init__.py'])
def test_wrong_current_source_rejected_before_boundary(layout, slot):
    (layout.source/slot).write_bytes(b'changed')
    with layout.stage() as tree:
        with pytest.raises((ExecutionPreparationError, NativeExecutionUnsupported)):
            subject.prepare_pcm_import_boundary(tree=tree, work_directory=layout.work, windows_root=layout.windows)


@pytest.mark.parametrize('slot', [subject.POLICY_SLOT, subject.BOOTSTRAP_SLOT, 'Lib/site-packages/new.py', 'python312._pth'])
def test_current_tree_changes_consume_boundary(boundary, slot):
    (boundary.tree.root/slot).write_bytes(b'changed')
    with pytest.raises(ExecutionPreparationError): boundary.verify()
    with pytest.raises(ExecutionPreparationError, match='consumed'): boundary.verify()


def test_changed_declared_environment_consumes_boundary(boundary):
    object.__setattr__(boundary.environment, 'environment', (('HF_HUB_OFFLINE', '1'),))
    with pytest.raises(ExecutionPreparationError, match='environment_changed'): boundary.verify()
    with pytest.raises(ExecutionPreparationError, match='consumed'): boundary.verify()


def test_windows_system_boundary_rechecked(boundary):
    (boundary.environment.windows_root/'System32').rmdir()
    with pytest.raises(ExecutionPreparationError): boundary.verify()
    with pytest.raises(ExecutionPreparationError, match='consumed'): boundary.verify()


def test_foreign_tree_and_declared_root_rejected(boundary, layout):
    with layout.stage() as foreign:
        with pytest.raises(ExecutionPreparationError, match='boundary_required'):
            subject.require_pcm_boundary(boundary, tree=foreign)
    with pytest.raises(ExecutionPreparationError):
        subject.prepare_pcm_import_boundary(tree=boundary.tree,
            work_directory=boundary.tree.root, windows_root=layout.windows)
    assert boundary.verify()  # A caller mismatch does not alter the original.


def test_full_boundary_preserves_fake_generation_controls(boundary, v2_tree, inputs, runtime, monkeypatch):
    for key, value in boundary.environment.environment: monkeypatch.setenv(key, value)
    runtime.numpy.ndarray = FakeArray
    runtime.numpy.dtype = lambda name: name
    def generate(_self, **kwargs):
        runtime.events.append(('generate', kwargs))
        return [FakeArray([0.25, -0.5, 1.0])]
    monkeypatch.setattr(runtime.model_class, 'generate', generate)
    def fake_load(value):
        assert value is boundary and value.verify()
        return runtime
    monkeypatch.setattr(entry, '_load_runtime', fake_load)  # Sole fake execution seam.
    target = entry.execute_local_clone_v3(v2_tree, parameters().model_dump_json(),
        inputs.model_dump_json(), pcm_boundary=boundary)
    assert target.is_file() and not any(event[0] == 'write' for event in runtime.events)
    generated = next(event[1] for event in runtime.events if event[0] == 'generate')
    assert generated['ref_text'] == inputs.reference_text
    assert all(generated[key] is True for key in ('denoise', 'preprocess_prompt', 'postprocess_output'))


@pytest.mark.parametrize('name', ['_torio_ffmpeg.pyd', 'libtorio_ffmpeg.dll', '_torio_ffmpeg6.pyd'])
def test_unversioned_ffmpeg_absence_not_blanket_removal(layout, name):
    path = layout.source/'Lib/site-packages/torio/lib'/name
    path.parent.mkdir(parents=True)
    path.write_bytes(b'synthetic native member, never loaded')
    with layout.stage() as tree:
        if name == '_torio_ffmpeg6.pyd':
            value = subject.prepare_pcm_import_boundary(tree=tree, work_directory=layout.work, windows_root=layout.windows)
            assert value.verify() and not value.verify().loading_authorized
            value.close()
        else:
            with pytest.raises(NativeExecutionUnsupported):
                subject.prepare_pcm_import_boundary(tree=tree, work_directory=layout.work, windows_root=layout.windows)


def test_prepared_identity_rechecks_same_boundary(ready, boundary):
    (ready.model/'config.json').write_text(json.dumps(dict(model_type='omnivoice',
        llm_config=dict(model_type='qwen3', rope_parameters=dict(rope_type='default')))))
    ready.host.tree = boundary.tree
    with ready.prepare(parameters=parameters(), runtime=boundary.tree, pcm_boundary=boundary) as identity:
        initial = identity.execution_use_snapshot()
        assert identity.execution_use_snapshot()['execution_sha256'] == initial['execution_sha256']
        boundary.close()
        with pytest.raises(ExecutionPreparationError, match='consumed'): identity.execution_use_snapshot()
    assert boundary.tree.verify()  # Boundary does not own tree cleanup.


def test_loader_remains_closed_before_vendor_import(boundary, monkeypatch):
    monkeypatch.setattr(entry, '__file__', str(boundary.tree.root/'Lib/site-packages/app/providers/omnivoice_entry_v3.py'))
    with pytest.raises(NativeExecutionUnsupported, match='dependency_closure'): entry._load_runtime(boundary)


@pytest.mark.parametrize('kind', ['missing_empty_value', 'foreign_path', 'wrong_boundary'])
def test_environment_and_boundary_failure_precede_generation(boundary, v2_tree, inputs, monkeypatch, kind):
    for key, value in boundary.environment.environment: monkeypatch.setenv(key, value)
    monkeypatch.setattr(entry, '_load_runtime', lambda *_: pytest.fail('no runtime load authorized'))
    value = boundary
    if kind == 'missing_empty_value': monkeypatch.delenv('TORIO_USE_FFMPEG_VERSION')
    elif kind == 'foreign_path': monkeypatch.setenv('PATH', 'foreign search path')
    else: value = SimpleNamespace(tree=boundary.tree)
    with pytest.raises(ExecutionPreparationError, match='environment_changed|boundary_required'):
        entry.execute_local_clone_v3(v2_tree, parameters().model_dump_json(), inputs.model_dump_json(), pcm_boundary=value)
    assert not Path(inputs.output_path).exists()
