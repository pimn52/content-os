"""Separate opt-in audio diagnostic protocol; no production authority."""
import ast
import hashlib
import json
from pathlib import Path
import secrets
import subprocess
from tempfile import TemporaryFile

from .audio_graph_child import VERSION, canonical, OUTPUT_LIMIT, INPUT_LIMIT, PREFIX
from .audio_native_preflight import preflight_audio_native_targets
from .python_origin_probe import ENTRY, owned_probe_files, TREE_LIMIT
from .runtime_tree import OwnedRuntimeFile, _scan_private
from .runtime_primitives import ExecutionPreparationError, _exact_path
from .windows_loader import require_no_redirection


def owned_audio_graph_probe_files():
    """New entry at an existing allowed destination; all old bundle functions unchanged."""
    directory = Path(__file__).parent
    def read(name):
        path = directory / name
        _exact_path(path, directory=False)
        with path.open('rb') as source: raw = source.read(1024 * 1024 + 1)
        if not 0 < len(raw) <= 1024 * 1024:
            raise ExecutionPreparationError('execution_audio_bundle_invalid')
        return raw
    scanner = read('python_origin_child.py')
    lines = scanner.splitlines(keepends=True)
    pieces = []
    wanted = {'ProbeFailure', '_exact_path', 'scan_private'}
    for node in ast.parse(scanner).body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in wanted:
            pieces.append(b''.join(lines[node.lineno - 1:node.end_lineno]) + b'\n\n')
            wanted.remove(node.name)
    if wanted: raise ExecutionPreparationError('execution_audio_bundle_invalid')
    # Scanner limits are explicit, same as its original protocol.
    entry = (b'TREE_LIMIT = 256 * 1024 * 1024\nFILE_LIMIT = 10000\nDIRECTORY_LIMIT = 3000\n' +
             b''.join(pieces) + read('audio_graph_child.py'))
    rows = [OwnedRuntimeFile(row.destination, entry, row.role) if row.destination == ENTRY else row
            for row in owned_probe_files()]
    for name in ('native_load.py', 'pe_imports.py', 'pe_resources.py', 'native_manifest.py',
                 'soundfile_binding.py', 'soundfile_handle.py',
                 'acquired_native_recipe.json', 'private_native_recipe.json',
                 'audio_native_graph.py', 'cpu_native_recipe.json'):
        rows.append(OwnedRuntimeFile(PREFIX + name, read(name)))
    if len({row.destination for row in rows}) != len(rows):
        raise ExecutionPreparationError('execution_audio_bundle_invalid')
    return tuple(rows)


class PreparedAudioGraphProbe:
    """Consumes a fixed tree once; construction and launch authority stay separate."""
    def __init__(self, tree, host, invocation, derivation):
        self._tree, self._host, self.invocation = tree, host, invocation
        self.derivation = derivation
        self._nonce, self._state = secrets.token_hex(32), 'prepared'

    def close(self):
        self._state = 'closed'
        self._tree.close()

    def __enter__(self): return self
    def __exit__(self, *_args): self.close()

    @property
    def inventory(self): return self._tree.inventory

    @property
    def host_observation(self): return self._host.observation

    @property
    def preparation_identity_sha256(self):
        return getattr(self, '_preparation_identity_sha256', None)

    def verify(self):
        try:
            self._verify()
        except Exception:
            if getattr(self, '_preparation_binding', None) is not None:
                self._state = 'failed'
            raise

    def _verify(self):
        if self._state != 'prepared':
            raise ExecutionPreparationError('execution_preparation_consumed')
        self._tree.verify()
        self._host.verify()
        binding = getattr(self, '_preparation_binding', None)
        if binding is not None:
            host, invocation, derivation, inventory, observation = binding
            if (self._host is not host or self.invocation != invocation or
                    self.derivation != derivation or self.inventory.descriptor != inventory or
                    host._runtime_root != self._tree.root or
                    host._inventory_json != self.inventory.model_dump_json() or
                    canonical(host.observation.canonical()) != observation):
                raise ExecutionPreparationError('execution_audio_preparation_changed')
            self._source_check()
        root = self._tree.root
        argv = (str(root / 'python.exe'), '-I', '-S', '-B', str(root / ENTRY))
        if (tuple(self.invocation.argv) != argv or self.invocation.cwd != root or
            type(self.invocation.timeout_seconds) not in (int, float) or
            not 0 < self.invocation.timeout_seconds <= 15 or
            {'PATH', 'PYTHONPATH', 'PYTHONHOME'} & {key.upper() for key, _ in self.invocation.environment}):
            raise ExecutionPreparationError('execution_audio_invocation_invalid')
        entries = {row.name: row for row in self.inventory.files}
        for row in owned_audio_graph_probe_files():
            actual = entries.get(row.destination)
            if (actual is None or actual.role != row.role or actual.size_bytes != len(row.payload) or
                actual.sha256 != hashlib.sha256(row.payload).hexdigest()):
                raise ExecutionPreparationError('execution_audio_bundle_changed')

    def run_probe(self):
        if self._state != 'prepared':
            raise ExecutionPreparationError('execution_preparation_consumed')
        try:
            self.verify()
            require_no_redirection(self)
            expected = preflight_audio_native_targets(self, self.derivation)
            inventory = self.inventory.model_dump(mode='json')
            inventory['directories'].sort()
            inventory['files'].sort(key=lambda row: row['name'])
            request = {'probe_version': VERSION, 'nonce': self._nonce, 'inventory': inventory,
                'preflight': json.loads(expected.record), 'derivation': json.loads(self.derivation.descriptor)}
            payload = canonical(request)
            if len(payload) > INPUT_LIMIT:
                raise ExecutionPreparationError('execution_audio_input_limit')
            self._tree.claim_for_launch()
            self._state = 'consumed'
            with TemporaryFile() as output:
                result = subprocess.run(list(self.invocation.argv), cwd=self.invocation.cwd,
                    env=dict(self.invocation.environment), input=payload, stdout=output,
                    stderr=subprocess.DEVNULL, timeout=self.invocation.timeout_seconds,
                    shell=False, check=False)
                output.seek(0)
                raw = output.read(OUTPUT_LIMIT + 1)
            if not 0 < len(raw) <= OUTPUT_LIMIT:
                raise ExecutionPreparationError('execution_audio_output_limit')
            try:
                report = json.loads(raw)
                exact = {'probe_version': VERSION, 'nonce': self._nonce,
                    'preflight': request['preflight'], 'report_sha256': expected.identity_sha256,
                    'load_attempts': 0, 'dispatch_authorized': False}
                if (type(result.returncode) is not int or result.returncode != 0 or
                    canonical(report) != raw or raw != canonical(exact)):
                    raise ValueError()
            except (ValueError, TypeError):
                raise ExecutionPreparationError('execution_audio_protocol_invalid') from None
            current = _scan_private(self.invocation.cwd,
                {row.name: row.role for row in self.inventory.files}, TREE_LIMIT)
            if current.descriptor != self.inventory.descriptor:
                raise ExecutionPreparationError('execution_fixed_runtime_changed')
            self._host.verify()
            return report
        except subprocess.TimeoutExpired:
            raise ExecutionPreparationError('execution_prepared_timeout') from None
        except OSError:
            raise ExecutionPreparationError('execution_prepared_launch_failed') from None
        finally:
            self._state = 'consumed'
