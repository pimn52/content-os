import hashlib
import pytest
from pydantic import ValidationError
from app.domain.models import ExecutionArtifact

EMPTY_SHA = hashlib.sha256(b"").hexdigest()


def test_empty_dependency_keeps_exact_identity_on_json_roundtrip():
    value = ExecutionArtifact(role="dependency", name="vendor/package/__init__.py",
                              size_bytes=0, sha256=EMPTY_SHA)
    assert ExecutionArtifact.model_validate_json(value.model_dump_json()) == value
    assert value.model_dump() == dict(role="dependency", name="vendor/package/__init__.py",
                                     size_bytes=0, sha256=EMPTY_SHA)


@pytest.mark.parametrize("role", ["weights", "interpreter", "entrypoint", "unknown"])
def test_empty_non_dependency_cannot_be_admitted_as_an_artifact(role):
    with pytest.raises(ValidationError, match="execution_empty_artifact_invalid"):
        ExecutionArtifact(role=role, name="resource", size_bytes=0, sha256=EMPTY_SHA)


@pytest.mark.parametrize("size,sha", [(0, "1"*64), (-1, EMPTY_SHA), (False, EMPTY_SHA), (0.0, EMPTY_SHA)])
def test_wrong_empty_hash_and_invalid_sizes_are_rejected(size, sha):
    with pytest.raises(ValidationError):
        ExecutionArtifact(role="dependency", name="resource", size_bytes=size, sha256=sha)
