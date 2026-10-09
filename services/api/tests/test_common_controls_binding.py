"""Temporary synthetic component-store bytes; no Windows API or loading."""
from dataclasses import replace
import hashlib

import pytest

from app.providers.common_controls_binding import _compare_common_controls
from app.providers.windows_activation import ActivationMetadata, ActivationAssembly, ActivationFile
from app.providers.runtime_primitives import NativeExecutionUnsupported


@pytest.fixture
def binding(tmp_path):
    root = tmp_path / 'Windows'
    component = root / 'WinSxS' / 'amd64_microsoft.windows.common-controls_6595b64144ccf1df_6.0.26100.9457_none_0123456789abcdef'
    component.mkdir(parents=True)
    manifests = root / 'WinSxS' / 'Manifests'
    manifests.mkdir()
    code = component / 'comctl32.dll'
    code.write_bytes(b'synthetic code')
    manifest = manifests / (component.name + '.manifest')
    manifest.write_bytes(b'synthetic OS manifest')
    source = tmp_path / 'source.dll'
    source.write_bytes(b'synthetic source: validated by reader separately')
    identity = 'Microsoft.Windows.Common-Controls,processorArchitecture="amd64",publicKeyToken="6595b64144ccf1df",type="win32",version="6.0.26100.9457",language="none"'
    root_assembly = ActivationAssembly('', str(source), '', '', 1, 0, (1, 0), (0, 0), 0, (), 0)
    assembly = ActivationAssembly(identity, str(manifest), '', component.name, 1, 0,
        (1, 0), (0, 0), 0, (ActivationFile('comctl32.dll', str(code), 0),), 0)
    metadata = ActivationMetadata(1, str(source), '', str(tmp_path), (1, 0, 1), (root_assembly, assembly), 0)
    kwargs = dict(source=source, application_directory=tmp_path, windows_root=root)
    return metadata, kwargs, code, manifest


def test_exact_component_bytes_paths_serviced_version_and_no_authority(binding):
    metadata, kwargs, code, manifest = binding
    result = _compare_common_controls(metadata, **kwargs)
    assert result.version == '6.0.26100.9457' and result.architecture == 'amd64'
    assert result.language == 'none' and not result.dispatch_authorized
    assert result.code.sha256 == hashlib.sha256(code.read_bytes()).hexdigest()
    assert result.manifest.sha256 == hashlib.sha256(manifest.read_bytes()).hexdigest()
    assert result.code.windows_relative_path.endswith('/comctl32.dll')
    assert result.policy is None


@pytest.mark.parametrize('problem', ['extra_assembly', 'extra_file', 'name', 'token', 'arch',
    'version', 'wildcard', 'duplicate', 'unknown_key', 'identity_length', 'private', 'driverstore',
    'directory', 'manifest', 'root_manifest', 'configuration', 'app_directory',
    'root_identity', 'root_files', 'satellite', 'policy', 'code_name'])
def test_named_binding_stops(binding, problem, tmp_path):
    metadata, kwargs, code, manifest = binding
    root, assembly = metadata.assemblies
    if problem == 'extra_assembly': metadata = replace(metadata, assemblies=(root, assembly, assembly))
    if problem == 'extra_file': assembly = replace(assembly, files=assembly.files * 2)
    if problem in ('name', 'token', 'arch', 'version', 'wildcard'):
        old, new = {'name': ('Microsoft.Windows.Common-Controls', 'Other'), 'token': ('6595b64144ccf1df', '0000000000000000'),
            'arch': ('amd64', 'x86'), 'version': ('6.0.26100.9457', '6.0.0.0'), 'wildcard': ('language="none"', 'language="*"')}[problem]
        assembly = replace(assembly, identity=assembly.identity.replace(old, new))
    if problem == 'duplicate': assembly = replace(assembly, identity=assembly.identity + ',type="win32"')
    if problem == 'unknown_key': assembly = replace(assembly, identity=assembly.identity + ',anything="value"')
    if problem == 'identity_length': assembly = replace(assembly, identity='x' * 2049)
    if problem in ('private', 'driverstore'):
        path = tmp_path / ('private' if problem == 'private' else 'Windows/DriverStore') / 'comctl32.dll'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'same name does not grant eligibility')
        assembly = replace(assembly, files=(ActivationFile('comctl32.dll', str(path), 0),))
    if problem == 'directory': assembly = replace(assembly, directory='foreign')
    if problem == 'manifest': assembly = replace(assembly, manifest=str(kwargs['source']))
    if problem == 'root_manifest': metadata = replace(metadata, root_manifest=str(manifest))
    if problem == 'configuration': metadata = replace(metadata, root_configuration='private.config')
    if problem == 'app_directory': metadata = replace(metadata, application_directory='foreign')
    if problem == 'root_identity': root = replace(root, identity='extra identity')
    if problem == 'root_files': root = replace(root, files=assembly.files)
    if problem == 'satellite': assembly = replace(assembly, satellite=1)
    if problem == 'policy': assembly = replace(assembly, policy=str(kwargs['source']))
    if problem == 'code_name': assembly = replace(assembly, files=(ActivationFile('other.dll', str(code), 0),))
    if problem != 'extra_assembly': metadata = replace(metadata, assemblies=(root, assembly))
    with pytest.raises(NativeExecutionUnsupported, match='common_controls_binding_unsupported'):
        _compare_common_controls(metadata, **kwargs)


@pytest.mark.parametrize('which', ['code', 'manifest'])
def test_reobservation_detects_changed_bytes_without_new_authority(binding, which):
    metadata, kwargs, code, manifest = binding
    before = _compare_common_controls(metadata, **kwargs)
    (code if which == 'code' else manifest).write_bytes(b'changed system component')
    after = _compare_common_controls(metadata, **kwargs)
    assert before != after and not after.dispatch_authorized


def test_foreign_root_and_relative_alias_rejected(binding):
    metadata, kwargs, code, _ = binding
    root, assembly = metadata.assemblies
    for path in (str(code.parent / '..' / code.parent.name / code.name), code.name):
        changed = replace(assembly, files=(ActivationFile(code.name, path, 0),))
        with pytest.raises(NativeExecutionUnsupported):
            _compare_common_controls(replace(metadata, assemblies=(root, changed)), **kwargs)


@pytest.mark.parametrize('level', ['context', 'root', 'component', 'file'])
@pytest.mark.parametrize('flags', [0x11, 0xffffffff, None, False])
def test_flags_have_no_implicit_admission_or_missing_zero_default(binding, level, flags, monkeypatch):
    from app.providers import common_controls_binding as mod
    metadata, kwargs, _, _ = binding
    root, assembly = metadata.assemblies
    if level == 'context': metadata = replace(metadata, flags=flags)
    if level == 'root': root = replace(root, flags=flags)
    if level == 'component': assembly = replace(assembly, flags=flags)
    if level == 'file': assembly = replace(assembly, files=(replace(assembly.files[0], flags=flags),))
    metadata = replace(metadata, assemblies=(root, assembly))
    def forbidden(*args): pytest.fail('unsupported flags must stop before component file reads')
    monkeypatch.setattr(mod, '_component_file', forbidden)
    with pytest.raises(NativeExecutionUnsupported, match='common_controls_binding_unsupported'):
        _compare_common_controls(metadata, **kwargs)


def test_three_assembly_roster_stays_unsupported_even_with_zero_flags(binding, monkeypatch):
    from app.providers import common_controls_binding as mod
    metadata, kwargs, _, _ = binding
    metadata = replace(metadata, assemblies=(*metadata.assemblies, metadata.assemblies[1]))
    def forbidden(*args): pytest.fail('extra assembly must stop before component reads')
    monkeypatch.setattr(mod, '_component_file', forbidden)
    with pytest.raises(NativeExecutionUnsupported): _compare_common_controls(metadata, **kwargs)


@pytest.mark.parametrize('projection', ['complete_roster', 'remove_resource', 'remove_resource_and_clear_flags'])
def test_retained_real_roster_shape_cannot_be_promoted_by_stripping(binding, monkeypatch, projection):
    """Actual identity/count/flags shape, projected onto synthetic local paths.

    No OS call, code-file observation, or complete real binding is represented.
    """
    from app.providers import common_controls_binding as mod
    metadata, kwargs, _, _ = binding
    root, assembly = metadata.assemblies
    root = replace(root, flags=0x11)
    assembly = replace(assembly, identity='Microsoft.Windows.Common-Controls,processorArchitecture="amd64",'
        'publicKeyToken="6595b64144ccf1df",type="win32",version="6.0.26100.9278"', files=())
    resource = replace(assembly, identity='Microsoft.Windows.Common-Controls.Resources,language="en-us",'
        'processorArchitecture="amd64",publicKeyToken="6595b64144ccf1df",type="win32",version="6.0.26100.8117"')
    roster = (root, assembly, resource)
    if projection != 'complete_roster': roster = (root, assembly)
    if projection == 'remove_resource_and_clear_flags': roster = (replace(root, flags=0), assembly)
    metadata = replace(metadata, assemblies=roster)
    def forbidden(*args): pytest.fail('unsupported evidence must not reach component reads')
    monkeypatch.setattr(mod, '_component_file', forbidden)
    with pytest.raises(NativeExecutionUnsupported, match='common_controls_binding_unsupported'):
        _compare_common_controls(metadata, **kwargs)
