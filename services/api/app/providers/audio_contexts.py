"""Audio transition comparison only, not an observation or loading authority.

A future guard must independently hash sources and query prediction/actual
contexts. Supplying these inputs to this comparator does not establish either.
Legacy loader/transition and diagnostic bundles are intentionally untouched.
"""
from pathlib import Path

from .audio_native_graph import TARGETS, SLOTS
from .native_load import NativeDependencyPlan
from .cpu_native_recipe import (PrivateRootPrediction, compare_private_root_association,
    compare_private_root_metadata)
from .common_controls_binding import CommonControlsBindingV2
from .component_contexts import normalized_binding
from .runtime_primitives import NativeExecutionUnsupported


def _pairs(rows):
    if type(rows) is not tuple or len(rows) > 64:
        raise ValueError()
    result = {}
    for item in rows:
        if type(item) is not tuple or len(item) != 2 or type(item[0]) is not str:
            raise ValueError()
        name, value = item
        if name in result or Path(name).is_absolute() or name != Path(name).as_posix() or '..' in Path(name).parts:
            raise ValueError()
        result[name] = value
    return result


def compare_audio_context_transition(before, after, plan, origins, root, predictions):
    """Require every loaded private node's exact source-associated metadata.

    Returns no receipt or authority. Unknown dynamic closure remains untouched.
    The snapshot producer still owns full CommonControls/source/OS validation.
    """
    try:
        if (type(plan) is not NativeDependencyPlan or plan.target not in TARGETS or
            not isinstance(root, Path) or not root.is_absolute() or
            type(before) is not tuple or len(before) != 2 or type(after) is not tuple or len(after) != 2 or
            type(origins) is not tuple or len(origins) > 2048 or
            any(not isinstance(path, Path) or not path.is_absolute() for path in origins) or
            len(set(origins)) != len(origins)):
            raise ValueError()
        effective, old = before[0], _pairs(before[1])
        current, new = after[0], _pairs(after[1])
        predicted = _pairs(predictions)
        nodes = {node.name: node for node in plan.nodes}
        if (not 1 <= len(nodes) <= len(SLOTS) or len(nodes) != len(plan.nodes) or
            set(nodes) - set(SLOTS) or plan.target not in nodes or
            current != effective or any(new.get(name) != binding for name, binding in old.items())):
            raise ValueError()
        private = {name: node for name, node in nodes.items()
            if node.resources.kind == 'requires_private_root_context'}
        common = {name: node for name, node in nodes.items()
            if node.resources.kind == 'requires_os_sxs_binding' and name != 'python.exe'}
        if set(predicted) - set(private) or set(new) - set(old) - set(private) - set(common):
            raise ValueError()
        for name in set(new) - set(old):
            if root / name not in origins: raise ValueError()
        for name, node in private.items():
            if root / name not in origins:
                if name in new and name not in old: raise ValueError()
                continue
            prediction = predicted.get(name)
            if (type(prediction) is not PrivateRootPrediction or name not in new or
                prediction.source != str(root / name) or prediction.source_parent != str((root / name).parent) or
                prediction.source_sha256 != node.sha256 or
                prediction.manifest_sha256 != node.resources.manifest_sha256 or
                prediction.metadata.root_manifest != prediction.source or
                Path(prediction.metadata.application_directory) != (root / name).parent):
                raise ValueError()
            compare_private_root_association(prediction, new[name])
            validated = compare_private_root_metadata(prediction.metadata, source=root / name,
                source_sha256=node.sha256, manifest_sha256=node.resources.manifest_sha256)
            if validated != prediction: raise ValueError()
        for name, node in common.items():
            if root / name not in origins: continue
            binding = new.get(name)
            if (type(binding) is not CommonControlsBindingV2 or
                type(effective) is not CommonControlsBindingV2 or
                binding.source_role != 'dll' or binding.query_role != 'associated' or
                binding.source_sha256 != node.sha256 or
                normalized_binding(binding) != normalized_binding(effective)):
                raise ValueError()
    except (ValueError, TypeError, AttributeError, KeyError):
        raise NativeExecutionUnsupported('execution_audio_context_transition_unsupported') from None
