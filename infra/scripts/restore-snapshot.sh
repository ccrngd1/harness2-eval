#!/usr/bin/env bash
# restore-snapshot.sh — roll the guest's root volume back to a snapshot
# (baseline-clean or pre-run-<n>) using EBS root-volume replacement.
#
#   restore-snapshot.sh --snapshot <snap-id | baseline-clean | pre-run-N>
#                       [--region <region>] [--dry-run] [--help]
#
# Uses create-replace-root-volume-task with --delete-replaced-root-volume:
# the instance reboots onto a fresh volume from the snapshot and the old
# (contaminated) volume is deleted by AWS — nothing to scrub by hand.
# The new volume inherits tags from the task's tag spec below.
#
# --dry-run: read-only lookups only; the replace task is printed.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

usage() { sed -n '2,14p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0; }

SNAPSHOT=""
REGION_FLAG=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --snapshot) SNAPSHOT="$2"; shift 2 ;;
    --region) REGION_FLAG="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --help|-h) usage ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done
[[ -n "$SNAPSHOT" ]] || die "--snapshot is required (snap-..., baseline-clean, or pre-run-N)"

REGION="$(resolve_region_operator "$REGION_FLAG")"

# Resolve a tag alias (baseline-clean / pre-run-N) to a snapshot ID.
if [[ "$SNAPSHOT" != snap-* ]]; then
  SNAP_ID="$(run_ro aws ec2 describe-snapshots --region "$REGION" --owner-ids self \
    --filters "Name=tag:h2-snapshot,Values=$SNAPSHOT" "Name=status,Values=completed" \
    --query 'sort_by(Snapshots,&StartTime)[-1].SnapshotId' --output text)"
  [[ -n "$SNAP_ID" && "$SNAP_ID" != "None" ]] || die "no completed snapshot tagged h2-snapshot=$SNAPSHOT"
  say "resolved $SNAPSHOT -> $SNAP_ID"
else
  SNAP_ID="$SNAPSHOT"
fi

EVAL_INSTANCE="$(run_ro aws ec2 describe-instances --region "$REGION" \
  --filters "Name=tag:project,Values=$PROJECT_TAG" "Name=private-ip-address,Values=$GUEST_IP" \
  "Name=instance-state-name,Values=running,stopped" \
  --query 'Reservations[0].Instances[0].InstanceId' --output text)"
[[ -n "$EVAL_INSTANCE" && "$EVAL_INSTANCE" != "None" ]] || die "cannot find the eval guest instance ($GUEST_IP)"

say "replacing root volume of $EVAL_INSTANCE from $SNAP_ID (old volume will be DELETED)"
run aws ec2 create-replace-root-volume-task --region "$REGION" \
  --instance-id "$EVAL_INSTANCE" \
  --snapshot-id "$SNAP_ID" \
  --delete-replaced-root-volume \
  --tag-specifications "ResourceType=volume,Tags=[{Key=project,Value=$PROJECT_TAG},{Key=h2-restored-from,Value=$SNAP_ID}]"

if [[ "$DRY_RUN" -eq 0 ]]; then
  say "task submitted. Watch it with:"
  say "  aws ec2 describe-replace-root-volume-tasks --region $REGION --filters Name=instance-id,Values=$EVAL_INSTANCE"
  say "After the instance is back: re-run smoke-isolation.sh (phase closed) before arming a new run."
fi
