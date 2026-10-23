#!/usr/bin/env bash
# smoke-isolation.sh — NetOps §9 step 3, run ON THE GUEST as root
# (root is needed to su to ubuntu/eval; copied over EIC by setup.sh).
#
#   smoke-isolation.sh [--phase <closed|setup|run>] [--report-dir <dir>]
#
# Every network check runs from THREE vantage points (C6: no Docker):
#   1. as user `ubuntu`
#   2. as user `eval`
#   3. as user `eval` inside `h2-jail`
#
# MUST FAIL (any success = isolation breach, exit non-zero):
#   * curl to LAN IPs 192.168.1.1 / .188 / .74 / .203
#   * https://1.1.1.1 direct (IP literal, no proxy)
#   * dig @10.99.0.2, @169.254.169.253, @1.1.1.1 for a random label
#   * curl IMDS 169.254.169.254
#   * curl http://10.99.0.10:4000 in phases closed and setup (C8)
# THROUGH THE PROXY:
#   * non-allowlisted name -> 403
#   * allowlisted name -> 200 ONLY in setup; 403 in closed/run
#   * allowlisted name forced to 10.99.0.10 / 169.254.169.254
#     (--connect-to) -> denied
#   * IP-literal CONNECT -> denied
#   * every §5.2 contamination name -> 403 in run
# JAIL:
#   * h2-jail true succeeds as eval
#   * unshare -Ur true as eval OUTSIDE the jail fails (AppArmor userns
#     restriction intact)
# LOCAL:
#   * eval has no sudo
#
# Output: timestamped report; exit 0 only if every check behaves.
set -euo pipefail

PHASE="closed"
REPORT_DIR="/var/log/h2"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --phase) PHASE="$2"; shift 2 ;;
    --report-dir) REPORT_DIR="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
case "$PHASE" in closed|setup|run) ;; *) echo "bad --phase" >&2; exit 2 ;; esac

if [[ "$(id -u)" -ne 0 ]]; then
  echo "smoke-isolation.sh must run as root (it su's to ubuntu/eval)" >&2
  exit 1
fi

mkdir -p "$REPORT_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
REPORT="$REPORT_DIR/smoke-isolation-$STAMP-$PHASE.txt"
PASS=0
FAIL=0

PROXY=http://10.99.0.10:3128
RANDOM_LABEL="h2-$(tr -dc a-z0-9 </dev/urandom | head -c 12)"
ALLOWED_NAME=pypi.org              # on the §5.1 setup list
DENIED_NAME=example.com            # never on any list
CONTAMINATION_NAMES=(
  models.dev registry.npmjs.org huggingface.co hf.co github.com
  raw.githubusercontent.com codeload.github.com mcp.exa.ai opencode.ai
)

note() { printf '%s\n' "$*" | tee -a "$REPORT"; }

record() {
  # record <ok|bad> <description>
  if [[ "$1" == ok ]]; then PASS=$((PASS+1)); note "PASS: $2";
  else FAIL=$((FAIL+1)); note "FAIL: $2"; fi
}

run_as() {
  # run_as <vantage> <cmd...> — vantage: ubuntu | eval | jail
  local vantage="$1"; shift
  case "$vantage" in
    ubuntu) su -s /bin/bash ubuntu -c "$(printf '%q ' "$@")" ;;
    eval)   su -s /bin/bash eval   -c "$(printf '%q ' "$@")" ;;
    jail)   su -s /bin/bash eval   -c "$(printf '%q ' h2-jail --workspace /tmp "$@")" ;;
  esac
}

expect_fail() {
  # expect_fail <vantage> <description> <cmd...>
  local vantage="$1" desc="$2"; shift 2
  if run_as "$vantage" "$@" >/dev/null 2>&1; then
    record bad "[$vantage] $desc: SUCCEEDED but must fail"
  else
    record ok "[$vantage] $desc: failed as required"
  fi
}

proxy_code() {
  # proxy_code <vantage> <url> [extra curl args...] -> prints HTTP code (000 on no-connect)
  local vantage="$1" url="$2"; shift 2
  run_as "$vantage" curl -sx "$PROXY" -o /dev/null -m 10 -w '%{http_code}' "$@" "$url" 2>/dev/null || echo 000
}

expect_proxy_code() {
  # expect_proxy_code <vantage> <desc> <want-regex> <url> [curl args...]
  local vantage="$1" desc="$2" want="$3" url="$4"; shift 4
  local code
  code="$(proxy_code "$vantage" "$url" "$@")"
  if [[ "$code" =~ $want ]]; then
    record ok "[$vantage] $desc (got $code)"
  else
    record bad "[$vantage] $desc: got HTTP $code, wanted $want"
  fi
}

note "harness2-eval isolation smoke test"
note "phase=$PHASE stamp=$STAMP random_label=$RANDOM_LABEL"
note "============================================================"

for V in ubuntu eval jail; do
  note "---- vantage: $V ----"

  # LAN must be unreachable.
  for ip in 192.168.1.1 192.168.1.188 192.168.1.74 192.168.1.203; do
    expect_fail "$V" "LAN http://$ip" curl -sf -m 5 "http://$ip/"
  done

  # Direct IP-literal HTTPS (no proxy).
  expect_fail "$V" "direct https://1.1.1.1" curl -sf -m 5 https://1.1.1.1/

  # DNS: all three resolvers must yield nothing (§4.2).
  for resolver in 10.99.0.2 169.254.169.253 1.1.1.1; do
    expect_fail "$V" "dig @$resolver $RANDOM_LABEL.example" \
      dig +time=3 +tries=1 "@$resolver" "$RANDOM_LABEL.example" +short +fail
  done

  # IMDS.
  expect_fail "$V" "IMDS 169.254.169.254" curl -sf -m 5 http://169.254.169.254/latest/meta-data/

  # LiteLLM :4000 is blocked in closed and setup (C8), open in run.
  if [[ "$PHASE" == "run" ]]; then
    note "SKIP: [$V] :4000 reachability not asserted in run (arm owns it)"
  else
    expect_fail "$V" "LiteLLM http://10.99.0.10:4000 in $PHASE" \
      curl -sf -m 5 http://10.99.0.10:4000/
  fi

  # Through squid: non-allowlisted always 403.
  expect_proxy_code "$V" "proxy deny $DENIED_NAME" '^(403|000)$' "https://$DENIED_NAME/"

  # Allowlisted: 200 only in setup.
  if [[ "$PHASE" == "setup" ]]; then
    expect_proxy_code "$V" "proxy allow $ALLOWED_NAME in setup" '^200$' "https://$ALLOWED_NAME/"
  else
    expect_proxy_code "$V" "proxy deny $ALLOWED_NAME in $PHASE" '^(403|000)$' "https://$ALLOWED_NAME/"
  fi

  # Allowed name forced onto private/IMDS IPs must be denied in EVERY
  # phase (squid denies the resolved/forced dst before any allow).
  expect_proxy_code "$V" "forced $ALLOWED_NAME->10.99.0.10 denied" '^(403|000)$' \
    "https://$ALLOWED_NAME/" --connect-to "$ALLOWED_NAME:443:10.99.0.10:443"
  expect_proxy_code "$V" "forced $ALLOWED_NAME->169.254.169.254 denied" '^(403|000)$' \
    "https://$ALLOWED_NAME/" --connect-to "$ALLOWED_NAME:443:169.254.169.254:443"

  # IP-literal CONNECT via proxy.
  expect_proxy_code "$V" "proxy IP-literal CONNECT denied" '^(403|000)$' "https://1.1.1.1/"

  # §5.2 contamination names: each must 403 in run (and every phase).
  if [[ "$PHASE" == "run" ]]; then
    for name in "${CONTAMINATION_NAMES[@]}"; do
      expect_proxy_code "$V" "contamination deny $name" '^(403|000)$' "https://$name/"
    done
  fi
done

note "---- jail/userns checks ----"
if run_as eval h2-jail --workspace /tmp true >/dev/null 2>&1; then
  record ok "h2-jail true succeeds as eval"
else
  record bad "h2-jail true FAILED as eval (jail broken)"
fi
expect_fail eval "unshare -Ur true outside jail (userns restriction intact)" \
  unshare -Ur true

note "---- local privilege ----"
if run_as eval sudo -n true >/dev/null 2>&1; then
  record bad "eval has passwordless sudo"
else
  record ok "eval has no sudo"
fi

note "============================================================"
note "RESULT: pass=$PASS fail=$FAIL report=$REPORT"
if [[ "$FAIL" -gt 0 ]]; then
  note "ISOLATION SMOKE TEST FAILED"
  exit 1
fi
note "isolation smoke test passed"
