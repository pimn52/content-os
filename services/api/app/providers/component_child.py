"""Owned protocol3 diagnostic body, imported only after root-owned tree scan.

No provider imports, plugins or approval. Only the explicit protocol5 recipe
permits a fixed native mapping/reference; older recipes never load a target.
Origin policy remains
System32/private only; unknown contexts and WinSxS loaded code still stop.
"""
from dataclasses import asdict

from .component_contexts import created_binding, compare_current, host_observation
from .runtime_primitives import require_search_topology
from .windows_native import python_origins, PythonOriginUnsupported


def observe(root, request, modules, reader_type, facts_reader, activation_type, scanner, *, owner=None, load_recipe=None):
    report = dict(probe_version=5 if load_recipe is not None else 4 if owner is not None else 3, nonce=request['nonce'], passed=False, stage='tree',
        inventory_sha256='', host_runtime=None, native_count=0, python_count=0,
        code='', unknown_module='', blocked_origins=[], effective=None, associated=[])
    if owner is not None:
        report.update(python_source_recipe='cpython312-expat-owner-v1', python_owner=None)
    if load_recipe is not None:
        report['native_load'] = load_recipe.evidence
    try:
        report['inventory_sha256'] = scanner.scan_private(root, request['inventory'])
        report['stage'] = 'host'
        facts = facts_reader()
        scanner.cpu_observation(facts)  # independent OS-root/architecture boundary
        names = scanner.load_profile(root)
        for name in names: scanner._exact_path(facts.system_directory / name, directory=False)
        require_search_topology(tuple(row['name'] for row in request['inventory']['files']),
            request['inventory']['directories'], names)
        reader = reader_type(facts.system_directory)
        def origins():
            native = reader.native_origins()
            if owner is None:
                python = python_origins(modules)
            else:
                rows = scanner.blocked_origins(root, request['inventory'], facts.system_directory,
                    names, (('native', native),))
                if rows:
                    report['blocked_origins'], report['unknown_module'] = rows, rows[0]['name']
                    raise scanner.ProbeFailure('execution_loaded_origin_unsupported')
                python, evidence = owner.observe(modules, native)
                report['python_owner'] = evidence
            report['native_count'], report['python_count'] = len(native), len(python)
            rows = scanner.blocked_origins(root, request['inventory'], facts.system_directory,
                names, (('native', native), ('python', python)))
            if rows:
                report['blocked_origins'], report['unknown_module'] = rows, rows[0]['name']
                raise scanner.ProbeFailure('execution_loaded_origin_unsupported')
            return frozenset(native)
        report['stage'] = 'baseline'
        origins()  # before any context query; cannot repair an unknown baseline
        private_files = tuple((row['name'], row['sha256']) for row in request['inventory']['files'])
        activation = activation_type(facts.system_directory)
        report['stage'] = 'contexts'
        prediction = created_binding(activation, root, private_files, facts.root)
        report['host_runtime'] = host_observation(facts, prediction)
        # Compare the canonical wire representation: dataclass tuples become
        # JSON arrays at stdin. Every field/value still participates.
        if scanner.canonical(report['host_runtime']) != scanner.canonical(request['host_runtime']):
            raise scanner.ProbeFailure('execution_host_runtime_changed')
        effective, associated = compare_current(activation, root, private_files, facts.root, prediction)
        report['effective'] = asdict(effective)
        report['associated'] = [dict(name=name, binding=asdict(binding)) for name, binding in associated]
        report['stage'] = 'search'
        # A valid resource1 effective default is not a null-context violation.
        if load_recipe is None: reader.restrict_search()
        if load_recipe is not None:
            def verify():
                if facts_reader() != facts or created_binding(activation, root, private_files, facts.root) != prediction:
                    raise scanner.ProbeFailure('execution_host_runtime_changed')
                if scanner.scan_private(root, request['inventory']) != report['inventory_sha256']:
                    raise scanner.ProbeFailure('execution_origin_probe_tree_changed')
            def contexts(): return compare_current(activation, root, private_files, facts.root, prediction)
            effective, associated = load_recipe.run(root=root, inventory=request['inventory'],
                windows_root=facts.root, prediction=prediction, activation=activation, reader=reader,
                scanner=scanner, origins=origins, contexts=contexts, verify=verify, report=report)
            report['effective'] = asdict(effective)
            report['associated'] = [dict(name=name, binding=asdict(binding)) for name, binding in associated]
        report['stage'] = 'recheck'
        origins()
        if compare_current(activation, root, private_files, facts.root, prediction) != (effective, associated):
            raise scanner.ProbeFailure('execution_component_context_changed')
        if (facts_reader() != facts or created_binding(activation, root, private_files, facts.root) != prediction):
            raise scanner.ProbeFailure('execution_host_runtime_changed')
        if scanner.scan_private(root, request['inventory']) != report['inventory_sha256']:
            raise scanner.ProbeFailure('execution_origin_probe_tree_changed')
        report['stage'], report['passed'] = 'complete', True
    except (OSError, ValueError, RuntimeError, TypeError, AttributeError, KeyError) as error:
        import re
        code = str(error)
        report['code'] = code if re.fullmatch('execution_[a-z0-9_]{1,80}', code) else 'execution_component_observation_unavailable'
        if isinstance(error, PythonOriginUnsupported):
            report['python_origin_failure'] = dict(error.diagnostic)
        if owner is not None and code == 'execution_python_owner_unsupported':
            report['python_owner_checkpoint'] = getattr(error, 'diagnostic_checkpoint', None)
        if owner is not None and hasattr(error, 'component_failure'):
            report['component_failure'] = error.component_failure
        if report['blocked_origins']:
            # A rejected baseline is evidence only while its whole-tree/OS
            # identity survives the observation. Never repair it by proceeding.
            try:
                if facts_reader() != facts:
                    raise scanner.ProbeFailure('execution_host_runtime_changed')
                if scanner.scan_private(root, request['inventory']) != report['inventory_sha256']:
                    raise scanner.ProbeFailure('execution_origin_probe_tree_changed')
            except (OSError, ValueError, RuntimeError, TypeError, AttributeError) as changed:
                value = str(changed)
                report['code'] = value if re.fullmatch('execution_[a-z0-9_]{1,80}', value) else 'execution_component_observation_unavailable'
                report['blocked_origins'], report['unknown_module'] = [], ''
    return report
