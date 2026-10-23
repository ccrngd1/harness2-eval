#!/usr/bin/env bash
# setup.sh — operator-side bring-up for the harness2-eval sandbox
# (NetOps §9 steps 1-6). Run from the operator workstation with AWS
# credentials; everything on-instance happens over EC2 Instance Connect.
#
#   setup.sh [--account <12-digit>] [--region <region>]
#            [--budget <usd>] [--credential-mode role|key]
#            [--ssh-key <path>] [--state <file>] [--dry-run] [--help]
#
# Account/region resolution (CC directive A): explicit flag wins; else
# ambient (sts get-caller-identity / AWS_REGION); explicit-vs-ambient
# mismatch ABORTS before any cdk call. A typed account confirmation is
# required before deploying.
#
# --dry-run prints every command it would run and makes ZERO AWS calls.
#
# Idempotent: progress is recorded in a state file (default
# .h2-setup-state in the repo's infra/ dir); re-running resumes after
# the last completed step. Delete the state file to start over.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="$(dirname "$SCRIPT_DIR")"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

usage() {
  sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 0
}

ACCOUNT_FLAG=""
REGION_FLAG=""
BUDGET_FLAG=""
CRED_MODE="role"
SSH_KEY="$HOME/.ssh/id_ed25519"
STATE_FILE="$INFRA_DIR/.h2-setup-state"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --account) ACCOUNT_FLAG="$2"; shift 2 ;;
    --region) REGION_FLAG="$2"; shift 2 ;;
    --budget) BUDGET_FLAG="$2"; shift 2 ;;
    --credential-mode) CRED_MODE="$2"; shift 2 ;;
    --ssh-key) SSH_KEY="$2"; shift 2 ;;
    --state) STATE_FILE="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --help|-h) usage ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done
case "$CRED_MODE" in role|key) ;; *) die "--credential-mode must be role or key" ;; esac

# Budget: the ONLY default lives in cdk.json context (CC directive B).
BUDGET="${BUDGET_FLAG:-$(budget_default_from_cdk_json "$INFRA_DIR/cdk.json")}"
[[ "$BUDGET" =~ ^[0-9]+$ && "$BUDGET" -gt 0 ]] || die "--budget must be a positive integer (got '$BUDGET')"

REGION="$(resolve_region_operator "$REGION_FLAG")"
ACCOUNT="$(resolve_account_operator "$ACCOUNT_FLAG")"

# ---------------------------------------------------------------------------
# Confirmation gate. The operator must see exactly which account is about
# to get a sandbox, and type the account ID back. Skipped under --dry-run
# (which changes nothing anyway) or when H2_SETUP_ASSUME_YES matches the
# resolved account (for the stubbed script tests only).
# ---------------------------------------------------------------------------
if [[ "$DRY_RUN" -eq 0 ]]; then
  CALLER_ARN="$(aws sts get-caller-identity --query Arn --output text)"
  say "============================================================"
  say " harness2-eval sandbox setup"
  say "   account : $ACCOUNT"
  say "   region  : $REGION"
  say "   caller  : $CALLER_ARN"
  say "   budget  : \$$BUDGET model-spend cap"
  say "   mode    : $CRED_MODE"
  say ""
  say " This deploys an ISOLATED EVAL SANDBOX (VPC, 2 instances, EIP,"
  say " IAM role/user, AWS Budget with automatic deny action) into the"
  say " account above. Make sure this is the sandbox account, NOT a"
  say " production account."
  say "============================================================"
  if [[ "${H2_SETUP_ASSUME_YES:-}" == "$ACCOUNT" ]]; then
    say "(confirmation satisfied via H2_SETUP_ASSUME_YES)"
  else
    read -r -p "Type the 12-digit account ID to continue: " TYPED
    [[ "$TYPED" == "$ACCOUNT" ]] || die "typed '$TYPED' does not match resolved account $ACCOUNT — aborting"
  fi
else
  say "[dry-run] would print account/region/caller ARN and require the"
  say "[dry-run] operator to type the account ID back before continuing"
fi

# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------
touch "$STATE_FILE"
done_step() { grep -qxF "$1" "$STATE_FILE"; }
mark_step() {
  if [[ "$DRY_RUN" -eq 1 ]]; then
    say "[dry-run] would mark step done: $1"
  else
    printf '%s\n' "$1" >> "$STATE_FILE"
  fi
}
step() {
  # step <name> <fn> — run fn unless already recorded in the state file.
  local name="$1" fn="$2"
  if done_step "$name"; then
    say "== step $name: already done, skipping (state: $STATE_FILE)"
    return 0
  fi
  say "== step $name"
  "$fn"
  mark_step "$name"
}

# Stack outputs needed on the way. Loaded lazily (so a resumed run that
# skips the wait step still gets them). Under --dry-run these stay
# placeholders because fetching them would be an AWS call.
EIC_ID="<eic-endpoint-id>"
PROXY_INSTANCE="<proxy-instance-id>"
EVAL_INSTANCE="<eval-instance-id>"
DEFAULT_SG="<default-sg-id>"
OUTPUTS_LOADED=0
load_outputs() {
  [[ "$OUTPUTS_LOADED" -eq 1 ]] && return 0
  OUTPUTS_LOADED=1
  if [[ "$DRY_RUN" -eq 1 ]]; then
    say "[dry-run] would read stack outputs (EIC endpoint, instance IDs, default SG) via cloudformation describe-stacks"
    return 0
  fi
  EIC_ID="$(stack_output H2Network EicEndpointId "$REGION")"
  PROXY_INSTANCE="$(stack_output H2Proxy ProxyInstanceId "$REGION")"
  EVAL_INSTANCE="$(stack_output H2Eval EvalInstanceId "$REGION")"
  DEFAULT_SG="$(stack_output H2Network DefaultSgId "$REGION")"
}

guest_ssh() { load_outputs; remote "$EIC_ID" "$GUEST_IP" "$@"; }
proxy_ssh() { load_outputs; remote "$EIC_ID" "$PROXY_IP" "$@"; }

# --- step 1: cdk deploy ----------------------------------------------------
step_deploy() {
  local cdk_args=(
    --no-notices --require-approval never
    -c "account=$ACCOUNT" -c "region=$REGION"
    -c "budgetCapUsd=$BUDGET" -c "credentialMode=$CRED_MODE"
  )
  run npx cdk deploy --all "${cdk_args[@]}"
}

# --- step 2: wait for instances + cloud-init --------------------------------
step_wait() {
  load_outputs
  run aws ec2 wait instance-status-ok --region "$REGION" \
    --instance-ids "$PROXY_INSTANCE" "$EVAL_INSTANCE"
  # cloud-init must have fully finished before we disable it (N.3 order).
  proxy_ssh sudo cloud-init status --wait
  guest_ssh sudo cloud-init status --wait
}

# --- step 3: harden instances (N.3: disable cloud-init BEFORE IMDS) ---------
step_harden() {
  # Both instances: /etc/cloud/cloud-init.disabled so a reboot can't
  # re-run user-data after we cut IMDS / rotate state.
  proxy_ssh sudo touch /etc/cloud/cloud-init.disabled
  guest_ssh sudo touch /etc/cloud/cloud-init.disabled
  # Guest only: IMDS off entirely (C3). The proxy keeps IMDSv2 hop-1
  # (its role credentials come from IMDS; nftables owner-match guards it).
  run aws ec2 modify-instance-metadata-options --region "$REGION" \
    --instance-id "$EVAL_INSTANCE" --http-endpoint disabled
}

# --- step 4: revoke default SG rules (R9 deviation) --------------------------
step_default_sg() {
  load_outputs
  # restrictDefaultSecurityGroup=false in the app (no Lambda/custom
  # resource allowed, R1); the equivalent hardening happens here instead.
  # Recorded as a deviation in README/NETOPS-CHECKLIST.
  run aws ec2 revoke-security-group-ingress --region "$REGION" \
    --group-id "$DEFAULT_SG" \
    --ip-permissions '[{"IpProtocol":"-1","UserIdGroupPairs":[{"GroupId":"'"$DEFAULT_SG"'"}]}]' || true
  run aws ec2 revoke-security-group-egress --region "$REGION" \
    --group-id "$DEFAULT_SG" \
    --ip-permissions '[{"IpProtocol":"-1","IpRanges":[{"CidrIp":"0.0.0.0/0"}]}]' || true
}

# --- step 5: install proxy stack (squid + nftables + LiteLLM) ----------------
step_proxy_bootstrap() {
  load_outputs
  remote_copy "$EIC_ID" "$PROXY_IP" \
    "$SCRIPT_DIR/proxy/proxy-bootstrap.sh" \
    "$SCRIPT_DIR/proxy/squid.conf" \
    "$SCRIPT_DIR/proxy/allow-setup.txt" \
    "$SCRIPT_DIR/proxy/allow-setup-http80.txt" \
    "$SCRIPT_DIR/proxy/allow-run.txt" \
    "$SCRIPT_DIR/proxy/allow-closed.txt" \
    "$SCRIPT_DIR/proxy/h2-egress" \
    "$SCRIPT_DIR/proxy/litellm-config.template.yaml" \
    "$SCRIPT_DIR/proxy/litellm-requirements.lock" \
    /tmp/h2-proxy/
  proxy_ssh sudo bash /tmp/h2-proxy/proxy-bootstrap.sh \
    --budget "$BUDGET" --region "$REGION"
}

# --- step 5b (key mode only): place the access key on the proxy -------------
step_key_mode_credentials() {
  if [[ "$CRED_MODE" != "key" ]]; then
    say "(role mode: skipping access-key placement)"
    return 0
  fi
  # CC creates the access key for IAM user harness2-eval-bedrock BY HAND
  # (never in CDK/CFN) and exports it in the operator's shell only for
  # this step. It lands in tmpfs (/run/h2gw/aws.env, root 0600), never
  # on the proxy's disk; teardown K-checks verify deletion.
  if [[ "$DRY_RUN" -eq 1 ]]; then
    say "[dry-run] would read H2GW_ACCESS_KEY_ID/H2GW_SECRET_ACCESS_KEY from the operator env"
    say "[dry-run] would write them over EIC to proxy tmpfs /run/h2gw/aws.env (0600) and restart litellm"
    return 0
  fi
  : "${H2GW_ACCESS_KEY_ID:?key mode: export H2GW_ACCESS_KEY_ID (created by hand for harness2-eval-bedrock)}"
  : "${H2GW_SECRET_ACCESS_KEY:?key mode: export H2GW_SECRET_ACCESS_KEY}"
  printf 'AWS_ACCESS_KEY_ID=%s\nAWS_SECRET_ACCESS_KEY=%s\nAWS_REGION=%s\n' \
    "$H2GW_ACCESS_KEY_ID" "$H2GW_SECRET_ACCESS_KEY" "$REGION" \
    | proxy_ssh 'sudo install -d -m 0700 /run/h2gw && sudo tee /run/h2gw/aws.env >/dev/null && sudo chmod 0600 /run/h2gw/aws.env && sudo systemctl restart litellm'
}

# --- step 6: isolation smoke test (phase closed) — ABORT on failure ----------
step_smoke_closed() {
  load_outputs
  remote_copy "$EIC_ID" "$GUEST_IP" "$SCRIPT_DIR/guest/smoke-isolation.sh" /tmp/
  if [[ -n "${H2_TEST_FORCE_SMOKE_FAIL:-}" ]]; then
    # Test hook: the stubbed script tests use this to prove setup.sh
    # aborts (and does NOT mark the step done) when the smoke test fails.
    die "isolation smoke test FAILED (forced by H2_TEST_FORCE_SMOKE_FAIL) — aborting setup; instances left up for debugging"
  fi
  if ! guest_ssh sudo bash /tmp/smoke-isolation.sh --phase closed; then
    die "isolation smoke test FAILED — aborting setup before any software install; instances left up for debugging"
  fi
}

# --- step 7: open setup egress, bootstrap the guest, close again -------------
step_guest_bootstrap() {
  load_outputs
  proxy_ssh sudo /usr/local/sbin/h2-egress setup
  remote_copy "$EIC_ID" "$GUEST_IP" \
    "$SCRIPT_DIR/guest/guest-bootstrap.sh" \
    "$SCRIPT_DIR/guest/h2-jail" \
    "$SCRIPT_DIR/guest/smoke-harness2.sh" \
    "$SCRIPT_DIR/guest/smoke-paid.sh" \
    /tmp/h2-guest/
  guest_ssh sudo bash /tmp/h2-guest/guest-bootstrap.sh
  # Re-check isolation while the setup allowlist is live.
  guest_ssh sudo bash /tmp/smoke-isolation.sh --phase setup \
    || die "isolation smoke test FAILED in phase setup — aborting"
  proxy_ssh sudo /usr/local/sbin/h2-egress closed
}

# --- step 8: no-cost harness2 smoke + squid zero-CONNECT check ---------------
step_smoke_harness2() {
  # Timestamp fence so the log check below only sees demo-era lines.
  proxy_ssh "sudo sh -c 'wc -l < /var/log/squid/access.log'" > /tmp/h2-squid-mark 2>/dev/null \
    || { [[ "$DRY_RUN" -eq 1 ]] && echo 0 > /tmp/h2-squid-mark; }
  guest_ssh su -s /bin/bash eval -c '"bash /tmp/h2-guest/smoke-harness2.sh"' \
    || die "harness2 no-cost smoke FAILED — aborting"
  # §6.5 item 1: ZERO allowed CONNECTs may appear while the demo ran.
  if [[ "$DRY_RUN" -eq 1 ]]; then
    say "[dry-run] would verify squid access.log shows no TCP_TUNNEL/200 lines after the mark"
  else
    local mark allowed
    mark="$(cat /tmp/h2-squid-mark)"
    allowed="$(proxy_ssh "sudo sh -c 'tail -n +$((mark+1)) /var/log/squid/access.log'" | grep -c 'TCP_TUNNEL/200' || true)"
    [[ "$allowed" -eq 0 ]] || die "squid log shows $allowed allowed CONNECT(s) during the closed-phase demo — egress leak, aborting"
    say "squid log: zero allowed CONNECTs during demo (good)"
  fi
}

# --- step 9: baseline-clean snapshot of the guest root volume -----------------
step_snapshot_baseline() {
  if [[ "$DRY_RUN" -eq 1 ]]; then
    run aws ec2 create-snapshot --region "$REGION" \
      --volume-id "<eval-root-volume-id>" \
      --description "harness2-eval baseline-clean" \
      --tag-specifications "ResourceType=snapshot,Tags=[{Key=project,Value=$PROJECT_TAG},{Key=h2-snapshot,Value=baseline-clean}]"
    return 0
  fi
  load_outputs
  local vol
  vol="$(aws ec2 describe-instances --region "$REGION" --instance-ids "$EVAL_INSTANCE" \
    --query 'Reservations[0].Instances[0].BlockDeviceMappings[0].Ebs.VolumeId' --output text)"
  run aws ec2 create-snapshot --region "$REGION" --volume-id "$vol" \
    --description "harness2-eval baseline-clean" \
    --tag-specifications "ResourceType=snapshot,Tags=[{Key=project,Value=$PROJECT_TAG},{Key=h2-snapshot,Value=baseline-clean}]"
  run aws ec2 wait snapshot-completed --region "$REGION" --filters "Name=tag:h2-snapshot,Values=baseline-clean"
}

step deploy            step_deploy
step wait-instances    step_wait
step harden            step_harden
step default-sg        step_default_sg
step proxy-bootstrap   step_proxy_bootstrap
step key-credentials   step_key_mode_credentials
step smoke-closed      step_smoke_closed
step guest-bootstrap   step_guest_bootstrap
step smoke-harness2    step_smoke_harness2
step snapshot-baseline step_snapshot_baseline

say ""
say "setup complete. Phase is CLOSED; a baseline-clean snapshot exists."
say "Next: scripts/run-arm.sh to arm a rollout window."
