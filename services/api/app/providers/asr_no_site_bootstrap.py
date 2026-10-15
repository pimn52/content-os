"""Owned ZIP __main__: stdlib only until fixed no-site startup is checked."""
import sys
import json
from pathlib import Path
import stat


def exact(path, directory=True):
    if (not path.is_absolute() or path != path.resolve(strict=True)
        or not (stat.S_ISDIR(path.lstat().st_mode) if directory else stat.S_ISREG(path.lstat().st_mode))):
        raise ValueError("maintained_asr_startup_path_invalid")


def distinct(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("maintained_asr_startup_invalid")
        result[key] = value
    return result


def configure():
    if (sys.version_info[:2] != (3, 12) or len(sys.argv) != 2
        or any(getattr(sys.flags, name) != 1 for name in
               ("isolated", "no_site", "ignore_environment", "no_user_site", "dont_write_bytecode"))):
        raise ValueError("maintained_asr_startup_flags_invalid")
    archive = Path(sys.argv[0])
    exact(archive, False)
    request = archive.with_name("request.json")
    if sys.argv[1] != str(request):
        raise ValueError("maintained_asr_startup_invalid")
    exact(request, False)
    startup = archive.with_name("startup.json")
    exact(startup, False)
    with startup.open("rb") as source:
        raw = source.read(8193)
    if not 0 < len(raw) <= 8192:
        raise ValueError("maintained_asr_startup_invalid")
    record = json.loads(raw, object_pairs_hook=distinct)
    if (type(record) is not dict or set(record) != {"policy", "environment_root", "base_root"}
        or record["policy"] != "cpython312-no-site-v1"
        or any(type(record[k]) is not str for k in ("environment_root", "base_root"))):
        raise ValueError("maintained_asr_startup_invalid")
    env, base = Path(record["environment_root"]), Path(record["base_root"])
    for path, value in ((env, record["environment_root"]), (base, record["base_root"])):
        exact(path)
        if str(path) != value:
            raise ValueError("maintained_asr_startup_invalid")
    if env.is_relative_to(base) or base.is_relative_to(env):
        raise ValueError("maintained_asr_startup_invalid")
    executable, packages = env / "Scripts/python.exe", env / "Lib/site-packages"
    exact(executable, False)
    exact(packages)
    if Path(sys.executable) != executable:
        raise ValueError("maintained_asr_startup_interpreter_invalid")
    standard = [base / "python312.zip", base / "DLLs", base / "Lib", base]
    for path in standard[1:]:
        exact(path)
    if standard[0].exists():
        exact(standard[0], False)
    expected = [str(archive), *(str(path) for path in standard)]
    if sys.path != expected:
        raise ValueError("maintained_asr_startup_search_invalid")
    sys.path[:] = [*expected, str(packages)]


if __name__ == "__main__":
    configure()
    import app
    if Path(app.__file__) != Path(sys.argv[0]) / "app/__init__.py":
        raise ValueError("maintained_asr_application_origin_invalid")
    from app.providers.maintained_asr_child import main
    raise SystemExit(main())
