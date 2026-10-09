"""Opt-in protocol 6: static audio identities only; no target imports or loads."""
import sys

VERSION = 6
INPUT_LIMIT = 16 * 1024 * 1024 + 65536
OUTPUT_LIMIT = 65536
PREFIX = 'Lib/site-packages/_content_os_host/'
TARGETS = ('Lib/site-packages/_cffi_backend.cp312-win_amd64.pyd',
           'Lib/site-packages/_soundfile_data/libsndfile_x64.dll')


def canonical(value):
    import json
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False).encode()


def read_request(stream):
    import json
    import re
    raw = stream.read(INPUT_LIMIT + 1)
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result: raise ValueError('execution_audio_protocol_invalid')
            result[key] = value
        return result
    value = json.loads(raw, object_pairs_hook=unique)
    if (not 0 < len(raw) <= INPUT_LIMIT or type(value) is not dict or
        set(value) != {'probe_version', 'nonce', 'inventory', 'preflight', 'derivation'} or
        type(value['probe_version']) is not int or value['probe_version'] != VERSION or
        type(value['nonce']) is not str or not re.fullmatch('[a-f0-9]{64}', value['nonce']) or
        canonical(value) != raw):
        raise ValueError('execution_audio_protocol_invalid')
    return value


def recheck(root, request, scan_private, plan_native_graph, failure_type):
    """Pure byte/AST/PE checks; helpers are supplied by the verified owned bundle."""
    import ast
    import hashlib
    import json
    from types import SimpleNamespace
    digest = lambda raw: hashlib.sha256(raw).hexdigest()
    inventory = request['inventory']
    inventory_id = scan_private(root, inventory)
    entries = {row['name']: row for row in inventory['files']}

    def read(slot, limit=4 * 1024**2):
        row = entries[slot]
        if row['role'] != 'dependency' or type(row['size_bytes']) is not int or not 0 < row['size_bytes'] <= limit:
            raise ValueError('execution_audio_source_identity_invalid')
        with (root / slot).open('rb') as source: raw = source.read(limit + 1)
        if len(raw) != row['size_bytes'] or digest(raw) != row['sha256']:
            raise ValueError('execution_audio_source_identity_changed')
        return raw

    acquired_raw = read(PREFIX + 'acquired_native_recipe.json', 65536)
    private_raw = read(PREFIX + 'private_native_recipe.json', 65536)
    acquired, private = json.loads(acquired_raw), json.loads(private_raw)
    cpu_raw = read(PREFIX + 'cpu_native_recipe.json', 65536)
    graph_raw = read(PREFIX + 'audio_native_graph.py', 65536)
    graph_identity = digest(canonical({'recipe': 'soundfile-audio-two-target-preflight-v2',
        'acquired_sha256': digest(acquired_raw), 'cpu_sha256': digest(cpu_raw),
        'implementation_sha256': digest(graph_raw)}))
    descriptor = request['derivation']
    binder = read(PREFIX + 'soundfile_binding.py')  # AST only, never imported.
    core = read(PREFIX + 'soundfile_handle.py')     # Bytes only, never imported.
    constants = {}
    for node in ast.parse(binder).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name in ('UPSTREAM_SHA256', 'BLOCK_SHA256', 'TRANSFORM_VERSION', 'REPLACEMENT'):
                if name in constants: raise ValueError('execution_audio_derivation_changed')
                constants[name] = ast.literal_eval(node.value)
    upstream_slot = 'Lib/site-packages/soundfile.py'
    ffi_slot = 'Lib/site-packages/_soundfile.py'
    upstream = read(upstream_slot)
    for slot in (upstream_slot, ffi_slot):
        row = next(row for row in private['sources'] if row['slot'] == slot)
        raw = read(slot)
        if (len(raw), digest(raw)) != (row['size_bytes'], row['sha256']):
            raise ValueError('execution_audio_source_identity_changed')
    blocks = [node for node in ast.parse(upstream).body if isinstance(node, ast.Try) and
        any(isinstance(child, ast.Name) and child.id == '_full_path' for child in ast.walk(node))]
    if len(blocks) != 1: raise ValueError('execution_audio_derivation_changed')
    block = blocks[0]
    if (block.lineno, block.end_lineno) != (162, 218):
        raise ValueError('execution_audio_derivation_changed')
    handler = block.handlers[0].type
    if not isinstance(handler, ast.Tuple) or [item.id for item in handler.elts] != ['OSError', 'ImportError', 'TypeError']:
        raise ValueError('execution_audio_derivation_changed')
    lines = upstream.splitlines(keepends=True)
    start, stop = sum(map(len, lines[:161])), sum(map(len, lines[:218]))
    if digest(upstream) != constants['UPSTREAM_SHA256'] or digest(upstream[start:stop]) != constants['BLOCK_SHA256']:
        raise ValueError('execution_audio_derivation_changed')
    derived = upstream[:start] + constants['REPLACEMENT'] + upstream[stop:]
    ast.parse(derived)
    observed_derivation = {
        'recipe': constants['TRANSFORM_VERSION'], 'source_kind': 'derived',
        'upstream_sha256': digest(upstream),
        'transform_sha256': digest(constants['TRANSFORM_VERSION'].encode() +
            constants['BLOCK_SHA256'].encode() + constants['REPLACEMENT']),
        'derived_sha256': digest(derived), 'binder_sha256': digest(binder),
        'handle_core_sha256': digest(core), 'private_policy_sha256': digest(private_raw),
        'dependencies': sorted((row['slot'], row['sha256']) for row in (*private['members'], *private['sources'])
            if row['slot'] in (*TARGETS, upstream_slot, ffi_slot)),
        'terms': [(row['slot'], row['sha256']) for row in private['terms']],
        'dispatch_authorized': False, 'backend_handle_semantics': 'not_verified',
    }
    if canonical(descriptor) != canonical(observed_derivation):
        raise ValueError('execution_audio_derivation_changed')
    policy = json.loads(read('Lib/site-packages/app/providers/windows_os_policy.json', 65536))
    names = frozenset(row['name'] for row in (*policy['components'], *policy['api_contracts']))
    graph_inventory = SimpleNamespace(files=tuple(SimpleNamespace(**row) for row in inventory['files']),
                                      directories=tuple(inventory['directories']))
    rows = []
    for target in TARGETS:
        recipe = next(row for row in acquired['members'] if row['slot'] == target)
        private_row = next(row for row in private['members'] if row['slot'] == target)
        entry = entries[target]
        if (entry['role'] != 'dependency' or
            (entry['size_bytes'], entry['sha256']) != (recipe['size_bytes'], recipe['sha256']) or
            (private_row['size_bytes'], private_row['sha256']) != (recipe['size_bytes'], recipe['sha256'])):
            raise ValueError('execution_audio_target_identity_changed')
        try:
            plan = plan_native_graph(root, graph_inventory, root / target, names)
            nodes = [node for node in plan.nodes if node.name == target]
            if (plan.target != target or len(nodes) != 1 or
                (nodes[0].size_bytes, nodes[0].sha256) != (recipe['size_bytes'], recipe['sha256'])):
                raise failure_type('execution_audio_target_graph_changed')
            rows.append({'target': target, 'blocker': None, 'plan': {
                'nodes': [(node.name, node.size_bytes, node.sha256, node.resources.kind,
                    node.resources.leaf_count, node.resources.manifest_sha256) for node in plan.nodes],
                'edges': plan.edges, 'required_loaded_base': plan.required_loaded_base,
                'requires_os_sxs_binding': plan.requires_os_sxs_binding,
                'requires_dynamic_closure': plan.requires_dynamic_closure}})
        except failure_type as failure:
            code = str(failure)
            import re
            if not re.fullmatch('execution_[a-z0-9_]{1,90}', code):
                raise ValueError('execution_audio_graph_invalid')
            rows.append({'target': target, 'blocker': code, 'plan': None})
    observed = {'recipe': 'soundfile-audio-two-target-preflight-v2',
        'audio_graph_policy_sha256': graph_identity,
        'acquired_descriptor_sha256': digest(acquired_raw), 'private_recipe_sha256': digest(private_raw),
        'inventory_sha256': inventory_id, 'soundfile_derivation_sha256': digest(canonical(descriptor)),
        'targets': rows, 'dispatch_authorized': False}
    if canonical(observed) != canonical(request['preflight']):
        raise ValueError('execution_audio_graph_changed')
    if scan_private(root, inventory) != inventory_id:
        raise ValueError('execution_audio_source_identity_changed')
    return {'probe_version': VERSION, 'nonce': request['nonce'], 'preflight': observed,
            'report_sha256': digest(canonical(observed)), 'load_attempts': 0, 'dispatch_authorized': False}


def main():
    # Same allowed startup destination, different opt-in payload. Guard before helpers.
    root_text = sys.executable.rsplit('\\', 1)[0]
    with open(root_text + '\\content_os_bootstrap.py', 'rb') as source: guard_bytes = source.read(65537)
    if len(guard_bytes) > 65536: return 1
    guard = {'__name__': '_owned_startup_guard'}
    exec(compile(guard_bytes, '<owned-startup-guard>', 'exec'), guard)
    guard['verify_startup'](sys, entry_name='_content_os_origin_probe.py')
    from pathlib import Path
    # scan_private is embedded from the frozen existing pre-import scanner by
    # the owned bundle builder. No helper import precedes the whole-tree scan.
    request = read_request(sys.stdin.buffer)
    root = Path(root_text)
    scan_private(root, request['inventory'])  # Whole tree before graph helper imports.
    from _content_os_host.audio_native_graph import plan_audio_native_graph
    from _content_os_host.runtime_primitives import NativeExecutionUnsupported
    report = canonical(recheck(root, request, scan_private, plan_audio_native_graph, NativeExecutionUnsupported))
    if len(report) > OUTPUT_LIMIT: return 1
    sys.stdout.buffer.write(report)
    return 0


if __name__ == '__main__':
    try: sys.exit(main())
    except Exception: sys.exit(1)  # No path/exception leakage, no retry or failure authority.
