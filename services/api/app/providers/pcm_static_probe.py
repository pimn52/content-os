"""One-use protocol 7 synthetic transport. Actual selected-child launch is closed."""
import ast
import json
from pathlib import Path
import secrets

from . import omnivoice_pcm_import as pcm
from . import pcm_static_core as core
from . import pcm_static_wire as wire
from .runtime_tree import OwnedRuntimeFile
from .prepared import ExecutionPreparationError, NativeExecutionUnsupported, PendingInvocation
from .component_probe import PreparedComponentHost


def _definitions(raw, wanted):
    lines, parts = raw.splitlines(keepends=True), []
    wanted = set(wanted)
    for node in ast.parse(raw).body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in wanted:
            parts.append(b''.join(lines[node.lineno-1:node.end_lineno]) + b'\n\n')
            wanted.remove(node.name)
    if wanted: raise ExecutionPreparationError('execution_pcm_bundle_invalid')
    return b''.join(parts)


def owned_pcm_static_files():
    """Finite current-owned bundle; no app initializer or application graph."""
    directory = Path(__file__).resolve().parent
    read = lambda name: core.read_source(directory/name)
    rules = pcm.owned_core_rules()
    rows = [OwnedRuntimeFile(wire.PREFIX+'__init__.py', b'"""Owned static PCM diagnostic only."""\n')]
    for name in ('pcm_static_core.py', 'omnivoice_audio_pcm.py'):
        rows.append(OwnedRuntimeFile(wire.PREFIX+name, read(name)))
    rows.append(OwnedRuntimeFile(wire.PREFIX+'rules.py', ('RULES = '+repr(rules)+'\n').encode()))
    helpers = {r.destination: dict(sha256=core.sha(r.payload), role=r.role) for r in rows}
    guard = _definitions(read('omnivoice_pcm_bootstrap.py'), {'verify_startup'})
    scanner = _definitions(read('python_origin_child.py'), {'ProbeFailure', '_exact_path', 'scan_private'})
    preamble = ('import sys\nENTRY = '+repr(wire.ENTRY)+'\nIMPORT_ROOTS = '+repr(('Lib', 'DLLs', 'Lib\\site-packages'))+
        '\nFORBIDDEN_PRELOADS = '+repr(('soundfile', '_soundfile', '_soundfile_data', 'librosa', 'soxr',
         'torchcodec', 'app', 'torch', 'numpy', 'torchaudio', 'torio', 'transformers', 'omnivoice', 'pydantic', '_content_os_pcm'))+
        '\nHELPER_RULES = '+repr(helpers)+'\n').encode()
    # Parser/scanner functions are embedded, not imported application helpers.
    # Their stdlib imports occur only after the sys-only guard in main.
    parser = read('pcm_static_wire.py')
    parser_tree = ast.parse(parser)
    parser_lines = parser.splitlines(keepends=True)
    # Defer all wire imports until after startup verification; definitions/constants are safe.
    parser = b''.join(b''.join(parser_lines[n.lineno-1:n.end_lineno])+b'\n'
        for n in parser_tree.body if not isinstance(n, (ast.Import, ast.ImportFrom)))
    body = read('pcm_static_child.py').replace(b'    from pathlib import Path\n    import os\n',
        b'    global hashlib, json, re, unicodedata\n    import hashlib, json, re, unicodedata\n    from pathlib import Path\n    import os\n')
    entry = preamble + guard + scanner + parser + body
    ast.parse(entry)
    rows.append(OwnedRuntimeFile(wire.ENTRY, entry, 'entrypoint'))
    return tuple(rows)


def bundle_identity(rows):
    return core.sha(core.canonical({r.destination: dict(sha256=core.sha(r.payload), role=r.role) for r in rows}))


class PreparedPCMStaticProbe:
    """Diagnostic results never satisfy native preparation or generation admission.

    No subprocess entry is exposed: launch prechecks remain unresolved. The
    explicit synthetic exchange seam tests the one-response protocol lifecycle.
    """
    def __init__(self, boundary, *, host, timeout_seconds=15):
        pcm.require_pcm_boundary(boundary)
        if (type(host) is not PreparedComponentHost or host.tree is not boundary.tree
                or host.windows_root != boundary.environment.windows_root):
            raise ExecutionPreparationError('execution_pcm_host_required')
        if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 15:
            raise ExecutionPreparationError('execution_pcm_invocation_invalid')
        self.boundary = self._boundary = boundary
        self.host = self._host = host
        self._host_bytes = core.canonical(host.verify().canonical())
        self._tree = boundary.tree
        self._nonce, self._state = secrets.token_hex(32), 'prepared'
        env = boundary.environment
        self.invocation = PendingInvocation((str(self._tree.root/'python.exe'), '-I', '-S', '-B',
            str(self._tree.root/wire.ENTRY)), self._tree.root, env.environment, timeout_seconds)
        self._invocation = self.invocation
        self._rules = core.canonical(pcm.owned_core_rules())
        self._bundle = bundle_identity(owned_pcm_static_files())
        self._inventory = self._tree.inventory.canonical_bytes()
        self._environment = env.canonical_bytes()
        self.verify()

    def verify(self):
        try:
            if self._state != 'prepared': raise ValueError('execution_pcm_probe_consumed')
            if (self.boundary is not self._boundary or self.host is not self._host
                    or self.host.tree is not self._tree
                    or self.host.windows_root != self.boundary.environment.windows_root
                    or core.canonical(self.host.verify().canonical()) != self._host_bytes):
                raise ValueError('execution_pcm_host_changed')
            pcm.require_pcm_boundary(self.boundary, tree=self._tree)
            inventory = self._tree.verify()
            if (self.invocation != self._invocation or inventory.canonical_bytes() != self._inventory
                    or self.boundary.environment.canonical_bytes() != self._environment
                    or core.canonical(pcm.owned_core_rules()) != self._rules
                    or bundle_identity(owned_pcm_static_files()) != self._bundle):
                raise ValueError('execution_pcm_preparation_changed')
            rows = {r.name: r for r in inventory.files}
            for row in owned_pcm_static_files():
                actual = rows[row.destination]
                if (actual.sha256 != core.sha(row.payload) or actual.size_bytes != len(row.payload)
                        or actual.role != row.role):
                    raise ValueError('execution_pcm_bundle_changed')
            return inventory
        except Exception as error:
            self._state = 'failed'
            raise ExecutionPreparationError(str(error)) from None

    def _request(self):
        inventory = json.loads(self.verify().canonical_bytes())
        env, root = self.boundary.environment, self._tree.root
        binding = dict(root=str(root), work=str(env.work_directory), windows=str(env.windows_root),
            argv=list(self.invocation.argv), cwd=str(self.invocation.cwd), environment=dict(env.environment),
            bundle_sha256=self._bundle, profile_sha256=core.sha(self._rules),
            inventory_sha256=core.sha(self._inventory), host_sha256=core.sha(self._host_bytes))
        payload = core.canonical(dict(probe_version=wire.VERSION, phase=wire.PHASE, nonce=self._nonce,
            inventory=inventory, binding=binding, caps=wire.validate_inventory(inventory)))
        wire.request(payload)  # Bounds/canonical protocol checked before any transport.
        return payload

    def exchange_synthetic(self, peer):
        """Fake peer only; no subprocess, child, vendor or provider launch."""
        try:
            payload = self._request()
            value = wire.request(payload)
            from .omnivoice_audio_pcm import derive_pcm_audio
            env = self.boundary.environment
            expected = core.inspect_pcm(self._tree.root, value['inventory'], dict(env.environment),
                env.work_directory, env.windows_root, json.loads(self._rules), derive_pcm_audio)
            raw = peer(payload, self.invocation)
            report = wire.decode(raw, wire.OUTPUT_LIMIT)
            self.verify()
            # Strict bytes also reject bool/int aliases, extras and unknown result classes.
            wanted = dict(probe_version=wire.VERSION, phase=wire.PHASE, nonce=self._nonce,
                binding_sha256=core.sha(core.canonical(value['binding'])), facts=expected,
                load_attempts=0, loading_authorized=False, dispatch_authorized=False)
            if raw != core.canonical(wanted) or report != wanted:
                raise ValueError('execution_pcm_response_invalid')
            # Recompute facts after the peer; a prior expected result is not authority.
            current = core.inspect_pcm(self._tree.root, value['inventory'], dict(env.environment),
                env.work_directory, env.windows_root, pcm.owned_core_rules(), derive_pcm_audio)
            if current != expected: raise ValueError('execution_pcm_preparation_changed')
            self.verify()
            return report
        except Exception as error:
            raise ExecutionPreparationError(str(error)) from None
        finally:
            self._state = 'consumed'

    def require_native_preparation(self):
        raise NativeExecutionUnsupported('execution_native_dependency_closure_unsupported')

    def close(self):
        self._state = 'closed'
