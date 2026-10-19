"""Build data/hanzi.json.gz from free dictionaries: Mandarin, Cantonese and Japanese side by side.

Sources (all free to use with attribution, see DATA_LICENSES.md):
  CC-CEDICT          Mandarin words: simplified, traditional, pinyin, English
  Unihan (Unicode)   per-character readings and variants (the second source for every reading)
  rime-cantonese     Cantonese readings (Jyutping) for characters and words
  KANJIDIC2          Japanese character readings and meanings
  JMdict             Japanese words

Only cross-checked data goes in:
  - a character reading is kept only if two independent sources agree on it
  - a word reading is kept only if every syllable matches a checked reading of its character
  - a Japanese form is linked to a Chinese one only if their English meanings overlap
Anything that fails a check is left out instead of shown. The build stops (and keeps the old data)
if the result looks too small, so a broken download can never replace good data.

Run by GitHub Actions (build-data workflow). Standard library only.
"""
import gzip
import io
import json
import re
import sys
import time
import unicodedata
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from datetime import date
from pathlib import Path

OUT = Path("data/hanzi.json.gz")
SOURCES = {
    "cedict": ["https://www.mdbg.net/chinese/export/cedict/cedict_1_0_ts_utf-8_mdbg.txt.gz"],
    "unihan": ["https://www.unicode.org/Public/UCD/latest/ucd/Unihan.zip"],
    "kanjidic": ["https://www.edrdg.org/kanjidic/kanjidic2.xml.gz", "http://www.edrdg.org/kanjidic/kanjidic2.xml.gz"],
    "jmdict": ["https://www.edrdg.org/pub/Nihongo/JMdict_e.gz", "http://ftp.edrdg.org/pub/Nihongo/JMdict_e.gz"],
    "rime_chars": [f"https://raw.githubusercontent.com/rime/rime-cantonese/{b}/jyut6ping3.chars.dict.yaml" for b in ("main", "master")],
    "rime_words": [f"https://raw.githubusercontent.com/rime/rime-cantonese/{b}/jyut6ping3.words.dict.yaml" for b in ("main", "master")],
}
# The build refuses to write data smaller than this (something went wrong with a download).
MIN_COUNTS = {"chars": 15000, "words": 80000, "ja": 5000, "pool": 800}
EXAMPLES_PER_CHAR = 2
HAN = re.compile(r"^[㐀-䶿一-鿿豈-﫿\U00020000-\U0003134f]+$")
STOPWORDS = {"the", "and", "for", "with", "one", "sth", "sb", "someone", "something", "etc", "used", "also", "from",
             "into", "that", "this", "its", "not", "are", "was", "who", "which", "make", "have", "get",
             "to", "of", "in", "on", "at", "by", "an", "or", "as", "is", "be", "it", "so", "no", "do"}
SUFFIXES = ("ings", "ing", "ed", "es", "s", "ure", "ment", "ion")
SKIP_GLOSS = re.compile(r"(?i)^(surname |variant of|old variant|archaic variant|japanese variant|used in |cl:|see |abbr\. for |\(old\))")


# ---------- Download ----------
def fetch(name):
    for url in SOURCES[name]:
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "hanzi-bot data build"})
                with urllib.request.urlopen(req, timeout=120) as r:
                    data = r.read()
                print(f"downloaded {name}: {len(data) // 1024} KB")
                return data
            except Exception as e:
                print(f"  {name} attempt {attempt + 1} failed: {type(e).__name__}")
                time.sleep(5)
    raise SystemExit(f"could not download {name}")


# ---------- Romanization helpers ----------
TONE_MARKS = {"̄": "1", "́": "2", "̌": "3", "̀": "4"}


def pinyin_key(s):
    """'xíng' / 'xing2' / 'nu:3' / 'nǚ' -> 'xing2' / 'nv3' (lowercase, tone number, ü as v; no tone -> 5)."""
    s = s.strip().lower().replace("u:", "v")
    tone = ""
    out = []
    for ch in unicodedata.normalize("NFD", s):
        if ch in TONE_MARKS:
            tone = TONE_MARKS[ch]
        elif ch == "̈":  # diaeresis: u + ¨ -> v
            if out and out[-1] == "u":
                out[-1] = "v"
        elif ch.isalpha():
            out.append(ch)
        elif ch.isdigit():
            tone = ch
    return "".join(out) + (tone or "5") if out else ""


KANA = {}
for row in ("ア イ ウ エ オ a i u e o", "カ キ ク ケ コ ka ki ku ke ko", "ガ ギ グ ゲ ゴ ga gi gu ge go",
            "サ シ ス セ ソ sa shi su se so", "ザ ジ ズ ゼ ゾ za ji zu ze zo", "タ チ ツ テ ト ta chi tsu te to",
            "ダ ヂ ヅ デ ド da ji zu de do", "ナ ニ ヌ ネ ノ na ni nu ne no", "ハ ヒ フ ヘ ホ ha hi fu he ho",
            "バ ビ ブ ベ ボ ba bi bu be bo", "パ ピ プ ペ ポ pa pi pu pe po", "マ ミ ム メ モ ma mi mu me mo",
            "ラ リ ル レ ロ ra ri ru re ro", "ヤ ユ ヨ ワ ヲ ya yu yo wa o"):
    parts = row.split()
    KANA.update(zip(parts[:5], parts[5:]))
KANA["ン"] = "n"
SMALL = {"ャ": "a", "ュ": "u", "ョ": "o"}


def kana_romaji(s):
    """Kana (hiragana or katakana) -> uppercase Hepburn as Unihan writes it: 'こう' -> 'KOU', 'ギョウ' -> 'GYOU'."""
    kata = "".join(chr(ord(c) + 0x60) if "\u3041" <= c <= "\u3096" else c for c in s)
    syllables, double = [], False
    for c in kata:
        if c == "ッ":
            double = True
        elif c in SMALL and syllables and syllables[-1].endswith("i"):
            last = syllables[-1]  # キ+ャ -> kya, シ+ャ -> sha, ジ+ャ -> ja
            syllables[-1] = (last[:-1] if last in ("shi", "chi", "ji") else last[:-1] + "y") + SMALL[c]
        elif c == "ー" and syllables:
            syllables.append(syllables[-1][-1])
        elif c in KANA:
            r = KANA[c]
            syllables.append(r[0] + r if double and r[0] not in "aiueon" else r)
            double = False
    return "".join(syllables).upper()


# ---------- Text helpers ----------
def tokens(glosses):
    """Content words of English glosses, stemmed and cut to 5 letters: 'going'/'go', 'departure'/'depart' match."""
    out = set()
    for g in glosses:
        g = re.sub(r"\([^)]*\)", " ", g.lower())
        for w in re.findall(r"[a-z]{2,}", g):
            if w in STOPWORDS:
                continue
            for suf in SUFFIXES:
                if w.endswith(suf) and len(w) - len(suf) >= 2:
                    w = w[: -len(suf)]
                    break
            out.add(w[:5])
    return out


def overlaps(a, b):
    return bool(tokens(a) & tokens(b))


def clean_glosses(gl, n=None):
    """Glosses without cross-references; n limits how many (for display), None keeps all (for meaning checks)."""
    return [g for g in gl if g and not SKIP_GLOSS.match(g)][:n]


# ---------- Parse sources ----------
def parse_cedict(raw):
    """-> list of (trad, simp, [pinyin keys], [glosses])"""
    out = []
    for line in gzip.decompress(raw).decode("utf-8").splitlines():
        m = re.match(r"^(\S+) (\S+) \[([^\]]*)\] /(.*)/\s*$", line)
        if m:
            trad, simp, py, gl = m.groups()
            out.append((trad, simp, [pinyin_key(p) for p in py.split()], gl.split("/")))
    return out


def parse_unihan(raw):
    """-> (readings {char: {"zh": set, "yue": set, "ja": set(romaji)}}, variants {char: set})"""
    z = zipfile.ZipFile(io.BytesIO(raw))
    readings, variants = {}, {}
    for name in z.namelist():
        if not name.endswith(".txt"):
            continue
        for line in z.read(name).decode("utf-8").splitlines():
            if not line.startswith("U+"):
                continue
            cp, field, value = line.split("\t", 2)
            ch = chr(int(cp[2:], 16))
            r = readings.setdefault(ch, {"zh": set(), "yue": set(), "ja": set()})
            if field in ("kMandarin", "kHanyuPinyin", "kXHC1983", "kTGHZ2013", "kHanyuPinlu"):
                for p in re.split(r"[\s,]+", re.sub(r"[\d.]+:", " ", re.sub(r"\(\d+\)", "", value))):
                    k = pinyin_key(p)
                    if k:
                        r["zh"].add(k)
            elif field == "kCantonese":
                r["yue"].update(value.split())
            elif field in ("kJapaneseOn", "kJapaneseKun"):
                r["ja"].update(value.upper().split())
            elif field == "kJapanese":  # newer Unihan: readings in kana
                r["ja"].update(kana_romaji(x) for x in value.split())
            elif field in ("kSimplifiedVariant", "kTraditionalVariant", "kSemanticVariant", "kZVariant",
                           "kSpecializedSemanticVariant"):
                for v in re.findall(r"U\+([0-9A-F]+)", value):
                    variants.setdefault(ch, set()).add(chr(int(v, 16)))
    return readings, variants


def parse_kanjidic(raw):
    """-> {char: {"on": [...], "kun": [...], "m": [...], "joyo": bool}}"""
    out = {}
    root = ET.fromstring(gzip.decompress(raw))
    for c in root.iter("character"):
        lit = c.findtext("literal")
        grade = c.findtext("misc/grade")
        on, kun, mean = [], [], []
        for rd in c.iter("reading"):
            if rd.get("r_type") == "ja_on":
                on.append(rd.text)
            elif rd.get("r_type") == "ja_kun":
                kun.append(rd.text)
        for m in c.iter("meaning"):
            if m.get("m_lang") in (None, "en"):
                mean.append(m.text)
        out[lit] = {"on": on, "kun": kun, "m": mean, "joyo": grade is not None and int(grade) <= 8}
    return out


def parse_jmdict(raw):
    """-> {kanji form: (kana reading, [glosses], common)} keeping the first (most common) entry per form"""
    out = {}
    for _, e in ET.iterparse(io.BytesIO(gzip.decompress(raw)), events=("end",)):
        if e.tag != "entry":
            continue
        rebs = [r.findtext("reb") for r in e.findall("r_ele")]
        glosses = [g.text for s in e.findall("sense") for g in s.findall("gloss") if g.get("{http://www.w3.org/XML/1998/namespace}lang") in (None, "eng")]
        for k in e.findall("k_ele"):
            keb = k.findtext("keb")
            common = k.find("ke_pri") is not None
            if keb and rebs and glosses and (keb not in out or (common and not out[keb][2])):
                out[keb] = (rebs[0], glosses[:4], common)
        e.clear()
    return out


def parse_rime(raw):
    """-> {text: [jyutping, ...]} from a rime dict.yaml (entries come after the '...' line)."""
    out, body = {}, False
    for line in raw.decode("utf-8").splitlines():
        if not body:
            body = line.strip() == "..."
            continue
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 2 and HAN.match(parts[0]) and re.fullmatch(r"([a-z]+[1-6] ?)+", parts[1].strip()):
            out.setdefault(parts[0], [])
            if parts[1].strip() not in out[parts[0]]:
                out[parts[0]].append(parts[1].strip())
    return out


# ---------- Build ----------
def main():
    cedict = parse_cedict(fetch("cedict"))
    uni, variants = parse_unihan(fetch("unihan"))
    kanjidic = parse_kanjidic(fetch("kanjidic"))
    jmdict = parse_jmdict(fetch("jmdict"))
    rime_chars = parse_rime(fetch("rime_chars"))
    rime_words = parse_rime(fetch("rime_words"))
    print(f"parsed: cedict {len(cedict)} · unihan {len(uni)} · kanjidic {len(kanjidic)} · jmdict {len(jmdict)} · "
          f"rime chars {len(rime_chars)} · rime words {len(rime_words)}")
    empty = {"zh": set(), "yue": set(), "ja": set()}

    # Mandarin per character: CC-CEDICT readings that Unihan confirms
    zh = {}  # trad -> {pinyin: [glosses]}
    simp_of = {}  # trad -> simplified
    for trad, simp, py, gl in cedict:
        if len(trad) == 1 and len(py) == 1 and HAN.match(trad):
            if py[0] in uni.get(trad, empty)["zh"] or py[0] in uni.get(simp, empty)["zh"]:
                g = clean_glosses(gl)
                if g:
                    zh.setdefault(trad, {}).setdefault(py[0], [])
                    zh[trad][py[0]] += [x for x in g if x not in zh[trad][py[0]]]
                    simp_of.setdefault(trad, simp)

    # Cantonese per character: rime-cantonese readings that Unihan confirms
    yue = {}
    for ch, readings in rime_chars.items():
        if len(ch) == 1:
            ok = [r for r in readings if r in uni.get(ch, empty)["yue"]]
            if ok:
                yue[ch] = ok

    # Japanese per character: KANJIDIC readings that Unihan confirms
    ja = {}
    for ch, k in kanjidic.items():
        uj = uni.get(ch, empty)["ja"]
        on = [r for r in k["on"] if kana_romaji(r) in uj]
        kun = [r for r in k["kun"] if kana_romaji(r.replace(".", "").replace("-", "")) in uj]
        if (on or kun) and k["m"]:
            ja[ch] = {"on": on[:3], "kun": kun[:3], "m": k["m"][:3], "joyo": k["joyo"]}

    # Which Japanese character goes with each traditional one: itself or a variant, preferring a joyo kanji
    # whose English meaning overlaps the Mandarin one. Same character with no overlap -> flagged as different meaning.
    chars = {}
    for trad in set(zh) | set(yue):
        zgl = [g for gl in zh.get(trad, {}).values() for g in gl]
        cands = [trad] + ([simp_of[trad]] if trad in simp_of else []) + sorted(variants.get(trad, ()))
        ja_form, diff = "", False
        matched = [c for c in cands if c in ja and zgl and overlaps(zgl, kanjidic[c]["m"])]  # all meanings, not just the 3 shown
        matched.sort(key=lambda c: (not ja[c]["joyo"], c != trad))
        if matched:
            ja_form = matched[0]
        elif trad in ja:
            ja_form, diff = trad, bool(zgl)  # same character exists in Japanese, but the meaning differs
        chars[trad] = {"s": simp_of.get(trad, trad), "zh": [[p, g[:3]] for p, g in zh.get(trad, {}).items()],
                       "yue": yue.get(trad, []), "ja": ja_form, "jdiff": diff}
    simp_index = {}
    for trad, c in chars.items():
        if c["s"] != trad:
            simp_index.setdefault(c["s"], []).append(trad)
    ja_to_trad = {}
    for trad, c in chars.items():
        if c["ja"] and not c["jdiff"] and c["ja"] != trad:
            ja_to_trad.setdefault(c["ja"], trad)

    # Words: pinyin and Jyutping kept only if every syllable is a checked reading of its character
    def zh_ok(trad, py):
        return len(trad) == len(py) and all(
            p[-1] == "5" and p[:-1] in {x[:-1] for x in uni.get(c, empty)["zh"]} or p in uni.get(c, empty)["zh"]
            for c, p in zip(trad, py))

    def yue_ok(trad, jy):
        syl = jy.split()
        return len(trad) == len(syl) and all(s in uni.get(c, empty)["yue"] for c, s in zip(trad, syl))

    words, simp_words = {}, {}
    for trad, simp, py, gl in cedict:
        if len(trad) < 2 or not HAN.match(trad) or trad in words:
            continue
        g = clean_glosses(gl)
        if not g:
            continue
        jy = next((j for j in rime_words.get(trad, []) if yue_ok(trad, j)), "")
        jform = "".join(chars[c]["ja"] if c in chars and chars[c]["ja"] and not chars[c]["jdiff"] else c for c in trad)
        jm = next(((f, *jmdict[f][:2]) for f in (jform, simp, trad) if f in jmdict and overlaps(g, jmdict[f][1])), None)
        words[trad] = [simp, " ".join(py) if zh_ok(trad, py) else "", g[:3], jy,
                       *(jm if jm else ("", "", [])), bool(jm and jmdict[jm[0]][2])]
        if simp != trad:
            simp_words.setdefault(simp, []).append(trad)

    # Random pool: characters with all three languages checked and a meaning-verified Japanese joyo form
    pool = sorted(t for t, c in chars.items()
                  if c["zh"] and c["yue"] and c["ja"] and not c["jdiff"] and ja[c["ja"]]["joyo"])
    # Examples: common two-character words where all three languages passed every check
    ex = {}
    for trad, w in words.items():
        if len(trad) == 2 and w[1] and w[3] and w[4] and w[7]:
            for c in set(trad):
                if c in chars and len(ex.setdefault(c, [])) < EXAMPLES_PER_CHAR:
                    ex[c].append(trad)

    data = {"chars": chars, "simp": simp_index, "ja": ja, "ja_to_trad": ja_to_trad, "words": words,
            "simp_words": simp_words, "pool": pool, "ex": ex,
            "meta": {"built": date.today().isoformat()}}
    counts = {"chars": len(chars), "words": len(words), "ja": len(ja), "pool": len(pool)}
    print(f"result: {counts} · pool with examples {sum(1 for t in pool if ex.get(t))} · "
          f"words with pinyin {sum(1 for w in words.values() if w[1])} · with jyutping {sum(1 for w in words.values() if w[3])} · "
          f"with japanese {sum(1 for w in words.values() if w[4])}")
    low = {k: v for k, v in counts.items() if v < MIN_COUNTS[k]}
    if low:
        raise SystemExit(f"result too small, keeping the old data: {low}")
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_bytes(gzip.compress(json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(), mtime=0))
    print(f"wrote {OUT} ({OUT.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
