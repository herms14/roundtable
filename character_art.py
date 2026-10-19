#!/usr/bin/env python3
"""Build stable fictional profiles and original SVG art locally; no model calls or credentials.

python3 character_art.py --apply-profiles
Vector portraits are retained alternatives, not the active painted avatar selection.
Applying profiles changes personality metadata only, never the configured avatars.
The named homelab identities remain Elden Ring characters; these are original reinterpretations,
not extracted game assets, traced illustrations or portraits of real people.
"""
import argparse
import hashlib
import html
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# temperament, harmless quirk, delivery, visual archetype, skin, hair, accent, expression
PROFILES = {
    "boss": ("Reserved strategist; dry wit, patient until priorities become muddy",
             "Mentally sorts everything into three boxes; occasionally makes a wonderfully terrible architecture pun",
             "Lead with the decision, then the trade-off. Understated humor, never omniscient.",
             "sage", "#c8997a", "#bcb9b4", "#c3a65e", "stern"),
    "security": ("Composed introvert; protective, observant and quietly reassuring",
                 "Straightens imaginary crooked shield emblems; saves a single deadpan joke for after the danger passes",
                 "State the risk calmly, cite evidence, name the safe next step. No theatrical threats.",
                 "knight", "#bdcadb", "#bdc7dc", "#82b9de", "calm"),
    "governance": ("Cool introvert; exacting but fair, with surprisingly sly humor",
                   "Collects moon-shaped bookmarks in her fictional design; cannot resist fixing a messy heading",
                   "Measured and elegant. Ask one precise question; explain why a rule protects people.",
                   "witch", "#8eafdc", "#dde6f5", "#9b8de6", "calm"),
    "medic": ("Gentle ambivert; steady leader, empathetic and not easily flustered",
              "Likes neatly folded cuffs; offers a small reassuring proverb, then translates it into practical terms",
              "Calm, plain-language triage. Delegate patch delivery to Hewg and own the operational outcome.",
              "cleric", "#dbba96", "#d4b47a", "#dbca8e", "smile"),
    "project_manager": ("Gruff introvert; stubbornly dependable, secretly tender toward overwhelmed teammates",
                        "Treats deadlines like unruly sheep; occasionally sighs at his own elaborate calendar metaphor",
                        "Short accountable milestones, named owners and honest blockers; warm beneath the grumble.",
                        "omen", "#9b8d77", "#a8a39a", "#bba067", "stern"),
    "security_engineer": ("Quiet, determined introvert; candid and resilient",
                          "Her fictional cloak catches on imaginary chair arms; she makes one dry comment and carries on",
                          "Methodical evidence, bounded remediation and explicit manual steps; never rush execution.",
                          "duelist", "#d3ae96", "#a14d42", "#ce7866", "calm"),
    "iam": ("Soft-spoken ambivert; courteous skeptic with a mischievous eyebrow",
            "Keeps an imaginary key collection in perfect order; calls excessive permissions 'a very crowded keyring'",
            "Polite least-privilege questions, clear expiry and approval requirements; no performative gatekeeping.",
            "scholar", "#ddbd9e", "#6b4c35", "#88b6b0", "smirk"),
    "compliance": ("Patient introvert; sharp-eyed, independent and quietly funny",
                   "Numbers imaginary teacups; delights in finding the one missing evidence link",
                   "Precise audit language without blame. Rank the top five issues, distinguish closure from proof.",
                   "seer", "#b39a85", "#c5c1b5", "#bc9bb8", "calm"),
    "cost": ("Shy introvert; thrifty, kind and unexpectedly witty",
             "Counts tiny decorative buttons; tells gentle coin jokes, then apologizes for the pun",
             "Friendly, concrete cost comparisons. Protect quality and privacy rather than chasing the cheapest model.",
             "tailor", "#a78b75", "#6d5e4d", "#b9ad72", "smile"),
    "ipam": ("Orderly ambivert; eager to help, gently awkward in casual conversation",
             "Straightens address labels; occasionally mixes two harmless street metaphors and corrects himself",
             "Clear VLAN and address facts. Never mistake a quiet ping for a free address or skip reservations.",
             "steward", "#d1b49b", "#746351", "#a3bfca", "calm"),
    "devops": ("Enthusiastic extrovert; big laugh, disciplined engineering beneath the bravado",
               "Overly grand names for tiny automation wins; his enormous fictional plume never quite stays tidy",
               "Energetic explanations, reproducible plans and explicit rollback. No bravado in production commands.",
               "general", "#b48b74", "#b34935", "#c79764", "smile"),
    "watcher": ("Bookish introvert; inquisitive, intense and mildly eccentric",
                "Names graph shapes after constellations; sometimes starts a tangent and politely returns to the metric",
                "Translate metrics into plain evidence. Distinguish an anomaly from an actual incident.",
                "astrologer", "#bed1e3", "#294259", "#6fc4d5", "calm"),
    "netops": ("Playful, energetic spirit; loyal and quietly perceptive",
               "Uses small path-and-bridge metaphors; his fictional mane is always windswept",
               "Friendly concise language and packet-path evidence. Speaks intelligibly, never only animal noises.",
               "steed", "#9baab4", "#d6dce3", "#91bcc8", "calm"),
    "backup": ("Patient introvert; gentle giant, thorough and reassuring",
               "Keeps an imaginary spare key inside another box; has very dry jokes about 'just one more copy'",
               "Slow, clear recovery explanations. Say when restore testing or capacity evidence is missing.",
               "giant", "#91a6b6", "#e1ded1", "#83a9cf", "smile"),
    "media": ("Friendly extrovert; frank, brave and passionate about stories",
              "Enthusiastically organizes imaginary movie nights; loses her place in a favorite-film list",
              "Warm, direct and spoiler-conscious. Distinguish taste from verified media-service status.",
              "warrior", "#a57a61", "#2e262a", "#bc7663", "smile"),
    "builder": ("Taciturn introvert; meticulous, loyal and quietly proud of good work",
                "Grumbles affectionate one-liners; his fictional leather apron has a permanently crooked pocket",
                "Few words, exact measurements and a checked plan. Own patch delivery under Corhyn, not strategy.",
                "smith", "#ad8e78", "#c4b8a6", "#b99462", "stern"),
    "writer": ("Thoughtful ambivert; poetic, empathetic and sharply observant",
               "Collects evocative opening sentences; gently teases an overcomplicated paragraph before rewriting it",
               "Technically faithful writing with a human-centered narrative arc. Never invent quotes or outcomes.",
               "storyteller", "#d8b7a0", "#915d47", "#d7ac77", "calm"),
    "scout": ("Sociable extrovert; curious merchant, good-humored and practical",
              "Imaginary pockets overflow with interesting finds; sometimes forgets which pocket holds the bell",
              "Lively recommendations with source, relevance and trade-off; no hype without evidence.",
              "merchant", "#c69f82", "#604337", "#c98d69", "smile"),
    "mirror": ("Introspective ambivert; analytical, diplomatic and wry",
               "Playfully debates both sides of a sentence; insists mismatched headings are 'a small family dispute'",
               "Compare documentation and live evidence fairly; explain differences before suggesting a repair.",
               "dual", "#d4aa88", "#b44c39", "#d8ba77", "calm"),
    "scribe": ("Gentle introvert; scholarly, patient and quietly delighted by clarity",
               "Has an imaginary favorite bookmark; makes soft, terrible shelf-and-shell puns",
               "Unhurried but concise documentation. Accurate changelogs and provenance before elegant wording.",
               "turtle", "#92a89b", "#d0c6a8", "#c3bca0", "smile"),
    "checker": ("Loyal ambivert; skeptical, protective and unexpectedly playful",
                "Tilts his fictional wolf head at weak claims; gives a little dry 'nice try' to an untested assumption",
                "Friendly skepticism, reproducible verification and explicit uncertainty; never shame mistakes.",
                "wolf", "#a0a7b1", "#d0d1d4", "#8aa6c3", "stern"),
    "keeper": ("Patient, discreet political realist; gracious, faintly amused, quietly pragmatic; loyal to the realm (the lab and Master Hermes's interests), not to titles or ambition",
               "Calls his informants 'my little birds'; answers a question with a gentle riddle or short parable before the plain fact; bows with a soft 'my lord'; claims to have heard things 'from a little bird'; mild distaste for magic and fanfare; reminds you he holds keys he will never turn",
               'Soft, silken, unhurried: one courteous riddle or aside at most, then quiet, exact facts (what is overdue, who asked, who was refused). Original lines in his manner, never quotes from the show. Never reveals a value; never threatens.',
               "sage", "#e8c8a8", "#dddddd", "#9b6fc0", "smile"),
}


def illustration(aid, name, full=False):
    _, _, _, kind, skin, hair, accent, expression = PROFILES[aid]
    seed = int(hashlib.sha256(aid.encode()).hexdigest()[:8], 16)
    # Deliberate silhouette variation: hair, headgear, species and role objects, not just palette swaps.
    curly = seed % 3 == 0
    eyes = ('<path d="M211 233q12 -9 23 0M278 233q12 -9 23 0" fill="none" stroke="#29323e" stroke-width="5"/>'
            if expression == "smile" else
            '<path d="M210 222l26 4M277 226l26 -4" fill="none" stroke="#342f36" stroke-width="5"/>'
            '<ellipse cx="224" cy="235" rx="5" ry="7" fill="#29323e"/><ellipse cx="289" cy="235" rx="5" ry="7" fill="#29323e"/>')
    mouth = {"smile": "M240 270q17 14 34 -1", "smirk": "M241 274q19 4 36 -7",
             "calm": "M244 274q13 5 26 0", "stern": "M244 276q13 -5 26 0"}[expression]
    face = f'<ellipse cx="256" cy="228" rx="63" ry="84" fill="{skin}"/><path d="M263 240l-7 16 12 1" fill="none" stroke="#755d60" stroke-opacity=".5" stroke-width="3"/>{eyes}<path d="{mouth}" stroke="#795057" stroke-width="4" fill="none" stroke-linecap="round"/>'
    hair_shape = (f'<path d="M188 240Q158 127 240 130Q336 111 327 242L308 212Q296 159 251 175Q211 166 200 239Z" fill="{hair}"/>'
                  if not curly else
                  f'<path d="M185 234Q164 183 183 159Q177 119 215 123Q235 98 269 120Q309 102 321 143Q353 165 327 235L305 195Q272 173 255 178Q218 183 201 219Z" fill="{hair}"/>')
    detail = ""
    if kind in ("sage", "smith", "giant", "merchant"):
        detail += f'<path d="M199 260Q255 294 315 258L300 318 256 343 209 315Z" fill="{hair}"/><path d="M225 289l10 33m22 -35v41m26 -39l-9 33" stroke="#fff" opacity=".15" stroke-width="3"/>'
    if kind == "witch":
        detail += f'<path d="M191 156L205 99 253 47 278 117 314 158Z" fill="#35445f"/><path d="M154 169Q248 129 365 174L332 196Q258 166 175 194Z" fill="#586884" stroke="{accent}" stroke-width="3"/><circle cx="250" cy="123" r="17" fill="{accent}" opacity=".8"/>'
    elif kind in ("knight", "general", "warrior", "steward"):
        detail += f'<path d="M184 233V163Q254 108 326 163V233L307 204 303 172 212 173 204 206Z" fill="#526476" stroke="{accent}" stroke-width="4"/><path d="M215 162Q256 99 298 162M253 145v37" fill="none" stroke="{accent}" stroke-width="7"/>'
        if kind == "general":
            detail += '<path d="M249 137Q193 66 236 51Q323 72 310 129L280 139Q278 101 245 92Z" fill="#a14a3a"/>'
    elif kind == "omen":
        detail += '<path d="M193 184Q140 127 166 81Q162 139 210 144M315 183Q368 142 348 103Q342 144 302 152" fill="#9d8d6c" stroke="#d3c5a4" stroke-width="4"/>'
    elif kind in ("seer", "cleric", "turtle"):
        detail += f'<path d="M204 166L213 107 256 87 303 112 312 166Z" fill="#c9c5b6" stroke="{accent}" stroke-width="4"/><path d="M256 112v51M217 150h80" stroke="{accent}" stroke-width="5"/>'
    elif kind == "astrologer":
        detail += '<path d="M179 159L202 114 225 153 248 83 278 144 305 102 330 166Z" fill="#7ec6d9" fill-opacity=".8" stroke="#d0edf6" stroke-width="3"/><path d="M248 93l7 63m50 -42l-16 44m-84 -37l14 39" stroke="#e1fbff" opacity=".5"/>'
    elif kind in ("scholar", "merchant", "tailor"):
        detail += f'<path d="M179 166Q256 120 334 164L324 183Q244 158 184 186Z" fill="#49414c" stroke="{accent}" stroke-width="4"/><path d="M202 152L215 119 295 132 317 158Z" fill="#5a4d59"/>'
    elif kind == "cyclist":
        detail += '<path d="M207 169h39v21h-39zm61 0h39v21h-39zm-22 7h22" fill="#715c3e" stroke="#d5c19a" stroke-width="4"/>'
    elif kind == "senator":
        detail += f'<path d="M197 193Q186 107 259 116Q335 104 318 185L290 153 224 158Z" fill="{hair}"/><ellipse cx="290" cy="125" rx="31" ry="25" fill="{hair}"/>'
    elif kind in ("companion", "traveler", "storyteller", "duelist"):
        detail += f'<path d="M191 196Q174 273 193 334L220 318 204 229M315 190Q345 263 321 332L298 316 308 230Z" fill="{hair}"/><path d="M198 172Q258 106 317 174" fill="none" stroke="{accent}" stroke-width="8"/>'
    if kind == "wolf":
        face = '<path d="M194 161L166 104 228 145 279 143 341 107 319 169 331 252 303 300 256 322 205 297 179 253Z" fill="#939ca8"/><path d="M210 260L256 225 303 261 277 289 233 289Z" fill="#cad0d4"/><path d="M242 257h29l-14 13Z" fill="#303944"/><path d="M202 211l31 5m47 0l31 -5" stroke="#3a444b" stroke-width="5"/><ellipse cx="221" cy="229" rx="6" ry="5" fill="#e0c58a"/><ellipse cx="291" cy="229" rx="6" ry="5" fill="#e0c58a"/>'
        hair_shape = detail = ""
    elif kind == "steed":
        face = '<path d="M210 164L194 107 226 129 274 135 310 103 304 173 327 240 296 306 230 323 201 285 194 224Z" fill="#91a2ae"/><path d="M268 155L284 208 272 258 297 270 270 300 237 298 252 265 244 218Z" fill="#b8c6cf"/><path d="M200 173Q177 83 203 67Q188 108 223 148M294 171Q338 114 310 72Q329 136 278 144" fill="#cbd0ca"/><ellipse cx="218" cy="216" rx="6" ry="9" fill="#283643"/><ellipse cx="287" cy="224" rx="6" ry="9" fill="#283643"/><ellipse cx="255" cy="287" rx="6" ry="4" fill="#495e69"/>'
        hair_shape = '<path d="M239 140Q256 86 281 139L273 210 254 180 246 226Z" fill="#d9e0e4"/>'
        detail = ""
    elif kind == "turtle":
        face = '<path d="M190 225Q176 171 215 156Q272 140 311 177L324 232 305 284 258 307 208 280Z" fill="#91a496"/><path d="M204 221q14 -8 28 0m43 0q14 -8 28 0M234 267q22 13 43 -1" fill="none" stroke="#455c51" stroke-width="5"/><path d="M195 235l26 9m73 0l24 -10m-88 -75l13 17m38 -16l-13 17" stroke="#c4d0b8" stroke-width="4"/>'
        hair_shape = ""
    elif kind == "dual":
        detail += '<path d="M258 148v155" stroke="#eed59d" stroke-width="4" opacity=".8"/><path d="M259 150Q313 129 321 188L308 227 306 179 266 178Z" fill="#ddb76b"/>'
    body = f'<path d="M210 298Q166 301 140 349L106 490H405L373 349Q350 300 301 298L256 328Z" fill="#303b4d" stroke="{accent}" stroke-width="3"/><path d="M213 302L256 329 235 386 181 336M299 302L256 329 279 386 329 335" fill="{accent}" fill-opacity=".55"/><path d="M255 335v146" stroke="{accent}" opacity=".55" stroke-width="3"/>'
    symbols = {
        "sage": '<path d="M181 398l56 -10 22 61 -58 15Z" fill="#786045" stroke="#b9a274" stroke-width="3"/><path d="M193 407l39 -8m-34 24l34 -8" stroke="#c8b68d" stroke-width="3"/>',
        "knight": '<path d="M129 363L175 345 211 369 203 428 170 456 136 421Z" fill="#5d6f80" stroke="#b3cfdf" stroke-width="4"/><path d="M168 363v70m-24 -42h47" stroke="#c2a975" stroke-width="4"/>',
        "smith": '<path d="M194 355h124v130H193Z" fill="#685244"/><path d="M279 377l25 69" stroke="#bfa078" stroke-width="12"/><path d="M255 363l53 -20 15 31 -52 21Z" fill="#85909b" stroke="#c6cbd0" stroke-width="4"/>',
        "steward": '<rect x="191" y="382" width="102" height="75" rx="10" fill="#d0bd91"/><path d="M207 398h67m-67 16h46m-46 16h67" stroke="#657d8c" stroke-width="5"/>',
        "scholar": '<circle cx="193" cy="397" r="16" fill="none" stroke="#c6b574" stroke-width="7"/><path d="M199 411l26 31m-8 -18l12 -8" stroke="#c6b574" stroke-width="7"/>',
        "tailor": '<path d="M211 404l34 31m-5 -33l-29 32" stroke="#cdc0a1" stroke-width="6"/><circle cx="207" cy="398" r="10" stroke="#cdc0a1" stroke-width="4" fill="none"/><circle cx="245" cy="397" r="10" stroke="#cdc0a1" stroke-width="4" fill="none"/>',
        "storyteller": '<path d="M207 433Q181 411 211 378Q202 408 225 400Q241 429 207 433Z" fill="#e3b079"/><circle cx="211" cy="421" r="7" fill="#fae1a5"/>',
        "merchant": '<path d="M199 384v18m-22 39l9 -33h26l13 33Z" fill="#b4a276" stroke="#e0cd95" stroke-width="3"/><ellipse cx="199" cy="444" rx="27" ry="5" fill="#81745c"/>',
        "seer": '<circle cx="224" cy="413" r="28" fill="#6e586e" stroke="#b79bc4" stroke-width="3"/><path d="M207 413q18 -18 35 0q-17 18 -35 0" fill="none" stroke="#d2b99c" stroke-width="3"/>',
        "astrologer": '<circle cx="227" cy="416" r="29" fill="#2d5674" stroke="#a6d6e5" stroke-width="2"/><path d="M210 426l15 -33 16 34 -31 -12h32Z" fill="none" stroke="#bce2e9" stroke-width="2"/>',
        "senator": '<path d="M216 400l28 36" stroke="#adbbb9" stroke-width="7"/><path d="M209 385l29 -21 16 21 -30 21Z" fill="#c6b1d4"/>',
        "coach": '<ellipse cx="209" cy="402" rx="22" ry="29" fill="none" stroke="#c3dcd6" stroke-width="5"/><path d="M219 427l20 36m-36 -82v41m12 -41v42m-28 -18h44" stroke="#c3dcd6" stroke-width="3"/>',
        "consultant": '<path d="M198 404h49l-6 49h-36Z" fill="#dfc8a1"/><path d="M222 398l9 -34" stroke="#9cc0c3" stroke-width="5"/><circle cx="217" cy="440" r="4" fill="#837158"/><circle cx="231" cy="438" r="4" fill="#837158"/>',
        "cyclist": '<circle cx="206" cy="426" r="17" fill="none" stroke="#cab67c" stroke-width="3"/><circle cx="260" cy="426" r="17" fill="none" stroke="#cab67c" stroke-width="3"/><path d="M206 426l16 -26 17 26h-33m33 0l16 -31 5 31m-46 -32h16" fill="none" stroke="#cab67c" stroke-width="3"/>',
        "traveler": '<rect x="197" y="389" width="61" height="66" rx="8" fill="#957b9c"/><path d="M212 390v-13h31v13m-17 11v38m-12 -25h24" fill="none" stroke="#d3c6d3" stroke-width="3"/>',
        "athlete": '<path d="M193 418h60m-60 -14v28m9 -34v40m42 -40v40m9 -34v28" stroke="#a4b9c5" stroke-width="7"/>',
        "analyst": '<circle cx="220" cy="416" r="29" fill="#bf965e"/><path d="M191 416h58m-29 -29v58m-20 -50q29 21 0 42m40 -42q-29 21 0 42" fill="none" stroke="#745438" stroke-width="2"/>',
        "companion": '<path d="M220 391l8 17 19 2 -14 13 4 19 -17 -10 -17 10 4 -19 -14 -13 19 -2Z" fill="#e4c58d"/>',
    }
    symbol = symbols.get(kind, '<circle cx="224" cy="417" r="24" fill="none" stroke="'+accent+'" stroke-width="4"/><path d="M214 417h20m-10 -10v20" stroke="'+accent+'" stroke-width="3"/>')
    if full:
        limbs = f'<path d="M144 346L100 509Q91 543 115 549L169 422M370 346L411 514Q420 543 395 549L343 422" fill="#334053" stroke="{accent}" stroke-width="3"/><ellipse cx="112" cy="539" rx="14" ry="20" fill="{skin}"/><ellipse cx="399" cy="539" rx="14" ry="20" fill="{skin}"/><path d="M180 479L168 680 244 680 258 509 276 680H348L331 479Z" fill="#273344" stroke="{accent}" stroke-width="3"/><path d="M171 669h74v37h-97q-2 -20 23 -37m106 0h70l22 37h-92Z" fill="#202630" stroke="{accent}" stroke-width="2"/>'
        body += f'<path d="M151 382L138 604 208 627 255 499 305 625 373 602 361 380" fill="#3b485c" stroke="{accent}" stroke-width="3"/>' if kind in ("witch", "sage", "cleric", "seer", "storyteller") else ""
        figure = limbs + body + hair_shape + face + detail + symbol
        if kind == "witch":
            figure = f'<path d="M150 383Q86 386 111 479M364 383Q424 386 402 479" fill="none" stroke="{skin}" stroke-width="14"/>' + figure
        elif kind == "steed":
            figure = ('<path d="M145 423Q81 428 70 540L91 555Q98 481 163 477" fill="#cbd9e0"/>'
                      '<path d="M153 472L125 655 160 670 203 474M246 470L220 680 255 689 279 475M319 467L337 657 370 669 357 459M356 454L391 646 426 656 391 447" fill="#758a99" stroke="#a8bfcc" stroke-width="3"/>'
                      '<ellipse cx="253" cy="446" rx="136" ry="84" fill="#91a2ae"/>'
                      '<path d="M305 446L305 292Q314 240 370 239L407 277 383 427Z" fill="#91a2ae"/>'
                      f'<g transform="translate(176 95) scale(.65)">{hair_shape}{face}</g>'
                      f'<path d="M213 378h64v124h-64Z" fill="#48596c" stroke="{accent}" stroke-width="4"/>'
                      f'<path d="M201 376q46 -32 84 0" fill="none" stroke="{accent}" stroke-width="10"/>'
                      '<path d="M122 655h38v25h-48m107 0h36v24h-45m82 -47h38v25h-36m39 -36h37v25h-35" fill="#344656"/>')
        elif kind == "turtle":
            figure = (f'<ellipse cx="256" cy="460" rx="143" ry="160" fill="#526a60" stroke="{accent}" stroke-width="4"/>'
                      '<path d="M256 321L194 393 202 492 257 548 313 492 320 393Z" fill="#6f8270" stroke="#b4bb98" stroke-width="4"/>'
                      '<path d="M194 393l-62 -20m70 119l-75 31m130 25v63m56 -119l73 31m-66 -130l60 -20" stroke="#b4bb98" stroke-width="4"/>'
                      '<path d="M127 455Q83 488 91 553Q124 565 157 510M385 455Q429 488 421 553Q385 565 355 510M184 588L154 656Q203 689 227 625M328 589L358 657Q309 689 285 625" fill="#91a496" stroke="#c3bca0" stroke-width="3"/>'
                      '<path d="M180 301L257 337 333 301 320 403 257 431 191 404Z" fill="#c9c5b6" stroke="#c3bca0" stroke-width="3"/>'
                      + face + detail)
        content = f'<g transform="translate(0 12)">{figure}</g>'
        box = "0 0 512 768"
    else:
        backdrop = f'<rect width="512" height="512" fill="#172130"/><circle cx="256" cy="218" r="188" fill="url(#halo)"/><circle cx="256" cy="224" r="164" fill="none" stroke="{accent}" stroke-opacity=".3" stroke-width="2"/><path d="M82 424Q256 321 430 424" fill="none" stroke="{accent}" stroke-opacity=".18" stroke-width="2"/>'
        content = backdrop + body + hair_shape + face + detail + symbol
        box = "0 0 512 512"
    title = html.escape(name + (" character art" if full else " portrait"))
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="{box}" role="img" aria-labelledby="title">
<title id="title">{title}</title><desc>Original fictional illustration; not a real person or extracted game artwork.</desc>
<defs><radialGradient id="halo"><stop stop-color="{accent}" stop-opacity=".24"/><stop offset="1" stop-color="{accent}" stop-opacity="0"/></radialGradient></defs>
{content}</svg>
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply-profiles", action="store_true")
    args = parser.parse_args()
    path = ROOT / "agents.json"
    data = json.loads(path.read_text())
    if {a["id"] for a in data["agents"]} != set(PROFILES):
        raise ValueError("Profile coverage must exactly match the active catalog")
    out = ROOT / "avatars"
    out.mkdir(exist_ok=True)
    for a in data["agents"]:
        aid = a["id"]
        temperament, quirks, delivery, *_ = PROFILES[aid]
        if args.apply_profiles:
            a["personality"] = {"temperament": temperament, "quirks": quirks, "delivery": delivery,
                                "fictional": True}
        (out / f"{aid}-persona.svg").write_text(illustration(aid, a["name"]), encoding="utf-8")
        (out / f"{aid}-persona-full.svg").write_text(illustration(aid, a["name"], True), encoding="utf-8")
    if args.apply_profiles:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Built {len(data['agents'])} original portraits and transparent character illustrations; no API calls.")


if __name__ == "__main__":
    main()
