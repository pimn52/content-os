"""Finite owner attribution; temp files/native snapshots are synthetic evidence."""
import hashlib
from importlib.machinery import ExtensionFileLoader, SourceFileLoader, ModuleSpec
from types import ModuleType

import pytest

from app.providers.python_owner_origins import ExpatOwnerOrigins, OWNER, WRAPPER
from app.providers.windows_native import python_origins
from app.providers.runtime_primitives import NativeExecutionUnsupported


@pytest.fixture
def owner_case(tmp_path, monkeypatch):
    import pyexpat
    import xml.parsers.expat as wrapper
    inventory = dict(files=[])
    for module, name, relative, loader_type in (
        (pyexpat, 'pyexpat', OWNER, ExtensionFileLoader),
        (wrapper, 'xml.parsers.expat', WRAPPER, SourceFileLoader)):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = b'synthetic owner file' if name == 'pyexpat' else b'synthetic wrapper'
        path.write_bytes(payload)
        loader = loader_type(name, str(path))
        monkeypatch.setattr(module, '__file__', str(path))
        monkeypatch.setattr(module, '__loader__', loader)
        monkeypatch.setattr(module, '__spec__', ModuleSpec(name, loader, origin=str(path)))
        inventory['files'].append(dict(name=relative, size_bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest()))
    modules = {'pyexpat': pyexpat, 'pyexpat.errors': pyexpat.errors, 'pyexpat.model': pyexpat.model}
    return ExpatOwnerOrigins(tmp_path, inventory), modules, (tmp_path / OWNER,), wrapper


def test_exact_objects_attribute_to_native_owner_and_legacy_still_rejects(owner_case):
    observer, modules, native, _ = owner_case
    paths, evidence = observer.observe(modules, native)
    assert paths == native and evidence['owner'] == OWNER
    assert evidence['modules'] == ('pyexpat.errors', 'pyexpat.model')
    assert observer.observe(modules, native) == (paths, evidence)
    with pytest.raises(NativeExecutionUnsupported, match='python_origin_unsupported'):
        python_origins(modules)


def test_exact_standard_library_aliases_are_observed(owner_case):
    observer, modules, native, wrapper = owner_case
    modules.update({'xml.parsers.expat': wrapper, 'xml.parsers.expat.errors': wrapper.errors,
        'xml.parsers.expat.model': wrapper.model})
    paths, evidence = observer.observe(modules, native)
    assert set(paths) == {observer.root / OWNER, observer.root / WRAPPER}
    assert len(evidence['modules']) == 4 and evidence['wrapper_sha256']


@pytest.mark.parametrize('problem', ['owner_missing', 'native_missing', 'native_shadow', 'native_duplicate',
    'file_changed', 'inventory_missing', 'fake_child', 'fake_owner', 'unknown_namespace',
    'unknown_alias', 'partial_alias', 'wrapper_wrong', 'executable_data', 'wrong_loader', 'wrong_spec'])
def test_owner_admission_negatives(owner_case, monkeypatch, problem):
    observer, modules, native, wrapper = owner_case
    owner = modules['pyexpat']
    if problem == 'owner_missing': del modules['pyexpat']
    elif problem == 'native_missing': native = ()
    elif problem == 'native_shadow': native = (observer.root / 'pyexpat.pyd',)
    elif problem == 'native_duplicate': native = native * 2
    elif problem == 'file_changed': (observer.root / OWNER).write_bytes(b'changed')
    elif problem == 'inventory_missing': observer.inventory['files'].clear()
    elif problem == 'fake_child': modules['pyexpat.errors'] = ModuleType('pyexpat.errors')
    elif problem == 'fake_owner':
        fake = ModuleType('pyexpat')
        fake.__dict__.update(owner.__dict__)
        modules['pyexpat'] = fake
    elif problem == 'unknown_namespace': modules['unknown'] = ModuleType('unknown')
    elif problem == 'unknown_alias': modules['other.errors'] = owner.errors
    elif problem == 'partial_alias': modules['xml.parsers.expat.errors'] = owner.errors
    elif problem == 'wrapper_wrong':
        modules.update({'xml.parsers.expat': wrapper, 'xml.parsers.expat.errors': wrapper.errors,
            'xml.parsers.expat.model': wrapper.model})
        monkeypatch.setattr(wrapper, '__file__', str(observer.root / OWNER))
    elif problem == 'executable_data': monkeypatch.setattr(owner.errors, 'extra', lambda: None, raising=False)
    elif problem == 'wrong_loader': monkeypatch.setattr(owner, '__loader__', object())
    elif problem == 'wrong_spec': monkeypatch.setattr(owner.__spec__, 'origin', 'outside.pyd')
    with pytest.raises(NativeExecutionUnsupported, match='python_(owner|origin)_unsupported'):
        observer.observe(modules, native)


@pytest.mark.parametrize('change', ['child', 'data', 'owner', 'wrapper_added', 'file'])
def test_recheck_detects_replacement_even_with_same_content(owner_case, monkeypatch, change):
    observer, modules, native, wrapper = owner_case
    observer.observe(modules, native)
    if change == 'child':
        clone = ModuleType('pyexpat.errors')
        clone.__dict__.update(modules['pyexpat.errors'].__dict__)
        modules['pyexpat.errors'] = clone
        monkeypatch.setattr(modules['pyexpat'], 'errors', clone)
    elif change == 'data': monkeypatch.setattr(modules['pyexpat.model'], 'XML_CTYPE_EMPTY', 99)
    elif change == 'owner':
        clone = ModuleType('pyexpat')
        clone.__dict__.update(modules['pyexpat'].__dict__)
        modules['pyexpat'] = clone
    elif change == 'wrapper_added':
        modules.update({'xml.parsers.expat': wrapper, 'xml.parsers.expat.errors': wrapper.errors,
            'xml.parsers.expat.model': wrapper.model})
    else: (observer.root / OWNER).write_bytes(b'changed')
    with pytest.raises(NativeExecutionUnsupported, match='python_owner_unsupported'):
        observer.observe(modules, native)
