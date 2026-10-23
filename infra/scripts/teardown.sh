#!/usr/bin/env bash
# teardown.sh — full sandbox teardown (NetOps §9 step 8, exact order).
#
#   teardown.sh [--account <12-digit>] [--region <region>]
#               [--credential-mode role|key] [--dry-run] [--help]
#
# Order (deviating from it can wedge cdk destroy or leak spend):
#   1. detach the Budget-action deny policy from the gateway principal
#      (if the Budget action fired, cdk destroy can't delete an attached
#      managed policy)
#   2. K4: key-mode only — verify the harness2-eval-bedrock user has NO
#      access keys left (CC deletes the hand-made key first); ABORT if
#      any remain
#   3. cdk destroy --all
#   4. delete leftover tagged snapshots, volumes, and the flow-log group
#      if CFN left them (log group has retention but DESTROY removal may
#      leave it on failed rollbacks)
#   5. verification pass: list anything still carrying project=harness2-eval
#   6. print manual-leftover checklist (EIP release confirmation, key-mode
#      IAM user, CloudWatch residue)
#
# --dry-run: READ-ONLY AWS calls only (describe/list/get). Every mutating
# call is printed, not executed.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

usage() { sed -n '2,26p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0; }

ACCOUNT_FLAG=""
REGION_FLAG=""
CRED_MODE="role"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --account) ACCOUNT_FLAG="$2"; shift 2 ;;
    --region) REGION_FLAG="$2"; shift 2 ;;
    --credential-mode) CRED_MODE="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --help|-h) usage ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done
case "$CRED_MODE" in role|key) ;; *) die "--credential-mode must be role or key" ;; esac

REGION="$(resolve_region_operator "$REGION_FLAG")"
# teardown's dry-run may resolve the account for real (read-only sts).
if [[ "$DRY_RUN" -eq 1 && -z "$ACCOUNT_FLAG" ]]; then
  ACCOUNT="$(run_ro aws sts get-caller-identity --query Account --output text)" \
    || die "cannot resolve account (sts failed)"
else
  SAVED_DRY=$DRY_RUN; DRY_RUN=0
  ACCOUNT="$(resolve_account_operator "$ACCOUNT_FLAG")"
  DRY_RUN=$SAVED_DRY
fi

if [[ "$DRY_RUN" -eq 0 ]]; then
  say "Tearing down harness2-eval in account $ACCOUNT / $REGION."
  read -r -p "Type the 12-digit account ID to continue: " TYPED
  [[ "$TYPED" == "$ACCOUNT" ]] || die "typed '$TYPED' does not match $ACCOUNT — aborting"
fi

# Physical names are CFN-generated; read them from stack outputs (these
# describe-stacks calls are read-only, fine under --dry-run).
DENY_POLICY_ARN="$(stack_output H2Guardrails DenyPolicyArn "$REGION" 2>/dev/null || true)"
GATEWAY_ROLE="$(stack_output H2Proxy GatewayRoleName "$REGION" 2>/dev/null || true)"

# --- 1. detach the deny policy (if the Budget action attached it) ------------
say "== 1. detach Budget-action deny policy (if attached)"
if [[ -z "$DENY_POLICY_ARN" || "$DENY_POLICY_ARN" == "None" ]]; then
  say "   H2Guardrails stack/output gone — nothing to detach"
elif [[ "$CRED_MODE" == "role" ]]; then
  if [[ -n "$GATEWAY_ROLE" && "$GATEWAY_ROLE" != "None" ]] \
     && run_ro aws iam list-attached-role-policies --role-name "$GATEWAY_ROLE" \
      --query 'AttachedPolicies[].PolicyArn' --output text 2>/dev/null | grep -q "$DENY_POLICY_ARN"; then
    run aws iam detach-role-policy --role-name "$GATEWAY_ROLE" --policy-arn "$DENY_POLICY_ARN"
  else
    say "   deny policy not attached to role (or role already gone)"
  fi
else
  if run_ro aws iam list-attached-user-policies --user-name harness2-eval-bedrock \
      --query 'AttachedPolicies[].PolicyArn' --output text 2>/dev/null | grep -q "$DENY_POLICY_ARN"; then
    run aws iam detach-user-policy --user-name harness2-eval-bedrock --policy-arn "$DENY_POLICY_ARN"
  else
    say "   deny policy not attached to user (or user already gone)"
  fi
fi

# --- 2. K4: key mode must have zero access keys before destroy ---------------
say "== 2. K4 access-key check"
if [[ "$CRED_MODE" == "key" ]]; then
  KEYS_LEFT="$(run_ro aws iam list-access-keys --user-name harness2-eval-bedrock \
    --query 'AccessKeyMetadata[].AccessKeyId' --output text 2>/dev/null || true)"
  if [[ -n "$KEYS_LEFT" && "$KEYS_LEFT" != "None" ]]; then
    die "K4 FAILED: harness2-eval-bedrock still has access key(s): $KEYS_LEFT. CC must delete them by hand first (they were created by hand). Aborting teardown."
  fi
  say "   K4 OK: no access keys on harness2-eval-bedrock"
else
  say "   role mode: K4 not applicable"
fi

# --- 3. cdk destroy -----------------------------------------------------------
say "== 3. cdk destroy --all"
run npx cdk destroy --all --force --no-notices \
  -c "account=$ACCOUNT" -c "region=$REGION" -c "credentialMode=$CRED_MODE"

# --- 4. tagged leftovers: snapshots, volumes, flow-log group -------------------
say "== 4. delete tagged leftovers"
SNAPS="$(run_ro aws ec2 describe-snapshots --region "$REGION" --owner-ids self \
  --filters "Name=tag:project,Values=$PROJECT_TAG" \
  --query 'Snapshots[].SnapshotId' --output text || true)"
for s in $SNAPS; do
  [[ "$s" == "None" ]] && continue
  run aws ec2 delete-snapshot --region "$REGION" --snapshot-id "$s"
done
[[ -z "${SNAPS// /}" || "$SNAPS" == "None" ]] && say "   no tagged snapshots"

VOLS="$(run_ro aws ec2 describe-volumes --region "$REGION" \
  --filters "Name=tag:project,Values=$PROJECT_TAG" "Name=status,Values=available" \
  --query 'Volumes[].VolumeId' --output text || true)"
for v in $VOLS; do
  [[ "$v" == "None" ]] && continue
  run aws ec2 delete-volume --region "$REGION" --volume-id "$v"
done
[[ -z "${VOLS// /}" || "$VOLS" == "None" ]] && say "   no orphaned tagged volumes"

if run_ro aws logs describe-log-groups --region "$REGION" \
    --log-group-name-prefix /harness2-eval/flow-logs \
    --query 'logGroups[].logGroupName' --output text 2>/dev/null | grep -q flow-logs; then
  run aws logs delete-log-group --region "$REGION" --log-group-name /harness2-eval/flow-logs
else
  say "   flow-log group already gone"
fi

# --- 5. verification pass ------------------------------------------------------
say "== 5. verification: anything still tagged project=$PROJECT_TAG?"
LEFT="$(run_ro aws resourcegroupstaggingapi get-resources --region "$REGION" \
  --tag-filters "Key=project,Values=$PROJECT_TAG" \
  --query 'ResourceTagMappingList[].ResourceARN' --output text || true)"
if [[ -n "${LEFT// /}" && "$LEFT" != "None" ]]; then
  warn "resources still tagged $PROJECT_TAG (CFN deletion may still be in flight, or leftovers):"
  printf '%s\n' "$LEFT"
else
  say "   clean: nothing tagged $PROJECT_TAG remains"
fi

# --- 6. manual checklist --------------------------------------------------------
cat <<'EOF'
== 6. manual leftovers to verify by hand:
   [ ] EIP released (it was CFN-managed; confirm no allocation remains:
       aws ec2 describe-addresses)
   [ ] key mode: IAM user harness2-eval-bedrock deleted by CFN; if destroy
       failed on it, delete login profiles/keys by hand then re-run destroy
   [ ] CloudWatch: no alarms/dashboards referencing harness2-eval
   [ ] AWS Budgets console: harness2-eval budget gone
   [ ] Local: shred any exported run logs you no longer need (runs/)
EOF
say "teardown finished."
