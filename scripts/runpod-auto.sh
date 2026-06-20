#!/usr/bin/env bash
# One-command RunPod retrain: launch a pod, push the dataset, train, pull
# the weights back, DELETE the pod, then TRT-export + swap into inference.
# RUN THIS ON THE ORIN.
#
# This collapses the manual 4-step flow (runpod-1..4) into a single
# unattended run. The old scripts still exist for a hands-on / debugging
# run; this one is what you'd eventually put behind cron.
#
# It does NOT use a public IP or any exposed port:
#   - bulk file transfer  -> runpodctl send/receive (croc p2p, keyless,
#     scripted with a fixed --code)
#   - remote commands      -> runpod proxy SSH `exec` (ssh.runpod.io)
#
# === ONE-TIME SETUP (per machine) ===
#   1. Get an API key: https://www.runpod.io/console/user/settings
#   2. export RUNPOD_API_KEY=...      (or `runpodctl config --apiKey=...`,
#      which writes ~/.runpod/config.toml; for cron put it in the unit's
#      Environment / EnvironmentFile so it's present non-interactively)
#   3. Register your SSH key so proxy-SSH exec works:
#        runpodctl ssh add-key            # uploads ~/.ssh/id_ed25519.pub
#      (generate one first if needed: ssh-keygen -t ed25519)
#
# === PER RUN (prereqs, same as the manual flow) ===
#   - bump VERSION in scripts/runpod-version
#   - the script re-runs split-dataset.py for you (pass --no-split to skip)
#
# Usage:  scripts/runpod-auto.sh [--no-split] [--keep-pod] [--no-swap]
#   --no-split   don't re-run split-dataset.py (use the existing split)
#   --keep-pod   don't delete the pod at the end (debugging; YOU must
#                delete it later or it keeps billing)
#   --no-swap    pull weights but skip the TRT-export + cameras.yaml swap
#                (run scripts/runpod-4-export-and-swap.sh yourself later)
set -euo pipefail

cd "$(dirname "$0")/.."
REPO="$PWD"
source scripts/runpod-version

# ---- tunables -------------------------------------------------------------
# RTX PRO 6000 (Blackwell, 96GB) ~halves wall-clock vs a 4090 at ~2x/hr — a
# cost wash, and the result lands sooner. Needs the cuda12.8+ image below
# (Blackwell sm_120). Override per-run with BELFRY_GPU_ID; `runpodctl gpu list
# -o json` shows the exact gpuId strings (pass the long "NVIDIA ..." gpuId,
# NOT the short displayName). train-on-pod batch=32 still fits a 24GB 4090 if
# you fall back; bump batch to actually use the PRO 6000's VRAM.
GPU_ID="${BELFRY_GPU_ID:-NVIDIA RTX PRO 6000 Blackwell Server Edition}"
# A CUDA+torch image; train-on-pod.sh pip-installs ultralytics on top.
POD_IMAGE="${BELFRY_POD_IMAGE:-runpod/pytorch:2.8.0-py3.11-cuda12.8.1-cudnn-devel-ubuntu22.04}"
CONTAINER_DISK_GB="${BELFRY_POD_DISK_GB:-40}"        # torch image + dataset + run dir
MAX_HOURS="${BELFRY_POD_MAX_HOURS:-2}"               # server-side auto-terminate backstop
SSH_KEY="${BELFRY_SSH_KEY:-$HOME/.ssh/runpod}"        # pub is the key registered via `runpodctl ssh add-key`
# The proxy SSH username ("<podId>-<perPodSuffix>") and real readiness come
# from RunPod's GraphQL API (machine.podHostId / runtime), NOT runpodctl —
# `runpodctl ssh info`/`pod get` report "pod not ready"/uptime 0 indefinitely
# on secure-cloud pods even while SSH works, and the suffix is per-pod (a
# counter, not a key fingerprint) so it can't be hardcoded.
RUNPODCTL_VER="${BELFRY_RUNPODCTL_VER:-v2.3.0}"      # pin pod's runpodctl to match the Orin's (croc protocol)
REMOTE_DIR="/home/paulm/belfry-training"             # train-on-pod.sh cds here, hardcoded
POLL_S=30                                            # training log poll interval
# ---------------------------------------------------------------------------

NO_SPLIT=0; KEEP_POD=0; NO_SWAP=0; AUTO_VERSION=0; REUSE_POD=""
for a in "$@"; do case "$a" in
  --no-split)     NO_SPLIT=1 ;;
  --keep-pod)     KEEP_POD=1 ;;
  --no-swap)      NO_SWAP=1 ;;
  --auto-version) AUTO_VERSION=1 ;;   # for cron: VERSION=auto-<date> so each run
                                      # keeps its own weights (free rollback) and
                                      # doesn't need a hand-bumped runpod-version
  --pod-id=*)     REUSE_POD="${a#*=}" ;;   # reuse an existing pod (skip create).
                                           # Still deleted after the pull unless
                                           # you also pass --keep-pod.
  *) echo "unknown arg: $a" >&2; exit 2 ;;
esac; done
[ "$AUTO_VERSION" = 1 ] && VERSION="auto-$(date +%Y%m%d-%H%M)"

log() { echo -e "\n\033[1;36m== $* ==\033[0m"; }
die() { echo -e "\033[1;31mFATAL: $*\033[0m" >&2; exit 1; }

# Best-effort phone push via the inference venv's FCM one-shot. Never fails the
# run — a missing venv / unconfigured push just no-ops.
notify() {
  [ -x "$REPO/.venv-inference/bin/python" ] || return 0
  ( cd "$REPO" && .venv-inference/bin/python -m inference.notify \
      --title "belfry retrain" --body "$1" ) >/dev/null 2>&1 || true
}

# Single-flight: a cron fire can't overlap a still-running or manual run.
exec 9>"/tmp/belfry-retrain.lock"
flock -n 9 || die "another retrain holds the lock (/tmp/belfry-retrain.lock) — aborting"

# Read a dotted field out of JSON on stdin: jget .id  /  jget .desiredStatus
jget() { python3 -c '
import sys, json
d = json.load(sys.stdin)
for k in sys.argv[1].lstrip(".").split("."):
    if isinstance(d, list):
        d = d[int(k)]
    else:
        d = (d or {}).get(k) if isinstance(d, dict) else None
print("" if d is None else d)
' "$1"; }

# Remaining RunPod credit in USD, formatted to cents. Best-effort: a failed
# API call / parse prints "?" rather than tripping set -e.
runpod_balance() {
  local raw b
  raw="$(runpodctl user -o json 2>/dev/null | jget .clientBalance 2>/dev/null || true)"
  if [ -n "$raw" ]; then
    b="$(printf '%.2f' "$raw" 2>/dev/null || true)"
    [ -n "$b" ] && { echo "$b"; return 0; }
  fi
  echo "?"
}

# RunPod GraphQL — the only reliable source for the proxy SSH username and pod
# readiness (runpodctl's ssh-info/uptime telemetry is broken on secure cloud).
RUNPOD_KEY="${RUNPOD_API_KEY:-$(python3 -c "import re; m=re.search(r\"apikey\\s*=\\s*'([^']+)'\", open('$HOME/.runpod/config.toml').read()); print(m.group(1) if m else '')" 2>/dev/null)}"

# pod_query <pod-id> -> prints "<podHostId>\t<ready 0|1>" (host empty if unknown,
# ready=1 once runtime is non-null). podHostId is the full "<id>-<suffix>" SSH user.
pod_query() {
  curl -s "https://api.runpod.io/graphql?api_key=${RUNPOD_KEY}" \
       -H 'Content-Type: application/json' \
       -d '{"query":"query { myself { pods { id machine { podHostId } runtime { uptimeInSeconds } } } }"}' \
  | python3 -c "
import sys, json
pid = sys.argv[1]
try: d = json.load(sys.stdin)
except Exception: print('\t0'); sys.exit(0)
pods = (((d.get('data') or {}).get('myself') or {}).get('pods')) or []
p = next((x for x in pods if x.get('id') == pid), None)
host = ((p or {}).get('machine') or {}).get('podHostId') or ''
ready = '1' if (p or {}).get('runtime') is not None else '0'
print(f'{host}\t{ready}')
" "$1"; }

# ---- unified cleanup (installed before anything can fail) -----------------
# One trap for the whole run: tidy temp files, and once a pod exists, pull the
# training log on failure (before the pod is gone) then delete it, and push a
# phone notification with the outcome. Guards on POD_ID / SSH_READY so it's safe
# whether we die in preflight or mid-train.
POD_ID=""; STAGE=""; TAR=""; SSH_READY=0; DONE_OK=0
START_BALANCE=""; END_BALANCE=""   # RunPod credit snapshots (USD), filled below
cleanup() {
  local rc=$?
  rm -rf "$STAGE" "$TAR" 2>/dev/null || true
  if [ -n "$POD_ID" ]; then
    if [ "$rc" != 0 ] && [ "$SSH_READY" = 1 ]; then
      log "pulling train.log off the pod before teardown"
      pod_ssh "tail -n 200 $REMOTE_DIR/train.log 2>/dev/null" \
        > "$REPO/last-retrain.log" 2>/dev/null || true
    fi
    if [ "$KEEP_POD" = 1 ]; then
      echo -e "\n--keep-pod: leaving $POD_ID alive. Delete it: runpodctl pod delete $POD_ID"
    else
      log "deleting pod $POD_ID"
      runpodctl pod delete "$POD_ID" || echo "WARN: pod delete failed — check console for $POD_ID"
    fi
  fi
  if [ "$DONE_OK" = 1 ]; then
    if [ -n "$END_BALANCE" ] && [ "$END_BALANCE" != "?" ]; then
      notify "✅ ${VERSION} trained & deployed — RunPod credit \$$END_BALANCE left"
    else
      notify "✅ ${VERSION} trained & deployed"
    fi
  else
    notify "❌ ${VERSION} retrain FAILED (rc=$rc) — see last-retrain.log"
  fi
}
trap cleanup EXIT

# ---- preflight ------------------------------------------------------------
log "preflight (VERSION=$VERSION)"
[ -n "$RUNPOD_KEY" ] || die "no RunPod API key (RUNPOD_API_KEY env or apikey in ~/.runpod/config.toml). See setup at top."
command -v runpodctl >/dev/null || die "runpodctl not installed"
command -v curl >/dev/null || die "curl not installed (needed for the GraphQL API)"
[ -f "$REPO/yolo11l-headext.pt" ] || die "missing yolo11l-headext.pt (run scripts/extend-head.py)"
[ -d "$HOME/belfry-training/images" ] || die "no training images at ~/belfry-training/images"
[ -f "$SSH_KEY" ] || die "no SSH key at $SSH_KEY — ssh-keygen one and 'runpodctl ssh add-key'"

START_BALANCE="$(runpod_balance)"
log "RunPod credit at start: \$$START_BALANCE"

if [ "$NO_SPLIT" = 0 ]; then
  log "regenerating train/val split"
  python3 scripts/split-dataset.py
fi
[ -f "$HOME/belfry-training/dataset.train.yaml" ] || die "split-dataset.py produced no dataset.train.yaml"

# Bundle exactly like runpod-1: dataset tree + headext base + the train
# script with VERSION baked in, all landing at the root of $REMOTE_DIR.
STAGE="$(mktemp -d)"; TAR="/tmp/belfry-${VERSION}.tar"   # cleaned up by the EXIT trap
sed "s/^VERSION=.*/VERSION=${VERSION}/" scripts/runpod-2-train-on-pod.sh > "$STAGE/train-on-pod.sh"
tar cf "$TAR" -C "$HOME/belfry-training" . \
    -C "$REPO" yolo11l-headext.pt \
    -C "$STAGE" train-on-pod.sh
log "bundled dataset: $(du -h "$TAR" | cut -f1)"

# ---- launch (or reuse) pod ------------------------------------------------
if [ -n "$REUSE_POD" ]; then
  POD_ID="$REUSE_POD"
  log "reusing existing pod $POD_ID (won't be deleted)"
else
  log "creating pod ($GPU_ID)"
  TERMINATE_AT="$(date -u -d "+${MAX_HOURS} hours" +%Y-%m-%dT%H:%M:%SZ)"
  CREATE_JSON="$(runpodctl pod create \
    --name "belfry-${VERSION}" \
    --gpu-id "$GPU_ID" \
    --image "$POD_IMAGE" \
    --container-disk-in-gb "$CONTAINER_DISK_GB" \
    --ssh \
    --terminate-after "$TERMINATE_AT" \
    -o json)"
  POD_ID="$(echo "$CREATE_JSON" | jget .id)"
  [ -n "$POD_ID" ] || { echo "$CREATE_JSON" >&2; die "pod create returned no id"; }
  echo "pod id: $POD_ID  (auto-terminate backstop: $TERMINATE_AT)"
fi
# From here the EXIT trap (installed at the top) owns teardown: pod delete on
# any exit so a crash can't leave a GPU billing (unless --keep-pod / --pod-id).

# ---- SSH plumbing ---------------------------------------------------------
# Resolve the proxy username + readiness from GraphQL (machine.podHostId /
# runtime). runpodctl's ssh-info/uptime are broken on secure cloud; GraphQL is
# the only source that reports the real "<podId>-<suffix>" user and a non-null
# runtime once the container is up. Generous budget for a cold image pull.
log "resolving pod SSH via GraphQL (waiting for container)"
USER_HOST=""
for i in $(seq 1 120); do           # ~10 min @ 5s
  IFS=$'\t' read -r PODHOST READY < <(pod_query "$POD_ID")
  [ -n "$PODHOST" ] && [ "$READY" = "1" ] && { USER_HOST="${PODHOST}@ssh.runpod.io"; break; }
  echo "  [$i] host=${PODHOST:-?} ready=${READY:-?}"; sleep 5
done
[ -n "$USER_HOST" ] || die "pod never reported a ready runtime + podHostId via GraphQL"
echo "  ssh user: $USER_HOST"
SSH_OPTS=(-tt -i "$SSH_KEY" -o BatchMode=yes -o StrictHostKeyChecking=no
          -o UserKnownHostsFile=/dev/null -o ConnectTimeout=25 -o LogLevel=ERROR
          -o ServerAliveInterval=30 -o ServerAliveCountMax=10)

# pod_ssh "<remote command>" [timeout_s]
# RunPod's proxy SSH drops into an INTERACTIVE shell and ignores a command
# argument, so we feed the command on stdin. The shell echoes input + a prompt
# + a login banner, so we: silence prompt/echo (PS1='' ; stty -echo), bracket
# the real command with unique markers, strip ANSI/CR, and print only the lines
# between the markers (dropping any residual prompt echo). Exit code rides back
# in the END marker. This is the workaround for proxy SSH having no clean exec.
pod_ssh() {
  local cmd="$1" tmo="${2:-600}" nonce beg end raw
  nonce="B$$_${RANDOM}"; beg="__BFY_${nonce}_BEG__"; end="__BFY_${nonce}_END__"
  # `|| true`: a non-zero ssh/timeout must not trip set -e here — the real
  # remote exit code is recovered from the END marker by awk below. If the
  # connection died, awk finds no markers and returns 99.
  raw="$(printf '%s\n' "export PS1=''; stty -echo 2>/dev/null" "echo ${beg}" \
           "${cmd}" "echo ${end}\$?" "exit" \
        | timeout "$tmo" ssh "${SSH_OPTS[@]}" "$USER_HOST" 2>/dev/null \
        | sed -e 's/\x1b\[[0-9;?]*[a-zA-Z]//g' -e 's/\x1b\][^\x07]*\x07//g' -e 's/\r//g' || true)"
  awk -v b="^${beg}\$" -v e="^${end}[0-9]+\$" '
    $0 ~ e { match($0, /[0-9]+$/); rc=substr($0,RSTART,RLENGTH); inb=0 }
    inb && $0 !~ /^[A-Za-z0-9._-]+@[A-Za-z0-9._-]+:.*[#$] / { print }
    $0 ~ b { inb=1 }
    END    { exit (rc=="" ? 99 : rc) }
  ' <<<"$raw"
}

# GraphQL says the runtime is up; confirm the proxy actually answers SSH (it
# can briefly return "container not found" right after runtime goes live).
log "confirming proxy SSH exec"
for i in $(seq 1 18); do            # ~3 min
  pod_ssh "echo ssh-ok" 2>/dev/null | grep -q ssh-ok && break
  echo "  [$i] proxy not answering yet"; sleep 10
done
pod_ssh "echo ssh-ok" | grep -q ssh-ok || die "proxy SSH never answered despite ready runtime"
SSH_READY=1   # cleanup may now pull train.log off the pod on failure

# Match the pod's runpodctl to the Orin's — RunPod images ship an old build
# (1.14.x) whose croc protocol is incompatible with 2.3.x ("Malformed relay").
log "pinning pod runpodctl to $RUNPODCTL_VER"
pod_ssh "wget -qO /usr/local/bin/runpodctl https://github.com/runpod/runpodctl/releases/download/${RUNPODCTL_VER}/runpodctl-linux-amd64 && chmod +x /usr/local/bin/runpodctl && hash -r && runpodctl version" \
  | sed 's/^/    pod runpodctl: /' || die "could not upgrade pod runpodctl"

# ---- push dataset (croc) --------------------------------------------------
# croc rewrites a passed --code (appends a shard count), so we DON'T pin the
# code — we let `send` print one and parse it from its output, then hand that
# exact code to the pod's receiver.
log "pushing dataset to pod"
TARNAME="$(basename "$TAR")"
SENDLOG="$(mktemp)"
runpodctl send "$TAR" >"$SENDLOG" 2>&1 &
SEND_PID=$!
CODE_DS=""
for i in $(seq 1 30); do
  # `|| true`: an empty log makes grep exit 1, which under set -e+pipefail
  # would silently kill the whole script on the first (pre-code) iteration.
  CODE_DS="$(grep -oE 'runpodctl receive [^ ]+' "$SENDLOG" 2>/dev/null | head -1 | awk '{print $3}' || true)"
  [ -n "$CODE_DS" ] && break
  kill -0 $SEND_PID 2>/dev/null || break   # sender died
  sleep 1
done
[ -n "$CODE_DS" ] || { cat "$SENDLOG" >&2; kill $SEND_PID 2>/dev/null||true; die "send never printed a code"; }
echo "  code: $CODE_DS"
# Receiver on the pod blocks until transfer completes; 20-min budget covers a
# slow residential uplink for the ~550 MB bundle.
pod_ssh "mkdir -p $REMOTE_DIR && cd $REMOTE_DIR && runpodctl receive $CODE_DS && tar xf $TARNAME && echo EXTRACTED_OK" 1200 \
  | sed 's/^/    /' || { kill $SEND_PID 2>/dev/null||true; rm -f "$SENDLOG"; die "dataset transfer/extract failed"; }
wait $SEND_PID 2>/dev/null || true
rm -f "$SENDLOG"

# ---- train (detached on pod, poll the log) --------------------------------
log "starting training on pod (detached)"
pod_ssh "cd $REMOTE_DIR && rm -f train.log train.rc && \
         setsid bash -c 'bash train-on-pod.sh > train.log 2>&1; echo \$? > train.rc' \
         </dev/null >/dev/null 2>&1 & echo launched" \
  | sed 's/^/    /' || die "could not launch training on pod"

log "training — polling every ${POLL_S}s (auto-terminates in ${MAX_HOURS}h max)"
RC=""
while true; do
  sleep "$POLL_S"
  RC="$(pod_ssh "cat $REMOTE_DIR/train.rc 2>/dev/null" || true)"
  pod_ssh "tail -n 3 $REMOTE_DIR/train.log 2>/dev/null" 2>/dev/null | sed 's/^/    | /' || true
  [ -n "$RC" ] && break
done
[ "$RC" = "0" ] || die "training failed on pod (rc=$RC) — inspect $REMOTE_DIR/train.log (use --keep-pod to keep it next time)"
log "training finished (rc=0)"

# ---- pull weights (croc) --------------------------------------------------
# Mirror of the push: the pod is the sender now. Launch `send` detached on the
# pod (it blocks until a receiver connects), parse the code it logs, then
# receive on the Orin.
log "pulling weights back"
WPATH="runs/detect/belfry-${VERSION}/weights/best.pt"
pod_ssh "cd $REMOTE_DIR && test -f $WPATH && rm -f send.log && setsid runpodctl send $WPATH >send.log 2>&1 </dev/null & echo SEND_LAUNCHED" \
  | sed 's/^/    /' || die "could not launch weight send on pod (missing $WPATH?)"
CODE_W=""
for i in $(seq 1 30); do
  CODE_W="$(pod_ssh "grep -oE 'runpodctl receive [^ ]+' $REMOTE_DIR/send.log | head -1 | awk '{print \$3}'" 2>/dev/null | tr -d '[:space:]' || true)"
  [ -n "$CODE_W" ] && break
  sleep 2
done
[ -n "$CODE_W" ] || die "pod send never printed a code (see $REMOTE_DIR/send.log)"
echo "  code: $CODE_W"
rm -f "$REPO/best.pt"
( cd "$REPO" && timeout 600 runpodctl receive "$CODE_W" >/dev/null 2>&1 ) || die "weights receive failed"
[ -f "$REPO/best.pt" ] || die "best.pt did not arrive"
mv -f "$REPO/best.pt" "inference/belfry-${VERSION}.pt"
ls -lh "inference/belfry-${VERSION}.pt"

# ---- delete the pod now ---------------------------------------------------
# Weights are home; the pod has nothing left to do. Delete it BEFORE the
# Orin-side TRT export + swap so we don't pay ~$2/hr for an idle GPU during
# those few minutes. Nulling POD_ID makes the EXIT trap skip a second delete.
# --keep-pod leaves it up (debugging); a failed delete leaves POD_ID set so the
# EXIT trap retries.
if [ "$KEEP_POD" = 1 ]; then
  echo "  --keep-pod: leaving $POD_ID up. Delete: runpodctl pod delete $POD_ID"
else
  log "deleting pod $POD_ID (weights pulled; no longer needed)"
  runpodctl pod delete "$POD_ID" && POD_ID="" || echo "WARN: delete failed; EXIT trap will retry"
fi

# ---- TRT export + swap ----------------------------------------------------
if [ "$NO_SWAP" = 1 ]; then
  log "done (--no-swap). Finish with: scripts/runpod-4-export-and-swap.sh"
else
  log "TRT-export + swap into inference"
  # runpod-4 stops/starts belfry-inference (needs sudo) and flips cameras.yaml.
  # Export VERSION so runpod-4 exports/deploys THIS run's model, not whatever
  # scripts/runpod-version holds (it would otherwise re-deploy the old v1.1).
  VERSION="$VERSION" scripts/runpod-4-export-and-swap.sh
fi

DONE_OK=1   # cleanup will push a success notification
log "ALL DONE — belfry-${VERSION} trained, pulled, and (unless --no-swap) live"

# Pod is already gone (deleted before the TRT export above), so this reflects
# the full charge for the run.
END_BALANCE="$(runpod_balance)"
if [ "$START_BALANCE" != "?" ] && [ "$END_BALANCE" != "?" ]; then
  SPENT="$(python3 -c "print(f'{${START_BALANCE}-${END_BALANCE}:.2f}')" 2>/dev/null || echo '?')"
  log "RunPod credit: \$$START_BALANCE → \$$END_BALANCE  (this run ≈ \$$SPENT)"
else
  log "RunPod credit at end: \$$END_BALANCE"
fi
