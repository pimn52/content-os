"""Pinned staged-source derivation and borrowed-handle audio binding.

Normal dispatch does not consume this seam. Native backend implementation,
context and whole-runtime admission still require separate evidence.
"""
import ast
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
from pathlib import Path
import sys
from types import ModuleType
from weakref import WeakSet

from .native_load import NativeLoadSession
from .private_native_recipe import _owned, SOURCE_LIMIT, AUDIO_SLOTS
from .runtime_primitives import NativeExecutionUnsupported, _exact_path
from .soundfile_handle import BoundSoundFile, _LibraryProxy

UPSTREAM_SHA256 = '085feb318464c2a5e82747e0d6da43c214f3a6a4ed3a7a18f2c07fe7b3750df2'
BLOCK_SHA256 = '2fd25a306fe012111dcf3e531ee9a233c81ef7a881a07e05baabc275be46e2c2'
TRANSFORM_VERSION = 'soundfile-owned-handle-v2'
REPLACEMENT = (b'from app.providers.soundfile_binding import bind_staged_soundfile as _bind_staged_soundfile\n'
    b'_soundfile_binding = _bind_staged_soundfile(_ffi)\n'
    b'_snd = _soundfile_binding.library\n')
GENERATED_SLOT = 'Lib/site-packages/_soundfile.py'
UPSTREAM_SLOT = 'Lib/site-packages/soundfile.py'
_active = ContextVar('content_os_soundfile_binding', default=None)


def _hash(raw): return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class SoundFileDerivation:
    payload: bytes
    descriptor: bytes

    @property
    def sha256(self): return _hash(self.payload)

    @property
    def dispatch_authorized(self): return False


def derive_soundfile(upstream):
    """Pure byte transform of one frozen block; never writes or executes it."""
    import json
    try:
        if type(upstream) is not bytes or len(upstream) > SOURCE_LIMIT or _hash(upstream) != UPSTREAM_SHA256:
            raise ValueError()
        tree = ast.parse(upstream)
        blocks = [node for node in tree.body if isinstance(node, ast.Try)
            and any(isinstance(child, ast.Name) and child.id == '_full_path' for child in ast.walk(node))]
        if len(blocks) != 1: raise ValueError()
        node = blocks[0]
        handler = node.handlers[0]
        if (node.lineno != 162 or node.end_lineno != 218 or not isinstance(handler.type, ast.Tuple)
                or [item.id for item in handler.type.elts] != ['OSError','ImportError','TypeError']):
            raise ValueError()
        lines = upstream.splitlines(keepends=True)
        start, stop = sum(map(len,lines[:node.lineno-1])), sum(map(len,lines[:node.end_lineno]))
        if _hash(upstream[start:stop]) != BLOCK_SHA256: raise ValueError()
        derived = upstream[:start] + REPLACEMENT + upstream[stop:]
        ast.parse(derived)  # Syntax only; do not compile/evaluate the vendor source.
        policy, identity = _owned()
        binder = Path(__file__)
        _exact_path(binder,directory=False)
        with binder.open('rb') as source: binder_bytes = source.read(SOURCE_LIMIT+1)
        if len(binder_bytes) > SOURCE_LIMIT: raise ValueError()
        core = binder.with_name('soundfile_handle.py')
        _exact_path(core,directory=False)
        with core.open('rb') as source: core_bytes = source.read(SOURCE_LIMIT+1)
        if len(core_bytes) > SOURCE_LIMIT: raise ValueError()
        deps = sorted((row['slot'],row['sha256']) for row in (*policy['members'],*policy['sources'])
            if row['slot'] in (*AUDIO_SLOTS,GENERATED_SLOT,UPSTREAM_SLOT))
        descriptor = {'recipe':TRANSFORM_VERSION,'source_kind':'derived','upstream_sha256':UPSTREAM_SHA256,
            'transform_sha256':_hash(TRANSFORM_VERSION.encode()+BLOCK_SHA256.encode()+REPLACEMENT),
            'derived_sha256':_hash(derived),'binder_sha256':_hash(binder_bytes),
            'handle_core_sha256':_hash(core_bytes),
            'private_policy_sha256':identity,'dependencies':deps,
            'terms':[(row['slot'],row['sha256']) for row in policy['terms']],
            'dispatch_authorized':False,'backend_handle_semantics':'not_verified'}
        return SoundFileDerivation(derived,json.dumps(descriptor,sort_keys=True,separators=(',',':')).encode())
    except (ValueError,TypeError,AttributeError,SyntaxError,IndexError,OSError):
        raise NativeExecutionUnsupported('execution_soundfile_source_derivation_invalid') from None


def _source(root, inventory, row):
    entries = [entry for entry in inventory.files if entry.name == row['slot']]
    if (len(entries) != 1 or entries[0].role != 'dependency'
            or (entries[0].size_bytes,entries[0].sha256) != (row['size_bytes'],row['sha256'])):
        raise NativeExecutionUnsupported('execution_soundfile_binding_source_changed')
    path = root / row['slot']
    _exact_path(path,directory=False)
    with path.open('rb') as source: raw = source.read(SOURCE_LIMIT+1)
    if len(raw) != row['size_bytes'] or _hash(raw) != row['sha256']:
        raise NativeExecutionUnsupported('execution_soundfile_binding_source_changed')
    return path


@dataclass(frozen=True)
class GeneratedFFIView:
    root: Path
    inventory: object
    generated: ModuleType
    backend: ModuleType
    ffi: object
    policy_sha256: str

    def verify(self):
        policy, identity = _owned()
        if identity != self.policy_sha256:
            raise NativeExecutionUnsupported('execution_soundfile_binding_source_changed')
        generated = next(row for row in policy['sources'] if row['slot'] == GENERATED_SLOT)
        backend = next(row for row in policy['members'] if row['slot'] == AUDIO_SLOTS[0])
        _exact_path(self.root,directory=True)
        for name,module,row in (('_soundfile',self.generated,generated),('_cffi_backend',self.backend,backend)):
            path = _source(self.root,self.inventory,row)
            if (type(module) is not ModuleType or sys.modules.get(name) is not module
                    or module.__name__ != name or Path(getattr(module,'__file__','')) != path
                    or getattr(getattr(module,'__spec__',None),'origin',None) != str(path)):
                raise NativeExecutionUnsupported('execution_soundfile_binding_origin_changed')
        if getattr(self.generated,'ffi',None) is not self.ffi or type(self.ffi) is not getattr(self.backend,'FFI',None):
            raise NativeExecutionUnsupported('execution_soundfile_binding_ffi_changed')


def observe_generated_ffi(*, root, inventory, generated, backend):
    """Observe already imported fixed modules; does not import them or admit ABI."""
    view = GeneratedFFIView(root,inventory,generated,backend,getattr(generated,'ffi',None),_owned()[1])
    view.verify()
    return view


class SoundFileBindingScope:
    """Owned import scope; successful consumers retain leases beyond scope exit."""
    def __init__(self, session, view):
        if type(session) is not NativeLoadSession or type(view) is not GeneratedFFIView:
            raise NativeExecutionUnsupported('execution_soundfile_binding_owner_invalid')
        self.session,self.view = session,view
        self._bindings,self._token,self._used,self._binding_attempted = WeakSet(),None,False,False

    def __enter__(self):
        if self._used or _active.get() is not None:
            raise NativeExecutionUnsupported('execution_soundfile_binding_scope_consumed')
        self._used = True
        self.view.verify()
        self._token = _active.set(self)
        return self

    def __exit__(self,kind,*_):
        if self._token is not None:
            _active.reset(self._token); self._token = None
        if kind is not None:
            for binding in tuple(self._bindings): binding.close()

    def bind(self, ffi):
        if _active.get() is not self or self._binding_attempted or ffi is not self.view.ffi:
            raise NativeExecutionUnsupported('execution_soundfile_binding_owner_invalid')
        self.view.verify()
        policy, _ = _owned()
        target = next(row for row in policy['members'] if row['slot'] == AUDIO_SLOTS[1])
        session = self.session
        if (session.state != 'loaded' or session.guard.root != self.view.root or session.plan is None
                or session.plan.target != target['slot'] or session.plan.requires_dynamic_closure):
            raise NativeExecutionUnsupported('execution_soundfile_binding_owner_invalid')
        nodes = [node for node in session.plan.nodes if node.name == target['slot']]
        if (len(nodes) != 1 or (nodes[0].size_bytes,nodes[0].sha256) != (target['size_bytes'],target['sha256'])
                or nodes[0].resources.kind != 'requires_private_root_context'
                or nodes[0].resources.manifest_sha256 != target['manifest_sha256']):
            raise NativeExecutionUnsupported('execution_soundfile_binding_owner_invalid')
        borrow = session.borrow_handle()
        self._binding_attempted = True
        try:
            pointer = ffi.cast('void *',borrow.require(session))
            if pointer is None or isinstance(pointer,(str,bytes,int)):
                raise NativeExecutionUnsupported('execution_soundfile_binding_pointer_invalid')
            shape = ffi.typeof(pointer)
            if shape.kind != 'pointer' or shape.cname != 'void *':
                raise NativeExecutionUnsupported('execution_soundfile_binding_pointer_invalid')
            library = ffi.dlopen(pointer)
            if library is None:
                raise NativeExecutionUnsupported('execution_soundfile_binding_library_missing')
            borrow.require(session)
            binding = BoundSoundFile(self,borrow,library)
            self._bindings.add(binding)
            return binding
        except BaseException:
            borrow.close()
            raise


def bind_staged_soundfile(ffi):
    scope = _active.get()
    if type(scope) is not SoundFileBindingScope:
        raise NativeExecutionUnsupported('execution_soundfile_binding_scope_missing')
    return scope.bind(ffi)
