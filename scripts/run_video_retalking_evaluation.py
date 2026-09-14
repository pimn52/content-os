"""Run the isolated VideoReTalking ordinary-material evaluation.

This runner waits for the exact public checkpoint set, invokes the upstream
inference entry point with batch size one, and then records independent
container/audio QA through the existing Content OS evaluator. It deliberately
does not update Core state or claim the human Talking gate.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time


REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = REPO_ROOT / "content-os-data"
SOURCE_ROOT = DATA_ROOT / "video-retalking"
EVAL_ROOT = DATA_ROOT / "video-retalking-evaluation-20260914"
CHECKPOINT_ROOT = DATA_ROOT / "video-retalking-checkpoints"
RUNTIME_PYTHON = DATA_ROOT / "video-retalking-runtime310" / "python.exe"
PROJECT_PYTHON = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
FFMPEG_ROOT = REPO_ROOT / "apps" / "renderer" / "node_modules" / "@remotion" / "compositor-win32-x64-msvc"

REQUIRED_CHECKPOINTS = (
    "30_net_gen.pth",
    "DNet.pt",
    "ENet.pth",
    "expression.mat",
    "face3d_pretrain_epoch_20.pth",
    "GFPGANv1.3.pth",
    "GPEN-BFR-512.pth",
    "LNet.pth",
    "ParseNet-latest.pth",
    "RetinaFace-R50.pth",
    "shape_predictor_68_face_landmarks.dat",
    "BFM/01_MorphableModel.mat",
    "BFM/BFM_exp_idx.mat",
    "BFM/BFM_front_idx.mat",
    "BFM/BFM_model_front.mat",
    "BFM/Exp_Pca.bin",
    "BFM/facemodel_info.mat",
    "BFM/select_vertex_id.mat",
    "BFM/similarity_Lm3D_all.mat",
    "BFM/std_exp.txt",
)


def checkpoints_ready() -> bool:
    return all((CHECKPOINT_ROOT / item).is_file() and (CHECKPOINT_ROOT / item).stat().st_size > 0 for item in REQUIRED_CHECKPOINTS)


def main() -> int:
    deadline = time.monotonic() + 3_600
    while not checkpoints_ready():
        if time.monotonic() >= deadline:
            raise SystemExit("timed out waiting for VideoReTalking checkpoints")
        print("waiting for official VideoReTalking checkpoints", flush=True)
        time.sleep(15)

    output = EVAL_ROOT / "videoretalking-biyingjie-ordinary-take-02.mp4"
    environment = os.environ.copy()
    torch_cache = DATA_ROOT / "video-retalking-torch-cache"
    (torch_cache / "checkpoints").mkdir(parents=True, exist_ok=True)
    environment["TORCH_HOME"] = str(torch_cache)
    environment["XDG_CACHE_HOME"] = str(DATA_ROOT / "video-retalking-cache")
    environment["PYTHONPATH"] = os.pathsep.join(
        item for item in (str(SOURCE_ROOT), str(SOURCE_ROOT / "third_part"), str(SOURCE_ROOT / "third_part" / "GPEN"), str(SOURCE_ROOT / "third_part" / "GFPGAN"), environment.get("PYTHONPATH", "")) if item
    )
    environment["PATH"] = str(FFMPEG_ROOT) + os.pathsep + environment.get("PATH", "")

    inference = [
        str(RUNTIME_PYTHON),
        str(SOURCE_ROOT / "inference.py"),
        "--face", "reference-biyingjie-ordinary.mp4",
        "--audio", "driving-audio-gate-d-take-02.wav",
        # The upstream script calls os.makedirs(dirname(outfile)) and then
        # builds an unquoted ffmpeg command.  A dot-prefixed relative path
        # keeps dirname non-empty without breaking on the workspace spaces.
        "--outfile", f"./{output.name}",
        "--face_det_batch_size", "1",
        "--LNet_batch_size", "1",
        "--tmp_dir", "temp-ordinary",
        "--re_preprocess",
    ]
    completed = subprocess.run(inference, cwd=EVAL_ROOT, env=environment, check=False)
    if completed.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
        return completed.returncode or 1

    qa = [
        str(PROJECT_PYTHON), str(REPO_ROOT / "scripts" / "evaluate_talking_output.py"),
        "--video", str(output),
        "--target-audio", str(EVAL_ROOT / "driving-audio-gate-d-take-02.wav"),
        "--target-text", "第一，先写出一句能被记住的结论。",
        "--reference-audio", str(DATA_ROOT / "omnivoice-evaluation" / "references" / "reference-2-clean.wav"),
        "--reference-text", "大家好，今天我想跟大家讲一讲，我去年暑假来到了希腊，来学古希腊语。",
        "--authorization-reference", "u1-20260913-biyingjie-tim-internal",
        "--provider", "VideoReTalking",
        "--provider-model", "OpenTalker/video-retalking@official-checkpoints",
        "--ffmpeg", str(FFMPEG_ROOT / "ffmpeg.exe"),
        "--ffprobe", str(FFMPEG_ROOT / "ffprobe.exe"),
        "--qa-output", str(EVAL_ROOT / "videoretalking-biyingjie-ordinary-take-02.talking-qa.json"),
    ]
    return subprocess.run(qa, cwd=REPO_ROOT, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
