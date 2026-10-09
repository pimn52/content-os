"""Owned PCM selection/current-layout witness. Actual startup/native proof pending."""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from . import cpu_native_recipe as cpu
from . import pcm_static_core as core
from .prepared import ExecutionPreparationError, NativeExecutionUnsupported
from .runtime_tree import PreparedRuntimeTree, _exact_path

POLICY_SLOT = 'Lib/site-packages/app/providers/omnivoice_pcm_import_policy.json'
BOOTSTRAP_SLOT = '_content_os_pcm_bootstrap.py'
IMPORT_ROOTS = ('Lib', 'DLLs', 'Lib/site-packages')
ABSENT_ROOTS = ('soundfile', 'librosa', 'soxr', 'torchcodec')
PTH = b'Lib\nDLLs\nLib/site-packages\n'
SOURCE_PINS = {
    'torchaudio/__init__.py': '9145a793070a111323ee6f81e04f36e9a1995866b9b09b350c4a4b3fa2d9a04b',
    'torchaudio/_backend/__init__.py': 'd9b313677b46dbfe67be7a80026bc49c60489cee5af437543cda325c49d7d4cd',
    'torchaudio/_backend/utils.py': 'f0792657f1821d9843f9768a350eb49c616cbbbad582bd523a16ac37abfdd5a2',
    'torchaudio/_backend/soundfile_backend.py': 'b154843363a7e8f318ec06cfaa97ef135bb5060dbfd1a892ad9e137250058bfc',
    'torchaudio/_extension/__init__.py': 'b3a0335e870372e8749ad61550238e8d9fe60a64829e8f236d937be74608f8fb',
    'torchaudio/_extension/utils.py': 'c050c5f01e90db650bdf3c97f2cc19f898e50e09bdf957637ab6ac950af6c886',
    'torchaudio/_internal/module_utils.py': '77419ffc385a03e7c4b712878d6843c18ac1b07e820a4e357948bff531e143d9',
    'torio/_extension/__init__.py': 'f469c588b58f0958936d450d8a8f40b7533400b1aa2ad67d9453ae3d49f549cf',
    'torio/_extension/utils.py': 'a6920604593cebccfb41b7d28dac0752448edf267b30bda31c5804fa3e86ca62',
    'transformers/audio_utils.py': 'f1e3bba3874a0166049cd1abd3e13eaa8ffed03a9c5d1db9501effbe9983e753',
    'transformers/utils/import_utils.py': 'e147f99cf68352e3260e9780930501c3becc0c40a5ff04150ff641b724161732',
    'transformers/models/higgs_audio_v2_tokenizer/modeling_higgs_audio_v2_tokenizer.py':
        'afd5d6e4dc55f32c9207513a43064d5c56d80de23e809dc47fb48d1e577bcb2d',
}
OWNED_FILES = ('omnivoice_pcm_import.py', 'cpu_native_recipe.py', 'cpu_native_recipe.json', 'pcm_static_core.py')
PENDING = ('actual_fresh_child_startup_and_import_finders', 'required_native_source_graph_and_origins',
           'context_search_handle_lifetime', 'pydub_executable_discovery', 'terms_and_normal_worker',
           'stdlib_native_bootstrap_prechecks', 'independent_child_host_observation')


def _sha(raw): return hashlib.sha256(raw).hexdigest()


def _source(path):
    return core.read_source(path)


def owned_pcm_import_policy_bytes():
    current = Path(__file__).resolve().parent
    record = dict(recipe='omnivoice-pcm-import-selection-v1', version=1,
        import_roots=IMPORT_ROOTS, absent_roots=ABSENT_ROOTS, source_pins=SOURCE_PINS,
        startup_config_sha256=_sha(PTH), cpu_policy_sha256=cpu._owned()[1],
        implementation_sources={name: _sha(_source(current/name)) for name in OWNED_FILES},
        bootstrap_sha256=_sha(owned_pcm_bootstrap_bytes()),
        pending=PENDING, dispatch_authorized=False)
    return (json.dumps(record, sort_keys=True, separators=(',', ':'))+'\n').encode()


def owned_pcm_bootstrap_bytes():
    return _source(Path(__file__).with_name('omnivoice_pcm_bootstrap.py'))


def _optional_name(name):
    return core.optional_name(name, ABSENT_ROOTS)


def owned_core_rules():
    """Current repository rules; never supplied by the protocol request."""
    from . import omnivoice_entry_v3 as entry
    current = Path(__file__).resolve().parent
    policy = owned_pcm_import_policy_bytes()
    files = {}
    def add(name, raw, role='dependency'):
        files[name] = dict(sha256=_sha(raw), role=role)
    for name in ('python._pth', 'python312._pth'): add(name, PTH)
    add(POLICY_SLOT, policy)
    add(BOOTSTRAP_SLOT, owned_pcm_bootstrap_bytes(), 'entrypoint')
    for name in set(OWNED_FILES) | set(entry.OWNED_MODULES):
        add('Lib/site-packages/app/providers/'+name, _source(current/name))
    for name, digest in {**cpu._owned()[0]['sources'], **entry.SOURCE_HASHES, **SOURCE_PINS}.items():
        files['Lib/site-packages/'+name] = dict(sha256=digest, role='dependency')
    return dict(import_roots=list(IMPORT_ROOTS), absent_roots=list(ABSENT_ROOTS), files=files,
        upstream=entry.UPSTREAM_SLOT, derived=entry.DERIVED_SLOT, descriptor=entry.DESCRIPTOR_SLOT,
        cpu_sha256=cpu._owned()[1], policy_sha256=_sha(policy), pending=list(PENDING))


def _inspect(tree, environment):
    from .omnivoice_audio_pcm import derive_pcm_audio
    inventory = tree.verify()
    try:
        facts = core.inspect_pcm(tree.root, json.loads(inventory.canonical_bytes()),
            dict(environment.environment), environment.work_directory, environment.windows_root,
            owned_core_rules(), derive_pcm_audio)
        environment.verify()
        if tree.verify().descriptor != inventory.descriptor:
            raise ValueError('omnivoice_pcm_import_identity_changed')
        return inventory, facts['policy_sha256']
    except ExecutionPreparationError: raise
    except Exception as error:
        code = str(error)
        if not code.startswith(('omnivoice_', 'execution_')):
            code = 'omnivoice_pcm_import_source_changed'
        if code.startswith('execution_cpu_'):
            raise NativeExecutionUnsupported(code) from None
        raise ExecutionPreparationError(code) from None


@dataclass(frozen=True)
class PCMImportObservation:
    inventory_sha256: str
    policy_sha256: str
    environment_sha256: str
    evidence_level: str = 'current_selected_layout_and_declared_environment'
    pending: tuple = PENDING

    @property
    def dispatch_authorized(self): return False

    @property
    def loading_authorized(self): return False


class PreparedPCMImportBoundary:
    """Rechecks the same selected tree and frozen CPU environment; never launches."""
    __slots__ = ('_tree', '_environment', '_env_bytes', '_observation', '_state')

    def __init__(self, tree, environment, inventory, policy_sha):
        self._tree, self._environment = tree, environment
        self._env_bytes = environment.canonical_bytes()
        self._observation = PCMImportObservation(inventory.descriptor.sha256, policy_sha, _sha(self._env_bytes))
        self._state = 'prepared'

    @property
    def tree(self): return self._tree

    @property
    def environment(self): return self._environment

    def verify(self):
        if self._state != 'prepared':
            raise ExecutionPreparationError('omnivoice_pcm_import_boundary_consumed')
        try:
            env = self._environment
            # Reconstruct the declared environment through its owned factory,
            # preserving canonical/disjoint root/work/Windows boundary checks.
            if (type(env) is not cpu.CPUEnvironment or env.root != self._tree.root
                    or cpu.cpu_environment(root=env.root, work_directory=env.work_directory,
                        windows_root=env.windows_root) != env
                    or env.canonical_bytes() != self._env_bytes):
                raise ExecutionPreparationError('omnivoice_pcm_environment_changed')
            _exact_path(env.windows_root/'System32', directory=True)
            inventory, policy = _inspect(self._tree, env)
            actual = PCMImportObservation(inventory.descriptor.sha256, policy, _sha(env.canonical_bytes()))
            if actual != self._observation:
                raise ExecutionPreparationError('omnivoice_pcm_import_identity_changed')
            return actual
        except Exception as error:
            self._state = 'failed'
            if isinstance(error, ExecutionPreparationError): raise
            raise ExecutionPreparationError('omnivoice_pcm_import_boundary_invalid') from None

    def require_native_preparation(self):
        raise NativeExecutionUnsupported('execution_native_dependency_closure_unsupported')

    def close(self): self._state = 'closed'  # Runtime ownership remains with caller.


def prepare_pcm_import_boundary(*, tree, work_directory, windows_root):
    try:
        if type(tree) is not PreparedRuntimeTree:
            raise ExecutionPreparationError('omnivoice_pcm_import_boundary_invalid')
        environment = cpu.cpu_environment(root=tree.root, work_directory=work_directory, windows_root=windows_root)
        _exact_path(windows_root/'System32', directory=True)
        inventory, policy = _inspect(tree, environment)
        value = PreparedPCMImportBoundary(tree, environment, inventory, policy)
        value.verify()
        return value
    except ExecutionPreparationError: raise
    except Exception:
        raise ExecutionPreparationError('omnivoice_pcm_import_boundary_invalid') from None


def require_pcm_boundary(boundary, *, tree=None, root=None):
    if (type(boundary) is not PreparedPCMImportBoundary
            or tree is not None and boundary.tree is not tree
            or root is not None and boundary.tree.root != root):
        raise ExecutionPreparationError('omnivoice_pcm_import_boundary_required')
    return boundary.verify()
