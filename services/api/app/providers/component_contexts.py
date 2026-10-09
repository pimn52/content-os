"""Stdlib-only policy2 context comparison; diagnostic evidence, not authority."""
from dataclasses import asdict
from pathlib import Path

from .common_controls_binding import (_compare_common_controls_v2,
    ComponentComparisonUnsupported, capture_component_metadata)
from .runtime_primitives import NativeExecutionUnsupported


def normalized_binding(binding):
    """Compare resolution facts across roles, not role-specific root metadata."""
    value = asdict(binding)
    return {key: value[key] for key in ('component_policy_version', 'component_policy_sha256',
        'manifest_contract_sha256', 'common_controls', 'resource')}


def created_binding(reader, root, private_files, windows_root):
    source = root / 'python.exe'
    fixed = dict(private_files)
    digest = fixed.get('python.exe')
    if digest is None:
        raise NativeExecutionUnsupported('execution_component_prediction_missing')
    metadata = reader.inspect_pe(source, digest, role='bootstrap_exe', language_policy='current_ui')
    return _compare_common_controls_v2(metadata, source=source,
        expected_source_sha256=digest, application_directory=root, windows_root=windows_root,
        source_role='bootstrap_exe', query_role='created')


def compare_current(reader, root, private_files, windows_root, prediction):
    """Actual query selection remains in reader; no expected-driven file choice."""
    snapshot = reader.inspect_private_contexts(private_files)
    # Reader already validates unique, non-alias private paths. Use the same
    # platform Path equality here; OS-loaded spelling need not equal inventory
    # string casing on Windows. Retain the inventory name in the output only.
    fixed = {Path(name): (name, digest) for name, digest in private_files}
    def capture(error, metadata, relative, role, checkpoint):
        # Failure evidence only; never an alternate binding or recovery action.
        import json
        captured = capture_component_metadata(metadata)
        if len(json.dumps(captured, ensure_ascii=True).encode('ascii')) > 48 * 1024:
            raise NativeExecutionUnsupported('execution_diagnostic_capture_limit') from None
        error.component_failure = dict(role=role, source=relative,
            checkpoint=checkpoint, metadata=captured)
        return error
    def compare(metadata, relative, role):
        try:
            source = root / relative
            # The independently inventoried module owns its resource2 context;
            # its context directory is not the executable's resource1 directory.
            application = source.parent if role == 'associated' else root
            return _compare_common_controls_v2(metadata, source=root / relative,
                expected_source_sha256=fixed[Path(relative)][1], application_directory=application,
                windows_root=windows_root, source_role='bootstrap_exe' if role == 'effective' else 'dll',
                query_role=role)
        except ComponentComparisonUnsupported as error:
            raise capture(error, metadata, relative, role, error.diagnostic_checkpoint)
    effective = compare(snapshot.effective, 'python.exe', 'effective')
    expected = normalized_binding(prediction)
    if normalized_binding(effective) != expected:
        raise capture(NativeExecutionUnsupported('execution_component_context_mismatch'),
            snapshot.effective, 'python.exe', 'effective', 'binding_mismatch')
    associated = []
    for name, metadata in snapshot.associated:
        source = Path(name)
        try:
            relative = source.relative_to(root).as_posix()
            binding = compare(metadata, relative, 'associated')
        except (ValueError, KeyError):
            raise NativeExecutionUnsupported('execution_component_context_mismatch') from None
        if normalized_binding(binding) != expected:
            raise capture(NativeExecutionUnsupported('execution_component_context_mismatch'),
                metadata, relative, 'associated', 'binding_mismatch')
        associated.append((fixed[Path(relative)][0], binding))
    return effective, tuple(associated)


def host_observation(facts, prediction):
    if (facts.native_machine != 0x8664 or facts.process_machine != 0
            or facts.system_directory != facts.root / 'System32'):
        raise NativeExecutionUnsupported('execution_host_architecture_unsupported')
    return dict(observation_recipe='windows-platform-ubr-component-bindings', observation_version=3,
        trust_recipe='windows-os-component-bindings-v3', os_family='windows', architecture='amd64',
        build=facts.build, revision=facts.revision, device={'kind': 'cpu'}, component_policy_version=2,
        component_policy_sha256=prediction.component_policy_sha256, binding=asdict(prediction))
