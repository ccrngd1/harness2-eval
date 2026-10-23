#!/usr/bin/env bash
# Run a command in an environment stripped of every AWS credential and
# AWS_* variable on this host. Used for ALL local cdk/pytest/shellcheck/
# dry-run invocations so that nothing here can ever reach a real AWS API
# with ambient credentials (build-host hard constraint: no deploys, no
# mutating calls, host credentials never reach cdk or the scripts).
set -euo pipefail

INFRA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

exec env -i \
  PATH="${INFRA_DIR}/.venv/bin:${INFRA_DIR}/node_modules/.bin:/usr/local/bin:/usr/bin:/bin" \
  HOME="${HOME}" \
  LANG="${LANG:-C.UTF-8}" \
  TERM="${TERM:-dumb}" \
  AWS_CONFIG_FILE=/dev/null \
  AWS_SHARED_CREDENTIALS_FILE=/dev/null \
  AWS_EC2_METADATA_DISABLED=true \
  CDK_DISABLE_CLI_TELEMETRY=true \
  "$@"
