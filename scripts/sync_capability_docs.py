from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]
REGISTRY = "docs/product/CAPABILITIES.json"
PRODUCT_README = "docs/product/README.md"
CAPABILITY_INDEX = "docs/product/CAPABILITY_INDEX.md"
GROUP_ORDER = (
    ("core", "核心内容能力"),
    ("scheduling", "生产资源调度"),
    ("supporting", "支撑模块"),
)


def _load_registry(root: Path) -> tuple[dict[str, object] | None, list[str]]:
    path = root / REGISTRY
    if not path.is_file():
        return None, [f"missing capability registry: {REGISTRY}"]
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, [f"capability registry is invalid JSON: {exc}"]
    return value, []


def validate_registry(root: Path = ROOT) -> tuple[list[str], dict[str, object] | None]:
    errors: list[str] = []
    value, load_errors = _load_registry(root)
    errors.extend(load_errors)
    if value is None:
        return errors, None
    if value.get("schema_version") != "1.0":
        errors.append("capability registry schema_version must be 1.0")
    modules = value.get("modules")
    if not isinstance(modules, list) or not modules:
        return [*errors, "capability registry needs a non-empty modules list"], value
    required = {"id", "name", "group", "authority", "marker", "summary", "related"}
    allowed_groups = {group for group, _ in GROUP_ORDER}
    identifiers: dict[str, dict[str, object]] = {}
    authorities: set[str] = set()
    markers: set[str] = set()
    for index, module in enumerate(modules):
        if not isinstance(module, dict) or not required <= set(module):
            errors.append(f"capability module {index} is missing required fields")
            continue
        identifier = module.get("id")
        name = module.get("name")
        group = module.get("group")
        authority = module.get("authority")
        marker = module.get("marker")
        summary = module.get("summary")
        related = module.get("related")
        code = module.get("code", [])
        tests = module.get("tests", [])
        if not all(isinstance(item, str) and item.strip() for item in (identifier, name, authority, marker, summary)):
            errors.append(f"capability module {index} has blank identity fields")
            continue
        if identifier in identifiers:
            errors.append(f"duplicate capability id: {identifier}")
        if group not in allowed_groups:
            errors.append(f"{identifier} has unknown group: {group}")
        if not isinstance(related, list) or any(not isinstance(item, str) for item in related):
            errors.append(f"{identifier} related must be a string list")
            related = []
        for field_name, paths in (("code", code), ("tests", tests)):
            if not isinstance(paths, list) or any(not isinstance(item, str) or not item for item in paths):
                errors.append(f"{identifier} {field_name} must be a path list")
                continue
            for relative in paths:
                if not (root / relative).is_file():
                    errors.append(f"{identifier} {field_name} path is missing: {relative}")
        authority_path = root / authority
        if not authority_path.is_file():
            errors.append(f"{identifier} authority is missing: {authority}")
        else:
            text = authority_path.read_text(encoding="utf-8")
            if f"<!-- {marker} -->" not in text:
                errors.append(f"{identifier} authority lacks marker <!-- {marker} -->")
        if authority in authorities:
            errors.append(f"capability authority is not unique: {authority}")
        if marker in markers:
            errors.append(f"capability marker is not unique: {marker}")
        authorities.add(authority)
        markers.add(marker)
        identifiers[identifier] = module
    authority_root = (root / "docs" / "product").resolve()
    for authority in authorities:
        try:
            (root / authority).resolve().relative_to(authority_root)
        except ValueError:
            errors.append(f"capability authority must live under docs/product: {authority}")
    marker_counts: dict[str, int] = {}
    for path in sorted((root / "docs" / "product").glob("*.md")):
        text = path.read_text(encoding="utf-8")
        for marker in re.findall(r"<!-- (capability-authority:[a-z0-9_]+) -->", text):
            marker_counts[marker] = marker_counts.get(marker, 0) + 1
    for marker, count in marker_counts.items():
        if count != 1:
            errors.append(f"capability authority marker must appear exactly once: {marker}")
        if marker not in markers:
            errors.append(f"unknown capability authority marker: {marker}")
    for identifier, module in identifiers.items():
        for related in module.get("related", []):
            if related not in identifiers:
                errors.append(f"{identifier} references unknown related capability: {related}")
    for identifier in identifiers:
        module = identifiers[identifier]
        if module.get("group") in {"core", "scheduling"} and not module.get("code"):
            errors.append(f"{identifier} needs at least one code path")
        if module.get("group") in {"core", "scheduling"} and not module.get("tests"):
            errors.append(f"{identifier} needs at least one test path")
    return errors, value


def _relative(path: str, *, base: Path = ROOT / "docs" / "product") -> str:
    return Path(os.path.relpath(ROOT / path, base)).as_posix()


def _link(label: str, path: str) -> str:
    return f"[{label}]({_relative(path)})"


def _path_links(paths: list[str]) -> str:
    return "<br>".join(_link(path, path) for path in paths)


def _related_links(module: dict[str, object], modules: dict[str, dict[str, object]]) -> str:
    values = []
    for identifier in module["related"]:
        target = modules[identifier]
        values.append(_link(str(target["name"]), str(target["authority"])))
    return "、".join(values)


def _grouped_modules(registry: dict[str, object]) -> dict[str, list[dict[str, object]]]:
    grouped = {group: [] for group, _ in GROUP_ORDER}
    for module in registry["modules"]:
        grouped[module["group"]].append(module)
    return grouped


def _module_map(registry: dict[str, object]) -> dict[str, dict[str, object]]:
    return {str(module["id"]): module for module in registry["modules"]}


def render_product_readme(registry: dict[str, object]) -> str:
    modules = _module_map(registry)
    lines = [
        "# 产品模块",
        "",
        "能力行为的唯一权威来源是下表中的模块文档；当前事实只看 [STATUS](../../STATUS.md)，持久决定只看 [DECISIONS](../../DECISIONS.md)。",
        "本表和 [能力权威索引](CAPABILITY_INDEX.md) 由 [CAPABILITIES.json](CAPABILITIES.json) 自动生成，不手工维护。",
        "",
        "| 模块（点击进入唯一权威） | 职责边界 |",
        "|---|---|",
    ]
    for group, _ in GROUP_ORDER:
        for module in modules.values():
            if module["group"] != group:
                continue
            lines.append(
                f"| {_link(str(module['name']), str(module['authority']))} | "
                f"{module['summary']} |"
            )
    lines.extend([
        "",
        f"机器注册表：[CAPABILITIES.json](CAPABILITIES.json)。修改权威文档路径或模块关系时先更新注册表，再运行 `python scripts/sync_capability_docs.py`。",
        "`python scripts/check_docs.py` 会核对权威 marker、唯一边界、代码/测试路径、相对链接及生成文件是否过期。",
        "",
        "从 [产品总规格](../../CONTENT_OS_EXECUTION_SPEC.md) 理解 R1，通过 [STATUS](../../STATUS.md) 看当前事实。模块文件只写应当如何工作及已经实现到哪一步；过程记录留在 Git。",
        "",
    ])
    return "\n".join(lines)


def render_capability_index(registry: dict[str, object]) -> str:
    modules = _module_map(registry)
    grouped = _grouped_modules(registry)
    lines = [
        "# 核心能力权威索引",
        "",
        "> 自动生成，请勿手工编辑。来源：[CAPABILITIES.json](CAPABILITIES.json)；生成：`python scripts/sync_capability_docs.py`。",
        "",
        "每个能力的完整行为规则、边界和已落地范围只在其“唯一权威”文档中定义。其他文档只链接，不复制第二套规则。",
        "当前运行事实见 [STATUS](../../STATUS.md)，持久架构决定见 [DECISIONS](../../DECISIONS.md)。",
        "",
    ]
    for group, title in GROUP_ORDER[:2]:
        lines.extend([f"## {title}", ""])
        for module in grouped[group]:
            lines.extend([
                f"### {module['name']}",
                "",
                f"- 唯一权威：{_link(Path(str(module['authority'])).name, str(module['authority']))}",
                f"- 职责边界：{module['summary']}",
                f"- 代码入口：{_path_links(module['code'])}",
                f"- 验证入口：{_path_links(module['tests'])}",
                f"- 关联模块：{_related_links(module, modules)}",
                "",
            ])
    lines.extend(["## 支撑模块", "", "| 模块（点击进入唯一权威） | 职责边界 |", "|---|---|"])
    for module in grouped["supporting"]:
        lines.append(
            f"| {_link(str(module['name']), str(module['authority']))} | "
            f"{module['summary']} |"
        )
    lines.extend([
        "",
        "## 维护规则",
        "",
        "- 行为变化先改对应权威文档；跨模块关系只改注册表，索引自动再生。",
        "- 新增核心能力必须提供唯一权威文件、marker、代码与测试入口，并加入关联模块。",
        "- `check_docs.py` 在权威文件缺失、重复、marker 漂移、代码/测试路径或相对链接失效、索引过期时失败。",
        "",
    ])
    return "\n".join(lines)


def expected_documents(root: Path = ROOT) -> tuple[dict[str, str], list[str]]:
    errors, registry = validate_registry(root)
    if errors or registry is None:
        return {}, errors
    return {
        PRODUCT_README: render_product_readme(registry),
        CAPABILITY_INDEX: render_capability_index(registry),
    }, []


def _relative_link_errors(root: Path, paths: list[str]) -> list[str]:
    errors: list[str] = []
    pattern = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
    for relative in paths:
        source = root / relative
        if not source.is_file():
            continue
        for raw in pattern.findall(source.read_text(encoding="utf-8")):
            target = raw.strip().strip("<>").split("#", 1)[0].split("?", 1)[0]
            if not target or "://" in target or target.startswith(("mailto:", "/")):
                continue
            resolved = (source.parent / unquote(target)).resolve()
            try:
                resolved.relative_to(root.resolve())
            except ValueError:
                errors.append(f"{relative} link escapes repository: {raw}")
                continue
            if not resolved.exists():
                errors.append(f"{relative} link target is missing: {raw}")
    return errors


def check_generated_documents(root: Path = ROOT) -> list[str]:
    documents, errors = expected_documents(root)
    if errors:
        return errors
    for relative, expected in documents.items():
        path = root / relative
        if not path.is_file():
            errors.append(f"missing generated capability document: {relative}")
        elif path.read_text(encoding="utf-8") != expected:
            errors.append(f"generated capability document is stale: {relative}")
    _, registry = validate_registry(root)
    if registry is not None:
        registry_paths = [str(module["authority"]) for module in registry["modules"]]
        errors.extend(_relative_link_errors(root, [
            *registry_paths, *documents, "START_HERE.md", "CONTENT_OS_EXECUTION_SPEC.md",
            "README.md", "AGENTS.md", "STATUS.md", "DECISIONS.md",
            "docs/architecture/SYSTEM_ARCHITECTURE.md", "docs/implementation/ROADMAP.md",
        ]))
    return errors


def sync(root: Path = ROOT) -> int:
    documents, errors = expected_documents(root)
    if errors:
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    for relative, content in documents.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        print(f"updated {relative}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate the capability authority index.")
    parser.add_argument("--check", action="store_true", help="fail if generated documents are stale")
    arguments = parser.parse_args()
    if arguments.check:
        errors = check_generated_documents()
        if errors:
            for error in errors:
                print(f"- {error}", file=sys.stderr)
            return 1
        print("Capability authority documents are current.")
        return 0
    return sync()


if __name__ == "__main__":
    sys.exit(main())
