#!/usr/bin/env bash
# Roundtable systemd installer - idempotent. Creates services/timers for every agent whose script exists.
# Usage: sudo -n bash ~/olympus/install.sh
set -euo pipefail
U=hermes-admin
D=/home/$U/olympus
PY=/usr/bin/python3
SD=/etc/systemd/system

# name | description | command | OnCalendar (Asia/Manila) | timeout
TIMERS=$(cat <<'EOF'
watcher|Sellen - Prometheus/Loki alert evaluation|watcher.py check|*-*-* *:00/2:00|300
watcher-digest|Sellen - morning health digest|watcher.py digest|*-*-* 08:00:00|600
medic-scan|Corhyn - incident scan (containers, units, hosts)|medic.py scan|*-*-* *:00/5:00|3300
netops-watch|Torrent - network watch (devices, gateways, internet, DNS)|netops.py watch|*-*-* *:00/10:00|300
netops|Torrent - daily network health check|netops.py check|*-*-* 07:00:00|1200
mirror|Radagon - live lab to homelab-hub docs sync|mirror.py run|*-*-* 07:30:00|3600
builder-audit|Hewg - daily compute and storage audit|builder.py audit|*-*-* 07:10:00|900
checker|Blaidd - daily verification of patches, builds and fixes|checker.py run|*-*-* 05:10:00|1800
writer|Melina - weekly blog pitch|writer.py pitch|Sat *-*-* 10:00:00|1800
scout|Kalé - weekly homelab scout report|scout.py run|Sun *-*-* 09:00:00|2400
security-scan|Loretta - security posture scan (all layers)|security.py scan|*-*-* 00/6:20:00|3000
security-watch|Loretta - attack signals (SSH brute force, auth bursts, rogue devices)|security.py watch|*-*-* *:07/15:00|300
security-brief|Loretta - daily security brief|security.py brief|*-*-* 07:45:00|300
access-check|Roundtable - access backlog sync (GitLab issues, Obsidian note)|access.py check|*-*-* 06:50:00|600
access-expire|Rogier - revoke expired time-limited access (GitLab, Authentik; Discord + Obsidian audit)|access.py expire|*-*-* *:02/5:00|240
vault-refresh|Miriel - permitted read-only note mirror (no personal profile generation)|scribe.py vault-sync|*-*-* 00/3:20:00|1200
transcripts-export|Roundtable - export conversation transcripts to the Obsidian vault|transcripts.py export|*:0/15|600
backup-watch|Iji - backup failures, stale guests, NAS copy, storage thresholds|backup.py watch|*:05/30|900
backup-digest|Iji - daily backup report|backup.py digest|*-*-* 08:00:00|600
backup-forecast|Iji - weekly backup capacity forecast|backup.py forecast|Sun *-*-* 18:00:00|600
backup-omada|Iji - weekly Omada controller config backup to the NAS|backup.py omada-export|Sun *-*-* 03:00:00|600
media-watch|Nepheli - media stack health, failed downloads, ready to watch|media.py watch|*:03/10|300
media-taste|Nepheli - rebuild taste profile from the library|media.py taste|*-*-* 05:30:00|600
media-digest|Nepheli - evening pick for tonight|media.py digest|*-*-* 19:30:00|300
ipmap-sync|Roundtable - Diallos refreshes the IP map page and the vault IP registry section|ipmap.py sync|*-*-* 06:40:00|900
ipmap-watch|Diallos - IP map watch (no model): vault note and Discord as soon as an address or its service changes|ipmap.py watch|*:0/15|300
glance-check|Sellen - Glance dashboard self-check (posts only when a widget breaks or recovers)|labfeed.py check|*:05/30|300
atlas-sweep|Miriel - redraw the Hermes Lab Atlas from the live lab (reports only additions, removals, renames)|atlas.py refresh --reason sweep|*:20/30|300
dns-sync|Torrent - copy Technitium local DNS records to Pi-hole (secondary); posts only on change|dnssync.py sync|*-*-* 05:50:00|300
keeper|Varys - OpenBao secrets check (metadata only); posts on a problem, Mondays the weekly report|keeper.py audit|*-*-* 08:15:00|300
EOF
)

changed=0
while IFS='|' read -r name desc cmd cal tmo; do
  script=${cmd%% *}
  [ -f "$D/$script" ] || { echo "skip $name ($script missing)"; continue; }
  cat > "$SD/$name.service.new" <<EOF
[Unit]
Description=Roundtable $desc
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=$U
WorkingDirectory=$D
ExecStart=$PY -u $D/$cmd
TimeoutStartSec=$tmo
Nice=5
Environment=PATH=/home/$U/.local/bin:/usr/local/bin:/usr/bin:/bin
EOF
  cat > "$SD/$name.timer.new" <<EOF
[Unit]
Description=Roundtable $desc (timer)

[Timer]
OnCalendar=$cal Asia/Manila
Persistent=$( [[ "$cal" == *"/"* ]] && echo false || echo true )
RandomizedDelaySec=$( [[ "$cal" == *"/"* ]] && echo 10 || echo 60 )

[Install]
WantedBy=timers.target
EOF
  for f in "$name.service" "$name.timer"; do
    if ! cmp -s "$SD/$f.new" "$SD/$f" 2>/dev/null; then mv "$SD/$f.new" "$SD/$f"; changed=1; echo "wrote $f"; else rm "$SD/$f.new"; fi
  done
  systemctl enable "$name.timer" >/dev/null
done <<< "$TIMERS"

# Self-hosted face-api for the portal webcam (expressions are read in the browser; nothing is sent anywhere)
FA="$D/vendor/faceapi"; FAV=1.7.15
if [ ! -s "$FA/face_expression_model.bin" ]; then
  install -d -o "$U" -g "$U" "$FA"
  for f in dist/face-api.esm.js model/tiny_face_detector_model-weights_manifest.json model/tiny_face_detector_model.bin \
           model/face_expression_model-weights_manifest.json model/face_expression_model.bin; do
    curl -fsSL "https://cdn.jsdelivr.net/npm/@vladmandic/face-api@$FAV/$f" -o "$FA/${f##*/}" && chown "$U:$U" "$FA/${f##*/}" \
      || echo "face-api download failed: $f"
  done
fi

# Always-on services
for svc in boss olympus-portal labfeed; do
  [ -f "$D/$svc.service" ] || continue
  if ! cmp -s "$D/$svc.service" "$SD/$svc.service" 2>/dev/null; then cp "$D/$svc.service" "$SD/$svc.service"; changed=1; echo "wrote $svc.service"; fi
done
[ "$changed" = 1 ] && systemctl daemon-reload
for svc in boss olympus-portal labfeed; do
  [ -f "$SD/$svc.service" ] && systemctl enable "$svc.service" >/dev/null 2>&1 && systemctl restart "$svc.service"
done
while IFS='|' read -r name _; do
  [ -f "$SD/$name.timer" ] && systemctl restart "$name.timer"
done <<< "$TIMERS"
systemctl list-timers --no-pager | grep -E "watcher|medic|netops|mirror|writer|scout|doc-agent|backup|media|transcripts|builder|checker" || true
