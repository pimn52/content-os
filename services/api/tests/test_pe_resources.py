"""Synthetic bytes and fixed-file reads only; no native calls."""
import hashlib
import struct

import pytest

from app.providers.native_manifest import CPYTHON_COMMON_CONTROLS
from app.providers.pe_resources import classify_pe_resources, private_export_dependencies
from app.providers.prepared import NativeExecutionUnsupported
from test_pe_imports import image


def resource_image(leaves=(), *, exe=False, ordinary=('kernel32.dll',), delayed=()):
    data = image(ordinary, delayed)
    struct.pack_into('<H', data, 150, 2 if exe else 0x2002)
    if not leaves: return bytes(data)
    tree = {}
    for kind, name, language, payload, codepage in leaves:
        tree.setdefault(kind, {}).setdefault(name, {})[language] = (payload, codepage)
    resource = bytearray()
    descriptions = []
    def directory(values, depth):
        offset = len(resource)
        resource.extend(bytes(16 + 8 * len(values)))
        struct.pack_into('<H', resource, offset + 14, len(values))
        for index, (identity, value) in enumerate(sorted(values.items())):
            if depth < 2:
                child = directory(value, depth + 1) | 0x80000000
            else:
                child = len(resource)
                resource.extend(bytes(16))
                descriptions.append((child, value))
            struct.pack_into('<II', resource, offset + 16 + index * 8, identity, child)
        return offset
    directory(tree, 0)
    for offset, (payload, codepage) in descriptions:
        resource.extend(bytes((-len(resource)) % 4))
        start = len(resource)
        resource.extend(payload)
        struct.pack_into('<IIII', resource, offset, 2800 + 3584 + start, len(payload), codepage, 0)
    # Extend the existing validated section to fit a real reviewed manifest.
    required = 2800 + len(resource)
    if required > len(data): data.extend(bytes(required - len(data)))
    struct.pack_into('<IIII', data, 400, len(data) - 512, 4096, len(data) - 512, 512)
    data[2800:2800 + len(resource)] = resource
    struct.pack_into('<II', data, 280, 2800 + 3584, len(resource))
    return bytes(data)


def manifest_image(*, exe=False, manifest=CPYTHON_COMMON_CONTROLS, codepage=0, name=None, language=1033):
    return resource_image([(16, 1, 1033, b'version data', 0),
        (24, (1 if exe else 2) if name is None else name, language, manifest, codepage)], exe=exe)


def test_owned_exact_bytes_have_reviewed_identity_and_pending_only():
    assert len(CPYTHON_COMMON_CONTROLS) == 1319
    digest = hashlib.sha256(CPYTHON_COMMON_CONTROLS).hexdigest()
    assert digest == 'bd76c737191489222cc219b605648fee50ae2721a7d1af7ebd756b429dee7ec2'
    for exe in (False, True):
        result = classify_pe_resources(manifest_image(exe=exe), role='bootstrap_exe' if exe else 'dll')
        assert result.kind == 'requires_os_sxs_binding'
        assert result.manifest_sha256 == digest and result.leaf_count == 2


def test_no_manifest_data_and_empty_resource_directory():
    assert classify_pe_resources(resource_image()).kind == 'no_resources'
    data = resource_image([(16, 1, 1033, b'opaque data', 0)])
    assert classify_pe_resources(data).kind == 'data_resources'
    empty = bytearray(resource_image())
    struct.pack_into('<II', empty, 280, 2800 + 3584, 16)
    assert classify_pe_resources(bytes(empty)).leaf_count == 0
    assert classify_pe_resources(bytes(empty)).kind == 'data_resources'


@pytest.mark.parametrize('problem', ['changed', 'encoding', 'dtd', 'name', 'language', 'codepage', 'role', 'double'])
def test_manifest_forms_require_exact_owned_bytes_and_role(problem):
    kwargs = {'manifest': CPYTHON_COMMON_CONTROLS + b' '} if problem == 'changed' else {}
    if problem == 'encoding': kwargs['manifest'] = CPYTHON_COMMON_CONTROLS.decode().encode('utf-16')
    if problem == 'dtd': kwargs['manifest'] = b'<!DOCTYPE assembly [<!ENTITY x SYSTEM "file:///secret">]>'
    if problem == 'name': kwargs['name'] = 1
    if problem == 'language': kwargs['language'] = 0
    if problem == 'codepage': kwargs['codepage'] = 65001
    data = manifest_image(**kwargs)
    if problem == 'double':
        data = resource_image([(24, 2, lang, CPYTHON_COMMON_CONTROLS, 0) for lang in (0, 1033)])
    with pytest.raises(NativeExecutionUnsupported, match='pe_resources_unsupported'):
        classify_pe_resources(data, role='bootstrap_exe' if problem == 'role' else 'dll')


@pytest.mark.parametrize('problem', ['named', 'cycle', 'overlap', 'type', 'leaf_directory', 'bounds',
    'data_bounds', 'reserved', 'order', 'unaligned', 'size', 'half_empty', 'nodes', 'leaves', 'unknown_role'])
def test_resource_tree_rejects_ambiguous_and_bounded_forms(problem, monkeypatch):
    from app.providers import pe_resources as mod
    data = bytearray(resource_image([(16, 1, 1033, b'version', 0)]))
    # root0/type child24/name child48/language data entry72/payload88.
    changes = {
        'named': (2812, '<H', 1), 'cycle': (2820, '<I', 0x80000000),
        'overlap': (2820, '<I', 0x80000010), 'type': (2816, '<I', 10),
        'leaf_directory': (2868, '<I', 0x80000048), 'bounds': (2820, '<I', 0xffffffff),
        'data_bounds': (2872, '<I', 0xffffffff), 'reserved': (2884, '<I', 1),
        'unaligned': (2820, '<I', 0x80000019), 'size': (284, '<I', 1024 * 1024 + 1),
        'half_empty': (284, '<I', 0),
    }
    if problem in changes:
        offset, fmt, value = changes[problem]
        struct.pack_into(fmt, data, offset, value)
    if problem == 'order':
        data = bytearray(manifest_image())
        struct.pack_into('<I', data, 2824, 16)
    if problem == 'nodes': monkeypatch.setattr(mod, 'RESOURCE_NODE_LIMIT', 2)
    if problem == 'leaves': monkeypatch.setattr(mod, 'RESOURCE_LEAF_LIMIT', 0)
    with pytest.raises(NativeExecutionUnsupported, match='pe_resources_unsupported'):
        classify_pe_resources(bytes(data), role='invented' if problem == 'unknown_role' else 'dll')


def test_icons_only_in_declared_bootstrap_exe():
    leaves = [(3, 1, 1033, b'icon', 0), (14, 1, 1033, b'group', 0)]
    assert classify_pe_resources(resource_image(leaves, exe=True), role='bootstrap_exe').kind == 'data_resources'
    with pytest.raises(NativeExecutionUnsupported): classify_pe_resources(resource_image(leaves))


def forwarder_image(text=b'python312.PyObject_Call\0'):
    data = bytearray(resource_image(ordinary=()))
    struct.pack_into('<II', data, 264, 2800 + 3584, 160)
    struct.pack_into('<II', data, 2820, 1, 0)  # EAT count; no names necessary.
    struct.pack_into('<I', data, 2828, 2850 + 3584)
    struct.pack_into('<I', data, 2850, 2860 + 3584)
    data[2860:2860 + len(text)] = text
    return bytes(data)


def test_export_forwarder_adds_dependency_with_empty_import_table():
    assert private_export_dependencies(forwarder_image()) == ('python312.dll',)
    assert private_export_dependencies(forwarder_image(b'KERNEL32.#123\0')) == ('kernel32.dll',)


def test_nonforwarded_export_may_be_mapped_zero_initialized_data():
    data = bytearray(forwarder_image())
    struct.pack_into('<I', data, 400, 4096)  # Virtual extent beyond raw bytes.
    struct.pack_into('<I', data, 2850, 8000)
    assert private_export_dependencies(bytes(data)) == ()
    struct.pack_into('<I', data, 2850, 9000)
    with pytest.raises(NativeExecutionUnsupported, match='pe_forwarders_unsupported'):
        private_export_dependencies(bytes(data))


@pytest.mark.parametrize('text', [b'../evil.X\0', b'python312.\0', b'python312.X Y\0', b'a' * 100])
def test_invalid_forwarder_remains_explicit_unsupported(text):
    with pytest.raises(NativeExecutionUnsupported, match='pe_forwarders_unsupported'):
        private_export_dependencies(forwarder_image(text))
