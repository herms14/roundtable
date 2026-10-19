#!/usr/bin/env python3
"""Generate the Roundtable agents' avatars with the OpenAI Images API (original art, not game assets).

  python3 avatars.py            # create missing avatars in ~/olympus/avatars/<id>.jpg
  python3 avatars.py --force boss watcher   # regenerate specific ones (catalog IDs)

Served publicly (no auth) by the portal at https://roundtable.hrmsmrflrii.xyz/avatars/<id>.jpg so Discord can use
them as embed author icons.
"""
import argparse
import base64
import concurrent.futures as cf
import os
import sys

import olympus_core as o

MODEL = "gpt-image-2"
OUT = o.CODE_DIR / "avatars"
STYLE = ("Dark-fantasy oil painting portrait for a square profile avatar: head and shoulders, centered, dramatic rim "
         "light, muted gold, ash and deep-blue palette, painterly brushwork, softly vignetted dark background. "
         "Original character design. No text, no letters, no logos, no watermark, no frame.")
PROMPTS = {
    "keeper": "An original medieval-court spymaster inspired by Varys, the Master of Whisperers: a heavy-set, completely "
              "bald and clean-shaven man in his fifties with smooth olive skin, a faint knowing smile and calm, "
              "watchful eyes. Distinct face of his own: a long narrow face with high sharp cheekbones, a slender hooked "
              "nose, thin arched dark eyebrows, deep-set pale grey eyes with heavy lids, a small mole on the left "
              "cheek and faint kohl at the lash line; layered embroidered silk robes in deep plum and dark teal with a high collar, hands folded "
              "inside wide sleeves, a small iron key on a cord at his chest. Candlelit stone castle corridor, warm "
              "flickering light. Original character that does not resemble any real actor or person.",
    "security": "A vigilant spectral knight-archer: a tall, slender figure in ornate silver-and-gold Haligtree "
                "armour with a crested helm, translucent ghostly blue aura, drawing a great bow with a glowing "
                "arrow nocked, eyes fixed on the viewer; frost-blue golden tree branches behind. A guardian who "
                "lets nothing pass the gate unchallenged.",
    "boss": "A stern, wise bearded sage in an ornate dark iron helm with a raised visor and a fur-trimmed scholar's "
            "mantle, holding a closed leather tome; candlelit round stone hall with a great round table behind him. "
            "He looks like someone who knows every secret and sends others where they are needed.",
    "medic": "A gentle young cleric with short blond hair and a kind, earnest face, in simple white-and-gold clerical "
             "robes, warm golden healing light glowing from his raised open hand.",
    "watcher": "An enigmatic sorceress-astrologer whose head is a translucent blue crystal crown filled with swirling "
               "stars and nebulae, wearing a deep-blue hooded robe, faint glowing constellations around her.",
    "netops": "A spectral steed: a sturdy dark-grey horse with two curved goat-like horns and a faintly glowing "
              "ethereal mane, three-quarter view, misty golden grassland at dusk behind it.",
    "builder": "An old, gaunt, weary blacksmith with long grey hair and beard and strong bare forearms, resting a "
               "hammer on an anvil; sparks and orange forge glow; few words, precise hands.",
    "checker": "A tall noble half-wolf warrior with a grey wolf's head and keen amber eyes, wearing dark iron armour "
               "and a tattered blue-grey cloak; loyal, watchful, suspicious.",
    "scribe": "A giant, wise old turtle wearing a tall white bishop's mitre and gold-trimmed vestments, calm scholarly "
              "expression, in a moonlit ruined stone chapel with candles and old books.",
    "mirror": "A regal figure split perfectly down the middle as a mirror image: the left half a radiant golden queen, "
              "the right half a red-haired warrior king with long crimson hair; a thin golden ring above them.",
    "scout": "A friendly, rugged travelling merchant with a dark beard and a leather hood, sitting by a small "
             "campfire holding a lantern and a little hand bell, packs of curious wares around him.",
    "writer": "A mysterious young maiden in a dark-brown hooded travelling cloak, one eye closed over a faint scar, "
              "a small amber flame glowing in her open palm; serene, guiding, a storyteller.",
    "backup": "A towering, gentle blue-skinned troll war counsellor and smith with a great horned iron helm, a long "
              "braided white beard and calm wise eyes, wearing heavy dark blue Carian armour and a leather smithing "
              "apron; he rests one huge hand on a massive iron-bound vault chest glowing with soft blue sorcery "
              "runes. Loyal, patient, protective — a guardian who keeps everything safe.",
    "media": "A proud young warrior woman with fierce yet warm amber eyes and a few strands of dark hair escaping "
             "a silver lion-crested Stormveil helm with an open face, wearing ornate silver-and-brown plate armour "
             "with a dark red cloak, a heavy war axe resting on her shoulder; behind her a faint glow like a cinema "
             "screen in a dark castle hall, drifting film-reel motes of light. Bold, honest, passionate.",
    "project_manager": "An original dark-fantasy omen magistrate: a dignified elderly figure with weathered grey "
                       "skin, two asymmetrical curling ivory horns, long silver hair and an austere, thoughtful "
                       "expression. Worn brown-and-gold layered travelling robes, an understated iron clasp and "
                       "a closed milestone ledger. A gruff but dependable keeper of promises in a candlelit stone "
                       "council chamber. No lettering, no game insignia, no copied costume.",
    "security_engineer": "An original resolute young adult fantasy duelist with long copper-red hair, composed "
                         "eyes and a subtle cheek scar. Weathered crimson cloak over practical bronze-and-leather "
                         "armour, a finely crafted articulated brass right forearm. A methodical guardian, steady "
                         "rather than aggressive; warm forge light against cool stone. Original armour and face, "
                         "not a recreation of any game portrait.",
    "iam": "An original courteous fantasy scholar-swordsman with warm brown eyes, a neatly trimmed chestnut beard "
           "and a broad, softly tilted scholar's hat. Muted teal-and-brown embroidered travelling robes, modest "
           "leather gloves and a small orderly ring of antique keys. A slight knowing smile in an old archive, "
           "quietly clever and trustworthy. Original costume; no lettering or borrowed game symbols.",
    "compliance": "An original elderly fantasy oracle and archivist with a lined, thoughtful face, bright observant "
                  "eyes and long silver hair beneath a simple dark plum hood. Layers of weathered linen and "
                  "antique-gold trim, one slender hand resting on a closed evidence book; a modest ceramic teacup "
                  "nearby. Patient, exacting, quietly kind in a shadowy candlelit library. Entirely original design.",
    "cost": "An original small, shy, kind-hearted fantasy demi-human tailor with delicate mouse-like ears, soft "
            "brown fur at the temples, expressive amber eyes and a gentle smile. Well-worn olive-and-ocher "
            "travelling coat with small decorative buttons, a needle case and neatly folded cloth; a few antique "
            "coins beside a candle. Charming and meticulous, not comic grotesquery. Original face and attire.",
    "ipam": "An original youthful fantasy steward-knight with neatly swept dark chestnut hair, earnest grey-blue "
            "eyes and a thoughtful, slightly shy expression. Understated silver plate over a muted slate-blue "
            "tunic, a closed map folio in his hands and a subtle compass-shaped brooch. Organized, courteous "
            "and attentive in a candlelit stone planning room. No written addresses or game emblems.",
    "devops": "An original imposing, good-humored fantasy general with a broad weathered face, flowing russet-red "
              "hair and a slightly untidy crimson plume. Practical engraved bronze armour, a worn dark-red "
              "mantle and an artisan's tool roll at his belt. Confident warm half-smile, disciplined rather than "
              "reckless; orange workshop embers and cool ash-blue shadows. Original armour, no copied game costume.",
}
# Per-agent art style (default: the Roundtable dark-fantasy look).
STYLE_OVERRIDE = {
    # Varys: photorealistic, as the owner asked (2026-10-19); an original face, never an actor's likeness
    "keeper": ("Photorealistic cinematic portrait photograph for a square profile avatar: head and shoulders, centered, "
               "shallow depth of field, 85 mm lens, soft candle key light with deep shadows, natural skin texture, "
               "film grain, period-drama production quality. No text, no letters, no logos, no watermark, no frame."),
}
FULL_STYLE_OVERRIDE = {
    "keeper": ("Full-body photorealistic character photograph, standing pose with hands folded in his sleeves, the "
               "entire figure visible from head to feet (soft slippers under the long robe), a little space above and "
               "below, facing the viewer at a slight three-quarter angle, calm knowing half-smile. Cinematic period-drama "
               "realism, warm candle key light. Isolated figure on a fully transparent background (no scenery, no "
               "floor, no shadow box). Original character that does not resemble any real actor or person. No text, no "
               "letters, no logos, no watermark, no frame."),
}
FULL_STYLE = ("Full-body character art, standing pose, the entire figure visible from head to feet with a little "
              "space above and below, facing the viewer at a slight three-quarter angle, friendly approachable "
              "expression. Dark-fantasy painterly style, dramatic rim light, muted gold, ash and deep-blue palette. "
              "Isolated figure on a fully transparent background (no scenery, no floor, no shadow box). Original "
              "character design. No text, no letters, no logos, no watermark, no frame.")
FULL_FALLBACK = FULL_STYLE.replace("Isolated figure on a fully transparent background (no scenery, no floor, no "
                                   "shadow box).", "Plain, very dark near-black background with no scenery.")


def gen(aid, key):
    body = {"model": MODEL, "prompt": f"{PROMPTS[aid]}\n\n{STYLE_OVERRIDE.get(aid, STYLE)}", "size": "1024x1024", "quality": "medium",
            "output_format": "jpeg", "output_compression": 82, "n": 1}
    code, d = o.http("POST", "https://api.openai.com/v1/images/generations", {"Authorization": f"Bearer {key}"},
                     body, timeout=300, retries=1)
    if code != 200 or not isinstance(d, dict) or not d.get("data"):
        return aid, False, f"HTTP {code}: {str(d)[:300]}", {}
    (OUT / f"{aid}.jpg").write_bytes(base64.b64decode(d["data"][0]["b64_json"]))
    return aid, True, "ok", d.get("usage") or {}


def gen_full(aid, key):
    """Full-body figure for the portal companion: <id>-full.png, transparent when the model supports it."""
    hdr = {"Authorization": "Bearer " + (key or "")}
    url = "https://api.openai.com/v1/images/generations"
    body = {"model": MODEL, "prompt": f"{PROMPTS[aid]}\n\n{FULL_STYLE_OVERRIDE.get(aid, FULL_STYLE)}", "size": "1024x1536", "quality": "medium",
            "output_format": "png", "background": "transparent", "n": 1}
    code, d = o.http("POST", url, hdr, body, timeout=300, retries=1)
    note = "transparent"
    if code == 400:
        body.pop("background")
        fb = FULL_STYLE_OVERRIDE.get(aid, FULL_STYLE).replace(
            "Isolated figure on a fully transparent background (no scenery, no floor, no shadow box).",
            "Plain, very dark near-black background with no scenery.")
        body["prompt"] = f"{PROMPTS[aid]}\n\n{fb}"
        code, d = o.http("POST", url, hdr, body, timeout=300, retries=1)
        note = "opaque (transparent unsupported)"
    if code != 200 or not isinstance(d, dict) or not d.get("data"):
        return aid, False, f"HTTP {code}: {str(d)[:300]}", {}
    (OUT / f"{aid}-full.png").write_bytes(base64.b64decode(d["data"][0]["b64_json"]))
    return aid, True, note, d.get("usage") or {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", nargs="*")
    ap.add_argument("--full", action="store_true", help="full-body companion art (<id>-full.png)")
    a = ap.parse_args()
    OUT.mkdir(exist_ok=True)
    key = os.environ.get("OPENAI_API_KEY") or os.environ.get("CODEX_API_KEY")
    if not key:
        raise RuntimeError("Image generation requires the existing OpenAI API credential")
    suffix, fn = ("-full.png", gen_full) if a.full else (".jpg", gen)
    active = {agent["id"] for agent in o.registry()["agents"]}
    todo = a.force if a.force else [i for i in PROMPTS if i in active and not (OUT / f"{i}{suffix}").exists()]
    if any(i not in active or i not in PROMPTS for i in todo):
        raise ValueError("Use active catalog IDs with a configured art prompt")
    if not todo:
        print("all avatars present")
        return
    with o.Run("boss", f"generate {'full-body ' if a.full else ''}avatars: {', '.join(todo)}", "manual") as run:
        o.check_budget()
        with cf.ThreadPoolExecutor(4) as ex:
            res = list(ex.map(lambda i: fn(i, key), todo))
        tin = sum(u.get("input_tokens", 0) for *_, u in res)
        tout = sum(u.get("output_tokens", 0) for *_, u in res)
        # gpt-image-2 pricing: ~$5/M text input, ~$40/M image output tokens
        cost = tin * 5 / 1e6 + tout * 40 / 1e6
        with o.contextlib.closing(o.db()) as con, con:
            con.execute("UPDATE runs SET model=?, calls=?, input_tokens=?, output_tokens=?, cost_usd=? WHERE id=?",
                        (MODEL, len(res), tin, tout, cost, run.id))
        run.summary = "; ".join(f"{i}: {m}" for i, ok, m, _ in res)
        if not all(ok for _, ok, *_ in res):
            run.finish("error", run.summary)
    for i, ok, m, u in res:
        print(i, "OK" if ok else "FAIL", m, u.get("output_tokens", ""))
    sys.exit(0 if all(ok for _, ok, *_ in res) else 1)


if __name__ == "__main__":
    main()
