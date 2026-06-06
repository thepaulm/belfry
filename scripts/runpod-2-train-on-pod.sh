#!/usr/bin/env bash
# Step 2 of the RunPod retrain flow — train. RUNS ON THE RUNPOD POD, not
# the Orin. You don't invoke this from the repo: runpod-1-send-to-pod.sh
# bundles a copy (with VERSION baked in) into the dataset tarball as
# `train-on-pod.sh`, and you run that copy on the pod:
#   bash /home/paulm/belfry-training/train-on-pod.sh
set -euo pipefail

# Overwritten by runpod-1-send-to-pod.sh at bundle time from scripts/runpod-version.
VERSION=v1.1

cd /home/paulm/belfry-training

pip install ultralytics

# Retrain from the clean headext base (NOT from the previous belfry-vX.pt)
# so head drift doesn't compound across generations; the dataset is a
# superset of every earlier run, so nothing is lost by restarting.
#
# freeze=10 pins the backbone, so the 6 new wildlife channels do the learning
# and the base head channels drift only slightly. batch=32 fits a 4090's 24GB
# and is faster than 16. ~30-45 min. Watch the per-class val mAP in the epoch
# table — under-fed classes (coyote, squirrel) will lag; that's the data, not
# the run.
yolo detect train model=yolo11l-headext.pt data=dataset.train.yaml \
    epochs=100 imgsz=640 batch=32 freeze=10 name="belfry-${VERSION}"

echo
echo "Done. Send the weights back (prints a one-time code):"
echo "  runpodctl send runs/detect/belfry-${VERSION}/weights/best.pt"
echo "Then on the Orin:  scripts/runpod-3-receive-weights.sh <code>"
