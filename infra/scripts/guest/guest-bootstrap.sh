#!/usr/bin/env bash
# guest-bootstrap.sh — run as root ON THE GUEST during phase `setup`
# (copied over EIC by setup.sh). Installs the pinned toolchain per
# NetOps §3 / PreCog §6.1. Never run on the build host.
#
# All downloads go through squid (phase `setup` allowlist). Every
# artifact is either version-pinned through a signed/locked channel
# (apt, npm with exact versions, pip --require-hashes, git by commit
# SHA) or sha256-verified by hand (Node tarball).
set -euo pipefail

# ---- pins (change only with a logged NetOps decision) ----
NODE_VERSION=22.23.4
# From https://nodejs.org/dist/v22.23.4/SHASUMS256.txt, fetched 2026-10-22
# on the build host (node-v22.23.4-linux-x64.tar.xz):
NODE_SHA256=1805bcd35e6815559bd2eb97db438d3fdb0eb3a5e6a977b7c1badca76337e9ce
BUN_VERSION=1.3.11
CODEX_VERSION=0.144.6
OPENCODE_COMMIT=23fb5e05
HARNESS2_COMMIT=1cfe254
HARVEY_LABS_COMMIT=b58a28ae
UV_VERSION=0.13.5
# uv wheel hashes from pypi.org/pypi/uv/0.13.5/json, fetched 2026-10-22:
UV_HASHES=(
  "--hash=sha256:fa8d26e08e34c29c0c4e820cf3181757086d547240b4f4d31ff1d8bd810ca946"  # manylinux_2_17_x86_64
  "--hash=sha256:979409b8c9a78df140ffb19c53f511b7a73526da8d1d5e36558833055e371c6c"  # sdist (fallback)
)

PROXY=http://10.99.0.10:3128
export HTTPS_PROXY="$PROXY" HTTP_PROXY="$PROXY" https_proxy="$PROXY" http_proxy="$PROXY"
export NO_PROXY=10.99.0.10 no_proxy=10.99.0.10

if [[ "$(id -u)" -ne 0 ]]; then
  echo "guest-bootstrap.sh must run as root on the guest" >&2
  exit 1
fi

H2=/opt/h2
install -d -m 0755 "$H2" "$H2/tools"

echo "[1/9] eval user (no sudo, no groups beyond its own)"
id eval >/dev/null 2>&1 || useradd --create-home --shell /bin/bash eval
# Belt and braces: eval must never gain sudo.
rm -f /etc/sudoers.d/eval 2>/dev/null || true
if id -nG eval | grep -qw sudo; then
  deluser eval sudo
fi

echo "[2/9] system-wide proxy env (§3: npm/bun/uv/pip/git all honor it)"
cat > /etc/profile.d/h2-proxy.sh <<EOF
export HTTPS_PROXY=$PROXY HTTP_PROXY=$PROXY
export https_proxy=$PROXY http_proxy=$PROXY
export NO_PROXY=10.99.0.10 no_proxy=10.99.0.10
EOF
cat >> /etc/environment <<EOF
HTTPS_PROXY=$PROXY
HTTP_PROXY=$PROXY
NO_PROXY=10.99.0.10
EOF
git config --system http.proxy "$PROXY"
git config --system https.proxy "$PROXY"

echo "[3/9] apt toolchain (through squid, signed repos)"
export DEBIAN_FRONTEND=noninteractive
cat > /etc/apt/apt.conf.d/90h2proxy <<EOF
Acquire::http::Proxy "$PROXY";
Acquire::https::Proxy "$PROXY";
EOF
apt-get update -q
apt-get install -qy python3-venv python3-pip git curl xz-utils bubblewrap pandoc poppler-utils

echo "[4/9] Node $NODE_VERSION (sha256-verified; fail closed)"
if [[ ! -x /usr/local/node/bin/node ]]; then
  cd /tmp
  TARBALL="node-v${NODE_VERSION}-linux-x64.tar.xz"
  curl -fsSLO "https://nodejs.org/dist/v${NODE_VERSION}/${TARBALL}"
  echo "${NODE_SHA256}  ${TARBALL}" | sha256sum -c -   # fails closed on mismatch
  mkdir -p /usr/local/node
  tar -xJf "$TARBALL" -C /usr/local/node --strip-components=1
  rm -f "$TARBALL"
fi
ln -sf /usr/local/node/bin/node /usr/local/bin/node
ln -sf /usr/local/node/bin/npm /usr/local/bin/npm
ln -sf /usr/local/node/bin/npx /usr/local/bin/npx

echo "[5/9] uv in its own venv (PEP 668: never pip into system python)"
if [[ ! -x "$H2/uv-venv/bin/uv" ]]; then
  python3 -m venv "$H2/uv-venv"
  "$H2/uv-venv/bin/pip" install --quiet --require-hashes \
    "uv==${UV_VERSION}" "${UV_HASHES[@]}" || {
      # --require-hashes wants a requirements file; fall back to one.
      REQ=/tmp/uv-req.txt
      {
        echo "uv==${UV_VERSION} \\"
        printf '    %s \\\n' "${UV_HASHES[@]::1}"
        printf '    %s\n' "${UV_HASHES[@]:1}"
      } > "$REQ"
      "$H2/uv-venv/bin/pip" install --quiet --require-hashes -r "$REQ"
      rm -f "$REQ"
    }
fi
ln -sf "$H2/uv-venv/bin/uv" /usr/local/bin/uv
# uv must never fetch a managed Python from GitHub (§3).
echo 'UV_PYTHON_DOWNLOADS=never' >> /etc/environment

echo "[6/9] pinned checkouts (shallow by SHA; no LFS)"
fetch_pinned() {
  # fetch_pinned <url> <dir> <sha>
  local url="$1" dir="$2" sha="$3"
  if [[ -d "$dir/.git" ]]; then return 0; fi
  mkdir -p "$dir"
  git -C "$dir" init -q
  git -C "$dir" remote add origin "$url"
  git -C "$dir" fetch -q --depth 1 origin "$sha"
  git -C "$dir" checkout -q FETCH_HEAD
}
fetch_pinned https://github.com/google-research/harness2 "$H2/harness2" "$HARNESS2_COMMIT"
fetch_pinned https://github.com/anomalyco/opencode "$H2/opencode" "$OPENCODE_COMMIT"
fetch_pinned https://github.com/harvey-labs/harvey-labs "$H2/harvey-labs" "$HARVEY_LABS_COMMIT"

echo "[7/9] npm pins: bun ${BUN_VERSION}, codex ${CODEX_VERSION} (+ plugin pre-seed)"
install -d -m 0755 "$H2/tools"
( cd "$H2/tools" && npm install --no-fund --no-audit --ignore-scripts \
    "bun@${BUN_VERSION}" "@openai/codex@${CODEX_VERSION}" "@opencode-ai/plugin" )
ln -sf "$H2/tools/node_modules/.bin/bun" /usr/local/bin/bun
# OpenCode deps, frozen lock, scripts off (§3).
( cd "$H2/opencode" && bun install --frozen-lockfile --ignore-scripts )

echo "[8/9] harness2 [render] extra via uv (never [bridge]: no LiteLLM on the guest)"
if [[ ! -d "$H2/harness2/.venv" ]]; then
  ( cd "$H2/harness2" && UV_PYTHON_DOWNLOADS=never uv venv --python 3.12 .venv \
      && UV_PYTHON_DOWNLOADS=never uv pip install --python .venv -e '.[render]' )
fi

echo "[9/9] models.dev one-time bake + OpenCode env"
if [[ ! -s "$H2/models.json" ]]; then
  curl -fsSL https://models.dev/api.json -o "$H2/models.json"
fi
cat > /etc/profile.d/h2-opencode.sh <<EOF
export OPENCODE_MODELS_PATH=$H2/models.json
export OPENCODE_DISABLE_MODELS_FETCH=1
export OPENCODE_DISABLE_AUTOUPDATE=1
export OPENCODE_DISABLE_LSP_DOWNLOAD=1
export OPENCODE_DISABLE_CLAUDE_CODE=1
export UV_PYTHON_DOWNLOADS=never
EOF
# Deliberately NOT set: OPENCODE_ENABLE_EXA (web search stays off).

chown -R eval:eval "$H2/harness2" "$H2/harvey-labs" 2>/dev/null || true
echo "guest-bootstrap complete."
