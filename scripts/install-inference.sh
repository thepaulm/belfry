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
# devpi mirror that publishes Jetson-specific torch wheels and PROXIES
# the rest of PyPI under the same URL. We point pip's --index-url at it
# so the cpython upstream torch wheels (which carry a +cu130 local tag
# that sorts higher than the Jetson plain build) don't shadow the
# Jetson-built torch. The .dev domain alias does not resolve; use .io.
JETSON_INDEX="https://pypi.jetson-ai-lab.io/jp6/cu126"

if [[ ! -x "$PYTHON" ]]; then
    echo "$PYTHON not found — Jetson torch wheels need Python 3.10" >&2
    exit 1
fi

# --- venv ---------------------------------------------------------------
# Check for bin/activate, not just the directory: a failed first run
# (e.g. missing python3.10-venv apt package) can leave an empty
# .venv-inference dir behind that would otherwise short-circuit this.
if [[ ! -f "$VENV/bin/activate" ]]; then
    echo "==> creating $VENV"
    rm -rf "$VENV"
    if ! "$PYTHON" -m venv "$VENV"; then
        echo "venv creation failed — try: sudo apt install python3.10-venv" >&2
        exit 1
    fi
fi
# shellcheck source=/dev/null
source "$VENV/bin/activate"

pip install --upgrade pip wheel >/dev/null

# --- jetson torch -------------------------------------------------------
# Reinstall if torch is missing OR if it's installed but CUDA can't
# initialize — the latter happens when pip pulled the upstream cu130
# wheel from PyPI instead of the Jetson cu126 build. The check exits
# 0 only when torch.cuda is fully functional.
torch_ok=0
if python -c "import torch, sys; sys.exit(0 if torch.cuda.is_available() else 1)" 2>/dev/null; then
    torch_ok=1
fi
if [[ "$torch_ok" == "0" ]]; then
    echo "==> installing Jetson PyTorch from $JETSON_INDEX (a few minutes)"
    pip uninstall -y torch torchvision 2>/dev/null || true
    pip install --index-url "$JETSON_INDEX" torch torchvision
fi

python -c "
import torch
print('torch :', torch.__version__)
print('cuda  :', torch.cuda.is_available())
if torch.cuda.is_available():
    print('device:', torch.cuda.get_device_name(0))
else:
    raise SystemExit('torch installed but cuda init failed; aborting')
"

# --- ultralytics + cv2 --------------------------------------------------
if ! python -c "import ultralytics" 2>/dev/null; then
    echo "==> installing ultralytics + opencv-python + pyyaml"
    pip install --index-url "$JETSON_INDEX" \
        ultralytics opencv-python numpy pyyaml
fi

# --- onnx export deps ---------------------------------------------------
# Needed by ultralytics' TRT export pipeline (torch → ONNX → TensorRT).
# Ultralytics tries to AutoUpdate-install them on demand, but its
# install call hits upstream PyPI without an --index-url, so the
# aarch64-only onnxruntime-gpu fails to resolve and the export crashes.
# Install up front, splitting the call to dodge two failure modes:
#   - onnxruntime-gpu has no aarch64 wheel on PyPI; pull from Jetson lab
#   - onnx / onnxslim are cross-platform; the Jetson lab proxy has
#     occasionally returned zero-byte responses for them (devpi mirror
#     glitch), so pull straight from upstream PyPI to skip the proxy.
if ! python -c "import onnxruntime" 2>/dev/null; then
    echo "==> installing onnxruntime-gpu (Jetson aarch64 wheel)"
    pip install --index-url "$JETSON_INDEX" onnxruntime-gpu
fi
if ! python -c "import onnx, onnxslim" 2>/dev/null; then
    echo "==> installing onnx + onnxslim (upstream PyPI)"
    pip install onnx onnxslim
fi

# --- tensorrt (from JetPack, not pip) -----------------------------------
# Ultralytics needs the `tensorrt` Python module for the engine build.
# PyPI's tensorrt-cu12 only publishes x86_64 / Windows wheels; on
# Jetson, TensorRT comes from JetPack and lives in the system Python's
# dist-packages. Symlink the modules into the venv so our isolated
# Python 3.10 can `import tensorrt` without exposing all of system
# site-packages (which would risk numpy version conflicts).
SYS_DIST="/usr/lib/python3.10/dist-packages"
VENV_SITE="$VENV/lib/python3.10/site-packages"
if ! python -c "import tensorrt" 2>/dev/null; then
    echo "==> linking JetPack tensorrt into $VENV_SITE"
    for pkg in tensorrt tensorrt_dispatch tensorrt_lean; do
        for suffix in "" "-10.3.0.dist-info"; do
            src="$SYS_DIST/${pkg}${suffix}"
            dst="$VENV_SITE/${pkg}${suffix}"
            if [[ -e "$src" && ! -e "$dst" ]]; then
                ln -s "$src" "$dst"
            fi
        done
    done
fi
python -c "import tensorrt; print('tensorrt:', tensorrt.__version__)"

# --- numpy<2 pin --------------------------------------------------------
# The Jetson aarch64 onnxruntime-gpu wheel was compiled against
# NumPy 1.x; importing it under NumPy 2+ raises with "A module that
# was compiled using NumPy 1.x cannot be run in NumPy 2.2.6". torch,
# ultralytics, and opencv all support both, so pinning down works.
if ! python -c "import numpy, sys; sys.exit(0 if numpy.__version__.startswith('1.') else 1)" 2>/dev/null; then
    echo "==> pinning numpy<2 (onnxruntime-gpu was built against NumPy 1.x)"
    pip install 'numpy<2'
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
    echo "==> downloading MegaDetector v1000 (Larch / YOLO11L) weights"
    # MegaDetector v1000 ships five variants (cedar/larch/redwood/sorrel/
    # spruce). Larch is YOLO11L at 640 px — native Ultralytics loader,
    # 0.969 AP, fits well alongside YOLO11n COCO on the Orin's GPU.
    # Redwood (YOLOv5x6 @ 1280) is more accurate but ~5x heavier;
    # cedar (YOLOv9c) needs an extra yolov9pip dep we don't want.
    MD_URL="https://github.com/agentmorris/MegaDetector/releases/download/v1000.0/md_v1000.0.0-larch.pt"
    curl -L --fail -o inference/megadetector.pt "$MD_URL" || {
        echo "MegaDetector download failed. Fetch md_v1000.0.0-larch.pt manually from"
        echo "  https://github.com/agentmorris/MegaDetector/releases/tag/v1000.0"
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
YOLO('$pt').export(format='engine', half=True, device=0, imgsz=640)
"
}
build_engine inference/yolo11n.pt
build_engine inference/megadetector.pt

echo
echo "Done. Smoke-test:"
echo "  $VENV/bin/python -m inference.cli --cam cam6"
