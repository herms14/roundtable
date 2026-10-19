#!/bin/bash
# openbao-lxc: the owner's day-to-day login (userpass "hermes", policy "owner") so the root token can stay offline.
# Root only. The password comes on stdin and is never printed or put on a command line:
#   <password> | ssh openbao-lxc 'sudo bash /usr/local/sbin/bao_owner_login.sh'
# Re-running sets a new password. Policy: full read/write on the homelab KV, nothing on policies, auth or audit.
set -euo pipefail
export BAO_ADDR=https://127.0.0.1:8200 BAO_CACERT=/etc/openbao/tls/ca.crt
BAO_TOKEN=$(cat /root/openbao-root.token)
export BAO_TOKEN
IFS= read -r PW
[ ${#PW} -ge 20 ] || { echo "password too short" >&2; exit 1; }

bao auth list -format=json | grep -q '"userpass/"' || bao auth enable userpass >/dev/null

bao policy write owner - >/dev/null <<'EOF'
# Hermes (owner): read and change every secret in the homelab KV; no policies, auth methods, tokens or audit.
path "homelab/data/*"     { capabilities = ["create", "read", "update", "delete", "list"] }
path "homelab/metadata/*" { capabilities = ["read", "list", "delete"] }
path "homelab/metadata"   { capabilities = ["list"] }
path "homelab/delete/*"   { capabilities = ["update"] }
path "homelab/undelete/*" { capabilities = ["update"] }
path "auth/userpass/users/hermes/password" { capabilities = ["update"] }
path "sys/policies/*" { capabilities = ["deny"] }
path "sys/policy/*"   { capabilities = ["deny"] }
path "sys/auth/*"     { capabilities = ["deny"] }
path "sys/audit*"     { capabilities = ["deny"] }
path "auth/token/create*" { capabilities = ["deny"] }
EOF

printf '%s' "$PW" | bao write auth/userpass/users/hermes password=- token_policies=owner \
    token_ttl=8h token_max_ttl=24h >/dev/null
echo "userpass user hermes ready (policy owner, 8 h sessions)"
