#!/usr/bin/env bash
# Step 1 of the RunPod retrain flow — bundle dataset + base weights +
# the train script and SEND to the pod via runpodctl.
# RUN THIS ON THE ORIN. The RunPod proxy SSH can't pipe/scp, so we use
# runpodctl's peer-to-peer transfer (no API key, no direct TCP needed).
#
# Before running: bump VERSION in scripts/runpod-version, and re-run
# scripts/split-dataset.py if the training set changed since the last run
# (it almost certainly did — that's why you're retraining).
#
# This prints a one-time code and then BLOCKS waiting for the pod to
# receive. Leave it running. YOU then have manual steps on the pod — this
# script can't reach the pod, so the receiving side is all you:
#
#   1. ssh into the pod (separate terminal)
#   2. mkdir -p /home/paulm/belfry-training     <- MUST create this yourself:
#      cd /home/paulm/belfry-training              receive downloads into the
#                                                  cwd, and the train script
#                                                  cds to this exact path —
#                                                  don't use /workspace
#   3. runpodctl receive <CODE printed below>
#   4. tar xf belfry-<version>.tar
#   5. bash train-on-pod.sh                     <- trains, ~30-45 min on a 4090
#
# When training finishes, the train script prints the send command; then
# back on the Orin: scripts/runpod-3-receive-weights.sh <code> followed by
# scripts/runpod-4-export-and-swap.sh
set -euo pipefail

cd "$(dirname "$0")/.."
source scripts/runpod-version

# runpodctl is preinstalled on pods; install it on the Orin if missing (arm64).
if ! command -v runpodctl >/dev/null; then
  echo "installing runpodctl..."
  wget -qO /tmp/runpodctl \
    https://github.com/runpod/runpodctl/releases/latest/download/runpodctl-linux-arm64
  sudo install /tmp/runpodctl /usr/local/bin/runpodctl
fi

# Bake the version into the train script copy that rides along, so the
# pod side can't disagree with this side about names.
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
sed "s/^VERSION=.*/VERSION=${VERSION}/" scripts/runpod-2-train-on-pod.sh \
    > "$STAGE/train-on-pod.sh"

# One tarball: dataset tree (.) plus the base weights and train script,
# all landing at the root of /home/paulm/belfry-training on extraction.
TAR="/tmp/belfry-${VERSION}.tar"
tar cf "$TAR" -C ~/belfry-training . \
    -C ~/code/belfry yolo11l-headext.pt \
    -C "$STAGE" train-on-pod.sh
echo "bundled $(du -h "$TAR" | cut -f1) → sending..."
echo
echo "============================================================"
echo " YOUR TURN — on the POD (ssh in, separate terminal), run:"
echo
echo "   mkdir -p /home/paulm/belfry-training   # <- yes, create it yourself"
echo "   cd /home/paulm/belfry-training"
echo "   runpodctl receive <CODE-printed-below>"
echo "   tar xf belfry-${VERSION}.tar"
echo "   bash train-on-pod.sh"
echo
echo " (exact path matters: train-on-pod.sh cds to /home/paulm/belfry-training)"
echo "============================================================"
echo
runpodctl send "$TAR"
