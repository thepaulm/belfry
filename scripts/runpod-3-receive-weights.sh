#!/usr/bin/env bash
# Step 3 of the RunPod retrain flow — RECEIVE the trained weights from
# the pod. RUN THIS ON THE ORIN, after training (step 2) finishes.
#
# First, ON THE POD (the train script prints this too):
#   cd /home/paulm/belfry-training
#   runpodctl send runs/detect/belfry-<version>/weights/best.pt
# That prints a code. Then here:
#   scripts/runpod-3-receive-weights.sh <CODE>
set -euo pipefail
CODE="${1:?usage: $0 <runpodctl-code-from-the-pod>}"

cd "$(dirname "$0")/.."
source scripts/runpod-version

runpodctl receive "$CODE"     # downloads best.pt into the cwd
mv -f best.pt "inference/belfry-${VERSION}.pt"
ls -lh "inference/belfry-${VERSION}.pt"

echo
echo "Pulled inference/belfry-${VERSION}.pt (older weights stay on disk as rollback)."
echo "Next: scripts/runpod-4-export-and-swap.sh — TRT-export on the Orin +"
echo "point cameras.yaml at the new weights + restart inference."
