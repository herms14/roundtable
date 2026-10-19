#!/usr/bin/env python3
"""Hewg - compute/storage engineer, IaC blueprints and SRE patch-delivery owner under Corhyn.

  builder.py ask "<text>" [--reply-to MID] [--channel CID]  # Master Hermes asks (Gideon routes it)
  builder.py plan '<json spec>'      # validate + terraform plan, then post the blueprint for ✅
  builder.py build <name>            # terraform apply + ansible + verify + report (runs only after ✅)
  builder.py audit                   # daily compute/storage review; high/critical findings go to Corhyn
  builder.py status                  # print forge state (builds + last audit)
  builder.py patches                 # read patch backlog/history; never execute or approve

Hewg never fixes anything he finds: findings are recommendations, and high/critical ones are handed to Corhyn
(medic.py), who diagnoses and proposes the fix. Builds run on ansible-controller01 (~/hewg) and always need ✅.
He never destroys a guest.
Patch delivery uses the existing ~/sre_agent.py executor only. Corhyn owns ITOps oversight; owner approvals,
backup gates, windows and rollback remain unchanged. Hewg coordinates delivery and escalates blockers.
"""
import argparse
import concurrent.futures as cf
import ipaddress
import json
import hashlib
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

import olympus_core as o

AGENT = "builder"
CHANNEL = "roundtable-hold"
PLAN_DRAFT = "PLAN DRAFT FOR REVIEW ONLY."  # prefix of voice post_plan tasks (voice.PLAN_TASK)
AUDIT_CHANNEL = "homelab-infrastructure"
CTL = "ansible-controller01"
FORGE = "~/hewg"
NODES = ["node01", "node02", "node03"]
VM_TEMPLATE = {"id": 1000, "node": "node01", "name": "tpl-ubuntuv24.04-v1", "os": "ubuntu-24.04"}
PROVIDER = "0.93.0"
DNS, DOMAIN = ["192.168.90.60", "192.168.90.53"], "hrmsmrflrii.xyz"  # Technitium primary, Pi-hole secondary
UNTAGGED = {20}
VLANS = {20, 40, 90}
LIMITS = {"cores": 8, "memory_mb": 32768, "disk_gb": 500}
RAM_HEADROOM_GB, DISK_HEADROOM = 2, 1.10
KV_BUILDS, KV_AUDIT, KV_HANDOFF = "builder:builds", "builder:audit", "builder:handoff"
SEV_ICON = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🔵"}
SEV_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}
IJI_STORES = {"pbs-daily", "pbs-main"}  # backup storage belongs to Iji


# --------------------------------------------------------------------------- helpers

def ctl(script, timeout=600):
    return o.ssh(CTL, script, timeout=timeout)


def _json(txt, default):
    try:
        return json.loads(txt) if txt else default
    except json.JSONDecodeError:
        return default


def pve(script, timeout=60):
    errs = []
    for n in NODES:
        rc, out, err = o.ssh(n, script, timeout=timeout)
        if rc == 0:
            return out
        errs.append(f"{n}: {(err or str(rc))[:120]}")
    raise RuntimeError("Proxmox unreachable: " + "; ".join(errs))


def builds():
    return o.kv_get(KV_BUILDS, {}) or {}


def save_build(name, **kv):
    b = builds()
    b[name] = {**b.get(name, {}), **kv, "updated": o.iso()}
    o.kv_set(KV_BUILDS, b)
    return b[name]


def lxc_templates():
    """{os: (node, volid)} for every node that has the LXC template on 'local'."""
    out = {}

    def one(n):
        rc, txt, _ = o.ssh(n, f"pvesh get /nodes/{n}/storage/local/content --content vztmpl --output-format json",
                           timeout=30)
        return n, _json(txt, []) if rc == 0 else []

    with cf.ThreadPoolExecutor(3) as ex:
        for n, items in ex.map(one, NODES):
            for it in items:
                v = it.get("volid", "")
                for os_, pat in (("debian-12", "debian-12-standard"), ("ubuntu-24.04", "ubuntu-24.04-standard")):
                    if pat in v:
                        out.setdefault(os_, {})[n] = v
    return out


def ping(ip):
    return subprocess.run(["ping", "-c", "1", "-W", "1", ip], capture_output=True).returncode == 0


def used_ips(inv):
    ips = set()
    for g in inv.get("guests", []):
        ips.update(i.strip() for i in (g.get("ip") or "").split(",") if i.strip())
    try:
        import inventory
        net = inventory.get("network")
        for c in net.get("clients", []) if isinstance(net, dict) else []:
            if c.get("ip"):
                ips.add(c["ip"])
    except Exception:  # noqa: BLE001
        pass
    for h in o.load_hosts().values():
        ips.add(h.get("ip"))
    return ips


# --------------------------------------------------------------------------- spec + validation

SPEC = o.S(action=o.E("build", "audit", "status", "answer"),
           kind=o.E("lxc", "vm"), name=o.STR, os=o.E("debian-12", "ubuntu-24.04"), cores={"type": "integer"},
           memory_mb={"type": "integer"}, disk_gb={"type": "integer"}, vlan={"type": "integer"}, ip=o.STR,
           node=o.STR, purpose=o.STR, docker={"type": "boolean"}, packages=o.A(o.STR), missing=o.A(o.STR))

BUILD_INTENT = re.compile(
    r"\b(deploy|create|build|provision|spin up|set up|forge|make)\b[^.\n]{0,80}\b(lxc|vm|container|guest|ct)\b|"
    r"\b(lxc|vm|container|guest|ct)\b[^.\n]{0,80}\b(deploy|create|build|provision)\b|\bnew (lxc|vm|container)\b", re.I)
PKG_RE = re.compile(r"[a-z0-9][a-z0-9.+-]{1,40}")


def validate(spec):
    """Fill auto fields and check everything. Returns (spec, problems, notes)."""
    import inventory
    inv = inventory.get("compute", fresh=True)
    if inv.get("error"):
        return spec, [inv["error"]], []
    p, notes = [], []
    s = dict(spec)
    s["kind"] = s.get("kind") if s.get("kind") in ("lxc", "vm") else "lxc"
    s["name"] = re.sub(r"[^a-z0-9-]", "-", (s.get("name") or "").strip().lower()).strip("-")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,40}", s["name"] or ""):
        p.append("a valid hostname (letters, digits, dashes)")
    elif any((g.get("name") or "").lower() == s["name"] for g in inv["guests"]):
        p.append(f"a new name: `{s['name']}` already exists")
    elif s["name"] in builds() and builds()[s["name"]].get("status") in ("planned", "building"):
        p.append(f"a new name: `{s['name']}` is already being forged")
    s["cores"] = int(s.get("cores") or 2)
    s["memory_mb"] = int(s.get("memory_mb") or 2048)
    s["disk_gb"] = int(s.get("disk_gb") or (8 if s["kind"] == "lxc" else 32))
    for k, lim in LIMITS.items():
        if not 1 <= s[k] <= lim:
            p.append(f"{k} between 1 and {lim} (asked {s[k]})")
    if s["kind"] == "vm":
        s["os"] = VM_TEMPLATE["os"]
        s["disk_gb"] = max(s["disk_gb"], 8)
        s["memory_mb"] = max(s["memory_mb"], 1024)
    else:
        s["os"] = s.get("os") if s.get("os") in ("debian-12", "ubuntu-24.04") else "debian-12"
        s["memory_mb"] = max(s["memory_mb"], 256)
    s["vlan"] = int(s.get("vlan") or 40)
    if s["vlan"] not in VLANS:
        p.append(f"a VLAN Hewg may build on ({', '.join(map(str, sorted(VLANS)))}); asked {s['vlan']}")
    net = ipaddress.ip_network(f"192.168.{s['vlan']}.0/24") if 1 < s["vlan"] < 255 else None
    # node
    nodes = {n["node"]: n for n in inv["nodes"] if n.get("status") == "online"}
    if s["kind"] == "vm":
        if s.get("node") not in (None, "", "auto", VM_TEMPLATE["node"]):
            notes.append(f"VMs are cloned from template {VM_TEMPLATE['id']}, which lives on {VM_TEMPLATE['node']}; "
                         f"building there (migrate later if you like)")
        s["node"] = VM_TEMPLATE["node"]
        s["template"] = f"{VM_TEMPLATE['id']} {VM_TEMPLATE['name']}"
    else:
        tpl = lxc_templates().get(s["os"], {})
        cand = [n for n in tpl if n in nodes]
        if s.get("node") not in (None, "", "auto"):
            if s["node"] not in cand:
                p.append(f"a node with the {s['os']} template (available on {', '.join(cand) or 'none'})")
            cand = [s["node"]] if s["node"] in cand else cand
        if not cand:
            p.append(f"the {s['os']} LXC template on an online node")
        else:
            def unalloc(nn):
                used = sum(g.get("ram_max_gb") or 0 for g in inv["guests"]
                           if g["node"] == nn and g["status"] == "running" and not g["template"])
                return nodes[nn]["ram_max_gb"] - used
            s["node"] = max(cand, key=lambda nn: min(unalloc(nn), nodes[nn]["ram_max_gb"] - nodes[nn]["ram_gb"]))
            s["template"] = tpl[s["node"]]
    n = nodes.get(s.get("node"))
    if n:
        free = n["ram_max_gb"] - n["ram_gb"]
        need = s["memory_mb"] / 1024 + RAM_HEADROOM_GB
        if free < need:
            p.append(f"more free RAM on {n['node']}: {free:.1f} GB free, needs {need:.1f} GB with headroom")
        ll = next((x for x in inv["storage"] if x["storage"] == "local-lvm" and x["node"] == n["node"]), None)
        if ll and ll["size_gb"] - ll["used_gb"] < s["disk_gb"] * DISK_HEADROOM:
            p.append(f"more space on {n['node']} local-lvm: {ll['size_gb'] - ll['used_gb']:.0f} GB free")
        s["capacity"] = (f"{n['node']}: {free:.1f}/{n['ram_max_gb']:.0f} GB RAM free, CPU {n['cpu_pct']}%"
                         + (f"; local-lvm {ll['size_gb'] - ll['used_gb']:.0f} GB free" if ll else ""))
    import ipam
    ip = (s.get("ip") or "auto").strip().split("/")[0]
    if net and not p:
        try:
            reservation = ipam.reserve(s["name"], s["vlan"], ip)
            ip = reservation["ip"]
            notes.append(f"Diallos/IPAM reserved {ip} for {s['name']} until {reservation['expires']} UTC")
        except (ValueError, RuntimeError) as error:
            p.append(f"IPAM approval: {error}")
    s["ip"], s["gw"] = ip, f"192.168.{s['vlan']}.1"
    try:
        s["vmid"] = int(pve("pvesh get /cluster/nextid").strip().strip('"'))
    except Exception as e:  # noqa: BLE001
        p.append(f"a free VMID ({e})")
    s["docker"] = bool(s.get("docker"))
    pkgs = []
    for x in (s.get("packages") or [])[:8]:
        x = str(x).strip().lower()
        if not PKG_RE.fullmatch(x):
            p.append(f"a plain apt package name (got `{x[:40]}`)")
        elif x not in pkgs:
            pkgs.append(x)
    s["packages"] = pkgs
    s["purpose"] = (s.get("purpose") or "").strip()[:200]
    return s, p, notes


# --------------------------------------------------------------------------- terraform + ansible

def _hcl(v):
    return json.dumps(v)


def render_tf(s, agent_on=False):
    vlan = "" if s["vlan"] in UNTAGGED else f"\n    vlan_id = {s['vlan']}"
    desc = f"{s['purpose'] or s['name']} - built by Hewg (Roundtable, Terraform) {o.local().strftime('%Y-%m-%d')}"
    head = f"""terraform {{
  required_providers {{
    proxmox = {{
      source  = "bpg/proxmox"
      version = "{PROVIDER}"
    }}
  }}
}}

variable "proxmox_api_url" {{
  type = string
}}

variable "proxmox_api_token" {{
  type      = string
  sensitive = true
}}

provider "proxmox" {{
  endpoint  = var.proxmox_api_url
  api_token = var.proxmox_api_token
  insecure  = true
}}

locals {{
  ssh_key = trimspace(file("${{path.module}}/../../guest_key.pub"))
}}
"""
    if s["kind"] == "lxc":
        ostype = "ubuntu" if s["os"].startswith("ubuntu") else "debian"
        return head + f"""
resource "proxmox_virtual_environment_container" "guest" {{
  node_name     = {_hcl(s['node'])}
  vm_id         = {s['vmid']}
  description   = {_hcl(desc)}
  tags          = ["hewg", "terraform"]
  unprivileged  = true
  start_on_boot = true
  started       = true

  features {{
    nesting = true
  }}

  cpu {{
    cores = {s['cores']}
  }}

  memory {{
    dedicated = {s['memory_mb']}
    swap      = 512
  }}

  disk {{
    datastore_id = "local-lvm"
    size         = {s['disk_gb']}
  }}

  operating_system {{
    template_file_id = {_hcl(s['template'])}
    type             = "{ostype}"
  }}

  network_interface {{
    name   = "eth0"
    bridge = "vmbr0"{vlan}
  }}

  initialization {{
    hostname = {_hcl(s['name'])}

    ip_config {{
      ipv4 {{
        address = "{s['ip']}/24"
        gateway = "{s['gw']}"
      }}
    }}

    dns {{
      domain  = "{DOMAIN}"
      servers = {json.dumps(DNS)}
    }}

    user_account {{
      keys = [local.ssh_key]
    }}
  }}
}}

output "summary" {{
  value = "LXC ${{proxmox_virtual_environment_container.guest.vm_id}} on {s['node']}"
}}
"""
    return head + f"""
resource "proxmox_virtual_environment_vm" "guest" {{
  name        = {_hcl(s['name'])}
  node_name   = {_hcl(s['node'])}
  vm_id       = {s['vmid']}
  description = {_hcl(desc)}
  tags        = ["hewg", "terraform"]
  on_boot     = true
  started     = true

  clone {{
    vm_id        = {VM_TEMPLATE['id']}
    full         = true
    datastore_id = "local-lvm"
  }}

  agent {{
    enabled = {str(agent_on).lower()}
    timeout = "5m"
  }}

  cpu {{
    cores = {s['cores']}
    type  = "host"
  }}

  memory {{
    dedicated = {s['memory_mb']}
  }}

  disk {{
    datastore_id = "local-lvm"
    interface    = "scsi0"
    size         = {s['disk_gb']}
  }}

  network_device {{
    bridge = "vmbr0"{vlan}
  }}

  initialization {{
    datastore_id = "local-lvm"

    ip_config {{
      ipv4 {{
        address = "{s['ip']}/24"
        gateway = "{s['gw']}"
      }}
    }}

    dns {{
      domain  = "{DOMAIN}"
      servers = {json.dumps(DNS)}
    }}

    user_account {{
      username = "hermes-admin"
      keys     = [local.ssh_key]
    }}
  }}
}}

output "summary" {{
  value = "VM ${{proxmox_virtual_environment_vm.guest.vm_id}} on {s['node']}"
}}
"""


PLAYBOOK = r"""---
# Hewg's base playbook (written by builder.py; edits are overwritten). Builtin modules only.
- name: Hewg base configuration
  hosts: all
  gather_facts: false
  vars:
    admin: hermes-admin
  tasks:
    - name: Wait for SSH
      ansible.builtin.wait_for_connection:
        timeout: 420

    - name: Gather facts
      ansible.builtin.setup:

    - name: Wait for cloud-init / first-boot apt locks
      ansible.builtin.shell: |
        command -v cloud-init >/dev/null && cloud-init status --wait >/dev/null 2>&1 || true
        for i in $(seq 1 60); do fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1 || exit 0; sleep 5; done
      become: true
      changed_when: false

    - name: Upgrade packages
      ansible.builtin.apt:
        update_cache: true
        upgrade: dist
      become: true

    - name: Base packages
      ansible.builtin.apt:
        name: [curl, ca-certificates, vim, htop, sudo, gnupg, unattended-upgrades]
        state: present
      become: true

    # Proxmox's one-shot first-boot unit can fail on a fresh LXC; redo its job (a unique snakeoil pair) and clear
    # the failed state so the guest starts clean. Output is kept so a real error is visible in the build log.
    - name: Repair Proxmox first-boot snakeoil unit (LXC)
      ansible.builtin.shell: |
        systemctl is-failed --quiet proxmox-regenerate-snakeoil.service || { echo "unit ok"; exit 0; }
        journalctl -u proxmox-regenerate-snakeoil.service -b --no-pager -n 15 || true
        if command -v make-ssl-cert >/dev/null; then make-ssl-cert -f generate-default-snakeoil && echo "regenerated"; fi
        systemctl reset-failed proxmox-regenerate-snakeoil.service
        echo "reset"
      become: true
      register: snakeoil
      changed_when: "'reset' in snakeoil.stdout"
      failed_when: false
      when: guest_kind == "lxc"

    - name: Snakeoil repair output
      ansible.builtin.debug:
        var: snakeoil.stdout_lines
      when: guest_kind == "lxc" and snakeoil is changed

    - name: QEMU guest agent (VMs)
      ansible.builtin.apt:
        name: qemu-guest-agent
        state: present
      become: true
      when: guest_kind == "vm"

    - name: Enable QEMU guest agent (VMs)
      ansible.builtin.systemd:
        name: qemu-guest-agent
        state: started
        enabled: true
      become: true
      when: guest_kind == "vm"
      failed_when: false

    - name: Timezone Asia/Manila
      ansible.builtin.shell: timedatectl set-timezone Asia/Manila && echo Asia/Manila > /etc/timezone
      become: true
      changed_when: false
      failed_when: false

    - name: Admin user (LXC)
      ansible.builtin.user:
        name: "{{ admin }}"
        shell: /bin/bash
        groups: sudo
        append: true
      when: guest_kind == "lxc"

    - name: Admin SSH key (LXC)
      ansible.builtin.shell: |
        install -d -m 700 -o {{ admin }} -g {{ admin }} /home/{{ admin }}/.ssh
        install -m 600 -o {{ admin }} -g {{ admin }} /root/.ssh/authorized_keys /home/{{ admin }}/.ssh/authorized_keys
      when: guest_kind == "lxc"

    - name: Passwordless sudo for the admin user
      ansible.builtin.copy:
        dest: /etc/sudoers.d/90-hermes-admin
        content: "{{ admin }} ALL=(ALL) NOPASSWD:ALL\n"
        mode: "0440"
        validate: visudo -cf %s
      become: true

    - name: Requested packages
      ansible.builtin.apt:
        name: "{{ packages }}"
        state: present
      become: true
      when: packages | length > 0

    - name: Enable requested services
      ansible.builtin.shell: "systemctl enable --now {{ item }} 2>&1 || true"
      loop: "{{ packages }}"
      become: true
      changed_when: false

    - name: Docker engine
      ansible.builtin.shell: curl -fsSL https://get.docker.com | sh
      args:
        creates: /usr/bin/docker
      become: true
      when: docker | bool

    - name: Admin user in docker group
      ansible.builtin.user:
        name: "{{ admin }}"
        groups: docker
        append: true
      become: true
      when: docker | bool
"""


def _deploy_dir(name):
    return f"{FORGE}/deployments/{name}"


def write_ctl_file(path, content):
    q = shlex.quote
    rc, out, err = ctl(f"mkdir -p $(dirname {path}) && cat > {path} <<'HEWG_EOF'\n{content}\nHEWG_EOF\n"
                       f"chmod 600 {path}", timeout=60)
    if rc != 0:
        raise RuntimeError(f"controller write failed ({q(path)}): {err[:200]}")


TF_ENV = f"export TF_PLUGIN_CACHE_DIR={FORGE}/.plugin-cache TF_IN_AUTOMATION=1 TF_INPUT=0; "


def tf(name, args, timeout=900):
    d = _deploy_dir(name)
    return ctl(f"{TF_ENV} cd {d} && ln -sf ../../secrets.auto.tfvars secrets.auto.tfvars && "
               f"terraform {args} -no-color 2>&1", timeout=timeout)


def plan(spec, reply_to=None, channel=None):
    channel = channel or CHANNEL
    try:
        return _plan(spec, reply_to, channel)
    except Exception as e:  # noqa: BLE001
        o.respond(AGENT, channel, "⚠️ Hewg could not draw the blueprint", f"{str(e)[:3000]}", reply_to=reply_to,
                  color=o.SEV_COLORS["high"])
        raise


def _post_iac(s, kind, channel, reply_to):
    """Attach the exact Terraform code, the rendered plan and the Ansible playbook for review before approval."""
    try:
        name = s["name"]
        rc, plan_txt, _ = tf(name, "show -no-color tfplan", timeout=120)
        _, main_tf, _ = ctl(f"cat {_deploy_dir(name)}/main.tf", timeout=30)
        files = [(f"{name}-main.tf", main_tf), (f"{name}-terraform-plan.txt", plan_txt if rc == 0 else "plan unavailable"),
                 (f"{name}-base-playbook.yml", PLAYBOOK)]
        o.post(channel, f"📜 **IaC for {kind} `{name}`** — Terraform code, rendered plan and Ansible playbook "
                        f"Radahn will run if you approve:", files=files, reply_to=reply_to)
    except Exception as e:  # review attachments must never block the approval request
        print(f"iac attach failed: {e}", flush=True)


def _plan(spec, reply_to, channel):
    with o.Run(AGENT, f"plan {spec.get('kind')} {spec.get('name')}", "chat" if reply_to else "manual") as run:
        s, problems, notes = validate(spec)
        if problems:
            run.summary = "blueprint rejected: " + "; ".join(problems)[:250]
            o.respond(AGENT, channel, "🔨 Hewg needs a better blueprint",
                      "I measured twice and it does not fit yet, Master Hermes. I need:\n" +
                      "\n".join(f"• {x}" for x in problems), reply_to=reply_to, run=run, color=o.SEV_COLORS["medium"])
            return None
        write_ctl_file(f"{_deploy_dir(s['name'])}/main.tf", render_tf(s))
        rc, out, _ = tf(s["name"], "init -upgrade=false", timeout=600)
        if rc != 0:
            raise RuntimeError("terraform init failed:\n" + out[-1500:])
        rc, out, _ = tf(s["name"], "validate", timeout=120)
        if rc != 0:
            raise RuntimeError("terraform validate failed:\n" + out[-1500:])
        rc, out, _ = tf(s["name"], "plan -out=tfplan", timeout=600)
        m = re.search(r"Plan: \d+ to add, \d+ to change, \d+ to destroy\.", out)
        if rc != 0 or not m:
            raise RuntimeError("terraform plan failed:\n" + out[-1500:])
        if not m.group(0).endswith("0 to destroy."):
            raise RuntimeError("New-service blueprints must not destroy existing resources")
        rc, hashes, _ = ctl(f"cd {_deploy_dir(s['name'])} && sha256sum main.tf tfplan", timeout=60)
        if rc:
            raise RuntimeError("Could not fingerprint the saved Terraform plan")
        fingerprints = {line.split()[1]: line.split()[0] for line in hashes.splitlines() if len(line.split()) == 2}
        if set(fingerprints) != {"main.tf", "tfplan"}:
            raise RuntimeError("Saved-plan fingerprints are incomplete")
        reviewed = {"name": s["name"], "spec_sha": hashlib.sha256(json.dumps(s, sort_keys=True).encode()).hexdigest(),
                    "fingerprints": fingerprints, "playbook_sha": hashlib.sha256(PLAYBOOK.encode()).hexdigest()}
        save_build(s["name"], spec=s, status="planned", plan=m.group(0), created=o.iso(), channel=channel,
                   reply_to=reply_to)
        kind = "LXC" if s["kind"] == "lxc" else "VM"
        desc = (f"**{kind} `{s['name']}`** · VMID **{s['vmid']}** on **{s['node']}**\n"
                f"• {s['cores']} vCPU · {s['memory_mb'] // 1024 if s['memory_mb'] >= 1024 else s['memory_mb']}"
                f"{' GB' if s['memory_mb'] >= 1024 else ' MB'} RAM · {s['disk_gb']} GB on local-lvm\n"
                f"• {s['os']} from `{s['template']}`\n"
                f"• VLAN {s['vlan']}{' (untagged)' if s['vlan'] in UNTAGGED else ''} · IP **{s['ip']}/24** · gw {s['gw']}"
                f" · DNS {', '.join(DNS)}\n"
                f"• Ansible: updates, base packages, timezone, `hermes-admin` with your homelab key + sudo"
                f"{', Docker engine' if s['docker'] else ''}{', QEMU guest agent' if s['kind'] == 'vm' else ''}"
                f"{', packages: ' + ', '.join(s['packages']) if s.get('packages') else ''}\n"
                f"• Purpose: {s['purpose'] or '-'}\n"
                f"• Capacity: {s.get('capacity', '?')}\n"
                f"• Terraform: `{m.group(0)}` (bpg/proxmox {PROVIDER}, `~/hewg/deployments/{s['name']}` on "
                f"ansible-controller01)"
                + ("\n" + "\n".join(f"ℹ️ {x}" for x in notes) if notes else "")
                + ("\nAfter Ansible, enable QEMU agent and reboot this new VM once." if s["kind"] == "vm" else "")
                + "\nRadahn executes this exact saved IaC plan; Blaidd re-checks it, Miriel logs it, Iji offers a backup job.")
        _post_iac(s, kind, channel, reply_to)
        o.request_approval("devops", f"Forge {kind} {s['name']} ({s['ip']})", desc,
                           [sys.executable, os.path.abspath(__file__), "build", s["name"]], channel,
                           severity="medium", payload=reviewed, reply_to=reply_to)
        run.summary = f"blueprint for {kind} {s['name']} {s['ip']} posted for approval"
        return s


def _ansible(s):
    user = "root" if s["kind"] == "lxc" else "hermes-admin"
    write_ctl_file(f"{FORGE}/playbooks/base.yml", PLAYBOOK)
    # own config so the controller's ~/ansible settings (vault files, inventories) never leak in
    write_ctl_file(f"{FORGE}/ansible.cfg", "[defaults]\nhost_key_checking = False\nretry_files_enabled = False\n"
                                           "interpreter_python = auto_silent\n")
    kh = f"{FORGE}/known_hosts"
    extra = json.dumps({"guest_kind": s["kind"], "docker": s["docker"], "packages": s.get("packages") or []})
    script = (f"ssh-keygen -f {kh} -R {s['ip']} >/dev/null 2>&1; cd {FORGE} && "
              f"ANSIBLE_HOST_KEY_CHECKING=False ANSIBLE_STDOUT_CALLBACK=default ANSIBLE_CALLBACKS_ENABLED= "
              f"ANSIBLE_CONFIG=\"$PWD/ansible.cfg\" ANSIBLE_RETRY_FILES_ENABLED=0 "
              f"ansible-playbook -i {s['ip']}, -u {user} --private-key ~/.ssh/homelab_ed25519 "
              f"--ssh-common-args '-o UserKnownHostsFile={kh} -o StrictHostKeyChecking=accept-new' "
              f"-e {shlex.quote(extra)} playbooks/base.yml 2>&1")
    return ctl(script, timeout=2400)


def _recap(out):
    m = re.search(r"(\S+)\s*:\s*ok=(\d+)\s+changed=(\d+)\s+unreachable=(\d+)\s+failed=(\d+)", out or "")
    return (f"ok={m.group(2)} changed={m.group(3)} unreachable={m.group(4)} failed={m.group(5)}", m.group(4) == "0"
            and m.group(5) == "0") if m else ("no recap", False)


def _register_inventory(s):
    line = f"{s['name']} ansible_host={s['ip']}"
    rc, _, error = ctl(f"cd ~/ansible && grep -q '^{s['name']} ' inventory.ini || {{ grep -q '^\\[hewg_built\\]' inventory.ini || "
        f"printf '\\n# Guests forged by Hewg (Roundtable)\\n[hewg_built]\\n' >> inventory.ini; "
        f"sed -i '/^\\[hewg_built\\]/a {line}' inventory.ini; }}", timeout=60)
    if rc:
        raise RuntimeError(f"Ansible inventory registration failed: {error[:300]}")


def build(name):
    import hierarchy
    import ipam
    approval = hierarchy.require_approval("devops")
    if approval["payload"].get("name") != name:
        raise PermissionError("Approved deployment does not match this blueprint")
    b = builds().get(name)
    if not b:
        print(f"no blueprint for {name}")
        sys.exit(1)
    s, channel, reply_to = b["spec"], b.get("channel") or CHANNEL, b.get("reply_to")
    reviewed = approval["payload"]
    if (reviewed.get("spec_sha") != hashlib.sha256(json.dumps(s, sort_keys=True).encode()).hexdigest() or
            reviewed.get("playbook_sha") != hashlib.sha256(PLAYBOOK.encode()).hexdigest()):
        raise PermissionError("Blueprint/playbook has changed or predates plan fingerprints; re-plan and re-approve")
    rc, hashes, _ = ctl(f"cd {_deploy_dir(name)} && sha256sum main.tf tfplan", timeout=60)
    if rc or {line.split()[1]: line.split()[0] for line in hashes.splitlines() if len(line.split()) == 2} != reviewed.get("fingerprints"):
        raise PermissionError("Saved Terraform files differ from the owner-reviewed plan")
    kind = "LXC" if s["kind"] == "lxc" else "VM"
    if b.get("status") == "built":
        print(f"{name} is already built")
        return True
    resume = b.get("status") in ("failed", "building")  # a retry: our own half-built guest may already exist
    save_build(name, status="building", started=o.iso())
    with o.Run("devops", f"build {kind} {name}", "approval") as run:
        steps = []

        def fail(msg):
            save_build(name, status="failed", error=msg[:1500])
            run.summary = f"{kind} {name} build failed: {msg[:200]}"
            o.respond(AGENT, channel, f"⚠️ Hewg's forge failed · {kind} {name}",
                      "\n".join(steps) + f"\n\n❌ {msg[:3000]}\n\nNothing was destroyed; the Terraform state is in "
                      f"`~/hewg/deployments/{name}` on ansible-controller01. Ask Corhyn to look if you want it fixed.",
                      reply_to=reply_to, run=run, color=o.SEV_COLORS["high"])
            run.finish("failed", run.summary)
            return False

        # re-check what may have changed since the plan
        import inventory
        inv = inventory.get("compute", fresh=True)
        if inv.get("error"):
            return fail(f"Compute revalidation failed: {inv['error']}")
        mine = any(g["id"] == s["vmid"] and (g.get("name") or "").lower() == name for g in inv.get("guests", []))
        try:
            ipam.verify(name, s["ip"], s["vlan"], existing=bool(resume and mine))
        except (ValueError, PermissionError, RuntimeError) as error:
            return fail(f"Diallos/IPAM refused deployment: {error}")
        if resume and mine:
            steps.append(f"↩️ Resuming: {kind} {s['vmid']} already exists from the earlier attempt")
        elif any(g["id"] == s["vmid"] or (g.get("name") or "").lower() == name for g in inv.get("guests", [])):
            return fail(f"VMID {s['vmid']} or name {name} was taken since the blueprint; ask me again for a new one")
        elif s["ip"] in used_ips(inv) or ping(s["ip"]):
            return fail(f"IP {s['ip']} is in use now; ask me again for a new blueprint")
        o.post(channel, f"🔨 Hewg is forging **{kind} `{name}`** (VMID {s['vmid']}, {s['ip']}) on {s['node']}…",
               reply_to=reply_to)
        t0 = time.time()
        ipam.activate(name)  # hold the address even if Terraform partially applies then fails
        rc, out, _ = tf(name, "apply -auto-approve tfplan", timeout=1800)
        if rc != 0:
            return fail("terraform apply failed:\n```\n" + out[-1800:] + "\n```")
        steps.append(f"✅ Terraform: {kind} {s['vmid']} created and started ({(time.time() - t0) / 60:.1f} min)")
        rc, out, _ = _ansible(s)
        recap, ok = _recap(out)
        if not ok:
            return fail("Ansible base configuration failed (" + recap + "):\n```\n" + out[-1800:] + "\n```")
        steps.append(f"✅ Ansible base playbook: {recap}")
        if s["kind"] == "vm":
            rc, out, error = o.ssh(s["node"], f"set -e\nqm set {s['vmid']} --agent enabled=1\nqm reboot {s['vmid']}", timeout=180)
            if rc:
                return fail(f"Reviewed QEMU agent enable/reboot failed: {error or out}")
            write_ctl_file(f"{_deploy_dir(name)}/main.tf", render_tf(s, agent_on=True))
            steps.append("✅ Reviewed QEMU guest agent flag enabled; new VM rebooted once")
        _register_inventory(s)
        steps.append("✅ Added to `~/ansible/inventory.ini` group `[hewg_built]`")
        # verify: Blaidd's independent check
        try:
            import checker
            v = checker.verify_guest(s["ip"], expect={"docker": s["docker"], "name": name}, quiet=True)
            steps.append(f"{'✅' if v['verdict'] == 'pass' else '🟡' if v['verdict'] == 'warn' else '❌'} Blaidd "
                         f"verified: {v['verdict']}" + (f" ({'; '.join(v['problems'])})" if v["problems"] else "")
                         + (f" · {'; '.join(v['info'][:4])}" if v.get("info") else ""))
            if v["verdict"] != "pass":
                return fail("Independent verification is incomplete: " + "; ".join(v["problems"]))
        except Exception as e:  # noqa: BLE001
            return fail(f"Blaidd could not verify: {e}")
        mins = (time.time() - t0) / 60
        try:
            import scribe
            if not scribe.log_change(f"Hewg forged {kind} `{name}` (VMID {s['vmid']}) on {s['node']}",
                              f"{s['cores']} vCPU, {s['memory_mb']} MB RAM, {s['disk_gb']} GB local-lvm, {s['os']}, "
                              f"VLAN {s['vlan']}, IP {s['ip']}. Purpose: {s['purpose'] or '-'}. Terraform "
                              f"(bpg/proxmox) + Ansible base playbook from ansible-controller01 "
                              f"`~/hewg/deployments/{name}`." + (" Docker installed." if s["docker"] else ""),
                              agent="devops", section="Added"):
                return fail("Resource exists but documentation did not complete")
            steps.append("✅ Miriel logged it in the Homelab Changelog")
        except Exception as e:  # noqa: BLE001
            return fail(f"Resource exists but changelog not updated: {e}")
        save_build(name, status="built", built=o.iso(), minutes=round(mins, 1), error=None)
        run.summary = f"{kind} {name} ({s['vmid']}, {s['ip']}) built in {mins:.1f} min"
        o.respond(AGENT, channel, f"🔨 Forged · {kind} {name}",
                  f"**{kind} `{name}`** is up, Master Hermes: VMID **{s['vmid']}** on **{s['node']}**, "
                  f"`ssh hermes-admin@{s['ip']}`.\n\n" + "\n".join(steps) +
                  f"\n\n⏱️ {mins:.1f} min · Iji will propose a backup job on his next watch.",
                  reply_to=reply_to, run=run, color=o.SEV_COLORS["ok"])
        return True


# --------------------------------------------------------------------------- decommission

DELETE_INTENT = re.compile(r"\b(delete|destroy|remove|decommission|tear ?down|retire|get rid of)\b", re.I)


def _resolve_targets(text):
    """Map VMIDs/names in the request to guests Hewg forged (the only ones with Terraform state)."""
    known = builds()
    ids = {int(x) for x in re.findall(r"\b\d{3,5}\b", text)}
    found, unknown = [], []
    for name, b in known.items():
        vmid = (b.get("spec") or {}).get("vmid")
        if b.get("status") != "destroyed" and (vmid in ids or re.search(rf"\b{re.escape(name)}\b", text, re.I)):
            found.append(name)
    named = {(known[n].get("spec") or {}).get("vmid") for n in found}
    unknown = sorted(str(i) for i in ids if i not in named)
    return found, unknown


def plan_destroy(text, reply_to=None, channel=None):
    channel = channel or CHANNEL
    with o.Run(AGENT, f"plan destroy: {text[:150]}", "chat" if reply_to else "manual") as run:
        names, unknown = _resolve_targets(text)
        if not names:
            o.respond(AGENT, channel, "🗑️ Hewg needs exact targets",
                      "I can only retire guests I forged with Terraform. Give me their name or VMID" +
                      (f" (I don't know {', '.join(unknown)})." if unknown else "."),
                      reply_to=reply_to, run=run, color=o.SEV_COLORS["medium"])
            return None
        o.post(channel, "🗑️ Hewg is drawing the teardown plan for " + ", ".join(f"`{n}`" for n in names) +
               " — the approval will follow in a moment.", reply_to=reply_to)
        import inventory
        inv = inventory.get("compute", fresh=True)
        live = {(g.get("name") or "").lower(): g for g in inv.get("guests", [])}
        fingerprints, lines, files = {}, [], []
        for n in names:
            s = builds()[n]["spec"]
            g = live.get(n)
            if not g or g["id"] != s["vmid"]:
                lines.append(f"• `{n}` (VMID {s['vmid']}) is not in the live inventory; nothing to destroy")
                continue
            rc, out, _ = tf(n, "plan -destroy -out=tfdestroy", timeout=300)
            m = re.search(r"Plan: 0 to add, 0 to change, (\d+) to destroy\.", out)
            if rc != 0 or not m:
                raise RuntimeError(f"terraform destroy plan failed for {n}:\n" + out[-1500:])
            rc, hashes, _ = ctl(f"cd {_deploy_dir(n)} && sha256sum main.tf tfdestroy", timeout=60)
            fp = {x.split()[1]: x.split()[0] for x in hashes.splitlines() if len(x.split()) == 2}
            if rc or set(fp) != {"main.tf", "tfdestroy"}:
                raise RuntimeError(f"Could not fingerprint the destroy plan for {n}")
            fingerprints[n] = fp
            _, shown, _ = tf(n, "show -no-color tfdestroy", timeout=120)
            files.append((f"{n}-terraform-destroy-plan.txt", shown))
            kind = "LXC" if s["kind"] == "lxc" else "VM"
            lines.append(f"• **{kind} `{n}`** · VMID **{s['vmid']}** on {g.get('node', s['node'])} · {s['ip']} · "
                         f"{g.get('status', '?')} · `{m.group(0)}`")
        if not fingerprints:
            o.respond(AGENT, channel, "🗑️ Nothing to destroy", "\n".join(lines), reply_to=reply_to, run=run)
            return None
        o.post(channel, "📜 **Terraform destroy plan** Radahn will execute if you approve:", files=files,
               reply_to=reply_to)
        desc = ("**Permanent deletion** of:\n" + "\n".join(lines) +
                "\n\n• Disks and data are destroyed; PBS backups already taken are kept.\n"
                "• Afterwards: IPAM address released, Ansible inventory entry removed, Blaidd confirms the guests "
                "are gone, Miriel logs it, and any backup-job entries are left for Iji.\n"
                "• Rollback: none for data; rebuild from a blueprint or restore from backup.")
        o.request_approval("devops", "Destroy " + ", ".join(fingerprints), desc,
                           [sys.executable, os.path.abspath(__file__), "destroy", ",".join(fingerprints)], channel,
                           severity="high", payload={"names": sorted(fingerprints), "fingerprints": fingerprints,
                                                     "action": "destroy"}, reply_to=reply_to)
        run.summary = "teardown plan for " + ", ".join(fingerprints) + " posted for approval"
        return list(fingerprints)


def destroy(names_csv):
    import hierarchy
    import ipam
    approval = hierarchy.require_approval("devops")
    names = [n for n in names_csv.split(",") if n]
    reviewed = approval["payload"]
    if reviewed.get("action") != "destroy" or sorted(names) != reviewed.get("names"):
        raise PermissionError("Approved teardown does not match these targets")
    channel = approval.get("channel_id") or CHANNEL
    with o.Run("devops", f"destroy {', '.join(names)}", "approval") as run:
        steps, ok_all = [], True
        for n in names:
            b = builds().get(n)
            if not b:
                steps.append(f"❌ `{n}`: no build record")
                ok_all = False
                continue
            s = b["spec"]
            rc, hashes, _ = ctl(f"cd {_deploy_dir(n)} && sha256sum main.tf tfdestroy", timeout=60)
            if rc or {x.split()[1]: x.split()[0] for x in hashes.splitlines() if len(x.split()) == 2} != reviewed["fingerprints"].get(n):
                steps.append(f"❌ `{n}`: saved destroy plan differs from the one you reviewed; re-plan")
                ok_all = False
                continue
            rc, out, _ = tf(n, "apply -auto-approve tfdestroy", timeout=900)
            if rc != 0:
                steps.append(f"❌ `{n}`: terraform destroy failed:\n```\n{out[-1200:]}\n```")
                ok_all = False
                continue
            import inventory
            still = any(g["id"] == s["vmid"] for g in inventory.get("compute", fresh=True).get("guests", []))
            if still:
                steps.append(f"❌ `{n}`: VMID {s['vmid']} still exists after destroy")
                ok_all = False
                continue
            steps.append(f"✅ `{n}` (VMID {s['vmid']}) destroyed and confirmed gone")
            ipam.release(n)
            ctl(f"cd ~/ansible && sed -i '/^{n} /d' inventory.ini", timeout=60)
            steps.append(f"✅ `{n}`: {s['ip']} released in IPAM, removed from Ansible inventory")
            save_build(n, status="destroyed", destroyed=o.iso())
            try:
                import scribe
                scribe.log_change(f"Hewg destroyed {s['kind'].upper()} `{n}` (VMID {s['vmid']})",
                                  f"Owner-approved Terraform destroy on {s['node']}; {s['ip']} released.",
                                  agent="devops", section="Removed")
            except Exception as e:  # noqa: BLE001
                steps.append(f"🟡 changelog not updated: {e}")
        run.summary = ("destroyed " if ok_all else "teardown incomplete: ") + ", ".join(names)
        o.respond(AGENT, channel, "🗑️ Teardown " + ("complete" if ok_all else "incomplete"), "\n".join(steps),
                  run=run, color=o.SEV_COLORS["ok" if ok_all else "high"])
        return ok_all


# --------------------------------------------------------------------------- audit (compute + storage)

AUDIT_SCRIPT = r"""set -e
pvesh get /cluster/resources --output-format json
echo '@@@'
for n in node01 node02 node03; do echo "##$n"; pvesh get /nodes/$n/status --output-format json 2>/dev/null || echo '{}'; done
"""


def _lvs(n):
    rc, out, _ = o.ssh(n, "lvs --noheadings --separator '|' -o vg_name,lv_name,data_percent,metadata_percent "
                          "--select 'lv_attr=~^t' 2>/dev/null", timeout=30)
    rows = []
    for line in out.splitlines() if rc == 0 else []:
        parts = [x.strip() for x in line.split("|")]
        if len(parts) == 4:
            rows.append({"node": n, "pool": f"{parts[0]}/{parts[1]}", "data": float(parts[2] or 0),
                         "meta": float(parts[3] or 0)})
    return rows


def collect_audit():
    out = pve(AUDIT_SCRIPT, timeout=90)
    res_txt, st_txt = out.split("@@@", 1)
    res = _json(res_txt.strip(), [])
    status = {}
    for blk in st_txt.split("##")[1:]:
        n, _, js = blk.partition("\n")
        status[n.strip()] = _json(js.strip(), {})
    with cf.ThreadPoolExecutor(3) as ex:
        thin = [r for rows in ex.map(_lvs, NODES) for r in rows]
    return {"res": res, "status": status, "thin": thin, "t": o.iso()}


def evaluate_audit(a):
    F = []

    def add(sev, key, what, rec):
        F.append({"sev": sev, "key": key, "what": what, "rec": rec})

    nodes = [r for r in a["res"] if r.get("type") == "node"]
    guests = [r for r in a["res"] if r.get("type") in ("qemu", "lxc") and not r.get("template")]
    stores = {}
    for r in a["res"]:
        if r.get("type") == "storage" and r.get("status") == "available" and r.get("maxdisk"):
            k = r["storage"] if r.get("shared") else f"{r['storage']}@{r['node']}"
            stores[k] = r
    rows_n = []
    for n in sorted(nodes, key=lambda x: x["node"]):
        name = n["node"]
        if n.get("status") != "online":
            add("critical", f"node-offline:{name}", f"{name} is {n.get('status')}", "Check power/network of the node; "
                "its guests are down or migrated.")
            continue
        mem, maxm = n.get("mem", 0), n.get("maxmem", 1)
        ram = mem / maxm * 100
        cpu = (n.get("cpu") or 0) * 100
        run_g = [g for g in guests if g.get("node") == name and g.get("status") == "running"]
        alloc = sum(g.get("maxmem", 0) for g in run_g) / maxm
        vcpu = sum(g.get("maxcpu", 0) for g in run_g) / max(n.get("maxcpu") or 1, 1)
        st = a["status"].get(name, {})
        sw = st.get("swap") or {}
        swap = (sw.get("used", 0) / sw["total"] * 100) if sw.get("total") else 0
        load = float((st.get("loadavg") or [0])[0] or 0)
        rows_n.append([name, f"{cpu:.0f}%", f"{mem / 1024 ** 3:.1f}/{maxm / 1024 ** 3:.0f} GB ({ram:.0f}%)",
                       f"{alloc * 100:.0f}%", f"{vcpu:.1f}×", f"{swap:.0f}%", f"{load:.1f}", len(run_g)])
        if ram >= 95:
            add("critical", f"node-ram:{name}", f"{name} RAM at {ram:.0f}%", "Move or shrink guests now; OOM kills likely.")
        elif ram >= 88:
            add("high", f"node-ram:{name}", f"{name} RAM at {ram:.0f}%",
                "Rebalance guests to a node with free RAM or trim over-sized guests.")
        if cpu >= 90:
            add("high", f"node-cpu:{name}", f"{name} CPU at {cpu:.0f}%", "Find the hot guest and move or limit it.")
        elif cpu >= 75:
            add("medium", f"node-cpu:{name}", f"{name} CPU at {cpu:.0f}%", "Watch it; consider spreading load.")
        if alloc >= 1.5:
            add("medium", f"node-overcommit:{name}", f"{name} RAM over-committed {alloc * 100:.0f}% (running guests)",
                "Fine while guests idle, risky under load: right-size guest memory or rebalance.")
        elif alloc >= 0.9:
            add("low", f"node-allocated:{name}", f"{name} has {alloc * 100:.0f}% of its RAM allocated to running guests",
                "Place new guests on another node, or trim guests that never use their full allocation.")
        if vcpu >= 4:
            add("low", f"node-vcpu:{name}", f"{name} vCPU ratio {vcpu:.1f}× cores", "Consider fewer vCPUs per guest.")
        if swap >= 50:
            add("medium", f"node-swap:{name}", f"{name} swap {swap:.0f}% used", "Memory pressure; free RAM on this node.")
    rows_s = []
    for k, r in sorted(stores.items()):
        if r["storage"] in IJI_STORES:
            continue
        pct = r.get("disk", 0) / r["maxdisk"] * 100
        rows_s.append([k, f"{r.get('disk', 0) / 1024 ** 3:.0f}/{r['maxdisk'] / 1024 ** 3:.0f} GB", f"{pct:.0f}%"])
        if pct >= 95:
            add("critical", f"storage:{k}", f"storage {k} at {pct:.0f}%", "Free space now: prune old images/ISOs, "
                "move disks, or extend the pool.")
        elif pct >= 90:
            add("high", f"storage:{k}", f"storage {k} at {pct:.0f}%", "Plan cleanup or expansion this week.")
        elif pct >= 80:
            add("medium", f"storage:{k}", f"storage {k} at {pct:.0f}%", "Keep an eye on growth.")
    for t in a["thin"]:
        if t["data"] >= 90 or t["meta"] >= 80:
            sev = "critical" if t["data"] >= 95 or t["meta"] >= 90 else "high"
            add(sev, f"thin:{t['node']}:{t['pool']}", f"{t['node']} thin pool {t['pool']} data {t['data']:.0f}% / "
                f"metadata {t['meta']:.0f}%", "A full thin pool corrupts guest disks: free space or extend it "
                "(lvextend, and --poolmetadatasize for metadata).")
        elif t["data"] >= 80:
            add("medium", f"thin:{t['node']}:{t['pool']}", f"{t['node']} thin pool {t['pool']} data {t['data']:.0f}%",
                "Plan cleanup (unused disks, old snapshots).")
    for g in guests:
        if g.get("type") == "lxc" and g.get("status") == "running" and g.get("maxdisk"):
            pct = g.get("disk", 0) / g["maxdisk"] * 100
            nm = f"{g['vmid']} {g.get('name')}"
            if pct >= 95:
                add("high", f"ct-disk:{g['vmid']}", f"LXC {nm} root disk {pct:.0f}% full",
                    "Clean logs/images inside it or grow the rootfs (pct resize).")
            elif pct >= 85:
                add("medium", f"ct-disk:{g['vmid']}", f"LXC {nm} root disk {pct:.0f}% full", "Clean up or grow soon.")
        if g.get("status") == "running" and g.get("maxmem") and g.get("type") == "lxc":
            m = g.get("mem", 0) / g["maxmem"] * 100
            if m >= 95:
                add("medium", f"ct-mem:{g['vmid']}", f"LXC {g['vmid']} {g.get('name')} memory {m:.0f}% of limit",
                    "Raise its memory limit or find the leak.")
    stopped = [f"{g['vmid']} {g.get('name')}" for g in guests if g.get("status") == "stopped"]
    if stopped:
        add("low", "stopped-guests", f"{len(stopped)} stopped guest(s): {', '.join(stopped[:8])}",
            "Keep, back up and remove, or start them; stopped guests still hold disk.")
    F.sort(key=lambda f: SEV_RANK[f["sev"]])
    return F, rows_n, rows_s


def _handoff(findings, channel):
    """High/critical findings go to Corhyn (once a day each). Hewg never fixes."""
    sent = o.kv_get(KV_HANDOFF, {}) or {}
    now = time.time()
    sent = {k: v for k, v in sent.items() if now - v < 86400}
    handed = []
    for f in findings:
        if SEV_RANK[f["sev"]] > 1 or f["key"] in sent:
            continue
        text = (f"Hewg's compute/storage audit found a {f['sev'].upper()} issue: {f['what']}. "
                f"Recommendation: {f['rec']} Please diagnose the cause and propose a safe fix.")
        subprocess.Popen([sys.executable, str(o.CODE_DIR / "medic.py"), "ask", text, "--channel", channel],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, cwd=str(o.CODE_DIR))
        sent[f["key"]] = now
        handed.append(f)
    o.kv_set(KV_HANDOFF, sent)
    return handed


def audit_text(F, rows_n, rows_s):
    txt = "**Nodes**\n" + o.md_table(rows_n, ["Node", "CPU", "RAM used", "RAM alloc", "vCPU", "Swap", "Load", "Guests"])
    txt += "\n\n**Storage** (backup stores are Iji's)\n" + o.md_table(rows_s, ["Storage", "Used", "%"])
    txt += "\n\n**Findings**\n" + ("\n".join(f"{SEV_ICON[f['sev']]} **{f['sev']}** · {f['what']} → {f['rec']}"
                                             for f in F) or "✅ Nothing to report.")
    return txt


def audit(reply_to=None, channel=None):
    channel = channel or AUDIT_CHANNEL
    with o.Run(AGENT, "compute/storage audit", "chat" if reply_to else "schedule") as run:
        F, rows_n, rows_s = evaluate_audit(collect_audit())
        txt = audit_text(F, rows_n, rows_s)
        recs = ""
        if any(SEV_RANK[f["sev"]] <= 2 for f in F):
            recs = o.llm(run, f"""You are Hewg, the Roundtable's smith (compute & storage). From this live audit, write
3-5 short, concrete capacity recommendations for Master Hermes (Discord Markdown bullets). Prioritise by risk. You
never fix anything yourself: say Corhyn handles high/critical ones. No preamble.

{txt}""", model="gpt-6-luna", max_output_tokens=700) or ""
        handed = _handoff(F, "infrastructure-alerts")
        o.kv_set(KV_AUDIT, {"t": o.iso(), "findings": F, "nodes": rows_n, "storage": rows_s, "recs": recs})
        worst = F[0]["sev"] if F else "ok"
        run.summary = (f"{len(F)} finding(s), worst {worst}" + (f"; {len(handed)} handed to Corhyn" if handed else ""))
        body = txt + (f"\n\n**Hewg recommends**\n{recs}" if recs else "") + (
            "\n\n🩺 Handed to Corhyn: " + "; ".join(f["what"] for f in handed) if handed else "")
        o.respond(AGENT, channel, "🔨 Hewg's compute & storage audit", body, reply_to=reply_to, run=run,
                  color=o.SEV_COLORS.get(worst if worst != "low" else "ok", o.SEV_COLORS["ok"]))
        return F


# --------------------------------------------------------------------------- ask / status / voice

def status_text():
    b = builds()
    a = o.kv_get(KV_AUDIT, {}) or {}
    lines = ["**Forge (Terraform builds)**"]
    for n, x in sorted(b.items(), key=lambda kv: kv[1].get("updated", ""), reverse=True)[:10]:
        s = x.get("spec", {})
        lines.append(f"- `{n}` {s.get('kind', '?').upper()} {s.get('vmid', '')} {s.get('ip', '')} on "
                     f"{s.get('node', '?')}: **{x.get('status')}** ({(x.get('updated') or '')[:16].replace('T', ' ')} UTC)"
                     + (f" - {x['error'][:120]}" if x.get("status") == "failed" and x.get("error") else ""))
    if len(lines) == 1:
        lines.append("- nothing forged yet")
    if a:
        lines.append(f"\n**Last audit** {a['t'][:16].replace('T', ' ')} UTC: {len(a['findings'])} finding(s)")
        lines += [f"{SEV_ICON[f['sev']]} {f['what']}" for f in a["findings"][:10]]
    return "\n".join(lines)


def patch_status():
    """Read the existing SRE backlog without approving, patching or replaying an executor."""
    import hierarchy
    issues = hierarchy._gl("GET", "/issues?state=opened&labels=patch-management&per_page=100")
    containers = hierarchy._gl("GET", "/issues?state=opened&labels=container-update&per_page=100")
    apps = hierarchy._gl("GET", "/issues?state=opened&labels=app-update&per_page=100")
    items = {i["iid"]: i for i in issues + containers + apps}
    history = Path.home() / ".sre-agent" / "history.jsonl"
    recent = []
    if history.exists():
        for line in history.read_text().splitlines()[-20:]:
            entry = json.loads(line)
            if entry.get("kind") not in ("os", "container", "app"):
                continue
            recent.append({k: entry.get(k) for k in ("iid", "host", "kind", "result", "ts", "time")})
    return {"as_of": o.iso(), "delivery_owner": "Hewg", "accountable_lead": "Corhyn",
            "executor": "sre-agent.service", "recent": recent,
            "backlog": [{k: i.get(k) for k in ("iid", "title", "labels", "web_url")} for i in items.values()],
            "scope": "Up to 100 open issues per patch label; recent recorded outcomes, not fresh host verification.",
            "policy": "No patches or approvals are triggered by this tool. Owner approves in Discord #patch-management "
                      "or the existing GitLab pipeline. SRE executes in 02:00-05:00 Asia/Manila with existing backup, "
                      "verification and rollback gates. Hewg coordinates delivery, Corhyn priorities/escalations, "
                      "Loretta security urgency, Ranni service objectives, Morgott project deadlines, "
                      "Blaidd independent verification and Miriel documentation."}


def ask(text, reply_to=None, channel=None):
    channel = channel or CHANNEL
    o.transcript(AGENT, "Request", text, "discord", channel, reply_to or "")
    draft = text.startswith(PLAN_DRAFT)  # voice post_plan: answer with a written plan, never a blueprint/approval
    if not draft and DELETE_INTENT.search(text) and re.search(r"\b(lxc|vm|container|guest|ct|vmid|id|ids)\b|\b\d{3,5}\b", text, re.I):
        plan_destroy(text, reply_to, channel)
        return
    # A request to create a guest often mentions patch gates/windows; that must not hijack it into patch delivery.
    if not draft and re.search(r"\b(patch(?:es|ing)?|updates?|upgrade[sd]?|maintenance)\b", text, re.I) and not BUILD_INTENT.search(text):
        with o.Run(AGENT, f"patch delivery: {text[:200]}", "chat") as run:
            facts = patch_status()
            answer = o.llm(run, f"Recorded patch evidence:\n{json.dumps(facts)}\nOwner asks: {text}",
                           instructions="You are Hewg, patch-delivery owner reporting to Corhyn. "
                           "Explain status, coordinate delivery and flag blockers. Never claim to execute or approve "
                           "a patch from chat; use only the existing SRE approval workflow. " + facts["policy"],
                           model="gpt-6-luna", max_output_tokens=1200)
            if not answer:
                raise RuntimeError("Patch-delivery consultation returned no answer")
            run.summary = answer[:700]
            if not o.respond(AGENT, channel, "Hewg · patch delivery", answer, reply_to=reply_to, run=run):
                raise RuntimeError("Patch-delivery reply could not be posted")
        return
    with o.Run(AGENT, f"ask: {text[:200]}", "chat") as run:
        spec = {"action": "answer"} if draft else o.llm(run, f"""Turn Master Hermes's request to Hewg (homelab builder) into a build spec.
action: build = create a new LXC or VM; audit = review compute/storage capacity now; status = what was built / build
progress; answer = any other question (capacity, advice, how builds work).
For build: kind lxc (default, "container", "CT") or vm; name = short hostname he gave or a sensible one from the
purpose (lowercase, dashes, e.g. uptime-kuma-lxc); os debian-12 (default) or ubuntu-24.04 (VMs are always
ubuntu-24.04); cores (default 2); memory_mb (GB*1024, default 2048); disk_gb (default 8 for LXC, 32 for VM);
vlan (default 40; 20 = homelab/infra; 90 = management); ip = exact IP he gave or "auto"; node = node01/02/03 or
"auto"; purpose = what it is for; docker = true if he wants Docker or a Docker app; packages = apt package names he
wants installed (e.g. ["nginx"]; [] if none); missing = [] (only list truly blocking gaps; defaults are fine).
Drawing a blueprint is always read-only and is gated by his Discord approval, so a request that says "proposal",
"approval-ready" or "do not change anything" is still action=build. node02 is a Proxmox node (node01/02/03).
For non-build actions fill the build fields with defaults.
Request: {text}""", schema=SPEC, model="gpt-6-luna")
        run.summary = f"intent {spec['action']}"
    act = spec.pop("action")
    if act == "build":
        plan(spec, reply_to, channel)
    elif act == "audit":
        audit(reply_to, channel)
    elif act == "status":
        with o.Run(AGENT, "status", "chat") as run:
            o.respond(AGENT, channel, "🔨 Hewg's forge", status_text(), reply_to=reply_to, run=run)
    else:
        with o.Run(AGENT, f"answer: {text[:200]}", "chat") as run:
            import inventory
            answer = o.llm(run, f"""You are Hewg, Compute & Storage Engineer reporting to Corhyn.
Expertise: Proxmox, Hyper-V and NFS architecture and troubleshooting. Coordinate IaC with Radahn (DevOps);
every new service must reserve its address with Diallos (IPAM). Distinguish live access from conceptual
expertise: do not claim a Hyper-V API exists here, and flag unsupported/manual remediation explicitly.
Few words, precise work. You build
Proxmox LXCs/VMs for Master Hermes with Terraform (bpg/proxmox) + Ansible from ansible-controller01 after his ✅, and you
audit compute & storage daily (you recommend; Corhyn fixes). Answer in Discord Markdown, short and practical. Use ONLY
the live data. To build, he can say e.g. "Hewg, make a 2-core Debian LXC with 2 GB on VLAN 40 for uptime-kuma".

FORGE + LAST AUDIT:
{status_text()}

LIVE COMPUTE:
{inventory.text('compute', limit=6000)[:9000]}

QUESTION: {text}""", model="gpt-6-luna", max_output_tokens=1200)
            run.summary = (answer or "")[:300]
            o.respond(AGENT, channel, "🔨 Hewg answers", answer or "No answer.", reply_to=reply_to, run=run)


VOICE_TOOLS = [
    {"type": "function", "name": "patch_status",
     "description": "Read Hewg's existing SRE patch backlog and recent outcomes; never approves or executes patches.",
     "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"type": "function", "name": "forge_status",
     "description": "Hewg's forge: guests he built or is building with Terraform/Ansible (status, VMID, IP, node) and "
                    "the last compute/storage audit findings and recommendations.",
     "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"type": "function", "name": "compute_audit",
     "description": "Run a fresh, read-only compute & storage audit now (node CPU/RAM, over-commit, swap, storage "
                    "pools, thin pools, LXC disks, stopped guests) and return the findings. Takes ~20 s.",
     "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
]


def voice_tool(name, args):
    if name == "patch_status":
        return patch_status()
    if name == "forge_status":
        a = o.kv_get(KV_AUDIT, {}) or {}
        return {"forge": status_text().replace("**", ""), "recommendations": a.get("recs", ""),
                "note": "To build, delegate to agent 'builder' with the full spec; the blueprint is posted in Discord "
                        "for Master Hermes's ✅."}
    if name == "compute_audit":
        F, rows_n, rows_s = evaluate_audit(collect_audit())
        return {"audit": audit_text(F, rows_n, rows_s).replace("**", "")[:9000],
                "note": "Hewg recommends only; high/critical findings are handed to Corhyn by the daily audit."}
    return {"error": f"unknown tool {name}"}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["ask", "plan", "build", "destroy", "audit", "status", "patches"])
    ap.add_argument("arg", nargs="?")
    ap.add_argument("--reply-to")
    ap.add_argument("--channel")
    a = ap.parse_args()
    if a.cmd == "ask":
        ask(a.arg, a.reply_to or None, a.channel or None)
    elif a.cmd == "patches":
        print(json.dumps(patch_status(), indent=2))
    elif a.cmd == "plan":
        plan(json.loads(a.arg), a.reply_to or None, a.channel or None)
    elif a.cmd == "build":
        sys.exit(0 if build(a.arg) else 1)
    elif a.cmd == "destroy":
        sys.exit(0 if destroy(a.arg) else 1)
    elif a.cmd == "audit":
        audit(a.reply_to or None, a.channel or None)
    else:
        print(status_text())


if __name__ == "__main__":
    main()
