"""Exact metadata/file comparison for the single owned OS assembly.

Offline-testable internal seam. Results do not authorize loading, broaden the
old System32 policy, or replace default/effective/associated context evidence.
"""
from dataclasses import dataclass
import hashlib
from pathlib import Path
import re

from .runtime_primitives import NativeExecutionUnsupported, _exact_path
from .windows_activation import ActivationMetadata
from .component_manifest import ComponentIdentity
from .pe_data import ResourceDataEvidence

NAME = 'Microsoft.Windows.Common-Controls'
TOKEN = '6595b64144ccf1df'
FILE_LIMIT = 64 * 1024 * 1024
IDENTITY_LIMIT = 2048


@dataclass(frozen=True)
class ComponentFile:
    windows_relative_path: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class CommonControlsBinding:
    name: str
    token: str
    architecture: str
    version: str
    language: str
    directory: str
    code: ComponentFile
    manifest: ComponentFile
    policy: ComponentFile | None
    metadata_versions: tuple[int, int, int, int]
    path_types: tuple[int, int, int, int, int]

    @property
    def dispatch_authorized(self):
        return False


def _identity(value):
    # Windows textually encoded identities are a name followed by quoted
    # key/value attributes. Narrow support; unknown formatting stops, no XML.
    if type(value) is not str or not 0 < len(value) <= IDENTITY_LIMIT:
        raise ValueError()
    parts = value.split(',')
    if parts[0] != NAME:
        raise ValueError()
    fields = {}
    for part in parts[1:]:
        match = re.fullmatch(r'\s*(processorArchitecture|publicKeyToken|type|version|language)="([A-Za-z0-9.\-]+)"', part)
        if not match or match[1] in fields:
            raise ValueError()
        fields[match[1]] = match[2]
    if set(fields) != {'processorArchitecture', 'publicKeyToken', 'type', 'version', 'language'}:
        raise ValueError()
    if fields['processorArchitecture'] != 'amd64' or fields['publicKeyToken'] != TOKEN or fields['type'] != 'win32':
        raise ValueError()
    version = fields['version']
    if not re.fullmatch(r'6\.(?:0|[1-9][0-9]{0,4})\.(?:0|[1-9][0-9]{0,4})\.(?:0|[1-9][0-9]{0,4})', version):
        raise ValueError()
    if any(int(item) > 65535 for item in version.split('.')) or version == '6.0.0.0':
        raise ValueError()
    language = fields['language']
    if not re.fullmatch(r'(?:none|neutral|[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*)', language):
        raise ValueError()
    return version, language


def _component_file(path, root):
    _exact_path(path, directory=False)
    _exact_path(root, directory=True)
    relative = path.relative_to(root)
    if len(relative.parts) < 3 or relative.parts[0] != 'WinSxS':
        raise ValueError()
    if not 0 < path.stat().st_size <= FILE_LIMIT:
        raise ValueError()
    with path.open('rb') as stream:
        payload = stream.read(FILE_LIMIT + 1)
    if not 0 < len(payload) <= FILE_LIMIT:
        raise ValueError()
    _exact_path(path, directory=False)
    if path.stat().st_size != len(payload):
        raise ValueError()
    return ComponentFile(relative.as_posix(), len(payload), hashlib.sha256(payload).hexdigest())


def _compare_common_controls(metadata: ActivationMetadata, *, source: Path,
        application_directory: Path, windows_root: Path):
    """Internal comparison; root is independently OS-selected by the caller.

    No public origin/path claims feed host eligibility. This function is not yet
    wired into prepared execution; snapshots from ABI fakes remain test evidence.
    """
    try:
        _exact_path(source, directory=False)
        _exact_path(application_directory, directory=True)
        _exact_path(windows_root, directory=True)
        if (type(metadata.flags) is not int or metadata.flags != 0 or metadata.format_version != 1 or metadata.root_manifest != str(source)
                or metadata.root_configuration or
                metadata.application_directory not in ('', str(application_directory))
                or len(metadata.assemblies) != 2):
            raise ValueError()
        root, assembly = metadata.assemblies
        # Decoder completeness is not flags interpretation. All nonzero flags
        # remain unsupported until a separately reviewed rule establishes them.
        if any(type(item.flags) is not int or item.flags != 0 or
                any(type(file.flags) is not int or file.flags != 0 for file in item.files)
                for item in metadata.assemblies):
            raise ValueError()
        if (root.identity or root.manifest != str(source) or root.policy or root.directory
                or root.files or root.satellite or assembly.satellite):
            raise ValueError()
        version, language = _identity(assembly.identity)
        if len(assembly.files) != 1 or assembly.files[0].name != 'comctl32.dll':
            raise ValueError()
        code_path = Path(assembly.files[0].path)
        code = _component_file(code_path, windows_root)
        directory = code_path.parent
        relative_directory = directory.relative_to(windows_root)
        # Exact canonical component path, not a WinSxS-wide code allowlist.
        if (len(relative_directory.parts) != 2 or code_path.name != 'comctl32.dll'
                or not re.fullmatch(r'amd64_microsoft\.windows\.common-controls_' + TOKEN +
                    '_' + re.escape(version) + r'_[a-zA-Z0-9\-]+_[0-9a-f]{16}', directory.name)
                or assembly.directory not in (str(directory), directory.name)):
            raise ValueError()
        if directory.name.split('_')[4] != language:
            raise ValueError()
        manifest_path = windows_root / 'WinSxS' / 'Manifests' / (directory.name + '.manifest')
        if assembly.manifest != str(manifest_path):
            raise ValueError()
        manifest = _component_file(manifest_path, windows_root)
        policy = None
        if assembly.policy:
            policy_path = Path(assembly.policy)
            relative = policy_path.relative_to(windows_root)
            if (relative.parts[:2] != ('WinSxS', 'Manifests') or len(relative.parts) != 3
                    or not policy_path.name.startswith('amd64_policy.6.0.microsoft.windows.common-controls_' + TOKEN + '_')
                    or not policy_path.name.endswith('.manifest')):
                raise ValueError()
            policy = _component_file(policy_path, windows_root)
        return CommonControlsBinding(NAME, TOKEN, 'amd64', version, language,
            relative_directory.as_posix(), code, manifest, policy,
            (*assembly.manifest_version, *assembly.policy_version),
            (*metadata.path_types, assembly.manifest_type, assembly.policy_type))
    except (OSError, ValueError, TypeError, AttributeError, RuntimeError):
        raise NativeExecutionUnsupported('execution_common_controls_binding_unsupported') from None


@dataclass(frozen=True)
class ComponentAssemblyBindingV2:
    identity: ComponentIdentity
    directory: str
    file: ComponentFile
    manifest: ComponentFile
    flags: int
    metadata_versions: tuple[int, int, int, int]
    path_types: tuple[int, int]
    data: ResourceDataEvidence | None


@dataclass(frozen=True)
class CommonControlsBindingV2:
    component_policy_version: int
    component_policy_sha256: str
    manifest_contract_sha256: str
    source_sha256: str
    source_role: str
    query_role: str
    root_flags: int
    context_flags: int
    root_path_types: tuple[int, int, int]
    common_controls: ComponentAssemblyBindingV2
    resource: ComponentAssemblyBindingV2 | None

    @property
    def dispatch_authorized(self): return False


def _uint32(value):
    return type(value) is int and 0 <= value <= 0xffffffff


COMPARISON_CHECKPOINTS = frozenset(('arguments', 'windows_root', 'application_root',
    'source_read', 'source_identity', 'context_format', 'context_flags', 'root_manifest',
    'root_configuration', 'application_directory', 'path_types', 'roster', 'root_assembly',
    'assembly_metadata', 'encoded_identity', 'duplicate_identity', 'component_directory',
    'directory_containment', 'directory_identity', 'manifest_path', 'manifest_read',
    'manifest_definition', 'definition_identity', 'component_read', 'reported_files',
    'resource_data', 'code_layout', 'resource_dependency', 'owned_policy', 'file_recheck',
    'policy_recheck'))


class ComponentComparisonUnsupported(NativeExecutionUnsupported):
    """Fixed local diagnostic checkpoint; same public error/acceptance semantics.

    No raw metadata, paths, values or caller-selected policy enter this error.
    A checkpoint locates a failed check, not its cause or an override permission.
    """
    def __init__(self, checkpoint):
        if type(checkpoint) is not str or checkpoint not in COMPARISON_CHECKPOINTS:
            raise ValueError('component_checkpoint_invalid')
        self.diagnostic_checkpoint = checkpoint
        super().__init__('execution_common_controls_policy2_unsupported')


def capture_component_metadata(metadata):
    """Detached bounded raw record for a local evaluator, never admission input.

    Decoder strings remain observations. This helper does not query, compare,
    redact/infer missing fields or serialize evidence into production payloads.
    """
    from dataclasses import asdict
    import json
    try:
        if type(metadata) is not ActivationMetadata: raise ValueError()
        value = asdict(metadata)
        payload = json.dumps(value, sort_keys=True, separators=(',', ':'),
            ensure_ascii=False, allow_nan=False).encode('utf-8')
        if len(payload) > 1024 * 1024: raise ValueError()
        return value
    except (TypeError, ValueError, RecursionError):
        raise NativeExecutionUnsupported('execution_diagnostic_capture_limit') from None


def _no_alias(path, *, directory):
    _exact_path(path, directory=directory)
    # resolve() alone can preserve some Windows junction/reparse aliases.
    for item in (path, *path.parents):
        if item.is_symlink() or getattr(item.lstat(), 'st_file_attributes', 0) & 0x400:
            raise ValueError()


def _read_stable(path, *, limit):
    _no_alias(path, directory=False)
    before = path.stat()
    if not 0 < before.st_size <= limit: raise ValueError()
    with path.open('rb') as stream:
        payload = stream.read(limit + 1)
    after = path.stat()
    if (len(payload) != before.st_size or len(payload) > limit or
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) !=
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
        raise ValueError()
    _no_alias(path, directory=False)
    return payload


def _compare_common_controls_v2(metadata: ActivationMetadata, *, source: Path,
        expected_source_sha256: str, application_directory: Path, windows_root: Path,
        source_role='dll', query_role='created'):
    """Independent static comparison; callers must independently obtain OS root.

    Old policy1 is intentionally unchanged. This internal result is inert and
    has no ExecutionSpecification, host-admission or loader integration yet.
    """
    from .component_manifest import MAIN, RESOURCE, encoded_identity, inspect_component_manifest
    from .native_manifest import CPYTHON_COMMON_CONTROLS
    from .pe_resources import classify_pe_resources
    from .pe_imports import parse_pe_imports
    from .pe_data import inspect_resource_data
    from .windows_component_policy import owned_component_policy_digest
    checkpoint = 'arguments'
    try:
        if (source_role not in ('dll', 'bootstrap_exe') or query_role not in ('created', 'effective', 'associated') or
                type(expected_source_sha256) is not str or not re.fullmatch('[0-9a-f]{64}', expected_source_sha256)):
            raise ValueError()
        checkpoint = 'windows_root'
        _no_alias(windows_root, directory=True)
        checkpoint = 'application_root'
        _no_alias(application_directory, directory=True)
        checkpoint = 'source_read'
        source_bytes = _read_stable(source, limit=FILE_LIMIT)
        checkpoint = 'source_identity'
        if (hashlib.sha256(source_bytes).hexdigest() != expected_source_sha256 or
                classify_pe_resources(source_bytes, role=source_role).kind != 'requires_os_sxs_binding'):
            raise ValueError()
        checkpoint = 'context_format'
        if type(metadata.format_version) is not int or metadata.format_version != 1: raise ValueError()
        checkpoint = 'context_flags'
        if type(metadata.flags) is not int or metadata.flags != 0: raise ValueError()
        checkpoint = 'root_manifest'
        if metadata.root_manifest != str(source): raise ValueError()
        checkpoint = 'root_configuration'
        if metadata.root_configuration: raise ValueError()
        checkpoint = 'application_directory'
        # Windows reports a directory with a trailing separator. Compare the
        # same absolute canonical path, not its spelling; never resolve an
        # untrusted alternative into the expected root to make it match.
        if type(metadata.application_directory) is not str: raise ValueError()
        observed_application = Path(metadata.application_directory)
        if not observed_application.is_absolute() or observed_application != application_directory:
            raise ValueError()
        _no_alias(observed_application, directory=True)
        checkpoint = 'path_types'
        if metadata.path_types != (2, 1, 2) or not all(_uint32(x) for x in metadata.path_types): raise ValueError()
        checkpoint = 'roster'
        if len(metadata.assemblies) not in (2, 3): raise ValueError()
        root, *assemblies = metadata.assemblies
        checkpoint = 'root_assembly'
        if (not _uint32(root.flags) or root.identity or root.manifest != str(source) or
                root.policy or root.directory or root.files or root.satellite or
                (root.manifest_type, root.policy_type) != (2, 1) or
                root.manifest_version != (1, 0) or root.policy_version != (0, 0) or
                not all(_uint32(x) for x in (root.satellite, root.manifest_type, root.policy_type,
                                             *root.manifest_version, *root.policy_version))):
            raise ValueError()
        pending, definitions, by_name = [], {}, {}
        for assembly in assemblies:
            checkpoint = 'assembly_metadata'
            if (type(assembly.flags) is not int or assembly.flags != 0 or assembly.policy or
                    type(assembly.satellite) is not int or assembly.satellite != 0 or
                    (assembly.manifest_type, assembly.policy_type) != (2, 1) or
                    assembly.manifest_version != (1, 0) or assembly.policy_version != (0, 0) or
                    not all(_uint32(x) for x in (assembly.manifest_type, assembly.policy_type,
                                                 *assembly.manifest_version, *assembly.policy_version))):
                raise ValueError()
            checkpoint = 'encoded_identity'
            identity = encoded_identity(assembly.identity)
            checkpoint = 'duplicate_identity'
            if identity.name in by_name: raise ValueError()
            checkpoint = 'component_directory'
            reported = Path(assembly.directory)
            directory = reported if reported.is_absolute() else windows_root / 'WinSxS' / reported
            _no_alias(directory, directory=True)
            checkpoint = 'directory_containment'
            relative = directory.relative_to(windows_root)
            if len(relative.parts) != 2 or relative.parts[0] != 'WinSxS': raise ValueError()
            # Check a reported name, never fabricate a version directory. The
            # OS shortened Resources name is explicit; no wildcard WinSxS trust.
            checkpoint = 'directory_identity'
            parts = directory.name.split('_')
            names = ('microsoft.windows.common-controls',) if identity.name == MAIN else (
                'microsoft.windows.common-controls.resources', 'microsoft.windows.c..-controls.resources')
            if (len(parts) != 6 or parts[0] != 'amd64' or parts[1] not in names or
                    parts[2] != identity.token or parts[3] != identity.version or
                    parts[4] != (identity.language if identity.encoded_language else 'none') or not re.fullmatch('[0-9a-f]{16}', parts[5])):
                raise ValueError()
            checkpoint = 'manifest_path'
            manifest_path = Path(assembly.manifest)
            expected_manifest = windows_root / 'WinSxS' / 'Manifests' / (directory.name + '.manifest')
            if manifest_path != expected_manifest: raise ValueError()
            checkpoint = 'manifest_read'
            manifest_bytes = _read_stable(manifest_path, limit=65536)
            checkpoint = 'manifest_definition'
            definition = inspect_component_manifest(manifest_bytes)
            checkpoint = 'definition_identity'
            if definition.identity != identity: raise ValueError()
            checkpoint = 'component_read'
            file_path = directory / definition.file_name
            file_bytes = _read_stable(file_path, limit=FILE_LIMIT)
            checkpoint = 'reported_files'
            if assembly.files:
                if (len(assembly.files) != 1 or type(assembly.files[0].flags) is not int or
                        assembly.files[0].flags != 0 or assembly.files[0].name != definition.file_name or
                        Path(assembly.files[0].path) != file_path):
                    raise ValueError()
            checkpoint = 'resource_data'
            data = inspect_resource_data(file_bytes) if identity.name == RESOURCE else None
            checkpoint = 'code_layout'
            if identity.name == MAIN:
                # Only the code architecture/layout is checked here; OS internal
                # dependencies stay inside D029's substrate, not a private graph.
                parse_pe_imports(file_bytes)
                import struct
                pe = struct.unpack_from('<I', file_bytes, 60)[0]
                if not struct.unpack_from('<H', file_bytes, pe + 22)[0] & 0x2000:
                    raise ValueError()
            def snapshot(path, payload):
                return ComponentFile(path.relative_to(windows_root).as_posix(), len(payload),
                                     hashlib.sha256(payload).hexdigest())
            result = ComponentAssemblyBindingV2(identity, relative.as_posix(), snapshot(file_path, file_bytes),
                snapshot(expected_manifest, manifest_bytes), assembly.flags,
                (*assembly.manifest_version, *assembly.policy_version),
                (assembly.manifest_type, assembly.policy_type), data)
            by_name[identity.name], definitions[identity.name] = result, definition
            pending.extend(((manifest_path, manifest_bytes), (file_path, file_bytes)))
        checkpoint = 'resource_dependency'
        if MAIN not in by_name or (RESOURCE in by_name and not definitions[MAIN].optional_resource):
            raise ValueError()
        checkpoint = 'owned_policy'
        policy_digest = owned_component_policy_digest()
        # Freeze only stable actual bytes. A caller-supplied expected digest does
        # not substitute for any source/component read.
        checkpoint = 'file_recheck'
        for path, payload in ((source, source_bytes), *pending):
            if _read_stable(path, limit=FILE_LIMIT) != payload: raise ValueError()
        checkpoint = 'policy_recheck'
        if owned_component_policy_digest() != policy_digest: raise ValueError()
        return CommonControlsBindingV2(2, policy_digest, hashlib.sha256(CPYTHON_COMMON_CONTROLS).hexdigest(),
            expected_source_sha256, source_role, query_role, root.flags, metadata.flags, metadata.path_types,
            by_name[MAIN], by_name.get(RESOURCE))
    except (OSError, ValueError, TypeError, AttributeError, RuntimeError):
        raise ComponentComparisonUnsupported(checkpoint) from None
