"""Fixed protocol 7 diagnostic body; bundle prepends guard and scanner.

No application entry, loader branch or request-selected import target exists.
"""
import sys


def diagnostic(root, request_value, observed_environment, observed_cwd, observed_argv):
    from pathlib import Path
    binding = request_value['binding']
    expected_argv = [str(root/'python.exe'), '-I', '-S', '-B', str(root/ENTRY)]
    if (binding['root'] != str(root) or binding['cwd'] != str(root)
            or observed_cwd != str(root) or binding['argv'] != expected_argv
            or observed_argv != [str(root/ENTRY)]
            or observed_environment != binding['environment']):
        raise ValueError('execution_pcm_child_binding_changed')
    # Independent scan BEFORE importing any helper from the finite owned bundle.
    scan_private(root, request_value['inventory'])
    rows = {r['name']: r for r in request_value['inventory']['files']}
    for name, rule in HELPER_RULES.items():
        if rows[name]['sha256'] != rule['sha256'] or rows[name]['role'] != rule['role']:
            raise ValueError('execution_pcm_bundle_changed')
    entry_row = rows[ENTRY]
    if entry_row['role'] != 'entrypoint':
        raise ValueError('execution_pcm_bundle_changed')
    bundle = dict(HELPER_RULES, **{ENTRY: {'sha256': entry_row['sha256'], 'role': 'entrypoint'}})
    from _content_os_pcm.pcm_static_core import inspect_pcm, read_source
    from _content_os_pcm.omnivoice_audio_pcm import derive_pcm_audio
    from _content_os_pcm.rules import RULES
    if (binding['profile_sha256'] != digest(canonical(RULES))
            or binding['bundle_sha256'] != digest(canonical(bundle))):
        raise ValueError('execution_pcm_child_binding_changed')
    for name, rule in bundle.items():
        raw = read_source(root/name)
        if (digest(raw) != rule['sha256'] or rows[name]['role'] != rule['role']
                or rows[name]['sha256'] != rule['sha256']):
            raise ValueError('execution_pcm_bundle_changed')
    facts = inspect_pcm(root, request_value['inventory'], observed_environment,
        Path(binding['work']), Path(binding['windows']), RULES, derive_pcm_audio)
    scan_private(root, request_value['inventory'])
    return {'probe_version': VERSION, 'phase': PHASE, 'nonce': request_value['nonce'],
            'binding_sha256': digest(canonical(binding)), 'facts': facts,
            'load_attempts': 0, 'loading_authorized': False, 'dispatch_authorized': False}


def main():
    # verify_startup is embedded owned sys-only code, with this fixed ENTRY.
    root_text = verify_startup(sys)
    from pathlib import Path
    import os
    value = request(sys.stdin.buffer.read(INPUT_LIMIT + 1))
    # Limits are selection-bound and no greater than RuntimeInventory's caps.
    global TREE_LIMIT, FILE_LIMIT, DIRECTORY_LIMIT
    TREE_LIMIT = value['caps']['bytes']
    FILE_LIMIT = value['caps']['files']
    DIRECTORY_LIMIT = value['caps']['directories']
    report = canonical(diagnostic(Path(root_text), value, dict(os.environ), os.getcwd(), sys.argv))
    if len(report) > OUTPUT_LIMIT: return 1
    sys.stdout.buffer.write(report)
    return 0


if __name__ == '__main__':
    try: sys.exit(main())
    except Exception: sys.exit(1)
