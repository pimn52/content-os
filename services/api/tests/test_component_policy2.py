"""Policy2 static proof on temporary files; legacy admission remains separate."""
from dataclasses import asdict, replace
import copy
import hashlib
import os

import pytest
from pydantic import ValidationError

from app.domain.execution_runtime import (HostRuntimeObservation, HostRuntimeObservationV3,
    HostRuntimeObservationV3Policy2)
from app.providers.component_manifest import inspect_component_manifest
from app.providers.common_controls_binding import _compare_common_controls_v2, _compare_common_controls
from app.providers.runtime_primitives import NativeExecutionUnsupported
from app.providers.windows_activation import ActivationMetadata, ActivationAssembly, ActivationFile
from app.providers.windows_component_policy import owned_component_policy_bytes, owned_component_policy_digest
from test_pe_data import data_image
from test_pe_imports import image
from test_pe_resources import manifest_image


@pytest.mark.parametrize('language', ['en-US', 'zh-CN'])
def test_mixed_case_language_keeps_raw_proof_and_canonical_directory(policy2, language):
    metadata, kwargs, paths = policy2
    root, main, resource = metadata.assemblies
    code, manifest = paths[1]
    directory = code.parent.with_name(code.parent.name.replace('en-us', language.lower()))
    if directory != code.parent:
        directory.mkdir()
        (directory / code.name).write_bytes(code.read_bytes())
    target = manifest.with_name(directory.name + '.manifest')
    target.write_bytes(definition(True).replace(b'en-us', language.encode()))
    resource = replace(resource, identity=resource.identity.replace('en-us', language),
                       directory=directory.name, manifest=str(target))
    updated = replace(metadata, assemblies=(root, main, resource))
    result = _compare_common_controls_v2(updated, **kwargs)
    assert result.resource.identity.encoded_language == language
    assert result.resource.identity.language == language.lower()
    carrier = HostRuntimeObservationV3Policy2.model_validate(host(result))
    assert HostRuntimeObservationV3Policy2.model_validate_json(carrier.model_dump_json()) == carrier
    # Neither a different definition language nor a forged directory is rescued.
    target.write_bytes(definition(True).replace(b'en-us', b'fr-FR'))
    with pytest.raises(NativeExecutionUnsupported):
        _compare_common_controls_v2(updated, **kwargs)
    forged = carrier.model_dump(mode='json')
    forged['binding']['resource']['identity']['language'] = 'fr-fr'
    with pytest.raises(ValidationError):
        HostRuntimeObservationV3Policy2.model_validate(forged)


def definition(resource=False, dependency=True):
    name = 'Microsoft.Windows.Common-Controls' + ('.Resources' if resource else '')
    version = '6.0.26100.8117' if resource else '6.0.26100.9278'
    language = ' language="en-us"' if resource else ''
    dep = ('<dependency optional="yes"><dependentAssembly><assemblyIdentity '
        'name="Microsoft.Windows.Common-Controls.Resources" version="6.0.0.0" '
        'processorArchitecture="amd64" language="*" publicKeyToken="6595b64144ccf1df" '
        'type="win32"/></dependentAssembly></dependency>') if dependency and not resource else ''
    return (f'<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">'
        f'<assemblyIdentity name="{name}" version="{version}" processorArchitecture="amd64" '
        f'publicKeyToken="6595b64144ccf1df" type="win32"{language}/>'
        f'<file name="comctl32.dll{".mui" if resource else ""}"/>{dep}</assembly>').encode()


@pytest.fixture
def policy2(tmp_path):
    windows = tmp_path / 'Windows'
    manifests = windows / 'WinSxS' / 'Manifests'
    manifests.mkdir(parents=True)
    source = tmp_path / 'source.dll'
    source.write_bytes(manifest_image())
    assemblies, paths = [], []
    for resource, directory in ((False, 'amd64_microsoft.windows.common-controls_6595b64144ccf1df_6.0.26100.9278_none_3e0d1ba8e3303201'),
            (True, 'amd64_microsoft.windows.c..-controls.resources_6595b64144ccf1df_6.0.26100.8117_en-us_541277d80fdbb4cb')):
        folder = windows / 'WinSxS' / directory
        folder.mkdir()
        code = folder / ('comctl32.dll.mui' if resource else 'comctl32.dll')
        payload = data_image(named=True) if resource else image()
        if not resource:
            import struct
            struct.pack_into('<H', payload, 150, 0x2002)
            payload = bytes(payload)
        code.write_bytes(payload)
        manifest = manifests / (directory + '.manifest')
        manifest.write_bytes(definition(resource))
        identity = ('Microsoft.Windows.Common-Controls' + ('.Resources' if resource else '') +
            ',processorArchitecture="amd64",publicKeyToken="6595b64144ccf1df",type="win32",'
            'version="' + ('6.0.26100.8117",language="en-us"' if resource else '6.0.26100.9278"'))
        assemblies.append(ActivationAssembly(identity, str(manifest), '', directory, 2, 1, (1, 0), (0, 0), 0, (), 0))
        paths.append((code, manifest))
    root = ActivationAssembly('', str(source), '', '', 2, 1, (1, 0), (0, 0), 0, (), 17)
    metadata = ActivationMetadata(1, str(source), '', str(tmp_path), (2, 1, 2), (root, *assemblies), 0)
    kwargs = dict(source=source, expected_source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                  application_directory=tmp_path, windows_root=windows)
    return metadata, kwargs, paths


def host(result):
    return dict(observation_recipe='windows-platform-ubr-component-bindings', observation_version=3,
        trust_recipe='windows-os-component-bindings-v3', os_family='windows', architecture='amd64',
        build=26100, revision=9457, device={'kind': 'cpu'}, component_policy_version=2,
        component_policy_sha256=result.component_policy_sha256, binding=asdict(result))


def test_independent_three_row_files0_neutral_and_pe32_data(policy2):
    metadata, kwargs, paths = policy2
    result = _compare_common_controls_v2(metadata, **kwargs)
    assert not result.dispatch_authorized and result.root_flags == 17
    assert result.common_controls.identity.language == 'neutral'
    assert result.common_controls.identity.encoded_language is None
    assert result.resource.identity.version != result.common_controls.identity.version
    assert result.resource.data.machine == '0x14c' and result.resource.data.leaf_count == 1
    assert result.resource.file.sha256 == hashlib.sha256(paths[1][0].read_bytes()).hexdigest()
    for cls in (HostRuntimeObservation, HostRuntimeObservationV3):
        with pytest.raises(ValidationError): cls.model_validate(host(result))
    carrier = HostRuntimeObservationV3Policy2.model_validate(host(result))
    assert HostRuntimeObservationV3Policy2.model_validate_json(carrier.model_dump_json()) == carrier
    for cls in (HostRuntimeObservation, HostRuntimeObservationV3):
        with pytest.raises(ValidationError): cls.model_validate(carrier)
    with pytest.raises(NativeExecutionUnsupported):
        old_kwargs = {key: value for key, value in kwargs.items() if key != 'expected_source_sha256'}
        _compare_common_controls(metadata, **old_kwargs)


@pytest.mark.parametrize('flags', [0, 17, 0xffffffff, 1234])
def test_root_flags_are_opaque_only_with_complete_proof(policy2, flags):
    metadata, kwargs, _ = policy2
    metadata = replace(metadata, assemblies=(replace(metadata.assemblies[0], flags=flags), *metadata.assemblies[1:]))
    assert _compare_common_controls_v2(metadata, **kwargs).root_flags == flags
    assert _compare_common_controls_v2(metadata, **kwargs).component_policy_sha256 == owned_component_policy_digest()


def test_optional_resource_absent_and_matching_file_records(policy2):
    metadata, kwargs, paths = policy2
    root, main, resource = metadata.assemblies
    main = replace(main, files=(ActivationFile('comctl32.dll', str(paths[0][0]), 0),))
    result = _compare_common_controls_v2(replace(metadata, assemblies=(root, main)), **kwargs)
    assert result.resource is None


@pytest.mark.skipif(os.name != 'nt', reason='Windows case-insensitive reported path')
def test_reported_manifest_case_uses_portable_owned_directory_spelling(policy2):
    metadata, kwargs, _ = policy2
    roster = (metadata.assemblies[0], *(replace(item, manifest=item.manifest.replace('Manifests', 'manifests'))
              for item in metadata.assemblies[1:]))
    result = _compare_common_controls_v2(replace(metadata, assemblies=roster), **kwargs)
    assert result.common_controls.manifest.windows_relative_path.startswith('WinSxS/Manifests/')
    HostRuntimeObservationV3Policy2.model_validate(host(result))


@pytest.mark.parametrize('problem', ['root_missing', 'root_bool', 'root_overflow', 'context_flag', 'component_flag',
    'file_flag', 'satellite', 'policy', 'configuration', 'root_path', 'source_digest', 'source_changed',
    'extra_assembly', 'duplicate', 'language', 'file_missing', 'extra_file', 'file_path', 'file_conflict',
    'manifest_identity', 'resource_dependency', 'unknown_dependency', 'extra_manifest_file',
    'dtd', 'include', 'malformed_xml', 'duplicate_attribute', 'manifest_code', 'resource_code',
    'directory', 'manifest_alias', 'bool_path_type', 'bool_manifest_version', 'exe_code'])
def test_incomplete_or_changed_facts_cannot_be_repaired_with_flags(policy2, problem):
    metadata, kwargs, paths = policy2
    root, main, resource = metadata.assemblies
    code, manifest = paths[0]
    if problem.startswith('root_') and problem in ('root_missing', 'root_bool', 'root_overflow'):
        root = replace(root, flags={'root_missing': None, 'root_bool': True, 'root_overflow': 0x100000000}[problem])
    if problem == 'context_flag': metadata = replace(metadata, flags=17)
    if problem == 'component_flag': main = replace(main, flags=17)
    if problem == 'file_flag': main = replace(main, files=(ActivationFile('comctl32.dll', str(code), 17),))
    if problem == 'satellite': main = replace(main, satellite=1)
    if problem == 'policy': main = replace(main, policy=str(manifest))
    if problem == 'configuration': metadata = replace(metadata, root_configuration='private.config')
    if problem == 'bool_path_type': metadata = replace(metadata, path_types=(2, True, 2))
    if problem == 'bool_manifest_version': main = replace(main, manifest_version=(True, 0))
    if problem == 'root_path': metadata = replace(metadata, root_manifest=str(manifest))
    if problem == 'source_digest': kwargs['expected_source_sha256'] = '0' * 64
    if problem == 'source_changed': kwargs['source'].write_bytes(b'changed')
    if problem == 'language': main = replace(main, identity=main.identity+',language="en-us"')
    if problem == 'file_missing': code.unlink()
    if problem == 'extra_file': main = replace(main, files=(ActivationFile('other.dll', str(code), 0),))
    if problem == 'file_path': main = replace(main, files=(ActivationFile('comctl32.dll', str(paths[1][0]), 0),))
    if problem == 'file_conflict': main = replace(main, files=(ActivationFile('comctl32.dll', str(code), 0),)*2)
    if problem == 'manifest_identity': manifest.write_bytes(definition().replace(b'6.0.26100.9278', b'6.0.26100.9279'))
    if problem == 'resource_dependency': manifest.write_bytes(definition(dependency=False))
    if problem == 'unknown_dependency': manifest.write_bytes(definition().replace(b'Common-Controls.Resources', b'Other.Assembly'))
    if problem == 'extra_manifest_file': manifest.write_bytes(definition().replace(b'</assembly>', b'<file name="extra.dll"/></assembly>'))
    if problem == 'dtd': manifest.write_bytes(b'<!DOCTYPE assembly [<!ENTITY x SYSTEM "file:///secret">]>'+definition())
    if problem == 'malformed_xml': manifest.write_bytes(definition()[:-2])
    if problem == 'duplicate_attribute': manifest.write_bytes(definition().replace(b'type="win32"', b'type="win32" type="win32"', 1))
    if problem == 'include': manifest.write_bytes(definition().replace(b'</assembly>', b'<include href="other"/></assembly>'))
    if problem == 'manifest_code': manifest.write_bytes(definition().replace(b'</assembly>', b'<comClass/></assembly>'))
    if problem == 'resource_code': paths[1][0].write_bytes(bytes(image()))
    if problem == 'exe_code': code.write_bytes(bytes(image()))
    if problem == 'directory': main = replace(main, directory=str(kwargs['windows_root'].parent))
    if problem == 'manifest_alias': main = replace(main, manifest=str(manifest.parent / '..' / manifest.parent.name / manifest.name))
    roster = (root, main, resource)
    if problem == 'extra_assembly': roster = (*roster, resource)
    if problem == 'duplicate': roster = (root, main, main)
    with pytest.raises(NativeExecutionUnsupported, match='policy2_unsupported'):
        _compare_common_controls_v2(replace(metadata, assemblies=roster), **kwargs)


def test_reobservation_freezes_file_and_root_flag_changes(policy2):
    metadata, kwargs, paths = policy2
    before = _compare_common_controls_v2(metadata, **kwargs)
    code, _ = paths[0]
    payload = bytearray(code.read_bytes()); payload[-1] ^= 1; code.write_bytes(payload)
    after = _compare_common_controls_v2(metadata, **kwargs)
    assert before != after
    changed = replace(metadata, assemblies=(replace(metadata.assemblies[0], flags=18), *metadata.assemblies[1:]))
    assert _compare_common_controls_v2(changed, **kwargs) != after


@pytest.mark.parametrize('problem', ['policy', 'bool', 'digest', 'missing_resource', 'downgrade', 'root_bool', 'neutral', 'data_arch', 'path'])
def test_policy2_typed_branch_has_no_implicit_upgrade(policy2, problem):
    metadata, kwargs, _ = policy2
    value = copy.deepcopy(host(_compare_common_controls_v2(metadata, **kwargs)))
    if problem == 'policy': value['component_policy_version'] = 1
    if problem == 'bool': value['component_policy_version'] = True
    if problem == 'digest': value['component_policy_sha256'] = '0' * 64
    if problem == 'missing_resource': del value['binding']['resource']
    if problem == 'downgrade': value['binding']['component_policy_version'] = 1
    if problem == 'root_bool': value['binding']['root_flags'] = True
    if problem == 'neutral': value['binding']['common_controls']['identity']['language'] = 'none'
    if problem == 'data_arch': value['binding']['resource']['data']['machine'] = '0x8664'
    if problem == 'path': value['binding']['resource']['file']['windows_relative_path'] = 'WinSxS/other.dll'
    with pytest.raises(ValidationError): HostRuntimeObservationV3Policy2.model_validate(value)


def test_owned_policy_is_canonical_not_a_receipt_selection():
    payload = owned_component_policy_bytes()
    assert payload.endswith(b'\n')
    assert hashlib.sha256(payload).hexdigest() == owned_component_policy_digest()
