#!/usr/bin/env bash
# smoke-harness2.sh — the NO-COST subset of PreCog §6.5 (items 1, 4, 5),
# run ON THE GUEST as `eval` after guest-bootstrap, phase `closed`.
# Items 2, 3, 6 need model keys and live in smoke-paid.sh.
set -euo pipefail

H2=/opt/h2
REPORT_DIR="${1:-/tmp}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
REPORT="$REPORT_DIR/smoke-harness2-$STAMP.txt"
FAIL=0

note() { printf '%s\n' "$*" | tee -a "$REPORT"; }
check() {
  # check <description> <cmd...>
  local desc="$1"; shift
  if "$@" >>"$REPORT" 2>&1; then
    note "PASS: $desc"
  else
    note "FAIL: $desc"
    FAIL=$((FAIL+1))
  fi
}

note "harness2 no-cost smoke test $STAMP (PreCog 6.5 items 1,4,5)"

# §6.5 item 1: harness2-demo with no credentials. The squid log check
# (zero allowed CONNECTs during the demo) happens operator-side in
# setup.sh right after this script.
check "harness2-demo (no credentials, phase closed)" \
  env -u AWS_BEARER_TOKEN_BEDROCK \
  "$H2/harness2/.venv/bin/python" "$H2/harness2/demo.py"

# §6.5 item 4: plugin smoke — a harness plugin that value-imports
# @opencode-ai/plugin must load offline (the cache was pre-seeded).
PLUGDIR="$(mktemp -d)/.opencode/plugin"
mkdir -p "$PLUGDIR"
cat > "$PLUGDIR/x.ts" <<'EOF'
import { tool } from "@opencode-ai/plugin"
export const noop = tool({ description: "noop", args: {}, async execute() { return "ok" } })
EOF
check "plugin value-import loads offline" \
  bun build --no-install "$PLUGDIR/x.ts" --outdir "$(mktemp -d)"

# §6.5 item 5: inside the jail, oracle/task material and the web are out
# of reach. (Each of these must FAIL; invert with !.)
jail_must_fail() {
  local desc="$1"; shift
  if h2-jail --workspace /tmp "$@" >/dev/null 2>&1; then
    note "FAIL: $desc succeeded inside jail"
    FAIL=$((FAIL+1))
  else
    note "PASS: $desc blocked inside jail"
  fi
}
TASKJSON="$(find "$H2/harvey-labs" -name task.json -print -quit 2>/dev/null || true)"
if [[ -n "$TASKJSON" ]]; then
  jail_must_fail "cat harvey-labs task.json" cat "$TASKJSON"
else
  note "WARN: no task.json found under $H2/harvey-labs to probe"
fi
jail_must_fail "curl https://huggingface.co" curl -sf -m 5 https://huggingface.co/
jail_must_fail "curl https://github.com" curl -sf -m 5 https://github.com/

note "RESULT: fail=$FAIL report=$REPORT"
[[ "$FAIL" -eq 0 ]]
