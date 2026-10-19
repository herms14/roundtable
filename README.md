# Roundtable: homelab agent team

Code for the "Roundtable", a team of AI agents that run Hermes's homelab, and their web portal. In the lab it is deployed on `codex-agent-lxc` (CT211) as the `olympus-portal` service (port 8088, behind Traefik and Authentik).

This is the public, homelab-only edition: it contains the operational agents and their tooling. Personal and specialized assistants that exist in the private deployment are not part of this repository.

Part of the homelab: see [homelab-hub](https://github.com/herms14/homelab-hub) (hub), [homelab-docs](https://github.com/herms14/homelab-docs) (documentation) and [vlan-flow](https://github.com/herms14/vlan-flow) (VLAN traffic-flow diagram).

## The team

Gideon reports to Hermes; four leads own the work and every other agent reports to one of them.

| Lead | Team |
|---|---|
| Gideon (`boss`): COO & Enterprise Architect | Loretta, Ranni and Corhyn |
| Loretta (`security`): Security Lead | Millicent (`security_engineer`, approved remediation), Rogier (`iam`, access requests), Varys (`keeper`, OpenBao secrets) |
| Ranni (`governance`): Governance, ITSM, Compliance & FinOps | Morgott (`project_manager`), Enia (`compliance`), Boc (`cost`), Melina (`writer`), Kalé (`scout`), Radagon (`mirror`), Miriel (`scribe`), Blaidd (`checker`) |
| Corhyn (`medic`): ITOps & SRE | Diallos (`ipam`), Radahn (`devops`), Sellen (`watcher`), Torrent (`netops`), Iji (`backup`), Nepheli (`media`), Hewg (`builder`) |

Changes to systems go through owner approval (Discord ✅/❌ or the portal), are verified by Blaidd and documented by Miriel.

## Layout

| Path | What |
|---|---|
| `portal.py` | Web UI and API: overview links with Uptime Kuma DNS checks (`/api/dnschecks`), shared themes (`/theme-kit.css|js`), voice calls, reporting hierarchy, team wall and roll call, IP map, `/atlas`, dependency map (`/depmap`, drawn by the Atlas engine from `graph.json`) |
| `office.html` | 3D Roundtable Office (`/office`): agents at work, moods from live status, board meetings |
| `atlas.py`, `atlas_layout.json`, `atlas_engine.html` | Hermes Lab Atlas kept live by Miriel: layout + live Proxmox/Docker/Omada/systemd/agents overlay, rendered to `/atlas` (`atlas-sweep.timer`, every changelog entry, Redraw button) |
| `ipmap.py`, `ipam.py`, `inventory.py` | Diallos: IP map page and the vault IP registry (daily AI-assisted sync, 15-minute `watch`), IPAM reservations, live inventory (Proxmox, Omada, Docker) |
| `netops.py`, `dnssync.py`, `aclctl.py`, `acl_ruleset.json` | Torrent: network checks (both DNS servers and blocking), daily copy of Technitium local records to Pi-hole (`dns-sync.timer`), Gateway ACL rule set |
| `dns/` | Technitium DNS on homelab-dns01: idempotent setup script (`technitium_setup.py`) and Prometheus exporter (`technitium_exporter.py` + unit, :9274) |
| `keeper.py`, `bao_broker.py`, `secret_scopes.json` | Varys, keeper of secrets: scope plan, daily OpenBao check and Monday report, approval-gated access grants; the broker `olympus_core.load_env` uses to read each script's own scopes (never reads values itself) |
| `openbao/` | OpenBao on openbao-lxc (CT213): install script, idempotent import/policy/token setup (`bao_setup.py`), admin-token helper, value-free audit summary (+ timer), Traefik route |
| `vault.py` | Read-only Markdown mirror of the Obsidian vault (private folders, transcripts and anything credential-like are filtered out) and section-level note writes that never overwrite a concurrent edit |
| `labfeed.py` | Lab Feed: read-only lab JSON for Glance and the agents (:8790) |
| `voice.py` | Voice backend: OpenAI Realtime config, tools, local-voice text brain |
| `olympus_core.py`, `council.py`, `hierarchy.py` | Shared core (registry, ledger, LLM/Codex calls, secret stripping), Gideon's council, reporting hierarchy and work tracking |
| `<agent>.py` | One file per agent (`boss.py`, `security.py`, `builder.py`, ...) |
| `agents.json` | Agent registry: names, roles, reporting lines, personalities, art |
| `avatars/`, `avatars.py`, `character_art.py` | Demo-set agent artwork and animation clips; generators for painted portraits and original SVG character art |
| `desktop/` | Electron desktop companion (Gideon on the desktop, tray, Ctrl+Alt+A) |
| `install.sh`, `*.service` | Services and the `TIMERS` table for every scheduled agent run |
| `tests/` | Unit and headless-browser tests |

## Local voice

Speech-to-text and text-to-speech run on a PC (Whisper, Kokoro, Chatterbox clones); only text reaches the LXC. The PC server is not part of this repo.

## Secrets

No keys or tokens live in this repo. Each script reads its own scopes from OpenBao (token in `~/.olympus/bao/tokens/<script>.token`, see `bao_broker.py`); otherwise they are provided through environment variables or files on the host (for example `LOCAL_VOICE_TOKEN`, `TECHNITIUM_MONITOR_TOKEN`, `UPTIME_KUMA_API_KEY` in `~/.olympus_env`), which are not committed. Discord ids are host config too: `DISCORD_OWNER_ID` and `DISCORD_GUILD_ID` in the environment, channel ids in `~/.olympus/discord_channels.json` (`{"general": "<id>", ...}`). `dns/technitium_setup.py` reads its admin token from `/root/technitium-admin.secret` on the DNS host.

## Tests

```
python3 -m unittest discover -s tests -p 'test_*.py'
```

The browser tests in `tests/test_portal.py` need Chrome or Chromium (set `CHROME_BIN`). Some checks expect artwork and lab state that exist only on the deployed LXC, so a few fail from a clean checkout (agent portrait files, the dependency-map fixture, and the ACL, PBS and RFC fixtures).
