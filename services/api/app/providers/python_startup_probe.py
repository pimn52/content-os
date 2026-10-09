"""Owned bounded stdlib witness, never imports providers/models or third parties."""
import sys


def verify_module_origins(modules, root, import_roots):
    import pathlib
    observed = []
    for name, module in tuple(modules.items()):
        path = getattr(module, "__file__", None)
        if path is not None:
            if ".." in pathlib.PureWindowsPath(path).parts:
                raise SystemExit("execution_python_probe_origin_invalid")
            # Only this exact owned main script may live at the executable
            # root. This is NOT a generic root/system-path exemption.
            owned_main = name == "__main__" and path == root + "\\_content_os_startup_probe.py"
            if not owned_main and not any(path.startswith(prefix + "\\") for prefix in import_roots):
                raise SystemExit("execution_python_probe_origin_invalid")
            observed.append(str(pathlib.PureWindowsPath(path).relative_to(root)))
    return sorted(observed)


def main():
    # Load only the same copied owned sys-only guard; no temporary sys.path or
    # argv edits to make a failed startup appear fixed.
    root = sys.executable.rsplit("\\", 1)[0]
    with open(root + "\\content_os_bootstrap.py", "rb") as source:
        guard_bytes = source.read(64 * 1024 + 1)
    if len(guard_bytes) > 64 * 1024:
        raise SystemExit("execution_python_probe_invalid")
    guard = {"__name__": "_owned_startup_guard"}
    exec(compile(guard_bytes, "<owned-startup-guard>", "exec"), guard)
    guard["verify_startup"](sys, entry_name="_content_os_startup_probe.py")
    import json
    import hashlib
    import pathlib
    import collections
    # Verify stdlib source/extension origins after these bounded operations.
    # This observes loaded stdlib modules, NOT complete native DLL closure.
    digest = hashlib.sha256(b"content-os-startup-witness").hexdigest()
    counts = collections.Counter(("a", "a", "b"))
    roots = tuple(root + "\\" + name for name in guard["IMPORT_ROOTS"])
    observed = verify_module_origins(sys.modules, root, roots)
    print(json.dumps({"probe_version": 1, "startup_verified": True,
        "version": list(sys.version_info[:3]), "source_module_count": len(observed),
        "origins": observed, "site_loaded": "site" in sys.modules,
        "sys_path_verified": True, "sha256_witness": digest, "counter_verified": counts["a"] == 2}))


if __name__ == "__main__":
    main()
