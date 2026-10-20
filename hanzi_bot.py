"""Hanzi in three languages -> Telegram: Mandarin, Cantonese and Japanese side by side.

Telegram commands (the menu shows /random /look /quiz /save /review /about; /level /remove /tones work too):
  /random          a random character used in all three languages, at your level (/random 3: a level just this once)
  /level 2         set your level, 1 (easiest) to 5, or "all"
  /about           what's inside: how many characters and words, what each level means, data sources
  /look 行         look up a character or word (simplified, traditional or Japanese form)
  /quiz            4-choice question from your saved list
  /save            save the last character or word you saw
  /review          your saved list, numbered, 30 per page (/review 2: page 2)
  /remove 2        remove by number or by character (/remove all asks to confirm)
  /tones marks     Mandarin tones as marks (xíng) or numbers (xing2)

The bot only answers; it never messages first.
Data: data/hanzi.json.gz, built and cross-checked by build_data.py (see DATA_LICENSES.md).
The repo is public, so your saved list and settings are stored encrypted in state.enc with the STATE_KEY secret,
and logs never show what you looked up.
Standard library only.
"""
import base64
import gzip
import hashlib
import hmac
import json
import os
import random
import re
import secrets
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

TOKEN = os.environ["TG_TOKEN"].strip()
CHAT_ID = os.environ["TG_CHAT_ID"].strip()
STATE_KEY = (os.environ.get("STATE_KEY") or "").strip()
PLAIN_STATE = Path("state.json")
SECRET_STATE = Path("state.enc")
DATA = Path("data/hanzi.json.gz")
MENU_VERSION = 4  # bump when COMMANDS change so Telegram's menu gets refreshed
DEFAULT_LEVEL = "1"
LEVELS = ("1", "2", "3", "4", "5")
LEVEL_INFO = {  # what each level means (matches build_data.py; the hardest of the three views counts)
    "1": "taught in grades 1–2 in Japan and Hong Kong, top 500 in Mandarin",
    "2": "grades 3–4, top 1,000 in Mandarin",
    "3": "grades 5–6, top 1,500 in Mandarin",
    "4": "Japanese secondary school or not in Hong Kong primary, top 2,500 in Mandarin",
    "5": "everything rarer",
}
REVIEW_PAGE = 30  # saved items per /review page (a Telegram message holds about 4,000 characters)
MAX_CARDS = 3  # /look with several matches (e.g. 发 -> 發, 髮) shows at most this many
GLOSS_LEN = 60  # long dictionary glosses are cut to this many characters

HELP = (
    "Commands:\n"
    "/random — a random character at your level (Mandarin · Cantonese · Japanese)\n"
    "/look 行 — look up a character or word (any form: 银 / 銀)\n"
    "/save — save what you just saw\n"
    "/quiz — a question from your saved list, answer with 1–4\n"
    "/review — your saved list (remove one: /remove 2)\n"
    "/about — what's inside, levels (change: /level 2), tone style\n\n"
    "Only dictionary data that passed cross-checks is shown."
)
COMMANDS = [  # the menu; /level, /remove and /tones still work, they're explained in /about and /review
    {"command": "random", "description": "A random character at your level"},
    {"command": "look", "description": "Look up: /look 行"},
    {"command": "quiz", "description": "Quiz from your saved list"},
    {"command": "save", "description": "Save what you just saw"},
    {"command": "review", "description": "Your saved list"},
    {"command": "about", "description": "What's inside, levels, settings"},
]
# word fields in data["words"][trad]
W_SIMP, W_PINYIN, W_GLOSS, W_JYUT, W_JA, W_JA_READ, W_JA_GLOSS, W_COMMON = range(8)


def why(e):
    """Short error text for the public log: the error type (and HTTP code), never what was looked up."""
    return f"{type(e).__name__} {e.code}" if isinstance(e, urllib.error.HTTPError) else type(e).__name__


# ---------- Telegram ----------
def tg(method, **params):
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    data = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(url, data=data, timeout=30) as r:
        return json.load(r)


def send(text):
    tg("sendMessage", chat_id=CHAT_ID, text=text[:4000], disable_web_page_preview="true")


# ---------- Data ----------
_data = None


def data():
    global _data
    if _data is None:
        _data = json.loads(gzip.decompress(DATA.read_bytes()))
    return _data


# ---------- Formatting ----------
VOWEL_MARKS = {"1": "̄", "2": "́", "3": "̌", "4": "̀"}


def pinyin(syllable, style):
    """'xing2' -> 'xíng' (marks) or 'xing2' (numbers); 'lv4' -> 'lǜ'; neutral tone 5 -> no mark."""
    base, tone = syllable[:-1], syllable[-1]
    base = base.replace("v", "ü")
    if style == "numbers":
        return base + (tone if tone != "5" else "")
    if tone not in VOWEL_MARKS:
        return base
    # standard placement: a or e takes the mark, then the o of "ou", else the last vowel
    i = next((base.index(v) for v in "ae" if v in base), None)
    if i is None:
        i = base.index("o") if "ou" in base else max(base.rfind(v) for v in "iouü")
    if i < 0:
        return base
    return unicodedata.normalize("NFC", base[: i + 1] + VOWEL_MARKS[tone] + base[i + 1:])


def forms(simp, trad):
    """'银 / 銀', or just '游' when both forms are the same."""
    return simp if simp == trad else f"{simp} / {trad}"


def short(glosses, n=3):
    return "; ".join(g if len(g) <= GLOSS_LEN else g[: GLOSS_LEN - 1].rstrip() + "…" for g in glosses[:n])


def pinyin_text(keys, style):
    """A word's syllables: 'yin2 hang2' -> 'yínháng' (one word, apostrophe before a/o/e: 'xī'ān'); numbers keep spaces."""
    if not keys:
        return ""
    sylls = [pinyin(k, style) for k in keys.split()]
    if style == "numbers":
        return " ".join(sylls)
    out = sylls[0]
    for s in sylls[1:]:
        out += ("'" if unicodedata.normalize("NFD", s)[0] in "aoe" else "") + s
    return out


def kun_text(r):
    """'い.く' -> 'い(く)'"""
    stem, _, oku = r.strip("-").partition(".")
    return f"{stem}({oku})" if oku else stem


def ja_line(form):
    j = data()["ja"].get(form)
    if not j:
        return ""
    reads = "・".join(j["on"] + [kun_text(k) for k in j["kun"]])
    return f"{reads} — {'; '.join(j['m'])}"


def example_line(trad, style):
    w = data()["words"][trad]
    return f"{w[W_SIMP]} {pinyin_text(w[W_PINYIN], style)} / {trad} {w[W_JYUT]} / {w[W_JA]} {w[W_JA_READ]} — {short(w[W_GLOSS], 1)}"


def char_card(trad, style):
    c = data()["chars"][trad]
    simp, ja = c["s"], c["ja"]
    lv = data()["level"].get(trad)
    lines = [f"{simp} / {trad} / {ja or '—'}" + (f"   · level {lv}" if lv else "")]
    if c["zh"]:
        lines.append(f"🇨🇳 {simp} " + " · ".join(f"{pinyin(p, style)} — {short(g)}" for p, g in c["zh"]))
    else:
        lines.append("🇨🇳 — not used in Mandarin")
    lines.append(f"🇭🇰 {trad} {' · '.join(c['yue'])}" if c["yue"] else "🇭🇰 — not in the Cantonese dictionary")
    if ja and ja_line(ja):
        lines.append(f"🇯🇵 {ja} {ja_line(ja)}")
    else:
        lines.append("🇯🇵 — not used in Japanese")
    examples = [example_line(w, style) for w in data()["ex"].get(trad, [])]
    if examples:
        lines.append("\ne.g. " + "\n     ".join(examples))
    return "\n".join(lines)


def ja_only_card(form):
    return f"— / — / {form}\n🇨🇳 — not used in Mandarin\n🇭🇰 — not in the Cantonese dictionary\n🇯🇵 {form} {ja_line(form)}"


def word_card(trad, style):
    w = data()["words"][trad]
    lines = [f"{w[W_SIMP]} / {trad} / {w[W_JA] or '—'}"]
    py = pinyin_text(w[W_PINYIN], style)
    lines.append(f"🇨🇳 {w[W_SIMP]} {py + ' ' if py else ''}— {short(w[W_GLOSS])}")
    lines.append(f"🇭🇰 {trad} {w[W_JYUT]}" if w[W_JYUT] else "🇭🇰 — not in the Cantonese dictionary")
    if w[W_JA]:
        lines.append(f"🇯🇵 {w[W_JA]} {w[W_JA_READ]} — {short(w[W_JA_GLOSS])}")
    else:
        lines.append("🇯🇵 — no matching Japanese word")
    return "\n".join(lines)


def find(text):
    """Text in any form -> list of ("char"|"word"|"ja", key)."""
    d = data()
    hits = []
    if len(text) == 1:
        trads = d["simp"].get(text, [])
        # a simplified character can also exist on its own without Mandarin data (气 next to 氣): skip that one
        own = [text] if text in d["chars"] and (d["chars"][text]["zh"] or not trads) else []
        for t in own + trads + ([d["ja_to_trad"][text]] if text in d["ja_to_trad"] else []):
            if t in d["chars"] and ("char", t) not in hits:
                hits.append(("char", t))
        pool = set(d["pool"])
        hits.sort(key=lambda h: (h[1] not in pool, not d["chars"][h[1]]["zh"]))  # everyday meaning first
        if not hits and text in d["ja"]:
            hits.append(("ja", text))
    else:
        for t in [text] + d["simp_words"].get(text, []) + ja_words().get(text, []):
            if t in d["words"] and ("word", t) not in hits:
                hits.append(("word", t))
    return hits[:MAX_CARDS]


_ja_words = None


def ja_words():
    """Japanese word form -> traditional words it was matched to (so /look 頭髪 finds 頭髮)."""
    global _ja_words
    if _ja_words is None:
        _ja_words = {}
        for t, w in data()["words"].items():
            if w[W_JA] and w[W_JA] != t:
                _ja_words.setdefault(w[W_JA], []).append(t)
    return _ja_words


def card(item, style):
    kind, key = item
    return {"char": char_card, "word": word_card}[kind](key, style) if kind != "ja" else ja_only_card(key)


def label(item, style):
    """One line for the saved list: forms, Mandarin · Cantonese · Japanese reading, meaning."""
    kind, key = item
    d = data()
    if kind == "char":
        c = d["chars"][key]
        zh = pinyin(c["zh"][0][0], style) if c["zh"] else "—"
        j = d["ja"].get(c["ja"]) or {}
        ja = (j.get("on") or [kun_text(k) for k in j.get("kun", [])] or ["—"])[0]  # first on'yomi, else first kun'yomi
        meaning = c["zh"][0][1][0] if c["zh"] else (j["m"][0] if j.get("m") else "")
        return f"{c['s']}/{key}/{c['ja'] or '—'}  {zh} · {c['yue'][0] if c['yue'] else '—'} · {ja} — {meaning}"
    if kind == "word":
        w = d["words"][key]
        return (f"{w[W_SIMP]}/{key}/{w[W_JA] or '—'}  {pinyin_text(w[W_PINYIN], style) or '—'} · {w[W_JYUT] or '—'} · "
                f"{w[W_JA_READ] or '—'} — {w[W_GLOSS][0]}")
    return f"{key}  {ja_line(key)}"


# ---------- Quiz ----------
def quiz_question(item, style):
    """(question, 4 options, index of the right one) from checked data only; None if this item can't make one."""
    d = data()
    kind, key = item
    pool = d["pool"]
    if kind == "char":
        c = d["chars"][key]
        asks = []
        same_simp = [key] + [t for t in d["simp"].get(c["s"], []) if t != key]  # 发 -> 發, 髮
        if c["zh"]:
            shown = c["s"] if c["s"] == key else f"{c['s']} ({key})"
            asks.append(("zh", f"{shown} — Mandarin reading?", pinyin(c["zh"][0][0], style),
                         {pinyin(p, style) for t in same_simp if t in d["chars"] for p, _ in d["chars"][t]["zh"]},
                         lambda t: pinyin(d["chars"][t]["zh"][0][0], style) if d["chars"][t]["zh"] else None))
        if c["yue"]:
            asks.append(("yue", f"{key} — Cantonese reading?", c["yue"][0], set(c["yue"]),
                         lambda t: d["chars"][t]["yue"][0] if d["chars"][t]["yue"] else None))
        if c["ja"] in d["ja"] and d["ja"][c["ja"]]["on"] and not c["jdiff"]:
            asks.append(("ja", f"{c['ja']} — Japanese on'yomi?", d["ja"][c["ja"]]["on"][0], set(d["ja"][c["ja"]]["on"]),
                         lambda t: next(iter((d["ja"].get(d["chars"][t]["ja"]) or {}).get("on", [])), None)))
        if c["zh"]:
            asks.append(("meaning", f"Which one means “{c['zh'][0][1][0]}”?", forms(c["s"], key), set(),
                         lambda t: forms(d["chars"][t]["s"], t)))
    elif kind == "word":
        w = d["words"][key]
        words = [t for t in d["words"] if len(t) == len(key) and d["words"][t][W_COMMON]] or list(d["words"])
        asks = []
        if w[W_PINYIN]:
            asks.append(("zh", f"{w[W_SIMP]} — Mandarin reading?", pinyin_text(w[W_PINYIN], style), set(),
                         lambda t: pinyin_text(d["words"][t][W_PINYIN], style) or None))
        if w[W_JYUT]:
            asks.append(("yue", f"{key} — Cantonese reading?", w[W_JYUT], set(), lambda t: d["words"][t][W_JYUT] or None))
        asks.append(("meaning", f"Which one means “{w[W_GLOSS][0]}”?", forms(w[W_SIMP], key), set(),
                     lambda t: forms(d["words"][t][W_SIMP], t)))
        pool = words
    else:
        return None
    random.shuffle(asks)
    target_gloss = set(re.findall(r"[a-z]{3,}", " ".join(
        g for _, gl in d["chars"][key]["zh"] for g in gl).lower())) if kind == "char" else set(
        re.findall(r"[a-z]{3,}", " ".join(d["words"][key][W_GLOSS]).lower()))
    for _, question, right, also_right, option_of in asks:
        wrong = []
        for t in random.sample(pool, min(len(pool), 200)):
            if t == key:
                continue
            o = option_of(t)
            if not o or o == right or o in also_right or o in wrong:
                continue
            if question.startswith("Which one means"):  # a wrong option must not share the meaning
                gl = d["chars"][t]["zh"] if kind == "char" else [[None, d["words"][t][W_GLOSS]]]
                if target_gloss & set(re.findall(r"[a-z]{3,}", " ".join(g for _, x in gl for g in x).lower())):
                    continue
            wrong.append(o)
            if len(wrong) == 3:
                options = wrong + [right]
                random.shuffle(options)
                return question, options, options.index(right)
    return None


def level_pool(level):
    """Pool characters at this level ("all" -> every level)."""
    d = data()
    return d["pool"] if level == "all" else [t for t in d["pool"] if str(d["level"].get(t)) == level]


def start_quiz(state):
    choices = [tuple(x) for x in state.get("saved", [])]
    if not choices:
        send("📚 Your saved list is empty. See characters with /random or /look, keep them with /save, then /quiz.")
        return
    style = state.get("tones", "marks")
    for _ in range(10):
        item = random.choice(choices)
        q = quiz_question(item, style)
        if q:
            question, options, answer = q
            state["quiz"] = {"item": list(item), "options": options, "answer": answer}
            send(f"❓ {question}\n\n" + "\n".join(f"{i + 1}) {o}" for i, o in enumerate(options))
                 + "\n\nAnswer with 1, 2, 3 or 4")
            return
    send("❔ Couldn't make a question from your saved list right now. Save a few more with /save.")


def answer_quiz(state, n):
    q = state.pop("quiz")
    item = tuple(q["item"])
    state["last"] = list(item)
    right = q["answer"]
    style = state.get("tones", "marks")
    head = "✅ Correct!" if n == right else f"❌ Answer: {right + 1}) {q['options'][right]}"
    send(f"{head}\n\n{card(item, style)}\n\n/save · /quiz for another")


# ---------- Commands ----------
def handle_commands(state):
    if state.get("menu_version") != MENU_VERSION:  # refresh Telegram's command menu
        tg("setMyCommands", commands=json.dumps(COMMANDS))
        state["menu_version"] = MENU_VERSION
    updates = tg("getUpdates", offset=state.get("offset", 0), timeout=0).get("result", [])
    print(f"{len(updates)} new Telegram message(s)")
    for u in updates:
        state["offset"] = u["update_id"] + 1
        msg = u.get("message") or {}
        # The repo (and its Actions logs) is public: never log chat ids, message text or lookups.
        if str(msg.get("chat", {}).get("id")) != CHAT_ID:
            print("  ignored a message from another chat (doesn't match TG_CHAT_ID)")
            continue
        text = (msg.get("text") or "").strip()
        if not text:
            continue
        try:
            if not DATA.exists():
                send("⏳ The dictionary isn't ready yet. Run the build-data workflow once, then try again.")
                continue
            if text in ("1", "2", "3", "4") and state.get("quiz"):
                answer_quiz(state, int(text) - 1)
                continue
            cmd, _, arg = text.partition(" ")
            run_command(state, cmd.lower().split("@")[0], arg.strip())
        except Exception as e:
            print(f"command failed: {why(e)}")
            send(f"⚠️ Something went wrong ({why(e)}). Try again in a few minutes.")


def run_command(state, cmd, arg):
    style = state.get("tones", "marks")
    saved = state.setdefault("saved", [])

    if cmd == "/random":
        level = arg if arg in LEVELS else state.get("level", DEFAULT_LEVEL)
        if not level_pool(level):
            send(f"No characters at level {level}. Try /level all")
            return
        item = ("char", random.choice(level_pool(level)))
        state["last"] = list(item)
        send(card(item, style) + "\n\n/save · /random for another")

    elif cmd == "/look" and arg:
        hits = find(arg.split()[0])
        if not hits:
            send(f"❔ \"{arg}\" isn't in the checked dictionary data. Try a single character, or the other form (银/銀).")
            return
        state["last"] = list(hits[0])
        more = "\n\n(/save keeps the first one)" if len(hits) > 1 else "\n\n/save to keep it"
        send("\n\n".join(card(h, style) for h in hits) + more)

    elif cmd == "/save":
        last = state.get("last")
        if not last:
            send("Nothing to save yet. Try /random or /look 行 first.")
        elif last in saved:
            send(f"Already saved: {label(tuple(last), style)}")
        else:
            saved.append(last)
            send(f"💾 Saved ({len(saved)}): {label(tuple(last), style)}")

    elif cmd == "/review":
        if not saved:
            send("📚 Your saved list is empty. /random or /look, then /save.")
        else:
            pages = (len(saved) + REVIEW_PAGE - 1) // REVIEW_PAGE
            page = min(max(int(arg), 1), pages) if arg.isdigit() else 1
            first = (page - 1) * REVIEW_PAGE
            rows = [f"{i + 1}. {label(tuple(x), style)}" for i, x in enumerate(saved[first:first + REVIEW_PAGE], first)]
            nav = []
            if page > 1:
                nav.append(f"◀ /review {page - 1}")
            if page < pages:
                nav.append(f"/review {page + 1} ▶")
            head = f"📚 Saved — {len(saved)}" + (f" · page {page}/{pages}" if pages > 1 else "")
            send(f"{head}\n\n" + "\n".join(rows) + "\n\n" + (" · ".join(nav) + "\n" if nav else "")
                 + "/remove 2 to remove · /quiz to practice")

    elif cmd == "/remove" and arg:
        if arg.lower() == "all":
            send(f"This removes all {len(saved)} saved items. Send /remove all yes to confirm.")
        elif arg.lower() == "all yes":
            n = len(saved)
            saved.clear()
            send(f"🗑 Removed all {n} items.")
        elif arg.isdigit() and 1 <= int(arg) <= len(saved):
            item = saved.pop(int(arg) - 1)
            send(f"🗑 Removed: {label(tuple(item), style)}")
        else:
            keys = {k for _, k in find(arg)} | {arg}
            keep = [x for x in saved if x[1] not in keys]
            if len(keep) == len(saved):
                send("Not in your saved list. /review shows the numbers.")
            else:
                send(f"🗑 Removed {len(saved) - len(keep)} item(s).")
                saved[:] = keep

    elif cmd == "/quiz":
        start_quiz(state)

    elif cmd == "/level":
        counts = " · ".join(f"{n}: {len(level_pool(n))}" for n in LEVELS)
        if arg.lower() in LEVELS + ("all",):
            state["level"] = arg.lower()
            send(f"📶 Level set to {arg.lower()} for /random\n\nCharacters per level — {counts}")
        else:
            send(f"📶 Your level: {state.get('level', DEFAULT_LEVEL)}\n"
                 "1 = taught early in both Japan and Hong Kong primary school … 5 = rarer secondary-school kanji\n\n"
                 f"Characters per level — {counts}\n\nChange it: /level 2 (or /level all)")

    elif cmd in ("/about", "/info"):
        d = data()
        rows = []
        for n in LEVELS:
            chars = level_pool(n)
            sample = random.Random(int(n)).sample(chars, min(6, len(chars)))  # same examples every time
            rows.append(f"{n} · {len(chars):,} — {LEVEL_INFO[n]}\n    e.g. {' '.join(x if d['chars'][x]['s'] == x else f"{d['chars'][x]['s']}({x})" for x in sample)}")
        send("ℹ️ What's inside\n\n"
             f"🎲 /random picks from {len(d['pool']):,} characters used in all three languages\n"
             f"🔎 /look covers {len(d['chars']):,} characters and {len(d['words']):,} words\n\n"
             f"📶 Levels (yours: {state.get('level', DEFAULT_LEVEL)}) — the hardest of Japan · Hong Kong · mainland China counts\n"
             + "\n".join(rows) + "\n\n"
             f"Change level: /level 2 (or /level all)\n"
             f"Tones: /tones marks (xíng) or /tones numbers (xing2) — now {state.get('tones', 'marks')}\n\n"
             "📚 Data: CC-CEDICT · rime-cantonese · KANJIDIC2 · JMdict · Unicode Unihan\n"
             f"Only readings two sources agree on are shown. Updated monthly (last: {d['meta']['built']}).")

    elif cmd == "/tones" and arg.lower() in ("marks", "numbers"):
        state["tones"] = arg.lower()
        send(f"Tones now shown as {'xíng' if arg.lower() == 'marks' else 'xing2'}")

    else:
        send(HELP)


# ---------- Encrypted state ----------
# Standard library only: SHA-256 keystream (counter mode) + HMAC-SHA256 to detect a wrong key or tampering.
def state_keys():
    k = hashlib.sha256(STATE_KEY.encode()).digest()
    return hashlib.sha256(b"enc" + k).digest(), hashlib.sha256(b"mac" + k).digest()


def keystream_xor(key, nonce, data_):
    stream = b"".join(hashlib.sha256(key + nonce + i.to_bytes(8, "big")).digest() for i in range((len(data_) + 31) // 32))
    return bytes(a ^ b for a, b in zip(data_, stream))


def encrypt(data_):
    enc_key, mac_key = state_keys()
    nonce = secrets.token_bytes(16)
    body = nonce + keystream_xor(enc_key, nonce, data_)
    return base64.b64encode(body + hmac.new(mac_key, body, hashlib.sha256).digest()).decode() + "\n"


def decrypt(text):
    enc_key, mac_key = state_keys()
    raw = base64.b64decode(text)
    body, tag = raw[:-32], raw[-32:]
    if not hmac.compare_digest(tag, hmac.new(mac_key, body, hashlib.sha256).digest()):
        raise SystemExit("state.enc can't be opened: the STATE_KEY secret is wrong or was changed")
    return keystream_xor(enc_key, body[:16], body[16:])


def load_state():
    if SECRET_STATE.exists():
        return json.loads(decrypt(SECRET_STATE.read_text()))
    return {"offset": 0}


def save_state(state):
    data_ = (json.dumps(state, indent=1, ensure_ascii=False, sort_keys=True) + "\n").encode()
    # Encryption is random each time, so only rewrite when something changed (no commit every run).
    if not SECRET_STATE.exists() or decrypt(SECRET_STATE.read_text()) != data_:
        SECRET_STATE.write_text(encrypt(data_))
    PLAIN_STATE.unlink(missing_ok=True)


def main():
    if not TOKEN or not CHAT_ID:  # clearer than Telegram's bare 404 when the secrets aren't added yet
        raise SystemExit("TG_TOKEN or TG_CHAT_ID secret is missing: add them in Settings -> Secrets and variables -> Actions")
    if not STATE_KEY:  # your saved list must never be stored unencrypted in this public repo
        raise SystemExit("STATE_KEY secret is missing: add it in Settings -> Secrets and variables -> Actions")
    state = load_state()
    handle_commands(state)
    # guarantees a commit at least monthly, so GitHub doesn't disable the schedule after 60 idle days
    state["heartbeat"] = time.strftime("%Y-%m")
    save_state(state)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # the default traceback could print a lookup in the public log
        raise SystemExit(f"run failed: {why(e)}")
