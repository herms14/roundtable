#!/usr/bin/env python3
"""Upload the agents' avatars to the Discord server as custom emojis (:gideon:, :sellen:, ...).

Discord cannot reach the portal (no public DNS), so embeds use the emoji CDN URL as the author icon:
https://cdn.discordapp.com/emojis/<id>.png. The ids are stored in kv "avatar_emojis".
  python3 avatar_emojis.py          # create missing / refresh changed
"""
import base64
import hashlib
import io
import os
from pathlib import Path
import re
import sys

import olympus_core as o


def small(path):
    """Emoji image (Discord limit 256 KB); Discord resizes it. Downscales with PIL only when needed."""
    data = path.read_bytes()
    if len(data) < 250_000:
        return data, "image/jpeg"
    from PIL import Image
    im = Image.open(path).convert("RGB").resize((256, 256), Image.LANCZOS)
    b = io.BytesIO()
    im.save(b, "JPEG", quality=90)
    return b.getvalue(), "image/jpeg"


def main():
    gid = os.environ["DISCORD_GUILD_ID"]
    have = o.kv_get("avatar_emojis", {})
    code, emojis = o.dc("GET", f"/guilds/{gid}/emojis")
    if code != 200:
        sys.exit(f"cannot list emojis: HTTP {code} {emojis}")
    by_id = {e["id"]: e for e in emojis}
    changed = False
    for a in o.registry()["agents"]:
        source = Path(a.get("avatar") or f"{a['id']}.jpg").name
        f = o.CODE_DIR / "avatars" / Path(source).with_suffix(".jpg")
        if not f.exists():
            raise FileNotFoundError(f"Discord raster portrait is missing: {f.name}")
        name = re.sub(r"[^a-z0-9_]", "", a["name"].lower().replace("é", "e"))
        digest = hashlib.sha1(f.read_bytes()).hexdigest()[:12]
        cur = have.get(a["id"])
        if cur and cur.get("id") in by_id and cur.get("sha") == digest:
            continue
        data, mime = small(f)
        code, e = o.dc("POST", f"/guilds/{gid}/emojis",
                       {"name": name, "image": f"data:{mime};base64,{base64.b64encode(data).decode()}"})
        if code not in (200, 201):
            raise RuntimeError(f"Cannot upload {a['id']} portrait: HTTP {code}")
        have[a["id"]] = {"id": e["id"], "name": name, "sha": digest}
        o.kv_set("avatar_emojis", have)
        if cur and cur.get("id") in by_id:
            status, _ = o.dc("DELETE", f"/guilds/{gid}/emojis/{cur['id']}")
            if status != 204:
                raise RuntimeError(f"New {a['id']} portrait saved, but old emoji cleanup failed: HTTP {status}")
        changed = True
        print(f"{a['id']}: :{name}: {e['id']}")
    if changed:
        o.kv_set("avatar_emojis", have)
    print({k: v["id"] for k, v in have.items()})


if __name__ == "__main__":
    main()
