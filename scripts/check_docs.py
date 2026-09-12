from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

ACTIVE_ROOT_DOCS = {
    "START_HERE.md",
    "CONTENT_OS_EXECUTION_SPEC.md",
    "STATUS.md",
    "DECISIONS.md",
    "AGENTS.md",
    "README.md",
}

FORBIDDEN_ROOT_DOCS = {
    "PRD.md",
    "DEVELOPMENT_PLAN.md",
    "BASELINE_FREEZE.md",
    "LOCAL_HANDOFF.md",
    "AUDIT_REPORT.md",
    "STRATEGY_BASELINE.md",
}

FORBIDDEN_ROOT_PREFIXES = (
    "CONTENT_OS_REVIEW_",
    "REVIEW_",
    "HANDOFF_",
    "FREEZE_",
)

REQUIRED_START_REFERENCES = {
    "STATUS.md",
    "CONTENT_OS_EXECUTION_SPEC.md",
    "AGENTS.md",
    "DECISIONS.md",
    "README.md",
}

STALE_REFERENCE_NAMES = FORBIDDEN_ROOT_DOCS | {"CONTENT_OS_REVIEW_2026-09-10.md"}


def fail(errors: list[str]) -> int:
    print("Documentation governance check failed:")
    for error in errors:
        print(f"- {error}")
    return 1


def main() -> int:
    errors: list[str] = []

    for name in ACTIVE_ROOT_DOCS:
        if not (ROOT / name).is_file():
            errors.append(f"missing active root document: {name}")

    for name in FORBIDDEN_ROOT_DOCS:
        if (ROOT / name).exists():
            errors.append(f"historical document must not remain at repository root: {name}")

    for path in ROOT.glob("*.md"):
        if any(path.name.startswith(prefix) for prefix in FORBIDDEN_ROOT_PREFIXES):
            errors.append(f"new parallel review/freeze/handoff document found at root: {path.name}")

    start_here = (ROOT / "START_HERE.md")
    if start_here.is_file():
        text = start_here.read_text(encoding="utf-8")
        for target in REQUIRED_START_REFERENCES:
            if target not in text:
                errors.append(f"START_HERE.md must reference {target}")

    # Active control docs must not point readers back to retired root controls.
    for name in ACTIVE_ROOT_DOCS:
        path = ROOT / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for stale in STALE_REFERENCE_NAMES:
            if re.search(rf"\b{re.escape(stale)}\b", text):
                # Mentioning a retired filename as a governance example is allowed only in check_docs.py,
                # not in active control docs.
                errors.append(f"{name} references retired root control {stale}")

    if errors:
        return fail(errors)

    print("Documentation governance check passed.")
    print("Active root controls:")
    for name in sorted(ACTIVE_ROOT_DOCS):
        print(f"- {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
