"""Fake sys/runpy only; no Windows child or vendor code imported."""
import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.providers import omnivoice_pcm_bootstrap as subject
from test_python_startup import child


def system():
    value = child()
    value.argv[0] = value.prefix+'\\'+subject.ENTRY
    builtin, frozen, path = object(), object(), object()
    value.modules = {'_frozen_importlib': SimpleNamespace(BuiltinImporter=builtin, FrozenImporter=frozen),
        '_frozen_importlib_external': SimpleNamespace(PathFinder=path)}
    value.meta_path = [builtin, frozen, path]
    return value


def test_fixed_target_and_arguments_after_valid_startup(monkeypatch):
    value = system()
    assert subject.verify_startup(value) == value.prefix
    monkeypatch.setattr(subject, 'sys', value)
    import runpy
    calls = []
    monkeypatch.setattr(runpy, 'run_module', lambda *a, **kw: calls.append((a, kw, value.argv.copy())))
    subject.main()
    assert calls == [((subject.MODULE,), {'run_name': '__main__', 'alter_sys': True},
                      [subject.MODULE, '--inputs', 'synthetic'])]


@pytest.mark.parametrize('problem', ['platform', 'version', 'isolated', 'ignore_environment',
    'no_site', 'no_user_site', 'dont_write_bytecode', 'prefix', 'exec_prefix', 'base_prefix',
    'base_exec_prefix', 'executable', 'base_executable', 'path', 'path_order', 'entry',
    'meta_extra', 'meta_order', 'missing_frozen', 'site', 'sitecustomize', 'usercustomize',
    *subject.FORBIDDEN_PRELOADS])
def test_invalid_state_never_imports_application(problem, monkeypatch):
    value = system()
    if problem == 'platform': value.platform = 'linux'
    elif problem == 'version': value.version_info = (3, 13, 0)
    elif problem in ('isolated', 'ignore_environment', 'no_site', 'no_user_site', 'dont_write_bytecode'):
        setattr(value.flags, problem, 0)
    elif problem in ('prefix', 'exec_prefix', 'base_prefix', 'base_exec_prefix'):
        setattr(value, problem, 'C:\\foreign')
    elif problem == 'executable': value.executable = 'C:\\foreign\\other.exe'
    elif problem == 'base_executable': value._base_executable = 'C:\\foreign\\python.exe'
    elif problem == 'path': value.path.append('C:\\foreign')
    elif problem == 'path_order': value.path.reverse()
    elif problem == 'entry': value.argv[0] = 'caller-selected.py'
    elif problem == 'meta_extra': value.meta_path.append(object())
    elif problem == 'meta_order': value.meta_path.reverse()
    elif problem == 'missing_frozen': value.modules.pop('_frozen_importlib')
    else: value.modules[problem+'.child'] = object()
    before = value.path.copy(), value.meta_path.copy(), value.modules.copy()
    monkeypatch.setattr(subject, 'sys', value)
    import runpy
    monkeypatch.setattr(runpy, 'run_module', lambda *_a, **_kw: pytest.fail('must not import application'))
    with pytest.raises(SystemExit, match='startup_invalid'): subject.main()
    assert before == (value.path, value.meta_path, value.modules)  # No repair.


def test_only_sys_imported_before_observation():
    tree = ast.parse(Path(subject.__file__).read_bytes())
    assert [node.names[0].name for node in tree.body if isinstance(node, ast.Import)] == ['sys']
    main = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'main')
    assert isinstance(main.body[0], ast.Expr) and main.body[0].value.func.id == 'verify_startup'


def test_finder_equality_is_not_identity():
    value = system()
    class Pretender:
        def __eq__(self, _other): return True
    value.meta_path[2] = Pretender()
    with pytest.raises(SystemExit, match='startup_invalid'): subject.verify_startup(value)
