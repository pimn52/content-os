"""Owned CPython 3.12 Windows entry; only sys imported before startup checks.

No provider/model import until startup state passes. This does not establish
native DLL origins/device identity; adapters remain unsupported until M3/M4.
"""
import sys

MODULE = "app.providers.omnivoice_entry"
IMPORT_ROOTS = ("Lib", "DLLs", "Lib\\site-packages")


def verify_startup(system, *, entry_name="content_os_bootstrap.py"):
    """Pure observer; no repair of an unexpected interpreter/path state."""
    try:
        if entry_name not in ("content_os_bootstrap.py", "_content_os_startup_probe.py", "_content_os_origin_probe.py"):
            raise ValueError()
        if system.platform != "win32" or tuple(system.version_info[:2]) != (3, 12):
            raise ValueError()
        if any(getattr(system.flags, key) != 1 for key in (
            "isolated", "ignore_environment", "no_site", "no_user_site", "dont_write_bytecode")):
            raise ValueError()
        executable = system.executable
        if not executable.endswith("\\python.exe"):
            raise ValueError()
        root = executable[:-len("\\python.exe")]
        # An absolute drive/UNC root is observed, never supplied by Job args.
        if not (len(root) > 3 and root[1:3] == ":\\" or root.startswith("\\\\")):
            raise ValueError()
        if any(getattr(system, name) != root for name in (
            "prefix", "base_prefix", "exec_prefix", "base_exec_prefix")):
            raise ValueError()
        if getattr(system, "_base_executable", executable) != executable:
            raise ValueError()
        if system.path != [root + "\\" + name for name in IMPORT_ROOTS]:
            raise ValueError()
        if any(name in system.modules for name in ("site", "sitecustomize", "usercustomize")):
            raise ValueError()
        if not system.argv or system.argv[0] != root + "\\" + entry_name:
            raise ValueError()
        return root
    except (AttributeError, TypeError, ValueError):
        raise SystemExit("execution_python_startup_invalid") from None


def main():
    verify_startup(sys)
    # Only the owned application entry is eligible. Input arguments cannot
    # choose an import target or inject interpreter switches after startup.
    import runpy
    sys.argv = [MODULE, *sys.argv[1:]]
    runpy.run_module(MODULE, run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main()
