#!/usr/bin/env bash
# proxy-bootstrap.sh — run as root ON THE PROXY (copied over EIC by
# setup.sh). Installs and hardens squid, nftables, Postgres and LiteLLM.
# Never run on the build host.
#
# Usage: proxy-bootstrap.sh --budget <usd> --region <region>
#
# Everything here is idempotent: re-running after a partial failure is safe.
set -euo pipefail

BUDGET=""
REGION="us-east-1"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --budget) BUDGET="$2"; shift 2 ;;
    --region) REGION="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
if [[ -z "$BUDGET" ]]; then
  echo "proxy-bootstrap.sh: --budget <usd> is required (budgetCapUsd)" >&2
  exit 2
fi

if [[ "$(id -u)" -ne 0 ]]; then
  echo "proxy-bootstrap.sh must run as root on the proxy" >&2
  exit 1
fi

ASSETS=/opt/h2/assets   # setup.sh scp's scripts/proxy/* here
LISTS_DIR=/opt/h2/allowlists
GW_DIR=/opt/h2gw
PROXY_IP=10.99.0.10
GUEST_IP=10.99.0.70

echo "[1/8] apt packages (squid, nftables, postgres, python venv)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -qy squid nftables postgresql python3-venv python3-pip

echo "[2/8] users"
id litellm >/dev/null 2>&1 || useradd --system --home "$GW_DIR" --shell /usr/sbin/nologin litellm
# squid's uid is 'proxy' on Ubuntu (used by the IMDS owner-match rule).
id proxy >/dev/null 2>&1 || { echo "expected squid uid 'proxy' to exist"; exit 1; }

echo "[3/8] squid config + allowlists"
install -d -m 0755 "$LISTS_DIR"
install -m 0644 "$ASSETS/allow-setup.txt" "$ASSETS/allow-setup-http80.txt" \
  "$ASSETS/allow-run.txt" "$ASSETS/allow-closed.txt" "$LISTS_DIR/"
install -m 0644 "$ASSETS/squid.conf" /etc/squid/squid.conf
install -m 0755 "$ASSETS/h2-egress" /usr/local/sbin/h2-egress
# Seed the active ACLs closed before squid ever starts.
echo "deny-all.invalid" > /etc/squid/h2-active-connect.acl
echo "deny-all.invalid" > /etc/squid/h2-active-http80.acl

echo "[4/8] nftables: phase gate + IMDS owner-match (R5)"
install -d -m 0755 /etc/nftables.d 2>/dev/null || true
cat > /etc/nftables.conf <<NFT
#!/usr/sbin/nft -f
flush ruleset

table inet h2 {
  # Phase gate: guest -> :4000. h2-egress swaps the single rule here.
  chain guest_gateway {
    type filter hook input priority -10; policy accept;
    ip saddr ${GUEST_IP} tcp dport 4000 drop comment "boot default: closed"
  }

  # R5: only uid root and uid litellm may reach IMDS. Everything else on
  # this box (including uid proxy/squid) is rejected. Defense in depth
  # behind the squid ACL.
  chain imds_owner_match {
    type filter hook output priority -10; policy accept;
    ip daddr 169.254.169.254 meta skuid root accept
    ip daddr 169.254.169.254 meta skuid litellm accept
    ip daddr 169.254.169.254 reject
  }
}
NFT
systemctl enable --now nftables
nft -f /etc/nftables.conf

echo "[5/8] postgres (127.0.0.1 only) + litellm db"
# Ubuntu default binds localhost only; enforce anyway.
PGCONF="$(find /etc/postgresql -name postgresql.conf -path '*/main/*' | head -1)"
sed -i "s/^#\?listen_addresses.*/listen_addresses = '127.0.0.1'/" "$PGCONF"
systemctl enable --now postgresql
systemctl reload postgresql || systemctl restart postgresql
DB_PASS_FILE="$GW_DIR/db-pass"
install -d -m 0750 -o root -g litellm "$GW_DIR"
if [[ ! -s "$DB_PASS_FILE" ]]; then
  umask 077
  openssl rand -hex 24 > "$DB_PASS_FILE"
  umask 022
fi
DB_PASS="$(cat "$DB_PASS_FILE")"
sudo -u postgres psql -tc "SELECT 1 FROM pg_roles WHERE rolname='litellm'" | grep -q 1 || \
  sudo -u postgres psql -c "CREATE ROLE litellm LOGIN PASSWORD '${DB_PASS}'"
sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='litellm'" | grep -q 1 || \
  sudo -u postgres createdb -O litellm litellm

echo "[6/8] litellm venv (pinned, --require-hashes; R4)"
if [[ ! -x "$GW_DIR/venv/bin/litellm" ]]; then
  python3 -m venv "$GW_DIR/venv"
  "$GW_DIR/venv/bin/pip" install --quiet --upgrade 'pip==25.2'
  "$GW_DIR/venv/bin/pip" install --quiet --require-hashes -r "$ASSETS/litellm-requirements.lock"
fi

echo "[7/8] master key (root-only, generated HERE, never in user-data/CFN)"
KEY_ENV="$GW_DIR/master-key.env"
if [[ ! -s "$KEY_ENV" ]]; then
  umask 077
  {
    echo "LITELLM_MASTER_KEY=sk-h2-$(openssl rand -hex 32)"
    echo "DATABASE_URL=postgresql://litellm:${DB_PASS}@127.0.0.1:5432/litellm"
  } > "$KEY_ENV"
  umask 022
fi
chown root:root "$KEY_ENV"
chmod 0600 "$KEY_ENV"

echo "[7b/8] litellm config (budget=${BUDGET}, region=${REGION})"
sed -e "s/__MAX_BUDGET__/${BUDGET}/" -e "s/__AWS_REGION__/${REGION}/" \
  "$ASSETS/litellm-config.template.yaml" > "$GW_DIR/litellm-config.yaml"
chown root:litellm "$GW_DIR/litellm-config.yaml"
chmod 0640 "$GW_DIR/litellm-config.yaml"

echo "[8/8] systemd units + boot-closed ordering (C8)"
cat > /etc/systemd/system/litellm.service <<UNIT
[Unit]
Description=harness2-eval LiteLLM gateway
After=network-online.target postgresql.service h2-egress-boot.service
Requires=postgresql.service
Wants=h2-egress-boot.service

[Service]
User=litellm
Group=litellm
EnvironmentFile=${GW_DIR}/master-key.env
Environment=LITELLM_LOCAL_MODEL_COST_MAP=True
Environment=NO_PROXY=127.0.0.1,169.254.169.254
Environment=DISABLE_ADMIN_UI=True
ExecStart=${GW_DIR}/venv/bin/litellm --config ${GW_DIR}/litellm-config.yaml --host ${PROXY_IP} --port 4000
Restart=on-failure
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/tmp
PrivateTmp=true

[Install]
WantedBy=multi-user.target
UNIT

# Boot default phase = closed, enforced BEFORE squid/litellm accept traffic.
cat > /etc/systemd/system/h2-egress-boot.service <<'UNIT'
[Unit]
Description=harness2-eval: force phase closed at boot
Before=squid.service litellm.service
After=nftables.service
Requires=nftables.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/h2-egress closed
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
UNIT

mkdir -p /run/h2
systemctl daemon-reload
systemctl enable h2-egress-boot.service litellm.service squid.service
systemctl restart h2-egress-boot.service
systemctl restart squid.service
systemctl restart litellm.service

echo "proxy-bootstrap complete. Phase: $(cat /run/h2/phase 2>/dev/null || echo unknown)"
echo "Listeners (expect :22, ${PROXY_IP}:3128, ${PROXY_IP}:4000, 127.0.0.1:5432):"
ss -ltn
