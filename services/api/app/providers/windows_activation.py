"""Bounded metadata-only ActCtx ABI. No activation or native target loading.

This reader is diagnostic, not an admission policy or a host witness. Windows
DWORDs are fixed-width even when tests run outside Windows; string pointers
remain integers until their complete UTF-16 span is proved inside the buffer.
"""
import ctypes as ct
from dataclasses import dataclass
import hashlib
from pathlib import Path

from .pe_resources import classify_pe_resources
from .runtime_primitives import NativeExecutionUnsupported, _exact_path
from .windows_native import WindowsModuleReader

BUFFER_LIMIT = 65536
ASSEMBLY_LIMIT = 16
FILE_LIMIT = 128
PE_LIMIT = 64 * 1024 * 1024
CONTEXT_MODULE_LIMIT = 64
USE_ACTIVE = 4
IS_MODULE = 8
D = ct.c_uint32
P = ct.c_void_p


class _ActCtx(ct.Structure):
    _fields_ = [('size', D), ('flags', D), ('source', ct.c_wchar_p),
        ('architecture', ct.c_uint16), ('language', ct.c_uint16),
        ('assembly_directory', P), ('resource', P), ('application', P), ('module', P)]


class _Detail(ct.Structure):
    _fields_ = [(name, D) for name in ('flags', 'format', 'assemblies',
        'manifest_type', 'manifest_chars', 'configuration_type', 'configuration_chars',
        'directory_type', 'directory_chars')] + [(name, P) for name in
        ('manifest', 'configuration', 'directory')]


class _Assembly(ct.Structure):
    _fields_ = [('flags', D), ('identity_bytes', D), ('manifest_type', D),
        ('manifest_bytes', D), ('manifest_time', ct.c_int64), ('policy_type', D),
        ('policy_bytes', D), ('policy_time', ct.c_int64), ('satellite', D),
        ('manifest_major', D), ('manifest_minor', D), ('policy_major', D),
        ('policy_minor', D), ('directory_bytes', D), ('identity', P),
        ('manifest', P), ('policy', P), ('directory', P), ('files', D)]


class _File(ct.Structure):
    _fields_ = [('flags', D), ('name_bytes', D), ('path_bytes', D), ('name', P), ('path', P)]


class _Index(ct.Structure):
    _fields_ = [('assembly', D), ('file', D)]


@dataclass(frozen=True)
class ActivationFile:
    name: str
    path: str
    flags: int


@dataclass(frozen=True)
class ActivationAssembly:
    identity: str
    manifest: str
    policy: str
    directory: str
    manifest_type: int
    policy_type: int
    manifest_version: tuple[int, int]
    policy_version: tuple[int, int]
    satellite: int
    files: tuple[ActivationFile, ...]
    flags: int


@dataclass(frozen=True)
class ActivationMetadata:
    format_version: int
    root_manifest: str
    root_configuration: str
    application_directory: str
    path_types: tuple[int, int, int]
    assemblies: tuple[ActivationAssembly, ...]
    flags: int

    @property
    def dispatch_authorized(self):
        return False


@dataclass(frozen=True)
class ActivationContextSnapshot:
    effective: ActivationMetadata
    associated: tuple[tuple[str, ActivationMetadata], ...]

    @property
    def dispatch_authorized(self):
        return False


class _Buffer:
    def __init__(self, data, written, structure):
        if not ct.sizeof(structure) <= written <= ct.sizeof(data) <= BUFFER_LIMIT:
            raise ValueError()
        self.data, self.written, self.structure = data, written, structure
        self.record = structure.from_buffer(data)

    def wide(self, pointer, length):
        # Length includes neither terminator nor any padding. Never dereference
        # an OS pointer directly (c_wchar_p.value / wstring_at are unsafe here).
        if length == 0 and pointer is None:
            return ''
        start = 0 if pointer is None else pointer - ct.addressof(self.data)
        if (pointer is None or length < 0 or length % 2 or
                start < ct.sizeof(self.structure) or start % 2 or
                start + length + 2 > self.written):
            raise ValueError()
        raw = bytes(self.data[start:start + length + 2])
        if raw[-2:] != b'\0\0':
            raise ValueError()
        value = raw[:-2].decode('utf-16-le', errors='strict')
        if '\0' in value:
            raise ValueError()
        return value


class WindowsActivationReader:
    """Owned verified Kernel32 entry points; no caller-supplied ABI in production."""
    def __init__(self, system_directory: Path):
        if ct.sizeof(P) != 8:
            raise NativeExecutionUnsupported('execution_activation_architecture_unsupported')
        reader = WindowsModuleReader(system_directory)
        self._modules = reader
        self._system_directory = system_directory
        self._kernel, self._last_error = reader._kernel, ct.get_last_error
        try:
            signatures = {
                'CreateActCtxW': ((ct.POINTER(_ActCtx),), P),
                'QueryActCtxW': ((D, P, P, D, P, ct.c_size_t, ct.POINTER(ct.c_size_t)), ct.c_int),
                'ReleaseActCtx': ((P,), None),
            }
            for name, (args, result) in signatures.items():
                function = getattr(self._kernel, name)
                function.argtypes, function.restype = args, result
        except (AttributeError, OSError, ValueError):
            raise NativeExecutionUnsupported('execution_activation_api_unavailable') from None

    def _query(self, handle, information, structure, sub=None, *, flags=0):
        if flags not in (0, USE_ACTIVE, IS_MODULE):
            raise ValueError()
        required = ct.c_size_t()
        index = None if sub is None else ct.byref(sub)
        if self._kernel.QueryActCtxW(flags, handle, index, information, None, 0, ct.byref(required)):
            raise ValueError()
        if self._last_error() != 122 or not ct.sizeof(structure) <= required.value <= BUFFER_LIMIT:
            raise ValueError()
        data = ct.create_string_buffer(required.value)
        written = ct.c_size_t()
        if not self._kernel.QueryActCtxW(flags, handle, index, information,
                data, len(data), ct.byref(written)):
            raise ValueError()
        return _Buffer(data, written.value, structure)

    def _metadata(self, handle, *, flags=0, assembly_limit=ASSEMBLY_LIMIT, file_limit=FILE_LIMIT):
        if (type(assembly_limit) is not int or not 1 <= assembly_limit <= ASSEMBLY_LIMIT
                or type(file_limit) is not int or not 0 <= file_limit <= FILE_LIMIT):
            raise ValueError()
        buffer = self._query(handle, 2, _Detail, flags=flags)
        row = buffer.record
        if row.format != 1 or not 1 <= row.assemblies <= assembly_limit:
            raise ValueError()
        root = (buffer.wide(row.manifest, row.manifest_chars * 2),
            buffer.wide(row.configuration, row.configuration_chars * 2),
            buffer.wide(row.directory, row.directory_chars * 2))
        types = (row.manifest_type, row.configuration_type, row.directory_type)
        context_flags = row.flags
        result, total = [], 0
        for number in range(1, row.assemblies + 1):
            buffer = self._query(handle, 3, _Assembly, D(number), flags=flags)
            assembly = buffer.record
            total += assembly.files
            if total > file_limit:
                raise ValueError()
            identity = buffer.wide(assembly.identity, assembly.identity_bytes)
            manifest = buffer.wide(assembly.manifest, assembly.manifest_bytes)
            policy = buffer.wide(assembly.policy, assembly.policy_bytes)
            directory = buffer.wide(assembly.directory, assembly.directory_bytes)
            files = []
            for file_index in range(assembly.files):
                entry = self._query(handle, 4, _File, _Index(number, file_index), flags=flags)
                item = entry.record
                files.append(ActivationFile(entry.wide(item.name, item.name_bytes),
                    entry.wide(item.path, item.path_bytes), item.flags))
            result.append(ActivationAssembly(identity, manifest, policy, directory,
                assembly.manifest_type, assembly.policy_type,
                (assembly.manifest_major, assembly.manifest_minor),
                (assembly.policy_major, assembly.policy_minor), assembly.satellite, tuple(files), assembly.flags))
        # Flags are observations, not a supported-semantics or admission claim.
        # Eligibility must check every level, including the root assembly.
        return ActivationMetadata(1, *root, types, tuple(result), context_flags)

    def inspect_current_contexts(self):
        """Observe effective and associated contexts at actual enumerated handles.

        This is diagnostic only. Missing/empty/unqueryable contexts stop rather
        than being inferred absent from GetCurrentActCtx. Bootstrap/default
        prediction and bound file eligibility must be compared separately.
        """
        try:
            before = self._modules._snapshot()
            if not 1 <= len(before) <= CONTEXT_MODULE_LIMIT:
                raise ValueError()
            effective = self._metadata(None, flags=USE_ACTIVE)
            associated = tuple((str(path), self._metadata(handle, flags=IS_MODULE))
                for handle, path in before)
            if before != self._modules._snapshot():
                raise ValueError()
            # An effective-context change during enumeration invalidates it.
            if effective != self._metadata(None, flags=USE_ACTIVE):
                raise ValueError()
            return ActivationContextSnapshot(effective, associated)
        except (OSError, ValueError, TypeError, AttributeError, RuntimeError):
            raise NativeExecutionUnsupported('execution_activation_context_snapshot_unsupported') from None

    def inspect_private_contexts(self, private_files):
        """Fresh-child reader seam; full inventory/origin gates remain separate.

        Actual executable selects the root. Expected file hashes are comparisons,
        not module handles or permission to classify arbitrary external files.
        No OS module-associated context is recursively queried. Results remain
        diagnostic until the owned child binds them through policy2 comparison.
        """
        import sys
        from .common_controls_binding import _read_stable, _no_alias
        try:
            executable = Path(sys.executable)
            root = executable.parent
            _no_alias(root, directory=True)
            _no_alias(self._system_directory, directory=True)
            if type(private_files) is not tuple or not 1 <= len(private_files) <= 10000:
                raise ValueError()
            fixed = {}
            for name, digest in private_files:
                path = Path(name)
                if (type(name) is not str or path.is_absolute() or '\\' in name or ':' in name or
                        any(part in ('', '.', '..') for part in name.split('/')) or
                        type(digest) is not str or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest) or
                        root / path in fixed):
                    raise ValueError()
                fixed[root / path] = digest
            if executable not in fixed: raise ValueError()
            source_bytes = _read_stable(executable, limit=PE_LIMIT)
            if (hashlib.sha256(source_bytes).hexdigest() != fixed[executable] or
                    classify_pe_resources(source_bytes, role='bootstrap_exe').kind != 'requires_os_sxs_binding'):
                raise ValueError()
            before = self._modules._snapshot()
            if (not 1 <= len(before) <= CONTEXT_MODULE_LIMIT or
                    len({p for _, p in before}) != len(before) or executable not in {p for _, p in before} or
                    len({h for h, _ in before}) != len(before) or
                    any(type(h) is not int or not 0 < h <= 0xffffffffffffffff for h, _ in before)):
                raise ValueError()
            effective = self._metadata(None, flags=USE_ACTIVE)
            if effective.root_manifest != str(executable): raise ValueError()
            associated, read_files = [], [(executable, source_bytes)]
            for handle, path in before:
                if path == executable: continue  # resource1 default is not resource2 association.
                if path.parent == self._system_directory: continue  # context scope only; no origin grant.
                if path not in fixed or path.suffix.lower() not in ('.dll', '.pyd'):
                    raise ValueError()
                payload = _read_stable(path, limit=PE_LIMIT)
                if hashlib.sha256(payload).hexdigest() != fixed[path]: raise ValueError()
                kind = classify_pe_resources(payload, role='dll').kind
                read_files.append((path, payload))
                if kind == 'requires_os_sxs_binding':
                    metadata = self._metadata(handle, flags=IS_MODULE)
                    if metadata.root_manifest != str(path): raise ValueError()
                    associated.append((str(path), metadata))
            if before != self._modules._snapshot() or effective != self._metadata(None, flags=USE_ACTIVE):
                raise ValueError()
            # Associated contexts also have a like-for-like stability check.
            handles = {str(path): handle for handle, path in before}
            for path, metadata in associated:
                if self._metadata(handles[path], flags=IS_MODULE) != metadata: raise ValueError()
            for path, payload in read_files:
                if _read_stable(path, limit=PE_LIMIT) != payload: raise ValueError()
            if before != self._modules._snapshot() or effective != self._metadata(None, flags=USE_ACTIVE):
                raise ValueError()
            return ActivationContextSnapshot(effective, tuple(associated))
        except (OSError, ValueError, TypeError, AttributeError, RuntimeError):
            raise NativeExecutionUnsupported('execution_private_context_snapshot_unsupported') from None

    def inspect_cpu_pe(self, source: Path, member_name: str):
        """Exact owned CPU member, independent resource2 prediction only.

        No activation or target load. Repeated metadata is compared within the
        same role; a successful result cannot authorize module association.
        """
        from . import cpu_native_recipe as cpu
        from .pe_bounded import open_bounded_pe
        try:
            policy, recipe_hash = cpu._owned()
            member = cpu._member(policy, member_name)
            if member is None:
                raise ValueError()
            with open_bounded_pe(source, size_bytes=member['size_bytes'], sha256=member['sha256']) as view:
                resources = cpu.classify_cpu_resources(view, member_name=member_name,
                    size_bytes=member['size_bytes'], sha256=member['sha256'])
                if resources.kind != 'requires_private_root_context':
                    raise ValueError()
                request = _ActCtx(size=ct.sizeof(_ActCtx), flags=0x9,
                    source=str(source), architecture=9, language=0, resource=2)
                handle = self._kernel.CreateActCtxW(ct.byref(request))
                if not handle or handle == ct.c_void_p(-1).value:
                    raise ValueError()
                try:
                    raw = self._metadata(handle, assembly_limit=1, file_limit=0)
                    result = cpu.compare_private_root_metadata(raw, source=source,
                        source_sha256=member['sha256'], manifest_sha256=resources.manifest_sha256)
                    repeated = self._metadata(handle, assembly_limit=1, file_limit=0)
                    if raw != repeated:
                        raise ValueError()
                finally:
                    try:
                        self._kernel.ReleaseActCtx(handle)
                    except (OSError, ValueError, RuntimeError, AttributeError):
                        raise NativeExecutionUnsupported('execution_activation_release_unavailable') from None
            if cpu._owned()[1] != recipe_hash:
                raise ValueError()
            return result
        except NativeExecutionUnsupported:
            raise
        except (OSError, ValueError, TypeError, AttributeError, RuntimeError, UnicodeError):
            raise NativeExecutionUnsupported('execution_cpu_activation_metadata_unsupported') from None

    def inspect_acquired_pe(self, source: Path, slot: str):
        """Exact acquired resource2 source, existing root-only rules; no loading."""
        from . import acquired_native_recipe as acquired, cpu_native_recipe as cpu
        from .pe_bounded import open_bounded_pe
        try:
            policy,identity=acquired.parse_acquired_descriptor(acquired.owned_acquired_bytes())
            member=next((row for row in policy['members'] if row['slot']==slot),None)
            if member is None or member['resources']['kind']!='requires_private_root_context':raise ValueError()
            with open_bounded_pe(source,size_bytes=member['size_bytes'],sha256=member['sha256']) as view:
                resources=acquired.classify_acquired_resources(view,slot=slot,
                    size_bytes=member['size_bytes'],sha256=member['sha256'])
                request=_ActCtx(size=ct.sizeof(_ActCtx),flags=0x9,source=str(source),
                    architecture=9,language=0,resource=2)
                handle=self._kernel.CreateActCtxW(ct.byref(request))
                if not handle or handle==ct.c_void_p(-1).value:raise ValueError()
                try:
                    raw=self._metadata(handle,assembly_limit=1,file_limit=0)
                    result=cpu.compare_private_root_metadata(raw,source=source,
                        source_sha256=member['sha256'],manifest_sha256=resources.manifest_sha256)
                    if raw!=self._metadata(handle,assembly_limit=1,file_limit=0):raise ValueError()
                finally:
                    try:self._kernel.ReleaseActCtx(handle)
                    except (OSError,ValueError,RuntimeError,AttributeError):
                        raise NativeExecutionUnsupported('execution_activation_release_unavailable') from None
            if acquired.parse_acquired_descriptor(acquired.owned_acquired_bytes())[1]!=identity:raise ValueError()
            return result
        except NativeExecutionUnsupported:raise
        except (OSError,ValueError,TypeError,AttributeError,RuntimeError,UnicodeError):
            raise NativeExecutionUnsupported('execution_acquired_activation_metadata_unsupported') from None

    def inspect_pe(self, source: Path, expected_sha256: str, *, role='dll', language_policy='en-us'):
        """Hash/role/owned resources before and after metadata resolution only.

        The expected digest is a comparison, never an alternative source. Host
        binding/path admission and a prepared inventory remain separate gates.
        """
        handle = None
        try:
            if language_policy not in ('en-us', 'current_ui'):
                raise ValueError()
            if type(expected_sha256) is not str or len(expected_sha256) != 64:
                raise ValueError()
            _exact_path(source, directory=False)
            payload = _pe_bytes(source)
            if hashlib.sha256(payload).hexdigest() != expected_sha256:
                raise ValueError()
            if classify_pe_resources(payload, role=role).kind != 'requires_os_sxs_binding':
                raise ValueError()
            # Legacy diagnostic keeps explicit English. Policy2 prediction uses
            # the OS current-user UI default, never a requested binding language.
            # ARCHITECTURE_VALID | RESOURCE_NAME_VALID (+ legacy LANGID_VALID).
            # No SET_PROCESS_DEFAULT, private probe directory, or HMODULE flag.
            request = _ActCtx(size=ct.sizeof(_ActCtx),
                flags=0x9 if language_policy == 'current_ui' else 0xB, source=str(source),
                architecture=9, language=0 if language_policy == 'current_ui' else 1033,
                resource=1 if role == 'bootstrap_exe' else 2)
            handle = self._kernel.CreateActCtxW(ct.byref(request))
            if not handle or handle == ct.c_void_p(-1).value:
                handle = None
                raise ValueError()
            result = self._metadata(handle)
            _exact_path(source, directory=False)
            if hashlib.sha256(_pe_bytes(source)).hexdigest() != expected_sha256:
                raise ValueError()
            return result
        except (OSError, ValueError, TypeError, AttributeError, RuntimeError, UnicodeError):
            raise NativeExecutionUnsupported('execution_activation_metadata_unsupported') from None
        finally:
            if handle is not None:
                try:
                    self._kernel.ReleaseActCtx(handle)
                except (OSError, ValueError, RuntimeError, AttributeError):
                    raise NativeExecutionUnsupported('execution_activation_release_unavailable') from None


def _pe_bytes(source):
    if not 0 < source.stat().st_size <= PE_LIMIT:
        raise ValueError()
    with source.open('rb') as stream:
        payload = stream.read(PE_LIMIT + 1)
    if not 0 < len(payload) <= PE_LIMIT:
        raise ValueError()
    return payload
