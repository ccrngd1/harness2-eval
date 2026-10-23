#!/usr/bin/env bash
# Local credential-free synth helper. Reads ALL context (including the
# placeholder account, which may only live under tests/fixtures/) from
# tests/fixtures/context.json and turns it into -c flags, so no account
# digits appear in the Makefile or anywhere else.
# Usage: dev/synth-local.sh [role|key] [extra cdk args...]
set -euo pipefail

INFRA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="${1:-role}"
shift || true
if [[ "$MODE" != "role" && "$MODE" != "key" ]]; then
  echo "usage: $0 [role|key] [extra cdk args...]" >&2
  exit 2
fi

FIXTURE="$INFRA_DIR/tests/fixtures/context.json"

mapfile -t CTX_ARGS < <(python3 - "$FIXTURE" "$MODE" <<'PYEOF'
import json, sys
ctx = json.load(open(sys.argv[1]))
ctx.pop("_comment", None)
ctx["credentialMode"] = sys.argv[2]
for key, val in ctx.items():
    if isinstance(val, (dict, list)):
        val = json.dumps(val, separators=(",", ":"))
    elif isinstance(val, bool):
        val = "true" if val else "false"
    print(f"{key}={val}")
PYEOF
)

ARGS=()
for kv in "${CTX_ARGS[@]}"; do
  ARGS+=(-c "$kv")
done

cd "$INFRA_DIR"
exec "$INFRA_DIR/dev/no-aws-env.sh" cdk synth --no-notices --quiet "${ARGS[@]}" "$@"
