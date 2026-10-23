#!/usr/bin/env bash
# Shared helpers for the harness2-eval operator scripts (setup.sh,
# run-arm.sh, teardown.sh, restore-snapshot.sh). Sourced, not executed.
#
# Design rules enforced here:
#   * --dry-run for setup.sh means ZERO AWS calls of any kind; `run`
#     prints instead of executing. teardown/restore dry-runs may make
#     read-only calls via `run_ro` only.
#   * The resolved account is never hardcoded; see resolve_account_operator.
#   * Remote access is EIC open-tunnel as ProxyCommand with the
#     operator's own key (C7); IMDS-based SendSSHPublicKey is dead once
#     the guest's IMDS is off.

# Used by the sourcing scripts, not in this file itself.
# shellcheck disable=SC2034
PROXY_IP=10.99.0.10
# shellcheck disable=SC2034
GUEST_IP=10.99.0.70
PROJECT_TAG=harness2-eval

DRY_RUN=0
# Operator SSH identity for the EIC tunnels; scripts may override via
# --ssh-key before calling remote/remote_copy.
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519}"

say() { printf '%s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

run() {
  # Mutating or remote command. Printed (never executed) under --dry-run.
  if [[ "$DRY_RUN" -eq 1 ]]; then
    printf '[dry-run] %s\n' "$(printf '%q ' "$@")"
  else
    "$@"
  fi
}

run_ro() {
  # Read-only AWS call. setup.sh never uses this under --dry-run (its
  # dry-run makes zero AWS calls); teardown/restore use it for listing.
  "$@"
}

valid_account() { [[ "$1" =~ ^[0-9]{12}$ ]]; }

resolve_region_operator() {
  # $1 = explicit --region or empty
  if [[ -n "${1:-}" ]]; then
    printf '%s' "$1"
  elif [[ -n "${AWS_REGION:-}" ]]; then
    printf '%s' "$AWS_REGION"
  elif [[ -n "${AWS_DEFAULT_REGION:-}" ]]; then
    printf '%s' "$AWS_DEFAULT_REGION"
  else
    printf 'us-east-1'
  fi
}

resolve_account_operator() {
  # $1 = explicit --account or empty. Echoes the account; dies on
  # explicit/ambient mismatch (CC directive A). Under --dry-run no sts
  # call is made: an explicit value passes through, otherwise the
  # <resolved-at-runtime> placeholder.
  local explicit="${1:-}" ambient
  if [[ -n "$explicit" ]] && ! valid_account "$explicit"; then
    die "--account '$explicit' is not a 12-digit AWS account ID"
  fi
  if [[ "$DRY_RUN" -eq 1 ]]; then
    say "[dry-run] would run: aws sts get-caller-identity --query Account --output text" >&2
    printf '%s' "${explicit:-<resolved-at-runtime>}"
    return 0
  fi
  ambient="$(aws sts get-caller-identity --query Account --output text)" \
    || die "aws sts get-caller-identity failed; configure credentials"
  if [[ -n "$explicit" && "$ambient" != "$explicit" ]]; then
    die "--account $explicit does not match the credentials' account ($ambient). Aborting before any cdk call."
  fi
  printf '%s' "${explicit:-$ambient}"
}

budget_default_from_cdk_json() {
  # Single source of truth for the cap: the budgetCapUsd context default
  # in cdk.json (CC directive B). No second copy of the number here.
  local cdk_json="$1"
  python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['context']['budgetCapUsd'])" "$cdk_json"
}

eic_ssh_args() {
  # eic_ssh_args <eic-endpoint-id> <private-ip> — emits ssh -o args that
  # tunnel through the EIC endpoint. Caller supplies the identity file.
  local eic_id="$1" ip="$2"
  printf -- '-oProxyCommand=aws ec2-instance-connect open-tunnel --instance-connect-endpoint-id %s --private-ip-address %s --remote-port 22 -oStrictHostKeyChecking=accept-new' \
    "$eic_id" "$ip"
}

remote() {
  # remote <eic-id> <ip> <cmd...> — run a command on an instance as
  # ubuntu over the EIC tunnel. Honors --dry-run via `run`.
  local eic_id="$1" ip="$2"; shift 2
  local proxy_arg
  proxy_arg="$(eic_ssh_args "$eic_id" "$ip")"
  run ssh -i "$SSH_KEY" "$proxy_arg" "ubuntu@$ip" -- "$@"
}

remote_copy() {
  # remote_copy <eic-id> <ip> <local-path...> <remote-dest>
  local eic_id="$1" ip="$2"; shift 2
  local proxy_arg
  proxy_arg="$(eic_ssh_args "$eic_id" "$ip")"
  run scp -i "$SSH_KEY" "$proxy_arg" -r "${@:1:$#-1}" "ubuntu@$ip:${*: -1}"
}

stack_output() {
  # stack_output <stack> <output-key> [region] — read-only describe.
  local stack="$1" key="$2" region="${3:-us-east-1}"
  aws cloudformation describe-stacks --region "$region" --stack-name "$stack" \
    --query "Stacks[0].Outputs[?OutputKey=='$key'].OutputValue" --output text
}
