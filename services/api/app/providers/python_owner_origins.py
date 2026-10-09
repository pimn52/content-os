"""Owned CPython3.12 Expat data objects; caller must first scan the full tree.

Only protocol4 uses this observer. It observes an already loaded extension,
never imports pyexpat or grants native-file/target admission.
"""
import hashlib
import json
from importlib.machinery import ExtensionFileLoader, SourceFileLoader, ModuleSpec
from pathlib import Path
from types import ModuleType, BuiltinFunctionType

from .runtime_primitives import NativeExecutionUnsupported, _exact_path
from .windows_native import _python_origins, PythonOriginUnsupported

RECIPE = 'cpython312-expat-owner-v1'
OWNER = 'DLLs/pyexpat.pyd'
WRAPPER = 'Lib/xml/parsers/expat.py'
CHILDREN = ('errors', 'model')


def _dictionary(module):
    if type(module) is not ModuleType: raise ValueError()
    return ModuleType.__getattribute__(module, '__dict__')


def _file(root, name, inventory):
    rows = [row for row in inventory['files'] if row['name'] == name]
    if len(rows) != 1: raise ValueError()
    path = root / name
    _exact_path(path, directory=False)
    if not 0 < rows[0]['size_bytes'] <= 16 * 1024 * 1024: raise ValueError()
    with path.open('rb') as source: payload = source.read(16 * 1024 * 1024 + 1)
    digest = hashlib.sha256(payload).hexdigest()
    if len(payload) != rows[0]['size_bytes'] or digest != rows[0]['sha256']: raise ValueError()
    return path, digest


def _source(module, name, path, loader_type):
    values = _dictionary(module)
    spec, loader = values.get('__spec__'), values.get('__loader__')
    if (values.get('__name__') != name or type(values.get('__file__')) is not str
        or Path(values['__file__']) != path or type(spec) is not ModuleSpec
        or getattr(spec, 'name', None) != name or getattr(spec, 'origin', None) != str(path)
        or getattr(spec, 'loader', None) is not loader or type(loader) is not loader_type
        or loader.name != name or loader.path != str(path)):
        raise ValueError()
    return values


def _data_digest(module, suffix):
    values = _dictionary(module)
    if not 5 <= len(values) <= 256 or values.get('__name__') != 'pyexpat.' + suffix:
        raise ValueError()
    metadata = {'__name__', '__doc__', '__package__', '__loader__', '__spec__'}
    if (values.get('__package__') is not None or values.get('__loader__') is not None
        or values.get('__spec__') is not None
        or (values.get('__doc__') is not None and (type(values['__doc__']) is not str or len(values['__doc__']) > 2048))):
        raise ValueError()
    data = {}
    for name, value in values.items():
        if type(name) is not str or len(name) > 128: raise ValueError()
        if name in metadata: continue
        if suffix == 'errors' and name in ('codes', 'messages'):
            if type(value) is not dict or not 1 <= len(value) <= 256: raise ValueError()
            for key, item in value.items():
                string, number = (key, item) if name == 'codes' else (item, key)
                if type(string) is not str or len(string) > 2048 or type(number) is not int or not 0 <= number <= 65535:
                    raise ValueError()
            data[name] = sorted(value.items())
        elif suffix == 'errors' and name.startswith('XML_ERROR_'):
            if type(value) is not str or len(value) > 2048: raise ValueError()
            data[name] = value
        elif suffix == 'model' and name.startswith(('XML_CTYPE_', 'XML_CQUANT_')):
            if type(value) is not int or not 0 <= value <= 65535: raise ValueError()
            data[name] = value
        else:
            raise ValueError()
    if not data or (suffix == 'errors' and not {'codes', 'messages'} <= data.keys()): raise ValueError()
    payload = json.dumps(data, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')
    if len(payload) > 65536: raise ValueError()
    return hashlib.sha256(payload).hexdigest()


class ExpatOwnerOrigins:
    """Finite observed owner, strong object references and same-child rechecks."""
    def __init__(self, root, inventory):
        self.root, self.inventory = root, inventory
        self._state = None

    def observe(self, modules, native):
        checkpoint = 'owner_file'
        try:
            if type(modules) is not dict: raise ValueError()
            owner = modules.get('pyexpat')
            path, digest = _file(self.root, OWNER, self.inventory)
            checkpoint = 'native'
            if sum(value == path for value in native) != 1: raise ValueError()
            checkpoint = 'owner_module'
            values = _source(owner, 'pyexpat', path, ExtensionFileLoader)
            method = values.get('ParserCreate')
            if type(method) is not BuiltinFunctionType or method.__self__ is not owner or method.__name__ != 'ParserCreate':
                raise ValueError()
            checkpoint = 'children'
            children = tuple(values.get(name) for name in CHILDREN)
            if any(modules.get('pyexpat.' + name) is not child for name, child in zip(CHILDREN, children)):
                raise ValueError()
            checkpoint = 'data'
            data = tuple(_data_digest(child, name) for name, child in zip(CHILDREN, children))
            allowed = {'pyexpat.' + name: child for name, child in zip(CHILDREN, children)}
            wrapper = modules.get('xml.parsers.expat')
            aliases = tuple(name for name in CHILDREN if 'xml.parsers.expat.' + name in modules)
            wrapper_digest = None
            checkpoint = 'wrapper'
            if wrapper is not None or aliases:
                wrapper_path, wrapper_digest = _file(self.root, WRAPPER, self.inventory)
                wrapped = _source(wrapper, 'xml.parsers.expat', wrapper_path, SourceFileLoader)
                if aliases != CHILDREN or wrapped.get('ParserCreate') is not method: raise ValueError()
                for name, child in zip(CHILDREN, children):
                    alias = 'xml.parsers.expat.' + name
                    if modules.get(alias) is not child or wrapped.get(name) is not child: raise ValueError()
                    allowed[alias] = child
            # Object identity matters even when replacement dictionaries match.
            objects = (owner, *children, wrapper, method)
            facts = (digest, data, wrapper_digest, aliases)
            checkpoint = 'stability'
            if self._state is not None:
                old_objects, old_facts = self._state
                if facts != old_facts or any(a is not b for a, b in zip(objects, old_objects)):
                    raise ValueError()
            def resolve(name, module):
                return path if name in allowed and allowed[name] is module else None
            checkpoint = 'python'
            paths = _python_origins(modules, resolve)
            # Reobserve registry relationships after the complete module walk.
            checkpoint = 'registry'
            if (modules.get('pyexpat') is not owner or modules.get('xml.parsers.expat') is not wrapper
                or any(modules.get(name) is not child for name, child in allowed.items())
                or any(values.get(name) is not child for name, child in zip(CHILDREN, children))
                or tuple(_data_digest(child, name) for name, child in zip(CHILDREN, children)) != data):
                raise ValueError()
            self._state = objects, facts
            evidence = dict(recipe=RECIPE, owner=OWNER, owner_sha256=digest,
                modules=tuple(sorted(allowed)), data_sha256=data, wrapper_sha256=wrapper_digest)
            return paths, evidence
        except PythonOriginUnsupported:
            raise
        except (OSError, ValueError, TypeError, AttributeError, KeyError, RuntimeError):
            failure = NativeExecutionUnsupported('execution_python_owner_unsupported')
            failure.diagnostic_checkpoint = checkpoint
            raise failure from None
