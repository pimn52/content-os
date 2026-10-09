"""Owned sys-only PCM entry guard; self-check is not parent/native authority."""
import sys

ENTRY = '_content_os_pcm_bootstrap.py'
MODULE = 'app.providers.omnivoice_entry_v3'
IMPORT_ROOTS = ('Lib', 'DLLs', 'Lib\\site-packages')
FORBIDDEN_PRELOADS = ('soundfile', '_soundfile', '_soundfile_data', 'librosa', 'soxr',
                      'torchcodec', 'app', 'torch', 'numpy', 'torchaudio', 'torio',
                      'transformers', 'omnivoice')


def verify_startup(system):
    """Observe, never repair paths, finder registrations or loaded modules."""
    try:
        if system.platform != 'win32' or tuple(system.version_info[:2]) != (3, 12):
            raise ValueError()
        if any(getattr(system.flags, key) != 1 for key in (
                'isolated', 'ignore_environment', 'no_site', 'no_user_site', 'dont_write_bytecode')):
            raise ValueError()
        executable = system.executable
        if not executable.endswith('\\python.exe'):
            raise ValueError()
        root = executable[:-len('\\python.exe')]
        if not (len(root) > 3 and root[1:3] == ':\\' or root.startswith('\\\\')):
            raise ValueError()
        if '..' in root.split('\\') or '/' in root:
            raise ValueError()
        if any(getattr(system, key) != root for key in (
                'prefix', 'base_prefix', 'exec_prefix', 'base_exec_prefix')):
            raise ValueError()
        if getattr(system, '_base_executable', executable) != executable:
            raise ValueError()
        if system.path != [root+'\\'+name for name in IMPORT_ROOTS]:
            raise ValueError()
        if not system.argv or system.argv[0] != root+'\\'+ENTRY:
            raise ValueError()
        if any(type(name) is not str or name.split('.')[0].casefold() in
                (*FORBIDDEN_PRELOADS, 'site', 'sitecustomize', 'usercustomize') for name in system.modules):
            raise ValueError()
        frozen = system.modules['_frozen_importlib']
        external = system.modules['_frozen_importlib_external']
        expected = (frozen.BuiltinImporter, frozen.FrozenImporter, external.PathFinder)
        if len(system.meta_path) != 3 or any(actual is not required for actual, required in zip(system.meta_path, expected)):
            raise ValueError()
        return root
    except (AttributeError, KeyError, TypeError, ValueError):
        raise SystemExit('omnivoice_pcm_python_startup_invalid') from None


def main():
    verify_startup(sys)
    # Fixed application target only, after the guard. The application still
    # denies native preparation and has no parent boundary transport.
    import runpy
    sys.argv = [MODULE, *sys.argv[1:]]
    runpy.run_module(MODULE, run_name='__main__', alter_sys=True)


if __name__ == '__main__':
    main()
