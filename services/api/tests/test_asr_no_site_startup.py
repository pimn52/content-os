"""Startup regression: controlled stdlib/stub children, never speech models."""
from dataclasses import replace
import json
from pathlib import Path
import shutil
import subprocess
import sys
from zipfile import ZipFile, ZIP_STORED

import pytest
from pydantic import ValidationError, TypeAdapter
from app.domain.models import ASRNoSiteStartup, ExecutionSpecificationV7, ExecutionSpecificationV8, ExecutionSpecificationRecord
from app.domain.asr_child import ASRChildRequest
from app.providers import maintained_asr_execution as execution
from app.providers.maintained_runtime import _check_startup_roots
from app.providers.prepared import ExecutionPreparationError
from test_voice_evaluation_qa import qa_case
from test_maintained_voice_job import bound_job
from test_maintained_voice_execution import child_case
from test_maintained_runtime import candidate, reference
from test_execution_admission import setup


@pytest.fixture
def stub_child(tmp_path):
    env = tmp_path/"env"
    site = env/"Lib/site-packages"
    site.mkdir(parents=True)
    (env/"Scripts").mkdir()
    shutil.copyfile(sys.executable, env/"Scripts/python.exe")
    base = Path(sys.base_prefix)
    (env/"pyvenv.cfg").write_text(f"home = {base}\ninclude-system-site-packages = false\nversion = 3.12.14\n")
    stage = tmp_path/"stage"; stage.mkdir()
    hostile = tmp_path/"hostile"; hostile.mkdir()
    sentinel = tmp_path/"unexpected-startup"
    evil = f"from pathlib import Path; Path({str(sentinel)!r}).write_text('BAD')\n"
    (site/"evil.pth").write_text("import pathlib; pathlib.Path("+repr(str(sentinel))+").write_text('BAD')\n"+str(hostile)+"\n")
    for name in ("sitecustomize.py", "usercustomize.py"):
        (site/name).write_text(evil)
    (hostile/"vendor_stub.py").write_text(evil)
    (stage/"app.py").write_text(evil)
    (stage/"json.py").write_text(evil)
    (site/"json.py").write_text(evil)
    (site/"vendor_stub.py").write_text("VALUE = 'inventoried-stub'\n")
    bootstrap = Path(execution.__file__).with_name("asr_no_site_bootstrap.py").read_bytes()
    archive = stage/"application.pyz"
    with ZipFile(archive,"w",compression=ZIP_STORED) as zipfile:
        zipfile.writestr("__main__.py",bootstrap)
        zipfile.writestr("app/__init__.py","")
        zipfile.writestr("app/providers/__init__.py","")
        zipfile.writestr("app/providers/maintained_asr_child.py",
            "def main():\n import vendor_stub, json, sys\n from pathlib import Path\n"
            " Path(sys.argv[1]).with_name('stub-result.json').write_text(json.dumps({'vendor': vendor_stub.__file__, 'value': vendor_stub.VALUE, 'paths':sys.path}))\n return 0\n")
    startup = ASRNoSiteStartup(policy="cpython312-no-site-v1", environment_root=str(env), base_root=str(base))
    (stage/"startup.json").write_text(startup.model_dump_json())
    (stage/"request.json").write_text("{}")
    return env, base, stage, hostile, sentinel


def launch_stub(case):
    env, _, stage, hostile, _ = case
    import os
    child_env = dict(os.environ, PYTHONPATH=str(hostile), PYTHONHOME=str(hostile), PYTHONDONTWRITEBYTECODE="0")
    return subprocess.run([str(env/"Scripts/python.exe"),"-I","-S","-B",str(stage/"application.pyz"),str(stage/"request.json")],
                          cwd=stage, env=child_env, capture_output=True, timeout=20)


def test_controlled_child_ignores_hooks_and_shadow_paths(stub_child):
    result = launch_stub(stub_child)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    env, base, stage, hostile, sentinel = stub_child
    record = json.loads((stage/"stub-result.json").read_text())
    assert record["value"] == "inventoried-stub"
    assert Path(record["vendor"]) == env/"Lib/site-packages/vendor_stub.py"
    assert record["paths"] == [str(stage/"application.pyz"),str(base/"python312.zip"),str(base/"DLLs"),str(base/"Lib"),str(base),str(env/"Lib/site-packages")]
    assert str(hostile) not in record["paths"] and not sentinel.exists()


@pytest.mark.parametrize("change", ["extra_root", "base_site", "missing_vendor", "duplicate_key"])
def test_bad_startup_stops_before_vendor(stub_child, change):
    env, base, stage, _, sentinel = stub_child
    p = stage/"startup.json"; record = json.loads(p.read_text())
    if change == "extra_root": record["extra_root"] = str(stage)
    elif change == "base_site": record["base_root"] = str(base/"Lib/site-packages")
    elif change == "missing_vendor": (env/"Lib/site-packages").rename(env/"Lib/disabled")
    if change == "duplicate_key": p.write_text(p.read_text()[:-1]+',"policy":"cpython312-no-site-v1"}')
    else: p.write_text(json.dumps(record))
    result = launch_stub(stub_child)
    assert result.returncode != 0
    assert not (stage/"stub-result.json").exists() and not sentinel.exists()


def prepared(q):
    request = ASRChildRequest(model_root=str(q.options["model_root"]),audio_path=q.audio.source_file,
                             audio_sha256=q.audio.content_hash,language="zh")
    return execution.prepare_maintained_asr_child(**q.options, request=request)


def test_explicit_schema_preserves_v1_and_rejects_downgrade(qa_case):
    q = qa_case; raw = q.spec.model_dump_json()
    parsed = TypeAdapter(ExecutionSpecificationRecord).validate_json(raw)
    assert parsed.model_dump_json() == raw and parsed.execution_sha256 == q.spec.execution_sha256
    is_v2 = q.options["invocation_policy"].endswith("v2")
    assert type(parsed) is (ExecutionSpecificationV8 if is_v2 else ExecutionSpecificationV7)
    fields = json.loads(raw); fields["observation_version"] = 1 if is_v2 else 2
    with pytest.raises(ValidationError): TypeAdapter(ExecutionSpecificationRecord).validate_json(json.dumps(fields))
    fields = json.loads(raw); fields["schema_version"] = True
    with pytest.raises(ValidationError): TypeAdapter(ExecutionSpecificationRecord).validate_json(json.dumps(fields))
    if is_v2:
        with pytest.raises(ValidationError): ExecutionSpecificationV7.model_validate_json(raw)


@pytest.mark.parametrize("change", ["startup", "bundle", "request", "flags", "cwd", "environment", "timeout"])
def test_prepared_integrity_denies_mutation_before_child(qa_case, change):
    q = qa_case
    with prepared(q) as p:
        stage = p.invocation.cwd
        if change == "startup" and type(q.spec) is ExecutionSpecificationV7:
            return
        if change in ("startup", "bundle", "request"):
            name = {"startup":"startup.json","bundle":"application.pyz","request":"request.json"}[change]
            (stage/name).write_bytes(b"changed")
        else:
            field, value = {"flags":("argv",tuple(x for x in p.invocation.argv if x != "-I")),
                "cwd":("cwd",stage.parent), "environment":("environment",p.invocation.environment+(("PYTHONPATH",str(stage)),)),
                "timeout":("timeout_seconds",p.invocation.timeout_seconds+1)}[change]
            p._invocation = replace(p.invocation,**{field:value})
        with pytest.raises(ExecutionPreparationError): p.verify()
    assert q.calls == []


def test_new_recipe_rejects_pth_control_but_keeps_legacy_root_check(qa_case):
    q = qa_case; env=q.options["environment"]; base=q.options["base"]
    hook = env/"Lib/site-packages/outside.pth"; hook.write_text(str(q.c.root)+"\n")
    from types import SimpleNamespace
    with pytest.raises(ExecutionPreparationError,match="maintained_startup_root_unsupported"):
        _check_startup_roots(SimpleNamespace(startup_hooks=["environment/Lib/site-packages/outside.pth"]),env,base)
    inventory = execution.observe_asr_inventory(env,base,invocation_policy="isolated-zip-asr-v2",max_bytes=q.options["max_bytes"])
    assert execution._no_site_startup(env,base,inventory).environment_root == str(env)
    (base/"python312._PTH").write_text("import site\n")
    inventory = execution.observe_asr_inventory(env,base,invocation_policy="isolated-zip-asr-v2",max_bytes=q.options["max_bytes"])
    with pytest.raises(ExecutionPreparationError,match="no_site_pth_unsupported"):
        execution._no_site_startup(env,base,inventory)


@pytest.mark.parametrize("root", ["relative", "C:/env", "C:\\env\\..\\outside", "\\\\server\\env", "C:\\env "])
def test_startup_record_rejects_noncanonical_roots(root):
    with pytest.raises(ValidationError):
        ASRNoSiteStartup(policy="cpython312-no-site-v1",environment_root=root,base_root="D:\\base")


@pytest.mark.parametrize("version", [1, 2])
def test_configuration_selects_explicit_recipe_and_denies_mismatch(tmp_path, version):
    from types import SimpleNamespace
    from app.jobs.voice_evaluation_qa_handler import configured_voice_evaluation_qa_handler
    from app.execution_admission import UseAdmissionError
    options = dict(environment=str(tmp_path/"env"),base=str(tmp_path/"base"),model_root=str(tmp_path/"model"),
                   origin_reference=dict(reference="origin.json",sha256="a"*64),installation_id="12345678-1234-4234-8234-123456789abc",
                   runtime_label="synthetic",machine_id="synthetic")
    if version == 2: options["invocation_policy"] = "isolated-zip-asr-v2"
    source = tmp_path/"config.json"
    source.write_text(json.dumps(dict(config_version=version,runtime_options=options)))
    config = SimpleNamespace(data_root=tmp_path)
    environment = dict(CONTENT_OS_MAINTAINED_ASR_CONFIG=str(source),CONTENT_OS_MAINTAINED_ASR_OPERATOR_KEY="fixture-only")
    handler = configured_voice_evaluation_qa_handler(config,None,environment)
    assert handler.options.get("invocation_policy","isolated-zip-asr-v1").endswith(f"v{version}")
    options["invocation_policy"] = "isolated-zip-asr-v1" if version == 2 else "isolated-zip-asr-v2"
    source.write_text(json.dumps(dict(config_version=version,runtime_options=options)))
    with pytest.raises(UseAdmissionError): configured_voice_evaluation_qa_handler(config,None,environment)
