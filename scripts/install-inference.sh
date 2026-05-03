#!/usr/bin/env bash
# Provision Jetson Orin for the belfry inference pipeline:
#   1. Create .venv-inference/ on system Python 3.10 (matches the cp310
#      tag of NVIDIA's Jetson PyTorch wheels — the main DVR's Python
#      3.14 uv venv won't fit those).
#   2. Install Jetson torch + ultralytics + opencv-python.
#   3. Download MegaDetector v6 + YOLO11n weights.
#   4. Build TensorRT engines (one-time per device).
#
# Run from the repo root:   scripts/install-inference.sh
# Re-runnable; skips work that's already done.

set -euo pipefail

cd "$(dirname "$0")/.."

VENV=".venv-inference"
PYTHON="/usr/bin/python3.10"
JETSON_INDEX="https://pypi.jetson-ai-lab.dev/jp6/cu126"

if [[ ! -x "$PYTHON" ]]; then
    echo "$PYTHON not found — Jetson torch wheels need Python 3.10" >&2
    exit 1
fi

# --- venv ---------------------------------------------------------------
if [[ ! -d "$VENV" ]]; then
    echo "==> creating $VENV"
    "$PYTHON" -m venv "$VENV"
fi
# shellcheck source=/dev/null
source "$VENV/bin/activate"

pip install --upgrade pip wheel >/dev/null

# --- jetson torch -------------------------------------------------------
if ! python -c "import torch" 2>/dev/null; then
    echo "==> installing Jetson PyTorch (this can take a few minutes)"
    pip install --extra-index-url "$JETSON_INDEX" torch torchvision
fi

python -c "
import torch
print('torch :', torch.__version__)
print('cuda  :', torch.cuda.is_available())
if torch.cuda.is_available():
    print('device:', torch.cuda.get_device_name(0))
"

# --- ultralytics + cv2 --------------------------------------------------
if ! python -c "import ultralytics" 2>/dev/null; then
    echo "==> installing ultralytics + opencv-python + pyyaml"
    pip install ultralytics opencv-python numpy pyyaml
fi

# --- weights ------------------------------------------------------------
mkdir -p inference

if [[ ! -f inference/yolo11n.pt ]]; then
    echo "==> downloading YOLO11n COCO weights"
    # ultralytics will fetch + cache yolo11n.pt on first construction.
    python -c "
from ultralytics import YOLO
import shutil
m = YOLO('yolo11n.pt')
shutil.copy(m.ckpt_path, 'inference/yolo11n.pt')
"
fi

if [[ ! -f inference/megadetector.pt ]]; then
    echo "==> downloading MegaDetector v6 (YOLOv9-c) weights"
    # MegaDetector v6 is published under agentmorris/MegaDetector releases.
    # If this URL 404s, the latest known-good URL is in the README of
    # https://github.com/agentmorris/MegaDetector
    MD_URL="https://github.com/agentmorris/MegaDetector/releases/download/v6.0.0/MDV6-yolov9-c.pt"
    curl -L --fail -o inference/megadetector.pt "$MD_URL" || {
        echo "MegaDetector download failed. Fetch MDV6-yolov9-c.pt manually from"
        echo "  https://github.com/agentmorris/MegaDetector/releases"
        echo "and drop it at inference/megadetector.pt, then re-run."
        exit 1
    }
fi

# --- tensorrt engines ---------------------------------------------------
# Engines are device-specific so they can't be checked into the repo;
# build once per Orin. Skip if the .engine already exists.
build_engine() {
    local pt="$1"
    local engine="${pt%.pt}.engine"
    if [[ -f "$engine" ]]; then
        echo "==> $engine already built, skipping"
        return
    fi
    echo "==> exporting $pt to TensorRT FP16 (1–2 min)"
    python -c "
from ultralytics import YOLO
YOLO('$pt').export(format='engine', half=True, device=0, imgsz=384)
"
}
build_engine inference/yolo11n.pt
build_engine inference/megadetector.pt

echo
echo "Done. Smoke-test:"
echo "  $VENV/bin/python -m inference.cli --cam cam6"
