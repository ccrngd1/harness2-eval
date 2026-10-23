#!/usr/bin/env bash
# smoke-paid.sh — the PAID subset of PreCog §6.5 (items 2, 3, 6), run ON
# THE GUEST as `eval` during phase `run` with a virtual key present in
# /run/h2/keys.env. REFUSES to run without --budget-confirmed: every
# check here spends real model dollars.
set -euo pipefail

CONFIRMED=0
STRETCH=0
for arg in "$@"; do
  case "$arg" in
    --budget-confirmed) CONFIRMED=1 ;;
    --include-codex-stretch) STRETCH=1 ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done
if [[ "$CONFIRMED" -ne 1 ]]; then
  cat >&2 <<'EOF'
smoke-paid.sh makes PAID model calls (one short call per model).
Re-run with --budget-confirmed once CC has confirmed the budget cap is
live on the gateway (LiteLLM max_budget) and phase is `run`.
EOF
  exit 1
fi

KEYS=/run/h2/keys.env
if [[ ! -r "$KEYS" ]]; then
  echo "no $KEYS (tmpfs virtual key); run this inside run-arm.sh" >&2
  exit 1
fi
# shellcheck source=/dev/null
source "$KEYS"
: "${H2_VIRTUAL_KEY:?keys.env must set H2_VIRTUAL_KEY}"

GATEWAY=http://10.99.0.10:4000
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
REPORT="/tmp/smoke-paid-$STAMP.txt"
FAIL=0
note() { printf '%s\n' "$*" | tee -a "$REPORT"; }

# §6.5 item 2: one 1-step call per model ID through the gateway; token
# counts (and cache-read on Claude) must appear in the response usage.
MODELS=(glm-5 opus-4-8 sonnet-4-6 haiku-4-5 gpt-oss-120b)
for model in "${MODELS[@]}"; do
  resp="$(curl -sf -m 120 "$GATEWAY/v1/chat/completions" \
    -H "Authorization: Bearer $H2_VIRTUAL_KEY" \
    -H 'Content-Type: application/json' \
    -d "{\"model\":\"$model\",\"max_tokens\":16,\"messages\":[{\"role\":\"user\",\"content\":\"Say ok.\"}]}" \
    || echo '')"
  if [[ -n "$resp" ]] && printf '%s' "$resp" | grep -q '"total_tokens"'; then
    note "PASS: $model one-call smoke (usage present)"
  else
    note "FAIL: $model one-call smoke; response: ${resp:0:300}"
    FAIL=$((FAIL+1))
  fi
done

# Spend must be REAL (R7): after the calls above, key spend must be > 0.
keyinfo="$(curl -sf -m 30 "$GATEWAY/key/info" -H "Authorization: Bearer $H2_VIRTUAL_KEY" || echo '')"
if printf '%s' "$keyinfo" | grep -qE '"spend":[[:space:]]*0(\.0+)?[,}]'; then
  note "FAIL: key spend is still 0 after paid calls — cost map broken, max_budget will never trip (R7)"
  FAIL=$((FAIL+1))
else
  note "PASS: key spend is nonzero (R7)"
fi

# §6.5 item 3: a LAB runner rollout on GLM-5 must not 400 on the
# Gemini-only thinkingConfig agent options the LAB runner always writes.
note "item 3 (LAB rollout, GLM-5, thinkingConfig tolerance): run via the"
note "eval kit's single-rollout entry point; placeholder here until the"
note "Stage 4 eval kit lands. Marked NOT RUN."

# §6.5 item 6 (stretch): Codex -> Mantle Responses API.
if [[ "$STRETCH" -eq 1 ]]; then
  note "item 6 (Codex/Mantle GPT-5.5 stretch): requires bedrock-mantle"
  note "egress approval (NetOps change request) — NOT part of this build."
fi

note "RESULT: fail=$FAIL report=$REPORT"
[[ "$FAIL" -eq 0 ]]
