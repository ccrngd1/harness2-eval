#!/usr/bin/env bash
# check-no-account-ids.sh — build-failing scan for AWS account-ID literals
# (CC directive A). The ONLY place a 12-digit account may appear is
# tests/fixtures/ (the documented placeholder).
#
#   check-no-account-ids.sh [--self-test] [root-dir]
#
# Patterns (both fail the build):
#   * bare 12 digits not embedded in a longer alnum run:
#       (?<![0-9A-Za-z])[0-9]{12}(?![0-9A-Za-z])
#   * dashed form: [0-9]{4}-[0-9]{4}-[0-9]{4} (same boundaries)
#
# Excluded: .venv/ node_modules/ cdk.out/ .git/ tests/fixtures/ and this
# script itself (it contains the patterns).
#
# --self-test plants a literal in a temp copy and asserts the scan fails;
# proves the check actually bites.
set -euo pipefail

SELF_TEST=0
ROOT=""
for arg in "$@"; do
  case "$arg" in
    --self-test) SELF_TEST=1 ;;
    *) ROOT="$arg" ;;
  esac
done
SCRIPT_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
ROOT="${ROOT:-$(dirname "$(dirname "$SCRIPT_PATH")")}"

scan() {
  # scan <dir> — prints offending file:line matches; rc 1 if any found.
  local dir="$1"
  python3 - "$dir" "$SCRIPT_PATH" <<'PYEOF'
import os
import re
import sys

root = sys.argv[1]
self_path = os.path.realpath(sys.argv[2])
bare = re.compile(r"(?<![0-9A-Za-z])[0-9]{12}(?![0-9A-Za-z])")
dashed = re.compile(r"(?<![0-9A-Za-z])[0-9]{4}-[0-9]{4}-[0-9]{4}(?![0-9A-Za-z])")
exclude_dirs = {".venv", "node_modules", ".git", "__pycache__", ".pytest_cache"}
hits = []
for dirpath, dirnames, filenames in os.walk(root):
    # cdk.out, cdk.out.key, ...: synth output legitimately renders the
    # fixture account into ARNs; it is generated, never shipped.
    dirnames[:] = [
        d for d in dirnames if d not in exclude_dirs and not d.startswith("cdk.out")
    ]
    rel = os.path.relpath(dirpath, root)
    # tests/fixtures/ is the one sanctioned home for the placeholder.
    if rel == os.path.join("tests", "fixtures") or rel.startswith(
        os.path.join("tests", "fixtures") + os.sep
    ):
        dirnames[:] = []
        continue
    for fn in filenames:
        path = os.path.join(dirpath, fn)
        if os.path.realpath(path) == self_path:
            continue
        try:
            with open(path, encoding="utf-8", errors="strict") as fh:
                for lineno, line in enumerate(fh, 1):
                    if bare.search(line) or dashed.search(line):
                        hits.append(f"{os.path.relpath(path, root)}:{lineno}: {line.rstrip()}")
        except (UnicodeDecodeError, OSError):
            continue  # binary or unreadable: not a source file we ship
if hits:
    print("account-ID literal(s) found outside tests/fixtures/:")
    for h in hits:
        print("  " + h)
    sys.exit(1)
PYEOF
}

if [[ "$SELF_TEST" -eq 1 ]]; then
  TMP="$(mktemp -d)"
  trap 'rm -rf "$TMP"' EXIT
  mkdir -p "$TMP/src"
  echo 'ok line' > "$TMP/src/clean.txt"
  if ! scan "$TMP" >/dev/null; then
    echo "self-test FAILED: clean tree flagged" >&2; exit 1
  fi
  echo 'account = "123456789012"' > "$TMP/src/planted.py"
  if scan "$TMP" >/dev/null; then
    echo "self-test FAILED: planted bare literal NOT flagged" >&2; exit 1
  fi
  echo 'acct 1234-5678-9012 here' > "$TMP/src/planted.py"
  if scan "$TMP" >/dev/null; then
    echo "self-test FAILED: planted dashed literal NOT flagged" >&2; exit 1
  fi
  # Boundary: 13 digits is NOT an account ID.
  echo 'x1234567890123' > "$TMP/src/planted.py"
  if ! scan "$TMP" >/dev/null; then
    echo "self-test FAILED: 13-digit run inside alnum flagged" >&2; exit 1
  fi
  echo "self-test passed"
  exit 0
fi

scan "$ROOT"
echo "no account-ID literals outside tests/fixtures/ (scanned $ROOT)"
