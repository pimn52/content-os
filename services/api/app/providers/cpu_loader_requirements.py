"""Owned original-loader requirements; declarations, never execution proof."""
from dataclasses import dataclass, asdict
import hashlib
import json
import re
from pathlib import PurePosixPath
from pathlib import Path
import os
from app.domain.execution_runtime import RuntimeInventory
from . import cpu_native_recipe as cpu
from .acquired_native_recipe import owned_acquired_bytes, parse_acquired_descriptor
from .runtime_primitives import NativeExecutionUnsupported
from .runtime_primitives import _exact_path, require_search_topology, NATIVE_SUFFIXES
from .pe_bounded import _PEFile
from .windows_os_policy import owned_os_policy_bytes, parse_os_policy


@dataclass(frozen=True)
class CPULoaderRequirements:
    cpu_policy_sha256: str
    acquired_policy_sha256: str
    os_policy_sha256: str
    torch_glob_members: tuple[tuple[str, int, str], ...]
    compiler_roots: tuple[tuple[str, int, str], ...]
    numpy_private_members: tuple[tuple[str, int, str], ...]
    source_identities: tuple[tuple[str, str], ...]
    private_search_directories: tuple[str, ...]
    absent_probe_directories: tuple[str, ...]
    pending: tuple[str, ...]

    @property
    def dispatch_authorized(self):
        return False

    @property
    def loading_authorized(self):
        return False

    def canonical_bytes(self):
        return json.dumps(dict(recipe='original-cpu-loader-requirements-v1', version=1,
                               requirements=asdict(self)), sort_keys=True, separators=(',', ':')).encode()

    @property
    def sha256(self):
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


def original_cpu_loader_requirements():
    """Pure owned declarations. Does not observe filesystem, prefixes or handles."""
    try:
        policy, identity = cpu._owned()
        acquired, acquired_identity = parse_acquired_descriptor(owned_acquired_bytes())
        torch = tuple(sorted((cpu.PREFIX+r['name'], r['size_bytes'], r['sha256'])
                             for r in policy['members']
                             if PurePosixPath(r['name']).parent == PurePosixPath('torch/lib')
                             and r['name'].endswith('.dll')))
        if len(torch) != 11 or len({PurePosixPath(r[0]).name.casefold() for r in torch}) != 11:
            raise ValueError()
        roots = ('msvcp140.dll', 'vcomp140.dll', 'vcruntime140.dll', 'vcruntime140_1.dll')
        members = {r['slot']:r for r in acquired['members']}
        compiler = tuple((slot, members[slot]['size_bytes'], members[slot]['sha256']) for slot in roots)
        numpy = tuple(sorted((slot, members[slot]['size_bytes'], members[slot]['sha256'])
                             for slot in acquired['numpy_loader']['members']))
        if len(numpy) != 2 or any(PurePosixPath(r[0]).parent != PurePosixPath('Lib/site-packages/numpy.libs') for r in numpy):
            raise ValueError()
        sources = tuple(sorted([(cpu.PREFIX+name, digest) for name,digest in policy['sources'].items()]
                               + [(row['slot'], row['sha256']) for row in acquired['sources']]))
        if len(sources) != 22 or len({slot.casefold() for slot,_ in sources}) != 22:
            raise ValueError()
        native = torch+compiler+numpy
        if len({slot.casefold() for slot,_,_ in native}) != len(native):
            raise ValueError()
        basenames = [PurePosixPath(slot).name.casefold() for slot,_,_ in native]
        os_bytes = owned_os_policy_bytes()
        os_policy = parse_os_policy(os_bytes)
        if len(set(basenames)) != len(basenames) or set(basenames) & (os_policy.component_names | os_policy.contract_names):
            raise ValueError()
        if sum(size for _,size,_ in native if type(size) is int) > cpu.GRAPH_BYTES:
            raise ValueError()
        for slot,size,digest in native:
            cpu.runtime_path(slot)
            if type(size) is not int or not 0 < size <= 256*1024**2 or type(digest) is not str or not re.fullmatch('[a-f0-9]{64}', digest):
                raise ValueError()
        return CPULoaderRequirements(identity, acquired_identity, hashlib.sha256(os_bytes).hexdigest(), torch, compiler, numpy, sources,
            ('Lib/site-packages/torch/lib', 'Lib/site-packages/numpy.libs'),
            ('Library/bin', '_cpu_disabled/userbase/Library/bin', '_cpu_disabled/nvtools/bin/x64'),
            ('actual_prefix_userbase_and_absent_probes', 'actual_search_collision_and_directory_registration',
             'compiler_and_torch_preloads_before_original_import', 'retained_exact_handles_and_vendor_reuse',
             'original_error126_PATH_and_VC_failure_branches_unreachable', 'actual_module_origins_and_associated_contexts',
             'whole_python_and_native_dynamic_closure', 'CFFI_source_build_and_borrowed_handle_binding',
             'toolset_ABI_terms_and_normal_worker'))
    except (ValueError, KeyError, TypeError, AttributeError):
        raise NativeExecutionUnsupported('execution_cpu_loader_requirements_unsupported') from None


@dataclass(frozen=True)
class CPULayoutObservation:
    requirements_sha256: str
    inventory_sha256: str
    verified_native: tuple[tuple[str, int, str], ...]
    verified_sources: tuple[tuple[str, int, str], ...]

    @property
    def dispatch_authorized(self):
        return False

    @property
    def loading_authorized(self):
        return False


def inspect_original_cpu_layout(root: Path, inventory: RuntimeInventory):
    """Current selected-file layout only, not prefix/search/lifetime attestation."""
    try:
        if type(inventory) is not RuntimeInventory:
            raise ValueError()
        inventory = RuntimeInventory.model_validate(inventory.model_dump())
        _exact_path(root, directory=True)
        required = original_cpu_loader_requirements()
        entries = {row.name:row for row in inventory.files}
        native = required.torch_glob_members+required.compiler_roots+required.numpy_private_members
        sources = []
        for slot,digest in required.source_identities:
            entry = entries.get(slot)
            if entry is None or entry.sha256 != digest or not 64 <= entry.size_bytes <= 256*1024:
                raise ValueError()
            sources.append((slot,entry.size_bytes,digest))
        if sum(size for _,size,_ in sources) > 1024**2:
            raise ValueError()
        if os.path.lexists(root/'_cpu_disabled') or os.path.lexists(root/'Library/bin'):
            raise ValueError()
        if os.path.lexists(root/'Library'):
            _exact_path(root/'Library', directory=True)
        reserved = parse_os_policy(owned_os_policy_bytes())
        search_roots = ('', 'Lib', 'DLLs', 'Lib/site-packages')+required.private_search_directories

        def verify_visible_members():
            names = (*inventory.directories, *entries)
            for directory in search_roots:
                path = root/directory
                expected = {PurePosixPath(name).name for name in names
                            if str(PurePosixPath(name).parent) == (directory or '.')}
                if not os.path.lexists(path):
                    if expected or directory in inventory.directories:
                        raise ValueError()
                    continue
                _exact_path(path, directory=True)
                actual = set()
                for index, child in enumerate(path.iterdir()):
                    if index >= 70000:
                        raise ValueError()
                    actual.add(child.name)
                if actual != expected:
                    raise ValueError()

        verify_visible_members()
        for directory in required.private_search_directories:
            path = root/directory
            _exact_path(path, directory=True)
            expected = {PurePosixPath(slot).name for slot,_,_ in native if str(PurePosixPath(slot).parent) == directory}
            actual = {p.name for p in path.iterdir() if p.suffix.casefold() in NATIVE_SUFFIXES}
            if actual != expected:
                raise ValueError()
            require_search_topology(tuple(entries), inventory.directories,
                                    reserved.component_names | reserved.contract_names,
                                    target_parent=Path(directory))
        for slot,size,digest in native+tuple(sources):
            entry = entries.get(slot)
            if entry is None or entry.role != 'dependency' or (entry.size_bytes,entry.sha256) != (size,digest):
                raise ValueError()
            source = _PEFile(root/slot,size,digest)
            source.close()
        verify_visible_members()
        if os.path.lexists(root/'_cpu_disabled') or os.path.lexists(root/'Library/bin'):
            raise ValueError()
        if original_cpu_loader_requirements().sha256 != required.sha256:
            raise ValueError()
        return CPULayoutObservation(required.sha256, inventory.descriptor.sha256, native, tuple(sources))
    except NativeExecutionUnsupported:
        raise
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        raise NativeExecutionUnsupported('execution_cpu_loader_layout_unsupported') from None
