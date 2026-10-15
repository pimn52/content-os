"""Bounded official LatentSync 1.5 CLI recipe with explicit local caches."""
import math

VAE_REVISION = "31f26fdeee1355a5c34592e401dd41e45d25a493"
VAE_CACHE = "hub/models--stabilityai--sd-vae-ft-mse"
REQUIRED_TALKING_ARTIFACTS = frozenset({
    "vendor/scripts/inference.py", "vendor/configs/unet/stage2.yaml",
    "vendor/configs/scheduler_config.json", "vendor/latentsync/utils/mask.png",
    "vendor/checkpoints/whisper/tiny.pt", "checkpoint/model.pt",
    f"hf/{VAE_CACHE}/refs/main", f"hf/{VAE_CACHE}/snapshots/{VAE_REVISION}/config.json",
    f"hf/{VAE_CACHE}/snapshots/{VAE_REVISION}/diffusion_pytorch_model.safetensors",
    "torch/hub/checkpoints/s3fd-619a316812.pth", "torch/hub/checkpoints/2DFAN4-cd938726ad.zip",
    "tools/ffmpeg.exe", "tools/ffprobe.exe",
})


def validate_parameters(values):
    if set(values) != {"device", "inference_steps", "guidance_scale", "seed", "subtitle_crop_bottom_ratio", "terminal_face_closeout"}:
        raise ValueError("maintained_talking_recipe_invalid")
    if (values["device"] != "cuda:0" or type(values["inference_steps"]) is not int
        or not 1 <= values["inference_steps"] <= 1000 or type(values["seed"]) is not int or values["seed"] < 0
        or type(values["guidance_scale"]) not in (int, float) or not math.isfinite(values["guidance_scale"])
        or values["guidance_scale"] <= 0 or type(values["subtitle_crop_bottom_ratio"]) is not int
        or values["subtitle_crop_bottom_ratio"] != 0 or values["terminal_face_closeout"] is not False):
        raise ValueError("maintained_talking_recipe_invalid")
