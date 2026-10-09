"""Protocol 7 canonical wire validation; stdlib only, no runtime authority."""
import hashlib
import json
import re
import unicodedata

VERSION = 7
PHASE = 'pcm-static-diagnostic'
ENTRY = '_content_os_pcm_static.py'
PREFIX = 'Lib/site-packages/_content_os_pcm/'
MANIFEST_LIMIT = 16*1024**2
INPUT_LIMIT = MANIFEST_LIMIT + 65536
OUTPUT_LIMIT = 65536
FILE_LIMIT = 50000
DIRECTORY_LIMIT = 20000
TREE_LIMIT = 64*1024**3


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False).encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def decode(raw, limit):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value: raise ValueError('execution_pcm_protocol_invalid')
            value[key] = item
        return value
    if type(raw) is not bytes or not 0 < len(raw) <= limit:
        raise ValueError('execution_pcm_protocol_invalid')
    value = json.loads(raw, object_pairs_hook=unique)
    if canonical(value) != raw:
        raise ValueError('execution_pcm_protocol_invalid')
    return value


def runtime_name(value):
    if (type(value) is not str or not value or len(value) > 500
            or value != unicodedata.normalize('NFC', value)
            or any(c in '\\:<>"|?*' or unicodedata.category(c).startswith('C') for c in value)):
        raise ValueError('execution_pcm_inventory_invalid')
    reserved = {'CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$'} | {
        p+d for p in ('COM', 'LPT') for d in '123456789¹²³'}
    parts = value.split('/')
    if len(parts) > 32 or any(not p or p in ('.', '..') or len(p) > 255
            or p != p.strip() or p.endswith('.')
            or p.split('.', 1)[0].rstrip(' .').upper() in reserved for p in parts):
        raise ValueError('execution_pcm_inventory_invalid')
    return value


def validate_inventory(value):
    if (type(value) is not dict or set(value) != {'inventory_version', 'files', 'directories'}
            or type(value['inventory_version']) is not int or value['inventory_version'] != 1
            or type(value['files']) is not list or not 3 <= len(value['files']) <= FILE_LIMIT
            or type(value['directories']) is not list or len(value['directories']) > DIRECTORY_LIMIT):
        raise ValueError('execution_pcm_inventory_invalid')
    names = [runtime_name(n) for n in value['directories']]
    roles, total = [], 0
    for row in value['files']:
        if (type(row) is not dict or set(row) != {'name', 'role', 'size_bytes', 'sha256'}
                or row['role'] not in ('interpreter', 'entrypoint', 'dependency')
                or type(row['size_bytes']) is not int or not 0 <= row['size_bytes'] <= TREE_LIMIT
                or type(row['sha256']) is not str or not re.fullmatch('[a-f0-9]{64}', row['sha256'])
                or row['size_bytes'] == 0 and (row['role'] != 'dependency' or row['sha256'] != digest(b''))):
            raise ValueError('execution_pcm_inventory_invalid')
        names.append(runtime_name(row['name']))
        roles.append(row['role'])
        total += row['size_bytes']
    dirs = set(value['directories'])
    if (len(set(n.casefold() for n in names)) != len(names) or roles.count('interpreter') != 1
            or not {'entrypoint', 'dependency'} <= set(roles) or not 0 < total <= TREE_LIMIT):
        raise ValueError('execution_pcm_inventory_invalid')
    for name in names:
        parts = name.split('/')
        if any('/'.join(parts[:i]) not in dirs for i in range(1, len(parts))):
            raise ValueError('execution_pcm_inventory_invalid')
    if (value['directories'] != sorted(value['directories'])
            or value['files'] != sorted(value['files'], key=lambda r: r['name'])
            or len(canonical(value)) > MANIFEST_LIMIT):
        raise ValueError('execution_pcm_inventory_invalid')
    return {'files': len(value['files']), 'directories': len(value['directories']), 'bytes': total}


def request(raw):
    value = decode(raw, INPUT_LIMIT)
    if (type(value) is not dict or set(value) != {'probe_version', 'phase', 'nonce', 'inventory', 'binding', 'caps'}
            or type(value['probe_version']) is not int or value['probe_version'] != VERSION
            or value['phase'] != PHASE or type(value['nonce']) is not str
            or not re.fullmatch('[a-f0-9]{64}', value['nonce'])):
        raise ValueError('execution_pcm_protocol_invalid')
    caps = validate_inventory(value['inventory'])
    if (type(value['caps']) is not dict or any(type(n) is not int for n in value['caps'].values())
            or value['caps'] != caps):
        raise ValueError('execution_pcm_protocol_invalid')
    binding = value['binding']
    if (type(binding) is not dict or set(binding) != {'root', 'work', 'windows', 'argv', 'cwd',
            'environment', 'bundle_sha256', 'profile_sha256', 'inventory_sha256', 'host_sha256'}
            or any(type(binding[k]) is not str or not binding[k] for k in ('root', 'work', 'windows', 'cwd'))
            or type(binding['argv']) is not list or any(type(v) is not str for v in binding['argv'])
            or type(binding['environment']) is not dict
            or any(type(k) is not str or type(v) is not str for k, v in binding['environment'].items())
            or any(type(binding[k]) is not str or not re.fullmatch('[a-f0-9]{64}', binding[k])
                   for k in ('bundle_sha256', 'profile_sha256', 'inventory_sha256', 'host_sha256'))
            or binding['inventory_sha256'] != digest(canonical(value['inventory']))):
        raise ValueError('execution_pcm_protocol_invalid')
    return value
