"""Frozen transform and fake native ABI; no vendor source is executed."""
from dataclasses import replace
import copy
import gc
import hashlib
from importlib.machinery import ModuleSpec
import json
from pathlib import Path
import subprocess
import sys
from types import ModuleType, SimpleNamespace
import weakref

import pytest

from app.providers import soundfile_binding as audio
from app.providers import private_native_recipe as private
from app.providers.native_load import NativeLoadSession, NativeNode, NativeDependencyPlan, NativeHandleBorrow
from app.providers.pe_resources import ResourceClassification
from app.providers.runtime_primitives import NativeExecutionUnsupported


def digest(raw): return hashlib.sha256(raw).hexdigest()


@pytest.fixture
def source(monkeypatch):
    prefix=b'# retained license notice\n'*161
    block=(b'try:\n    _snd = _ffi.dlopen(_full_path)\n'
        b'except (OSError, ImportError, TypeError):\n'+b'    # retained branch\n'*53+
        b'    _snd = _ffi.dlopen(_explicit_libname)\n')
    assert len(block.splitlines())==57
    suffix=b'\n__libsndfile_version__ = _ffi.string(_snd.sf_version_string())\n'
    upstream=prefix+block+suffix
    monkeypatch.setattr(audio,'UPSTREAM_SHA256',digest(upstream))
    monkeypatch.setattr(audio,'BLOCK_SHA256',digest(block))
    return upstream,prefix,block,suffix


def test_derivation_is_exact_span_notice_preserving_and_identity_bound(source):
    upstream,prefix,block,suffix=source
    result=audio.derive_soundfile(upstream)
    assert result.payload==prefix+audio.REPLACEMENT+suffix
    record=json.loads(result.descriptor)
    assert record['source_kind']=='derived' and record['upstream_sha256']==digest(upstream)
    assert record['derived_sha256']==digest(result.payload) and len(record['dependencies'])==4
    assert not result.dispatch_authorized and record['backend_handle_semantics']=='not_verified'
    assert record['binder_sha256'] and record['private_policy_sha256'] and record['transform_sha256']
    assert record['recipe']=='soundfile-owned-handle-v2'
    assert record['handle_core_sha256']==digest(Path(audio.__file__).with_name('soundfile_handle.py').read_bytes())
    assert result==audio.derive_soundfile(upstream)


def test_core_bytes_are_part_of_derivation_identity(source,tmp_path,monkeypatch):
    upstream,_,_,_=source
    binder=tmp_path/'soundfile_binding.py'
    binder.write_bytes(Path(audio.__file__).read_bytes())
    core=tmp_path/'soundfile_handle.py'
    core.write_bytes(Path(audio.__file__).with_name(core.name).read_bytes())
    monkeypatch.setattr(audio,'__file__',str(binder))
    first=audio.derive_soundfile(upstream)
    core.write_bytes(core.read_bytes()+b'\n# changed owned core\n')
    second=audio.derive_soundfile(upstream)
    before,after=json.loads(first.descriptor),json.loads(second.descriptor)
    assert first.payload==second.payload and before['binder_sha256']==after['binder_sha256']
    assert before['handle_core_sha256']!=after['handle_core_sha256']
    assert first.descriptor!=second.descriptor


def test_core_imports_in_isolated_stdlib_process_without_application_or_vendor():
    directory=Path(audio.__file__).parent
    code='''
import builtins, importlib, sys, types
package=types.ModuleType('_owned_audio_test')
package.__path__=[sys.argv[1]]
sys.modules[package.__name__]=package
original=builtins.__import__
def guarded(name,*args,**kwargs):
    if name.split('.')[0] in {'app','pydantic','numpy','torch','soundfile','_cffi_backend'}:
        raise AssertionError('forbidden import: '+name)
    return original(name,*args,**kwargs)
builtins.__import__=guarded
core=importlib.import_module('_owned_audio_test.soundfile_handle')
assert core.BoundSoundFile.__module__==core.__name__
assert not any(name.split('.')[0] in {'app','pydantic','numpy','torch','soundfile','_cffi_backend'} for name in sys.modules)
print('stdlib core import PASS')
'''
    result=subprocess.run([sys.executable,'-I','-S','-c',code,str(directory)],
                          capture_output=True,text=True,timeout=15,check=False)
    assert result.returncode==0,result.stderr
    assert result.stdout.strip()=='stdlib core import PASS'


@pytest.mark.parametrize('problem',['byte','newlines','syntax','multiple','shape','too_large','wrong_type'])
def test_derivation_refuses_changes_before_loading(source,monkeypatch,problem):
    upstream,prefix,block,suffix=source
    if problem=='byte': upstream=upstream.replace(b'notice',b'noticE',1)
    if problem=='newlines': upstream=upstream.replace(b'\n',b'\r\n')
    if problem=='syntax': upstream=prefix+b'not valid python!\n'+suffix
    if problem=='multiple': upstream=upstream+b'\n'+block
    if problem=='shape': upstream=upstream.replace(b'ImportError',b'ValueError')
    if problem in ('syntax','multiple','shape'): monkeypatch.setattr(audio,'UPSTREAM_SHA256',digest(upstream))
    if problem=='too_large': upstream=b'x'*(audio.SOURCE_LIMIT+1)
    if problem=='wrong_type': upstream=bytearray(upstream)
    with pytest.raises(NativeExecutionUnsupported,match='derivation_invalid'): audio.derive_soundfile(upstream)


@pytest.fixture
def prepared(tmp_path,monkeypatch):
    policy=copy.deepcopy(private._owned()[0])
    selected=[next(row for row in policy['sources'] if row['slot']==audio.GENERATED_SLOT),
        next(row for row in policy['members'] if row['slot']==private.AUDIO_SLOTS[0])]
    files=[]
    for row in selected:
        raw=b'synthetic source only '+row['slot'].encode()
        row.update(size_bytes=len(raw),sha256=digest(raw))
        path=tmp_path/row['slot'];path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
        files.append(SimpleNamespace(name=row['slot'],role='dependency',size_bytes=len(raw),sha256=digest(raw)))
    target=next(row for row in policy['members'] if row['slot']==private.AUDIO_SLOTS[1])
    calls=[]
    class FakeFFI:
        error=None
        def cast(self,kind,handle):
            assert kind=='void *' and handle==11
            calls.append(('cast',handle));return SimpleNamespace(handle=handle)
        def dlopen(self,pointer):
            assert not isinstance(pointer,(str,bytes,int)) and pointer.handle==11
            calls.append(('handle_dlopen',pointer.handle))
            if self.error: raise self.error('injected binding failure')
            return SimpleNamespace(sf_version_string=lambda:b'fake version')
        def typeof(self,pointer):
            assert pointer.handle==11
            return SimpleNamespace(kind='pointer',cname='void *')
        def dlclose(self,_): raise AssertionError('borrowed handle must not be closed by FFI')
    ffi=FakeFFI()
    generated=ModuleType('_soundfile');backend=ModuleType('_cffi_backend')
    for module,row in zip((generated,backend),selected):
        module.__file__=str(tmp_path/row['slot'])
        module.__spec__=ModuleSpec(module.__name__,None,origin=module.__file__)
        monkeypatch.setitem(__import__('sys').modules,module.__name__,module)
    generated.ffi=ffi;backend.FFI=FakeFFI
    monkeypatch.setattr(audio,'_owned',lambda:(policy,'f'*64))
    node=NativeNode(target['slot'],target['size_bytes'],target['sha256'],
        ResourceClassification('requires_private_root_context',2,target['manifest_sha256']))
    class Reader:
        def release(self,handle): calls.append(('native_release',handle))
    guard=SimpleNamespace(root=tmp_path,phase=lambda phase:calls.append(('phase',phase)))
    session=NativeLoadSession(Reader(),guard)
    # Fake postchecked state solely for this ABI/lifetime seam, not graph proof.
    session.state,session.handle='loaded',11
    session.plan=NativeDependencyPlan(node.name,(node,),(),(),False,False)
    inventory=SimpleNamespace(files=tuple(files))
    view=audio.observe_generated_ffi(root=tmp_path,inventory=inventory,generated=generated,backend=backend)
    return SimpleNamespace(policy=policy,session=session,view=view,ffi=ffi,calls=calls,root=tmp_path)


def test_handle_binding_keeps_owner_across_import_scope_and_blocks_early_release(prepared):
    p=prepared
    with audio.SoundFileBindingScope(p.session,p.view):
        binding=audio.bind_staged_soundfile(p.ffi)
        proxy=binding.library;export=proxy.sf_version_string
        assert export()==b'fake version'
    assert p.calls==[('cast',11),('handle_dlopen',11)]
    with pytest.raises(NativeExecutionUnsupported,match='in_use'): p.session.close()
    assert p.session.state=='loaded' and p.session.release_attempts==0
    binding.close();binding.close()
    with pytest.raises(NativeExecutionUnsupported,match='closed'): export()
    with pytest.raises(NativeExecutionUnsupported,match='closed'): proxy.sf_version_string
    p.session.close();p.session.close()
    assert p.session.release_attempts==1 and p.calls.count(('native_release',11))==1


@pytest.mark.parametrize('error',[OSError,ImportError,TypeError])
def test_original_error_propagates_without_lookup_close_or_retry(prepared,error):
    p=prepared;p.ffi.error=error
    with audio.SoundFileBindingScope(p.session,p.view):
        with pytest.raises(error,match='injected'): audio.bind_staged_soundfile(p.ffi)
        with pytest.raises(NativeExecutionUnsupported): audio.bind_staged_soundfile(p.ffi)
    assert p.calls==[('cast',11),('handle_dlopen',11)] and not p.session._borrows
    p.session.close()
    assert p.session.release_attempts==1


def test_failed_import_scope_releases_only_lease_and_invalidates_exports(prepared):
    p=prepared
    with pytest.raises(ImportError):
        with audio.SoundFileBindingScope(p.session,p.view):
            binding=audio.bind_staged_soundfile(p.ffi)
            raise ImportError('after binding')
    assert not p.session._borrows and p.session.release_attempts==0
    with pytest.raises(NativeExecutionUnsupported,match='closed'): binding.library.sf_version_string()
    p.session.close()


def test_consumer_gc_releases_borrow_after_cached_export_dies(prepared):
    p=prepared
    with audio.SoundFileBindingScope(p.session,p.view): binding=audio.bind_staged_soundfile(p.ffi)
    export=binding.library.sf_version_string
    reference=weakref.ref(binding)
    del binding;gc.collect()
    assert reference() is not None and p.session._borrows
    del export;gc.collect()
    assert reference() is None and not p.session._borrows
    p.session.close()


@pytest.mark.parametrize('problem',['closed','not_loaded','target','hash','root','dynamic','foreign_ffi',
    'source','origin','backend_type','module_replacement'])
def test_wrong_changed_or_foreign_binding_refuses_before_dlopen(prepared,problem,monkeypatch):
    p=prepared
    if problem=='closed': p.session.close();p.calls.clear()
    if problem=='not_loaded': p.session.state='ready'
    if problem=='target': p.session.plan=replace(p.session.plan,target='foreign.dll')
    if problem=='hash': p.session.plan=replace(p.session.plan,nodes=(replace(p.session.plan.nodes[0],sha256='a'*64),))
    if problem=='root': p.session.guard.root=p.root/'foreign'
    if problem=='dynamic': p.session.plan=replace(p.session.plan,requires_dynamic_closure=True)
    if problem=='source': (p.root/audio.GENERATED_SLOT).write_bytes(b'changed')
    if problem=='origin': p.view.generated.__spec__.origin='foreign'
    if problem=='backend_type': p.view.backend.FFI=object
    if problem=='module_replacement': monkeypatch.setitem(__import__('sys').modules,'_soundfile',ModuleType('_soundfile'))
    with pytest.raises(NativeExecutionUnsupported):
        with audio.SoundFileBindingScope(p.session,p.view):
            audio.bind_staged_soundfile(object() if problem=='foreign_ffi' else p.ffi)
    assert p.calls==[] and not p.session._borrows


def test_scope_and_raw_handle_cannot_supply_authority(prepared):
    p=prepared
    with pytest.raises(NativeExecutionUnsupported,match='scope_missing'): audio.bind_staged_soundfile(p.ffi)
    with pytest.raises(NativeExecutionUnsupported): audio.SoundFileBindingScope(11,p.view)
    scope=audio.SoundFileBindingScope(p.session,p.view)
    with scope:
        with pytest.raises(NativeExecutionUnsupported):
            with audio.SoundFileBindingScope(p.session,p.view): pass
    with pytest.raises(NativeExecutionUnsupported,match='consumed'):
        with scope: pass


def test_borrow_registration_owner_and_lifetime_are_exact(prepared):
    p=prepared
    reference=p.session.borrow_handle()
    other=NativeLoadSession(p.session.reader,p.session.guard)
    with pytest.raises(NativeExecutionUnsupported): reference.require(other)
    unregistered=NativeHandleBorrow(p.session)
    with pytest.raises(NativeExecutionUnsupported): unregistered.require(p.session)
    p.session.handle=12
    with pytest.raises(NativeExecutionUnsupported): reference.require(p.session)
    p.session.handle=11
    reference.close();reference.close()
    with pytest.raises(NativeExecutionUnsupported): reference.require(p.session)
    p.session.close()


@pytest.mark.parametrize('pointer',[None,'libsndfile.dll',b'libsndfile.dll',11])
def test_cast_cannot_turn_borrowed_handle_into_a_filename_load(prepared,pointer):
    p=prepared;p.ffi.cast=lambda *_:pointer
    with audio.SoundFileBindingScope(p.session,p.view):
        with pytest.raises(NativeExecutionUnsupported,match='pointer_invalid'):
            audio.bind_staged_soundfile(p.ffi)
    assert p.calls==[] and not p.session._borrows


@pytest.mark.parametrize('handle',[None,True,0,-1,2**64])
def test_borrow_requires_a_real_handle_value(prepared,handle):
    prepared.session.handle=handle
    with pytest.raises(NativeExecutionUnsupported,match='not_loaded'): prepared.session.borrow_handle()
