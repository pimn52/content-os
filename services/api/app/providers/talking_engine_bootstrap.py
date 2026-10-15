"""Python3.10 stdlib startup; no application imports or Conda activation."""
import json
import sys
import os
from pathlib import Path
import stat


def exact(path, directory=True):
    if (not path.is_absolute() or path != path.resolve(strict=True)
        or not (stat.S_ISDIR(path.lstat().st_mode) if directory else stat.S_ISREG(path.lstat().st_mode))):
        raise ValueError("maintained_talking_startup_path_invalid")


def distinct(pairs):
    value = {}
    for k, v in pairs:
        if k in value:
            raise ValueError("maintained_talking_startup_invalid")
        value[k] = v
    return value


def configure():
    if (sys.version_info[:2] != (3, 10) or len(sys.argv) != 2
        or any(getattr(sys.flags, k) != 1 for k in
            ("isolated", "no_site", "ignore_environment", "no_user_site", "dont_write_bytecode"))
        or "site" in sys.modules):
        raise ValueError("maintained_talking_startup_flags_invalid")
    archive, request = Path(sys.argv[0]), Path(sys.argv[1])
    exact(archive, False); exact(request, False)
    if request != archive.with_name("engine-request.json"):
        raise ValueError("maintained_talking_startup_invalid")
    raw = request.read_bytes()
    if not 0 < len(raw) <= 16384:
        raise ValueError("maintained_talking_startup_invalid")
    r = json.loads(raw, object_pairs_hook=distinct)
    if (set(r) != {"policy", "engine", "vendor", "work", "hf", "torch", "tools", "checkpoint", "steps", "guidance", "seed", "attention", "allocator"}
        or r["policy"] != "latentsync-dual-no-site-v1"
        or r["attention"] != "latentsync-retained-explicit-math-v1"
        or r["allocator"] != "expandable_segments:True"
        or type(r["steps"]) is not int or not 1 <= r["steps"] <= 1000
        or type(r["seed"]) is not int or r["seed"] < 0
        or type(r["guidance"]) not in (float, int) or not 0 < r["guidance"] < float("inf")):
        raise ValueError("maintained_talking_startup_invalid")
    for k in ("engine", "vendor", "work", "hf", "torch", "tools"):
        if type(r[k]) is not str:
            raise ValueError("maintained_talking_startup_invalid")
        exact(Path(r[k]))
    engine, vendor, work = (Path(r[k]) for k in ("engine", "vendor", "work"))
    if (Path(sys.executable) != engine/"python.exe" or Path.cwd() != work
        or work != archive.parent/"work" or engine == vendor):
        raise ValueError("maintained_talking_startup_interpreter_invalid")
    standard = [engine/"python310.zip", engine/"DLLs", engine/"Lib", engine]
    for p in standard[1:]: exact(p)
    if standard[0].exists(): exact(standard[0], False)
    expected = [str(archive), *(str(p) for p in standard)]
    # CPython3.10 on Windows emits "lib" even when the declared directory is
    # "Lib". Compare native filesystem paths, retaining the exact allowlist,
    # length and order; a spelling case is not an extra import root.
    if (len(sys.path) != len(expected)
        or any(type(actual) is not str or Path(actual) != Path(wanted)
               for actual,wanted in zip(sys.path,expected))):
        raise ValueError("maintained_talking_startup_search_invalid")
    for p in (engine/"Lib/site-packages", engine/"Library/bin", vendor/"scripts/inference.py"):
        exact(p, p.suffix != ".py")
    checkpoint = Path(r["checkpoint"]); exact(checkpoint, False)
    for name in ("reference.mp4", "narration-model.wav", "configs/unet/stage2.yaml", "configs/scheduler_config.json",
                 "checkpoints/whisper/tiny.pt", "latentsync/utils/mask.png"):
        exact(work/name, False)
    # Explicit private names are the only media operands seen by vendor shells.
    sys.path[:] = [*expected, str(engine/"Lib/site-packages"), str(vendor)]
    required_env = {"HF_HOME":r["hf"], "TORCH_HOME":r["torch"], "HF_HUB_OFFLINE":"1", "TRANSFORMERS_OFFLINE":"1",
        "PYTORCH_CUDA_ALLOC_CONF":r["allocator"], "PYTHONDONTWRITEBYTECODE":"1"}
    if any(os.environ.get(k) != v for k,v in required_env.items()):
        raise ValueError("maintained_talking_startup_environment_invalid")
    return r


if __name__ == "__main__":
    record = configure()
    # Standard Conda library registration, not a native-loader attestation.
    handle = os.add_dll_directory(str(Path(record["engine"])/"Library/bin")) if os.name == "nt" else None
    import torch
    from compatibility import explicit_scaled_dot_product_attention
    torch.nn.functional.scaled_dot_product_attention = explicit_scaled_dot_product_attention
    sys.argv = [str(Path(record["vendor"])/"scripts/inference.py"),
        "--unet_config_path", "configs/unet/stage2.yaml", "--inference_ckpt_path", record["checkpoint"],
        "--video_path", "reference.mp4", "--audio_path", "narration-model.wav", "--video_out_path", "generated.mp4",
        "--inference_steps", str(record["steps"]), "--guidance_scale", f'{record["guidance"]:g}', "--seed", str(record["seed"])]
    import runpy
    runpy.run_path(sys.argv[0], run_name="__main__")
