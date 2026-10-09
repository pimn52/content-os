"""Pure owned PCM rules. No application, vendor or native loader imports.

Rules are supplied by the owned bundle builder, never by a wire request.
The caller must independently scan the whole tree before and after inspection.
"""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False).encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def read_source(path):
    if not path.is_absolute() or path != path.resolve(strict=True):
        raise ValueError('omnivoice_pcm_import_source_changed')
    before = path.lstat()
    def identity(value):
        return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
    if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= 4*1024**2:
        raise ValueError('omnivoice_pcm_import_source_changed')
    with path.open('rb') as stream:
        opened = os.fstat(stream.fileno())
        raw = stream.read(4*1024**2 + 1)
        stream.seek(0)
        if (identity(opened)[:4] != identity(before)[:4] or len(raw) != before.st_size
                or stream.read(4*1024**2 + 1) != raw
                or identity(os.fstat(stream.fileno())) != identity(opened)):
            raise ValueError('omnivoice_pcm_import_source_changed')
    if identity(path.lstat()) != identity(before):
        raise ValueError('omnivoice_pcm_import_source_changed')
    return raw


def environment_values(root, work_directory, windows_root):
    disabled = root / '_cpu_disabled'
    return (('SystemRoot', str(windows_root)), ('WINDIR', str(windows_root)),
        ('TEMP', str(work_directory)), ('TMP', str(work_directory)),
        ('PATH', str(disabled / 'path')), ('APPDATA', str(disabled / 'appdata')),
        ('PYTHONUSERBASE', str(disabled / 'userbase')),
        ('ProgramFiles', str(disabled / 'program-files')),
        ('NVTOOLSEXT_PATH', str(disabled / 'nvtools')),
        ('TORCH_DEVICE_BACKEND_AUTOLOAD', '0'), ('TORIO_USE_FFMPEG_VERSION', ''),
        ('TORCHAUDIO_USE_SOX', '0'), ('HF_HUB_OFFLINE', '1'), ('TRANSFORMERS_OFFLINE', '1'),
        ('HF_HOME', str(work_directory / 'hf')), ('TORCH_HOME', str(work_directory / 'torch')))


def optional_name(name, absent_roots):
    lower = name.casefold()
    for root in (*absent_roots, '_soundfile', '_soundfile_data'):
        if (lower == root or lower in (root+'.py', root+'.pyc', root+'.pyo', root+'.dist-info', root+'.egg-info')
                or lower.startswith(root+'.') and lower.endswith(('.pyd', '.so', '.dll'))
                or lower.startswith(root+'-') and lower.endswith(('.dist-info', '.egg-info'))):
            return True
    return False


def inspect_pcm(root, inventory, environment, work, windows, rules, derive):
    """Recompute actual static facts using fixed owned rules, not expected facts."""
    rows = {row['name']: row for row in inventory['files']}
    if rows.get('python.exe', {}).get('role') != 'interpreter':
        raise ValueError('omnivoice_pcm_import_source_changed')
    names = (*rows, *inventory['directories'])
    roots = rules['import_roots']
    if not set(roots) <= set(inventory['directories']):
        raise ValueError('omnivoice_pcm_import_roots_invalid')
    for name in names:
        path = PurePosixPath(name)
        lower = path.name.casefold()
        if (lower.endswith(('.zip', '.egg', '.whl', '.pyc', '.pyo', '.pth'))
                or lower in ('__pycache__', 'pyvenv.cfg')
                or lower.endswith('._pth') and name not in ('python._pth', 'python312._pth')):
            raise ValueError('omnivoice_pcm_startup_layout_unsupported')
        if str(path.parent) in roots:
            if optional_name(path.name, rules['absent_roots']):
                raise ValueError('omnivoice_pcm_optional_package_visible')
            if lower in ('sitecustomize', 'usercustomize') or lower.startswith(('sitecustomize.', 'usercustomize.')):
                raise ValueError('omnivoice_pcm_startup_layout_unsupported')
        if any(lower_name == p or lower_name.startswith((p+'.', p+'/'))
               for lower_name in (name.casefold(),)
               for p in ('lib/site-packages/torio/lib/_torio_ffmpeg',
                         'lib/site-packages/torio/lib/libtorio_ffmpeg')):
            raise ValueError('execution_cpu_ffmpeg_exclusion_changed')
    def read(name, digest=None, role='dependency'):
        raw = read_source(root/name)
        row = rows[name]
        if (row['role'] != role or row['size_bytes'] != len(raw)
                or row['sha256'] != sha(raw) or digest is not None and sha(raw) != digest):
            raise ValueError('omnivoice_pcm_import_source_changed')
        return raw
    for name, rule in rules['files'].items():
        read(name, rule['sha256'], rule['role'])
    derived = derive(read(rules['upstream']))
    if (read(rules['derived']) != derived.payload or read(rules['descriptor']) != derived.descriptor):
        raise ValueError('omnivoice_pcm_recipe_source_changed')
    for path in (root, work, windows, windows/'System32'):
        if not path.is_absolute() or path != path.resolve(strict=True) or not path.is_dir():
            raise ValueError('omnivoice_pcm_environment_changed')
    if (work.is_relative_to(root) or windows.is_relative_to(root)
            or root.is_relative_to(work) or root.is_relative_to(windows)
            or os.path.lexists(root/'_cpu_disabled')
            or environment != dict(environment_values(root, work, windows))):
        raise ValueError('omnivoice_pcm_environment_changed')
    return {'inventory_sha256': sha(canonical(inventory)),
            'policy_sha256': rules['policy_sha256'],
            'environment_sha256': sha(canonical({'recipe_sha256': rules['cpu_sha256'],
                                               'environment': environment})),
            'derivation_sha256': derived.identity_sha256,
            'pending': rules['pending'], 'loading_authorized': False, 'dispatch_authorized': False}
