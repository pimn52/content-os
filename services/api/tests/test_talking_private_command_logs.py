"""Fake command only; preserve bounded failed logs without public disclosure."""
import json
import time
from types import SimpleNamespace
from pathlib import Path
from tempfile import TemporaryDirectory
import pytest
from app.providers import maintained_talking_dual as dual
from app.providers.prepared import ExecutionPreparationError


@pytest.mark.parametrize("case", ["success", "nonzero", "oversized"])
def test_owned_command_private_failure_logs_are_bounded(tmp_path, monkeypatch, case):
    private = tmp_path / "private"
    private.mkdir()
    owner = dual.OwnedDualLatentSync({}, private, time.monotonic() + 20)
    secret = b"fixture-private-path-and-token"
    raw = secret if case != "oversized" else b"x" * (1024**2 + 10) + secret

    def launch(argv, **kw):
        kw["stderr"].write(raw)
        kw["stderr"].flush()
        return SimpleNamespace(wait=lambda **_kw: 0 if case == "success" else 1, poll=lambda: 0)

    monkeypatch.setattr(dual.subprocess, "Popen", launch)
    if case == "oversized":
        with pytest.raises(ExecutionPreparationError, match="^maintained_owned_output_limit$"):
            owner.process(["fixture-python", "private-argument"], private, {"PRIVATE_KEY": "fixture-secret"})
    else:
        result = owner.process(["fixture-python", "private-argument"], private, {"PRIVATE_KEY": "fixture-secret"})
        assert result.returncode == (0 if case == "success" else 1)
    folder = private / "command-failure"
    if case == "success":
        assert not folder.exists()
        return
    metadata = json.loads((folder / "metadata.json").read_text())
    assert metadata["private_only"] and metadata["returncode"] == 1
    assert metadata["reason"] == ("output_limit" if case == "oversized" else "nonzero_exit")
    assert metadata["logs"]["stderr"]["truncated"] == (case == "oversized")
    assert sum((folder / (label + ".log")).stat().st_size for label in ("stdout", "stderr")) <= 1024**2
    assert (folder / "stderr.log").read_bytes().endswith(secret)
    assert "private-argument" not in json.dumps(metadata) and "PRIVATE_KEY" not in json.dumps(metadata)


def test_owned_application_failure_keeps_bounded_private_trace_and_fixed_error(tmp_path, monkeypatch):
    # Inert object exercises only process/error retention, not preparation or
    # real admission. Fake Popen below cannot launch an interpreter/model.
    prepared = dual.PreparedDualTalkingChild.__new__(dual.PreparedDualTalkingChild)
    prepared._temporary = TemporaryDirectory(dir=tmp_path)
    prepared._options = dict(environment=tmp_path,tools=tmp_path,hf=tmp_path,torch=tmp_path,data_root=tmp_path)
    prepared._state, prepared._files = "prepared", {}
    spec = SimpleNamespace(timeout_seconds=30,model_artifacts=(),resource_copies=())
    monkeypatch.setattr(prepared,"verify",lambda:spec)
    monkeypatch.setattr(dual,"_system_root",lambda:tmp_path)
    private = Path(prepared._temporary.name)
    raw = b"x" * (512*1024 + 20) + b"fixture-private-startup-error"

    def launch(argv,**kw):
        assert kw["shell"] is False
        kw["stderr"].write(raw)
        kw["stderr"].flush()
        return SimpleNamespace(wait=lambda **_kw:1,poll=lambda:1)

    monkeypatch.setattr(dual.subprocess,"Popen",launch)
    with pytest.raises(ExecutionPreparationError,match="^maintained_talking_child_failed$"):
        prepared.execute()
    prepared.close()
    assert private.exists()
    metadata = json.loads((private/"application-failure.json").read_text())
    assert metadata["private_only"] and metadata["logs"]["stderr"]["truncated"]
    assert (private/"application-stderr.log").stat().st_size == 512*1024
    assert (private/"application-stderr.log").read_bytes().endswith(b"fixture-private-startup-error")
    assert "fixture-private" not in json.dumps(metadata)
