#!/bin/bash
# OpenBao install on openbao-lxc (CT213). Run as root. Idempotent where possible. Never prints secrets.
set -euo pipefail
VER=2.7.1
IP=192.168.40.39
W=/root/bao-install
mkdir -p "$W"; cd "$W"

# 1. package, checksum-verified
if ! command -v bao >/dev/null; then
  base=https://github.com/openbao/openbao/releases/download/v$VER
  curl -fsSLO "$base/openbao_${VER}_linux_amd64.deb"
  curl -fsSLO "$base/checksums.txt"
  grep " openbao_${VER}_linux_amd64.deb\$" checksums.txt | sha256sum -c -
  apt-get install -y -qq "./openbao_${VER}_linux_amd64.deb" >/dev/null
fi
bao version

# 2. own CA + server cert
T=/etc/openbao/tls
install -d -m 750 -o root -g openbao "$T"
if [ ! -f "$T/ca.crt" ]; then
  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 3650 \
    -subj "/CN=Hermes Homelab OpenBao CA" -keyout "$T/ca.key" -out "$T/ca.crt" 2>/dev/null
  chmod 600 "$T/ca.key"
fi
if [ ! -f "$T/server.crt" ]; then
  cat > "$W/san.cnf" <<EOF
basicConstraints=CA:FALSE
keyUsage=digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=IP:$IP,IP:127.0.0.1,DNS:openbao-lxc,DNS:openbao-lxc.hrmsmrflrii.xyz,DNS:bao.hrmsmrflrii.xyz,DNS:localhost
EOF
  openssl req -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -subj "/CN=openbao-lxc" \
    -keyout "$T/server.key" -out "$W/server.csr" 2>/dev/null
  openssl x509 -req -in "$W/server.csr" -CA "$T/ca.crt" -CAkey "$T/ca.key" -CAcreateserial -days 825 \
    -extfile "$W/san.cnf" -out "$T/server.crt" 2>/dev/null
  chown root:openbao "$T/server.key" "$T/server.crt" "$T/ca.crt"; chmod 640 "$T/server.key"; chmod 644 "$T/server.crt" "$T/ca.crt"
fi
cp "$T/ca.crt" /usr/local/share/ca-certificates/openbao-ca.crt; update-ca-certificates >/dev/null

# 3. config: raft storage, TLS listener, UI, prometheus metrics, audit log dir
install -d -m 750 -o openbao -g openbao /opt/openbao/data /var/log/openbao
[ -f /etc/openbao/openbao.hcl.dist ] || cp /etc/openbao/openbao.hcl /etc/openbao/openbao.hcl.dist 2>/dev/null || true
cat > /etc/openbao/openbao.hcl <<EOF
# OpenBao on openbao-lxc (CT213). Managed by Claude Code, 2026-10-18.
ui            = true
api_addr      = "https://$IP:8200"
cluster_addr  = "https://$IP:8201"
disable_mlock = true

storage "raft" {
  path    = "/opt/openbao/data"
  node_id = "openbao-lxc"
}

listener "tcp" {
  address       = "0.0.0.0:8200"
  tls_cert_file = "$T/server.crt"
  tls_key_file  = "$T/server.key"
  telemetry {
    unauthenticated_metrics_access = true
  }
}

telemetry {
  prometheus_retention_time = "24h"
  disable_hostname          = true
}
EOF
chown root:openbao /etc/openbao/openbao.hcl; chmod 640 /etc/openbao/openbao.hcl

# 4. auto-unseal unit (key on the backup=0 mount /etc/openbao-unseal)
chmod 700 /etc/openbao-unseal
cat > /usr/local/sbin/openbao-unseal <<'EOF'
#!/bin/bash
# Unseal the local OpenBao with the key on /etc/openbao-unseal (never printed, never on the command line).
set -euo pipefail
K=/etc/openbao-unseal/unseal.key
A=https://127.0.0.1:8200/v1/sys
for i in $(seq 60); do
  s=$(curl -s --cacert /etc/openbao/tls/ca.crt "$A/seal-status" || true)
  [ -n "$s" ] && break; sleep 1
done
[ "$(echo "$s" | jq -r .initialized)" = "true" ] || { echo "not initialized"; exit 0; }
[ "$(echo "$s" | jq -r .sealed)" = "true" ] || { echo "already unsealed"; exit 0; }
jq -Rn '{key: input}' < "$K" | curl -s --cacert /etc/openbao/tls/ca.crt -X PUT --data @- "$A/unseal" | jq -r '"sealed=" + (.sealed|tostring)'
EOF
chmod 700 /usr/local/sbin/openbao-unseal
cat > /etc/systemd/system/openbao-unseal.service <<'EOF'
[Unit]
Description=Auto-unseal OpenBao from the local key (no-backup mount)
After=openbao.service
PartOf=openbao.service
Requires=openbao.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/openbao-unseal

[Install]
WantedBy=openbao.service
EOF

# audit log rotation (OpenBao reopens the file on SIGHUP)
cat > /etc/logrotate.d/openbao <<'EOF'
/var/log/openbao/audit.log {
    daily
    rotate 30
    compress
    delaycompress
    missingok
    notifempty
    create 0600 openbao openbao
    postrotate
        systemctl kill -s HUP openbao.service >/dev/null 2>&1 || true
    endscript
}
EOF

systemctl daemon-reload
systemctl enable -q openbao.service openbao-unseal.service
systemctl restart openbao.service
sleep 3
systemctl is-active openbao.service
curl -s --cacert $T/ca.crt https://127.0.0.1:8200/v1/sys/seal-status | jq -c '{initialized, sealed, version}'
