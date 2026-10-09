"""Small offline regression tests for documentation ownership rules."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def checker(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "check_docs_under_test", Path(__file__).resolve().parents[1] / "check_docs.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.ROOT = tmp_path
    for name in (
        module.ACTIVE_ROOT_DOCS
        | module.PRODUCT_DOCS
        | module.ARCHITECTURE_DOCS
        | module.IMPLEMENTATION_DOCS
    ):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Document\n", encoding="utf-8")
    (tmp_path / "START_HERE.md").write_text(
        "\n".join(sorted(module.REQUIRED_START_REFERENCES)), encoding="utf-8"
    )
    (tmp_path / "STATUS.md").write_text("## Active work package\n", encoding="utf-8")
    return module


def test_valid_hierarchy(checker):
    assert checker.main() == 0


def test_implementation_roadmap_is_required(checker, capsys):
    (checker.ROOT / "docs/implementation/ROADMAP.md").unlink()
    assert checker.main() == 1
    assert "missing active document: docs/implementation/ROADMAP.md" in capsys.readouterr().out


def test_entry_must_link_roadmap(checker, capsys):
    (checker.ROOT / "START_HERE.md").write_text(
        "\n".join(sorted(checker.REQUIRED_START_REFERENCES - checker.IMPLEMENTATION_DOCS)),
        encoding="utf-8",
    )
    assert checker.main() == 1
    assert "START_HERE.md must reference docs/implementation/ROADMAP.md" in capsys.readouterr().out


@pytest.mark.parametrize("model", ["Luna", "Terra", "Sol", "Astra", "gpt-6-astra"])
def test_roadmap_uses_work_classes_not_duplicate_model_mapping(checker, capsys, model):
    (checker.ROOT / "docs/implementation/ROADMAP.md").write_text(model, encoding="utf-8")
    assert checker.main() == 1
    assert "repeats implementation-model tier policy" in capsys.readouterr().out


def test_agent_policy_and_active_assignment_may_name_models(checker):
    (checker.ROOT / "AGENTS.md").write_text("gpt-6-luna / gpt-6-sol / gpt-6-astra", encoding="utf-8")
    (checker.ROOT / "STATUS.md").write_text("## Active work package\ngpt-6-sol", encoding="utf-8")
    assert checker.main() == 0
