#!/usr/bin/env bash
# Step 4 of the RunPod retrain flow — TRT-export the fine-tune on the
# Orin and swap it into inference. RUN THIS ON THE ORIN. The .engine is
# device-specific (built against the Orin's TensorRT), so this must run
# here, not on the pod.
#
# Flips cameras.yaml's two model-path lines to the new version. Older
# belfry-vX files stay on disk as the rollback: flip the lines back +
# sudo systemctl restart belfry-inference.service
set -euo pipefail

cd "$(dirname "$0")/.."
# Honor a VERSION inherited from the caller (runpod-auto.sh exports its
# possibly --auto-version value); only fall back to runpod-version when run
# standalone. Without this, an auto-version run would re-export/deploy the OLD
# v1.1 model instead of the one just trained.
[ -n "${VERSION:-}" ] || source scripts/runpod-version

# Stop inference first so the TRT engine build doesn't fight the running
# detector for the Jetson's (unified) GPU memory. Recording is a separate
# service and keeps running — we just miss new events during the ~few-min build.
sudo systemctl stop belfry-inference.service

# Build the FP16 TensorRT engine next to the .pt (~few min, uses the GPU).
.venv-inference/bin/yolo export model="inference/belfry-${VERSION}.pt" \
    format=engine half=True device=0
ls -lh "inference/belfry-${VERSION}.engine"

# Point cameras.yaml at the new weights (only the two model-path lines).
sed -i \
  -e "s|^\(\s*yolo_pt:\s*\).*|\1inference/belfry-${VERSION}.pt|" \
  -e "s|^\(\s*yolo_engine:\s*\).*|\1inference/belfry-${VERSION}.engine|" \
  cameras.yaml
grep -n "yolo_pt\|yolo_engine" cameras.yaml

# Swap in: start inference back up on the new engine.
sudo systemctl start belfry-inference.service
sleep 3
systemctl status belfry-inference.service --no-pager | head -12
echo
echo "Watch it load the new engine + class count:"
echo "  journalctl -u belfry-inference -n 30 --no-pager"
echo "Then verify on /events: new-version goals met (e.g. false positives"
echo "down), wildlife still firing, person/vehicle not regressed. Eyeball"
echo "dog/cat — freeze=10 drift risk."
