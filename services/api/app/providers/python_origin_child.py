"""Owned stdlib-only diagnostic child. No provider/model is imported.

Expected inventory is comparison evidence, never a file selection recipe.
The root comes from the already checked actual interpreter, not stdin/CWD.
"""
import sys

REQUEST_LIMIT = 16 * 1024 * 1024 + 64 * 1024
TREE_LIMIT = 256 * 1024 * 1024
FILE_LIMIT = 10000
DIRECTORY_LIMIT = 3000
COMPONENT_PROTOCOL = False  # only the independently owned v3 bundle replaces this constant
OWNER_PROTOCOL = False  # only the independently owned v4 bundle enables attribution
NATIVE_PROTOCOL = False  # only the independently owned v5 fixed-target diagnostic enables load


class ProbeFailure(RuntimeError):
    pass


def _exact_path(path, *, directory):
    # Root-owned pre-import scanner: no application/helper imports yet.
    import stat
    try:
        if not path.is_absolute() or path != path.resolve(strict=True): raise ValueError()
        mode = path.lstat().st_mode
        if not (stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode)): raise ValueError()
    except (OSError, ValueError, RuntimeError):
        raise ProbeFailure("execution_runtime_path_invalid") from None


def guard_startup(system):
    root = system.executable.rsplit("\\", 1)[0]
    with open(root + "\\content_os_bootstrap.py", "rb") as source:
        payload = source.read(65537)
    if len(payload) > 65536:
        raise SystemExit("execution_python_startup_invalid")
    guard = {"__name__": "_owned_startup_guard"}
    exec(compile(payload, "<owned-startup-guard>", "exec"), guard)
    return guard["verify_startup"](system, entry_name="_content_os_origin_probe.py")


def canonical(value):
    import json
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False).encode("utf-8")


def read_request(stream, *, allow_component=False, allow_owner=False, allow_native=False):
    import json
    import re
    try:
        payload = stream.read(REQUEST_LIMIT + 1)
        if not 0 < len(payload) <= REQUEST_LIMIT:
            raise ValueError()
        value = json.loads(payload)
        if (set(value) != {"probe_version", "nonce", "inventory", "host_runtime"}
            or type(value["probe_version"]) is not int
            or value["probe_version"] not in ((5,) if allow_component and allow_owner and allow_native else
                (4,) if allow_component and allow_owner and not allow_native else (3,) if allow_component and not allow_native else (1, 2) if not allow_native else ())
            or type(value["nonce"]) is not str or not re.fullmatch("[0-9a-f]{64}", value["nonce"])
            or canonical(value) != payload):
            raise ValueError()
        return value
    except (ValueError, TypeError, KeyError):
        raise ProbeFailure("execution_origin_probe_protocol_invalid") from None


def scan_private(root, expected):
    import os
    import stat
    import hashlib
    try:
        if (set(expected) != {"inventory_version", "files", "directories"}
            or type(expected["inventory_version"]) is not int or expected["inventory_version"] != 1
            or type(expected["files"]) is not list or not 3 <= len(expected["files"]) <= FILE_LIMIT
            or type(expected["directories"]) is not list or len(expected["directories"]) > DIRECTORY_LIMIT):
            raise ValueError()
        roles = {entry["name"]: entry["role"] for entry in expected["files"]}
        pending, directories, files, total = [root], [], [], 0
        while pending:
            current = pending.pop()
            _exact_path(current, directory=True)
            if current != root:
                directories.append(current.relative_to(root).as_posix())
            if len(pending) + len(directories) > DIRECTORY_LIMIT:
                raise ValueError()
            with os.scandir(current) as children:
                for child in children:
                    path = type(root)(child.path)
                    mode = child.stat(follow_symlinks=False).st_mode
                    if stat.S_ISDIR(mode):
                        _exact_path(path, directory=True)
                        pending.append(path)
                        if len(pending) + len(directories) > DIRECTORY_LIMIT: raise ValueError()
                        continue
                    if not stat.S_ISREG(mode): raise ValueError()
                    _exact_path(path, directory=False)
                    name, size, digest = path.relative_to(root).as_posix(), 0, hashlib.sha256()
                    with path.open("rb") as source:
                        for chunk in iter(lambda: source.read(1024 * 1024), b""):
                            size += len(chunk)
                            total += len(chunk)
                            if total > TREE_LIMIT: raise ValueError()
                            digest.update(chunk)
                    files.append({"name": name, "role": roles.get(name, "dependency"),
                        "size_bytes": size, "sha256": digest.hexdigest()})
                    if len(files) > FILE_LIMIT: raise ValueError()
        observed = {"inventory_version": 1, "directories": sorted(directories),
            "files": sorted(files, key=lambda entry: entry["name"])}
        ordered = dict(expected, directories=sorted(expected["directories"]),
            files=sorted(expected["files"], key=lambda entry: entry["name"]))
        if canonical(observed) != canonical(ordered):
            raise ValueError()
        return hashlib.sha256(canonical(observed)).hexdigest()
    except (ValueError, OSError, TypeError, KeyError, RuntimeError):
        raise ProbeFailure("execution_origin_probe_tree_changed") from None


def cpu_observation(facts):
    if facts.native_machine != 0x8664 or facts.process_machine != 0 or facts.system_directory != facts.root / "System32":
        raise ProbeFailure("execution_host_architecture_unsupported")
    _exact_path(facts.root, directory=True)
    _exact_path(facts.system_directory, directory=True)
    return {"observation_recipe": "windows-platform-ubr-selected-device", "observation_version": 2,
        "trust_recipe": "windows-os-selected-driver-v2", "os_family": "windows", "architecture": "amd64",
        "build": facts.build, "revision": facts.revision, "device": {"kind": "cpu"}}


def load_profile(root, *, include_contracts=False):
    import json
    import re
    path = root / "Lib/site-packages/app/providers/windows_os_policy.json"
    _exact_path(path, directory=False)
    with path.open("rb") as source: payload = source.read(65537)
    try:
        value = json.loads(payload)
        if (not 0 < len(payload) <= 65536 or canonical(value) + b"\n" != payload
            or set(value) != {"policy_version", "architecture", "components", "api_contracts"}
            or type(value["policy_version"]) is not int or value["policy_version"] != 1
            or value["architecture"] != "amd64"
            or not 1 <= len(value["components"]) <= 128 or len(value["api_contracts"]) > 256):
            raise ValueError()
        names = tuple(entry["name"] for entry in value["components"])
        if names != tuple(sorted(set(names))) or any(not re.fullmatch("[a-z0-9_][a-z0-9_-]*\\.dll", name)
            or name.startswith(("api-", "ext-")) for name in names):
            raise ValueError()
        if include_contracts:
            contracts = tuple(entry['name'] for entry in value['api_contracts'])
            if (contracts != tuple(sorted(set(contracts))) or set(names) & set(contracts)
                or any(not re.fullmatch(r'(?:api|ext)-[a-z0-9]+(?:-[a-z0-9]+)+\.dll', n) for n in contracts)):
                raise ValueError()
            return frozenset(names + contracts)
        return frozenset(names)
    except (ValueError, TypeError, KeyError):
        raise ProbeFailure("execution_os_policy_invalid") from None


def check_origins(root, inventory, system, names, paths):
    from _content_os_host.runtime_primitives import require_unique_native_origins
    if type(paths) is not tuple or not 1 <= len(paths) <= 2048 or len(set(paths)) != len(paths):
        raise ProbeFailure("execution_module_observation_unavailable")
    fixed = {root / entry["name"] for entry in inventory["files"]}
    require_unique_native_origins(paths)
    for path in paths:
        _exact_path(path, directory=False)
        if path.name.casefold() in names:
            allowed = path == system / path.name.lower()
        else:
            allowed = path in fixed
        if not allowed:
            error = ProbeFailure("execution_loaded_origin_unsupported")
            error.unknown_module = path.name
            raise error


def blocked_origins(root, inventory, system, names, snapshots):
    """Complete bounded evidence for these snapshots, never an admission list."""
    import hashlib
    import re
    from _content_os_host.runtime_primitives import require_unique_native_origins
    fixed = {root / entry["name"] for entry in inventory["files"]}
    rows = []
    for kind, paths in snapshots:
        if type(paths) is not tuple or not 1 <= len(paths) <= 2048 or len(set(paths)) != len(paths):
            raise ProbeFailure("execution_module_observation_unavailable")
        require_unique_native_origins(paths)
        for path in paths:
            _exact_path(path, directory=False)
            name = path.name.casefold()
            os_name = name in names
            if (path == system / name if os_name else path in fixed):
                continue
            if not re.fullmatch("[a-z0-9_][a-z0-9_.-]{0,254}", name):
                raise ProbeFailure("execution_origin_probe_evidence_invalid")
            category = ("system_exact" if path == system / name else "private_inventory" if path in fixed
                else "private_unlisted" if path.is_relative_to(root) else "outside")
            rows.append({"kind": kind, "name": name, "category": category,
                "origin_id": hashlib.sha256(str(path).encode("utf-8")).hexdigest(),
                "reason": "os_named_wrong_origin" if os_name else "unlisted_origin"})
            if len(rows) > 256:
                raise ProbeFailure("execution_origin_probe_evidence_overflow")
    rows.sort(key=lambda row: (row["kind"], row["name"], row["category"], row["origin_id"]))
    if len(canonical(rows)) > 48 * 1024:
        raise ProbeFailure("execution_origin_probe_evidence_overflow")
    return rows


def observe(root, request, modules, reader_type, facts_reader):
    """Dependency-injected offline seam; main selects only owned actual readers."""
    import hashlib
    from _content_os_host.windows_native import python_origins
    version = request["probe_version"]
    report = {"probe_version": version, "nonce": request["nonce"], "passed": False, "stage": "tree",
        "inventory_sha256": "", "host_runtime": None, "native_count": 0, "python_count": 0,
        "code": "", "unknown_module": ""}
    if version == 2:
        report["blocked_origins"] = []
    try:
        report["inventory_sha256"] = scan_private(root, request["inventory"])
        report["stage"] = "host"
        facts = facts_reader()
        report["host_runtime"] = cpu_observation(facts)
        if report["host_runtime"] != request["host_runtime"]:
            raise ProbeFailure("execution_host_runtime_changed")
        names = load_profile(root)
        for name in names: _exact_path(facts.system_directory / name, directory=False)
        from _content_os_host.runtime_primitives import require_search_topology
        require_search_topology(tuple(entry["name"] for entry in request["inventory"]["files"]),
            request["inventory"]["directories"], names)
        report["stage"] = "baseline"
        reader = reader_type(facts.system_directory)
        def origins():
            native, python = reader.native_origins(), python_origins(modules)
            report["native_count"], report["python_count"] = len(native), len(python)
            if version == 2:
                rows = blocked_origins(root, request["inventory"], facts.system_directory, names,
                    (("native", native), ("python", python)))
                if rows:
                    # Verify evidence is still bound before exposing a blocked
                    # baseline. Do not call activation/search/target APIs.
                    current = facts_reader()
                    if current != facts or cpu_observation(current) != request["host_runtime"]:
                        raise ProbeFailure("execution_host_runtime_changed")
                    if scan_private(root, request["inventory"]) != report["inventory_sha256"]:
                        raise ProbeFailure("execution_origin_probe_tree_changed")
                    error = ProbeFailure("execution_loaded_origin_unsupported")
                    error.unknown_module, error.blocked_origins = rows[0]["name"], rows
                    raise error
                return
            check_origins(root, request["inventory"], facts.system_directory, names, native)
            check_origins(root, request["inventory"], facts.system_directory, names, python)
        origins()
        report["stage"] = "search"
        reader.require_no_activation()
        reader.restrict_search()
        report["stage"] = "recheck"
        origins()
        current = facts_reader()
        if current != facts or cpu_observation(current) != request["host_runtime"]:
            raise ProbeFailure("execution_host_runtime_changed")
        if scan_private(root, request["inventory"]) != report["inventory_sha256"]:
            raise ProbeFailure("execution_origin_probe_tree_changed")
        report["stage"], report["passed"] = "complete", True
    except (OSError, ValueError, RuntimeError, TypeError, AttributeError) as error:
        import re
        code = str(error)
        report["code"] = code if re.fullmatch("execution_[a-z0-9_]{1,80}", code) else "execution_origin_probe_observation_unavailable"
        name = getattr(error, "unknown_module", "")
        report["unknown_module"] = name if type(name) is str and len(name) <= 255 else ""
        if version == 2:
            report["blocked_origins"] = getattr(error, "blocked_origins", [])
    return report


def main():
    # The actual root is checked before stdlib/helper/package imports.
    root = guard_startup(sys)
    from pathlib import Path
    request = read_request(sys.stdin.buffer, allow_component=COMPONENT_PROTOCOL, allow_owner=OWNER_PROTOCOL, allow_native=NATIVE_PROTOCOL)
    root = Path(root)
    # Enumerate/hash the whole actual tree BEFORE importing owned helper code.
    try:
        scan_private(root, request["inventory"])
    except ProbeFailure as error:
        # Preserve the named pre-import tree failure without importing helpers
        # or exposing a traceback/path. This cannot grant a success witness.
        report = {"probe_version": request["probe_version"], "nonce": request["nonce"], "passed": False, "stage": "tree",
            "inventory_sha256": "", "host_runtime": None, "native_count": 0, "python_count": 0,
            "code": str(error), "unknown_module": ""}
        if request["probe_version"] in (2, 3, 4, 5):
            report["blocked_origins"] = []
        if request["probe_version"] in (3, 4, 5):
            report.update(effective=None, associated=[])
        if request["probe_version"] in (4, 5):
            report.update(python_source_recipe='cpython312-expat-owner-v1', python_owner=None)
        if request['probe_version'] == 5: report['native_load'] = None
    else:
        from _content_os_host.windows_native import WindowsModuleReader, _read_windows_facts
        if request['probe_version'] in (3, 4, 5):
            from _content_os_host.component_child import observe as observe_components
            from _content_os_host.windows_activation import WindowsActivationReader
            owner = None
            if request['probe_version'] in (4, 5):
                from _content_os_host.python_owner_origins import ExpatOwnerOrigins
                owner = ExpatOwnerOrigins(root, request['inventory'])
            if request['probe_version'] == 5:
                from _content_os_host.component_load_child import Bz2MappingProbe
                report = observe_components(root, request, sys.modules, WindowsModuleReader,
                    _read_windows_facts, WindowsActivationReader, sys.modules[__name__], owner=owner,
                    load_recipe=Bz2MappingProbe())
            else:
                report = observe_components(root, request, sys.modules, WindowsModuleReader,
                    _read_windows_facts, WindowsActivationReader, sys.modules[__name__], owner=owner)
        else:
            report = observe(root, request, sys.modules, WindowsModuleReader, _read_windows_facts)
    payload = canonical(report)
    if len(payload) > 65536: raise SystemExit("execution_origin_probe_output_limit")
    sys.stdout.buffer.write(payload)
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
