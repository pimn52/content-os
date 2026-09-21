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

PRODUCT_DOCS = {
    "docs/product/README.md",
    "docs/product/CREATOR_INTELLIGENCE.md",
    "docs/product/MEDIA_ASSET_SYSTEM.md",
    "docs/product/VOICE_TALKING.md",
    "docs/product/EXECUTION_COMPUTE.md",
    "docs/product/TIMELINE_RENDER_LEARNING.md",
}

ARCHITECTURE_DOCS = {
    "docs/architecture/SYSTEM_ARCHITECTURE.md",
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
    "CONTENT_OS_EXECUTION_SPEC.md",
    "docs/product/README.md",
    "docs/architecture/SYSTEM_ARCHITECTURE.md",
    "STATUS.md",
    "AGENTS.md",
    "DECISIONS.md",
    "README.md",
}

STALE_REFERENCE_NAMES = FORBIDDEN_ROOT_DOCS | {"CONTENT_OS_REVIEW_2026-09-10.md"}

# Implementation-model policy belongs only in AGENTS and the one active STATUS
# package. Product/architecture specs must not repeat it.
IMPLEMENTATION_TIER_TERMS = re.compile(r"\b(?:Luna|Terra|Sol)\b")

MAX_LINES = {
    "STATUS.md": 220,
    "CONTENT_OS_EXECUTION_SPEC.md": 260,
    "DECISIONS.md": 220,
    "START_HERE.md": 180,
    "docs/architecture/SYSTEM_ARCHITECTURE.md": 300,
}


def fail(errors: list[str]) -> int:
    print("Documentation governance check failed:")
    for error in errors:
        print(f"- {error}")
    return 1


def read(path_name: str) -> str:
    return (ROOT / path_name).read_text(encoding="utf-8")


def main() -> int:
    errors: list[str] = []

    required_docs = ACTIVE_ROOT_DOCS | PRODUCT_DOCS | ARCHITECTURE_DOCS
    for name in required_docs:
        if not (ROOT / name).is_file():
            errors.append(f"missing active document: {name}")

    for name in FORBIDDEN_ROOT_DOCS:
        if (ROOT / name).exists():
            errors.append(f"historical document must not remain at repository root: {name}")

    for path in ROOT.glob("*.md"):
        if any(path.name.startswith(prefix) for prefix in FORBIDDEN_ROOT_PREFIXES):
            errors.append(f"new parallel review/freeze/handoff document found at root: {path.name}")

    start_here = ROOT / "START_HERE.md"
    if start_here.is_file():
        text = start_here.read_text(encoding="utf-8")
        for target in REQUIRED_START_REFERENCES:
            if target not in text:
                errors.append(f"START_HERE.md must reference {target}")

    # Active documents must not point readers back to retired root controls.
    for name in required_docs:
        path = ROOT / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for stale in STALE_REFERENCE_NAMES:
            if re.search(rf"\b{re.escape(stale)}\b", text):
                errors.append(f"{name} references retired root control {stale}")

    # STATUS is a current-state control, never a completed-work archive.
    status_path = ROOT / "STATUS.md"
    if status_path.is_file():
        status = status_path.read_text(encoding="utf-8")
        if re.search(r"^## Completed work package", status, flags=re.MULTILINE):
            errors.append("STATUS.md must not retain completed work-package history")
        if status.count("## Active work package") != 1:
            errors.append("STATUS.md must contain exactly one active work package")
        if len(status.splitlines()) > MAX_LINES["STATUS.md"]:
            errors.append(
                f"STATUS.md exceeds {MAX_LINES['STATUS.md']} lines; move history to Git/evaluation evidence and current behavior to module specs"
            )

    # Keep high-level/current specs overview-friendly.
    for name, limit in MAX_LINES.items():
        if name == "STATUS.md":
            continue
        path = ROOT / name
        if path.is_file() and len(path.read_text(encoding="utf-8").splitlines()) > limit:
            errors.append(f"{name} exceeds overview limit of {limit} lines")

    # Implementation-agent tier language must not spread through product specs.
    tier_free_docs = (
        PRODUCT_DOCS
        | ARCHITECTURE_DOCS
        | {
            "CONTENT_OS_EXECUTION_SPEC.md",
            "DECISIONS.md",
            "README.md",
            "START_HERE.md",
        }
    )
    for name in tier_free_docs:
        path = ROOT / name
        if path.is_file() and IMPLEMENTATION_TIER_TERMS.search(path.read_text(encoding="utf-8")):
            errors.append(
                f"{name} repeats implementation-model tier policy; keep Luna/Terra/Sol policy in AGENTS.md (and active STATUS assignment) only"
            )

    # Product specs describe current capability, not task lifecycle.
    for name in PRODUCT_DOCS:
        path = ROOT / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        if re.search(r"\b(?:READY|RUNNING|AWAITING_U_REVIEW)\b", text):
            errors.append(f"{name} contains active task-lifecycle state; move it to STATUS.md")

    if errors:
        return fail(errors)

    print("Documentation governance check passed.")
    print("Root controls:")
    for name in sorted(ACTIVE_ROOT_DOCS):
        print(f"- {name}")
    print("Product module specs:")
    for name in sorted(PRODUCT_DOCS):
        print(f"- {name}")
    print("Architecture:")
    for name in sorted(ARCHITECTURE_DOCS):
        print(f"- {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
