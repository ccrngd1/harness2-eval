#!/usr/bin/env bash
# run-arm.sh — arm one rollout window (NetOps §9 step 7 / §7 key flow).
#
#   run-arm.sh --run-id <n> [--region <region>] [--max-budget <usd>]
#              [--rpm <n>] [--tpm <n>] [--duration <e.g. 24h>]
#              [--eval-cmd <command run as eval on the guest>]
#              [--dry-run] [--help]
#
# Order is load-bearing:
#   1. snapshot pre-run-<n> of the guest root volume  (BEFORE any key exists)
#   2. generate a capped LiteLLM virtual key on the proxy (/key/generate)
#   3. place it in guest tmpfs /run/h2/keys.env (0600, owner eval)
#   4. phase run (opens :4000 and loads the empty run allowlist)
#   5. run the eval command (or wait for operator Ctrl-C)
# DISARM (EXIT/ERR trap, runs even on failure):
#   6. phase closed
#   7. wipe /run/h2/keys.env on the guest
#   8. /key/delete the virtual key on the gateway
#   9. export results + squid access log + LiteLLM spend log to the operator
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

usage() { sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0; }

RUN_ID=""
REGION_FLAG=""
MAX_BUDGET="100"          # per-run key cap in USD; must be <= remaining global cap
RPM="60"
TPM="200000"
DURATION="24h"
EVAL_CMD=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --run-id) RUN_ID="$2"; shift 2 ;;
    --region) REGION_FLAG="$2"; shift 2 ;;
    --max-budget) MAX_BUDGET="$2"; shift 2 ;;
    --rpm) RPM="$2"; shift 2 ;;
    --tpm) TPM="$2"; shift 2 ;;
    --duration) DURATION="$2"; shift 2 ;;
    --eval-cmd) EVAL_CMD="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --help|-h) usage ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done
[[ -n "$RUN_ID" ]] || die "--run-id is required (e.g. --run-id 1)"
[[ "$RUN_ID" =~ ^[A-Za-z0-9-]+$ ]] || die "--run-id must be alphanumeric/dashes"

REGION="$(resolve_region_operator "$REGION_FLAG")"

EIC_ID="<eic-endpoint-id>"
EVAL_INSTANCE="<eval-instance-id>"
if [[ "$DRY_RUN" -eq 0 ]]; then
  EIC_ID="$(stack_output H2Network EicEndpointId "$REGION")"
  EVAL_INSTANCE="$(stack_output H2Eval EvalInstanceId "$REGION")"
else
  say "[dry-run] would read stack outputs (EIC endpoint id, eval instance id)"
fi

guest_ssh() { remote "$EIC_ID" "$GUEST_IP" "$@"; }
proxy_ssh() { remote "$EIC_ID" "$PROXY_IP" "$@"; }

EXPORT_DIR="runs/run-$RUN_ID"
KEY_CREATED=0
ARMED=0

# ---------------------------------------------------------------------------
# Disarm trap: whatever happens after arming (eval crash, operator Ctrl-C,
# ssh drop on our side), the window is closed, the guest key is wiped and
# the gateway key is deleted. Runs on EXIT and ERR.
# ---------------------------------------------------------------------------
disarm() {
  local rc=$?
  trap - EXIT ERR
  say "== disarm (exit code so far: $rc)"
  if [[ "$ARMED" -eq 1 || "$KEY_CREATED" -eq 1 || "$DRY_RUN" -eq 1 ]]; then
    proxy_ssh sudo /usr/local/sbin/h2-egress closed || warn "h2-egress closed failed — close it by hand NOW"
    guest_ssh sudo shred -u /run/h2/keys.env || warn "guest keys.env wipe failed (may not exist)"
    if [[ "$DRY_RUN" -eq 1 ]]; then
      say "[dry-run] would POST /key/delete for the run key on the gateway (master key, localhost-side via ssh)"
    elif [[ "$KEY_CREATED" -eq 1 ]]; then
      proxy_ssh "sudo sh -c '. /etc/litellm/master-key.env && curl -sf -m 30 http://10.99.0.10:4000/key/delete -H \"Authorization: Bearer \$LITELLM_MASTER_KEY\" -H \"Content-Type: application/json\" -d \"{\\\"key_aliases\\\": [\\\"run-$RUN_ID\\\"]}\"'" \
        || warn "/key/delete failed — delete key alias run-$RUN_ID by hand"
    fi
    step_export || warn "log export failed — pull logs by hand before teardown"
  fi
  exit "$rc"
}

step_export() {
  run mkdir -p "$EXPORT_DIR"
  # Results (if the eval wrote any), squid access log, LiteLLM spend log.
  remote_copy "$EIC_ID" "$GUEST_IP" "/home/eval/results" "$EXPORT_DIR/" || true
  proxy_ssh "sudo sh -c 'cp /var/log/squid/access.log /tmp/squid-access-run-$RUN_ID.log && chown ubuntu /tmp/squid-access-run-$RUN_ID.log'" || true
  remote_copy "$EIC_ID" "$PROXY_IP" "/tmp/squid-access-run-$RUN_ID.log" "$EXPORT_DIR/" || true
  if [[ "$DRY_RUN" -eq 1 ]]; then
    say "[dry-run] would export LiteLLM spend via /spend/logs (master key) to $EXPORT_DIR/spend-run-$RUN_ID.json"
  else
    proxy_ssh "sudo sh -c '. /etc/litellm/master-key.env && curl -sf -m 60 http://10.99.0.10:4000/spend/logs -H \"Authorization: Bearer \$LITELLM_MASTER_KEY\"'" \
      > "$EXPORT_DIR/spend-run-$RUN_ID.json" || true
  fi
}

# --- 1. snapshot pre-run-<n> BEFORE the key exists ---------------------------
say "== snapshot pre-run-$RUN_ID (before any key is created)"
if [[ "$DRY_RUN" -eq 1 ]]; then
  run aws ec2 create-snapshot --region "$REGION" \
    --volume-id "<eval-root-volume-id>" \
    --description "harness2-eval pre-run-$RUN_ID" \
    --tag-specifications "ResourceType=snapshot,Tags=[{Key=project,Value=$PROJECT_TAG},{Key=h2-snapshot,Value=pre-run-$RUN_ID}]"
else
  VOL="$(aws ec2 describe-instances --region "$REGION" --instance-ids "$EVAL_INSTANCE" \
    --query 'Reservations[0].Instances[0].BlockDeviceMappings[0].Ebs.VolumeId' --output text)"
  SNAP="$(aws ec2 create-snapshot --region "$REGION" --volume-id "$VOL" \
    --description "harness2-eval pre-run-$RUN_ID" \
    --tag-specifications "ResourceType=snapshot,Tags=[{Key=project,Value=$PROJECT_TAG},{Key=h2-snapshot,Value=pre-run-$RUN_ID}]" \
    --query SnapshotId --output text)"
  say "waiting for snapshot $SNAP to complete..."
  aws ec2 wait snapshot-completed --region "$REGION" --snapshot-ids "$SNAP"
fi

trap disarm EXIT
trap disarm ERR

# --- 2. generate the capped virtual key on the gateway -----------------------
say "== generate virtual key (alias run-$RUN_ID, max_budget=\$$MAX_BUDGET, rpm=$RPM, tpm=$TPM, duration=$DURATION)"
KEYGEN_PAYLOAD='{"key_alias": "run-'"$RUN_ID"'", "duration": "'"$DURATION"'", "max_budget": '"$MAX_BUDGET"', "rpm_limit": '"$RPM"', "tpm_limit": '"$TPM"', "models": ["glm-5", "opus-4-8", "sonnet-4-6", "haiku-4-5", "gpt-oss-120b"], "allowed_routes": ["llm_api_routes"]}'
if [[ "$DRY_RUN" -eq 1 ]]; then
  say "[dry-run] would POST /key/generate on the gateway with: $KEYGEN_PAYLOAD"
  VIRTUAL_KEY="<virtual-key>"
else
  VIRTUAL_KEY="$(proxy_ssh "sudo sh -c '. /etc/litellm/master-key.env && curl -sf -m 30 http://10.99.0.10:4000/key/generate -H \"Authorization: Bearer \$LITELLM_MASTER_KEY\" -H \"Content-Type: application/json\" -d '\\''$KEYGEN_PAYLOAD'\\''' " \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["key"])')"
  [[ -n "$VIRTUAL_KEY" ]] || die "/key/generate returned no key"
fi
KEY_CREATED=1

# --- 3. place the key in guest tmpfs (never on disk) -------------------------
say "== inject key into guest tmpfs /run/h2/keys.env (0600, owner eval)"
if [[ "$DRY_RUN" -eq 1 ]]; then
  say "[dry-run] would write H2_VIRTUAL_KEY + gateway URL to /run/h2/keys.env via EIC ssh"
else
  printf 'H2_VIRTUAL_KEY=%s\nH2_GATEWAY=http://10.99.0.10:4000\n' "$VIRTUAL_KEY" \
    | guest_ssh 'sudo install -d -m 0755 /run/h2 && sudo tee /run/h2/keys.env >/dev/null && sudo chown eval:eval /run/h2/keys.env && sudo chmod 0600 /run/h2/keys.env'
fi

# --- 4. phase run -------------------------------------------------------------
say "== phase run (opens :4000; run allowlist is EMPTY)"
proxy_ssh sudo /usr/local/sbin/h2-egress run
ARMED=1

# --- 5. the rollout -----------------------------------------------------------
if [[ -n "$EVAL_CMD" ]]; then
  say "== running eval command as eval: $EVAL_CMD"
  guest_ssh su -s /bin/bash eval -c "$(printf '%q' "$EVAL_CMD")"
elif [[ "$DRY_RUN" -eq 1 ]]; then
  say "[dry-run] no --eval-cmd: would hold the window open until Ctrl-C"
else
  say "window is ARMED. No --eval-cmd given; run your eval over EIC now."
  say "Press Ctrl-C (or kill this script) to disarm."
  # Trap-covered sleep: Ctrl-C lands in disarm().
  while true; do sleep 60; done
fi

say "eval command finished; disarming via trap"
