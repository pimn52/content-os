"""Stdlib-only borrowed audio library lifetime; no load or admission authority."""
from .runtime_primitives import NativeExecutionUnsupported


class _LibraryProxy:
    def __init__(self, binding): self._binding = binding

    def __getattr__(self, name):
        binding = self._binding
        binding._require()
        value = getattr(binding._library,name)
        if not callable(value): return value
        def call(*args,**kwargs):
            binding._require()
            result = value(*args,**kwargs)
            binding._require()
            return result
        return call


class BoundSoundFile:
    def __init__(self, scope, borrow, library):
        self._scope,self._borrow,self._library = scope,borrow,library
        self._closed = False
        self.library = _LibraryProxy(self)

    def _require(self):
        if self._closed:
            raise NativeExecutionUnsupported('execution_soundfile_binding_closed')
        self._borrow.require(self._scope.session)

    def close(self):
        # Ordered consumer shutdown invalidates cached export wrappers first.
        # No ffi.dlclose and no native FreeLibrary occur here.
        if self._closed: return
        self._closed = True
        self._library = None
        self._borrow.close()
        self._scope._bindings.discard(self)

    def __del__(self):
        # Cached proxies/export wrappers retain this object until consumers die.
        # Releasing a lease here cannot release the session's native reference.
        self.close()
