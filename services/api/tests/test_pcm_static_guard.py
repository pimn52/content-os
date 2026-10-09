"""Fake startup and legacy bundle equality; no selected-runtime launch."""
import ast
from pathlib import Path

import pytest

from app.providers import pcm_static_probe as subject, pcm_static_wire as wire
from app.providers import audio_graph_probe, python_origin_probe, component_probe
from test_omnivoice_pcm_bootstrap import system


def namespace():
    rows = subject.owned_pcm_static_files()
    entry = next(r.payload for r in rows if r.destination == wire.ENTRY)
    value = {'__name__': '_synthetic_pcm_startup'}
    exec(compile(entry, '<synthetic-pcm-startup>', 'exec'), value)
    return value


def test_new_bundle_does_not_modify_protocols_one_to_six():
    factories = (python_origin_probe.owned_probe_files, component_probe.owned_component_probe_files,
        component_probe.owned_native_load_probe_files, audio_graph_probe.owned_audio_graph_probe_files)
    before = tuple(factory() for factory in factories)
    rows = subject.owned_pcm_static_files()
    assert len(rows) == 5 and rows[-1].destination == wire.ENTRY
    assert tuple(factory() for factory in factories) == before
    assert audio_graph_probe.TREE_LIMIT == 256*1024**2
    assert audio_graph_probe.INPUT_LIMIT == 16*1024**2+65536
    assert audio_graph_probe.OUTPUT_LIMIT == 65536
    for row in rows:
        tree = ast.parse(row.payload)
        if row.destination.endswith('rules.py'):
            assert len(tree.body) == 1 and isinstance(tree.body[0], ast.Assign)
        imports = [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
        imports += [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
        assert not any(n and n.split('.')[0] in ('app', 'pydantic', 'numpy', 'torch', 'omnivoice') for n in imports)


@pytest.mark.parametrize('problem', ['valid', 'entry', 'pydantic', 'app', '_content_os_pcm', 'flags', 'finder'])
def test_protocol_seven_embedded_sys_guard(problem):
    owned = namespace()
    value = system()
    value.argv = [value.prefix+'\\'+wire.ENTRY]
    if problem == 'entry': value.argv[0] = 'caller.py'
    elif problem == 'flags': value.flags.no_site = 0
    elif problem == 'finder': value.meta_path.append(object())
    elif problem != 'valid': value.modules[problem] = object()
    before = value.path.copy(), value.meta_path.copy(), value.modules.copy()
    if problem == 'valid':
        assert owned['verify_startup'](value) == value.prefix
    else:
        with pytest.raises(SystemExit, match='startup_invalid'): owned['verify_startup'](value)
    assert before == (value.path, value.meta_path, value.modules)
