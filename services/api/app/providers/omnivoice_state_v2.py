"""Source-bound component ownership and registered CPU precision checks."""
from .prepared import ExecutionPreparationError

ROTARY_BUFFERS = frozenset({"inv_freq", "original_inv_freq"})


def require_component_state(model, runtime, *, sampling_rate):
    try:
        torch = runtime.torch
        aux, llm = model.audio_tokenizer, model.llm
        rotary = llm.rotary_emb
        if (type(model) is not runtime.model_class or type(aux) is not runtime.audio_class
                or type(llm) is not runtime.llm_class or type(rotary) is not runtime.rotary_class
                or not all(isinstance(m, torch.nn.Module) for m in (model, aux, llm, rotary))
                or model.sampling_rate != sampling_rate or type(model.sampling_rate) is not int
                or model.training or aux.training or llm.training or rotary.training
                or model._asr_pipe is not None
                or str(model.device) != "cpu" or model.dtype != torch.float16
                or model.config.model_type != "omnivoice" or llm.config.model_type != "qwen3"
                or llm.config.rope_parameters.get("rope_type") != "default"
                or llm.config._attn_implementation != "eager"
                or model.config._attn_implementation != "eager"
                or aux.config._attn_implementation != "eager"
                or set(rotary._buffers) != ROTARY_BUFFERS or rotary._parameters
                or not ROTARY_BUFFERS <= rotary._non_persistent_buffers_set):
            raise ValueError()
        # Get owners from the actual registration tree, not names alone.
        modules = {}
        for name, module in model.named_modules(remove_duplicate=False):
            if (len(modules) >= 100_000 or type(name) is not str or len(name) > 1024
                    or name in modules or not isinstance(module, torch.nn.Module)):
                raise ValueError()
            modules[name] = module
        if (modules.get("") is not model or modules.get("audio_tokenizer") is not aux
                or modules.get("llm") is not llm or modules.get("llm.rotary_emb") is not rotary):
            raise ValueError()
        auxiliary_owners = {id(m) for n, m in modules.items()
            if n == "audio_tokenizer" or n.startswith("audio_tokenizer.")}
        names, identities, counts, rotary_seen = set(), {}, {"main": 0, "aux": 0}, set()
        ints = (torch.bool, torch.uint8, torch.int8, torch.int16, torch.int32, torch.int64)
        for kind, rows in (("parameter", model.named_parameters(recurse=True, remove_duplicate=False)),
                           ("buffer", model.named_buffers(recurse=True, remove_duplicate=False))):
            for row in rows:
                if len(names) >= 100_000:
                    raise ExecutionPreparationError("omnivoice_loaded_configuration_limit")
                if type(row) is not tuple or len(row) != 2:
                    raise ValueError()
                name, tensor = row
                if type(name) is not str or not name or len(name) > 1024 or name in names:
                    raise ValueError()
                names.add(name)
                owner_name, _, local_name = name.rpartition(".")
                owner = modules.get(owner_name)
                registered = getattr(owner, "_parameters" if kind == "parameter" else "_buffers", {})
                if registered.get(local_name) is not tensor or not torch.is_tensor(tensor):
                    raise ValueError()
                if (torch.nn.parameter.is_lazy(tensor) or tensor.is_meta or tensor.is_complex()
                        or getattr(tensor, "is_quantized", False) or str(tensor.device) != "cpu"):
                    raise ValueError()
                role = "aux" if owner_name == "audio_tokenizer" or owner_name.startswith("audio_tokenizer.") else "main"
                # Verify the same owner is reachable through the auxiliary object.
                if role == "aux":
                    suffix = owner_name[len("audio_tokenizer"):].lstrip(".")
                    if (aux if not suffix else aux.get_submodule(suffix)) is not owner:
                        raise ValueError()
                elif id(owner) in auxiliary_owners:
                    raise ValueError()
                if id(tensor) in identities and identities[id(tensor)] != role:
                    raise ValueError()
                identities[id(tensor)] = role
                special = owner is rotary and local_name in ROTARY_BUFFERS
                if special:
                    if kind != "buffer" or owner_name != "llm.rotary_emb":
                        raise ValueError()
                    rotary_seen.add(local_name)
                expected = torch.float32 if role == "aux" or special else torch.float16
                if tensor.is_floating_point():
                    if tensor.dtype != expected:
                        raise ValueError()
                elif special or kind != "buffer" or tensor.dtype not in ints:
                    raise ValueError()
                if kind == "parameter":
                    counts[role] += 1
        if not all(counts.values()) or rotary_seen != ROTARY_BUFFERS:
            raise ValueError()
    except ExecutionPreparationError:
        raise
    except Exception:
        raise ExecutionPreparationError("omnivoice_loaded_component_configuration_changed") from None
