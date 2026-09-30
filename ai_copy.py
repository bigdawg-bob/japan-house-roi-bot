"""
ai_copy.py – DeepSeek writes the WORDS, our data supplies the NUMBERS.

The model gets every fact we already have + the rules for each line (words, characters,
which info goes where). It chooses the angle and phrasing. Every field is checked:
a field that breaks a rule is dropped and the template text is used for it.
No key / API down / bad JSON -> {} -> the post goes out exactly as before.

Env: DEEPSEEK_API_KEY (required), DEEPSEEK_MODEL (default deepseek-flash),
     AI_COPY=0 turns it off, AI_COPY_TEMP (default 1.3).
Evidence loop: state/performance.json, filled from Instagram Insights:
     {"2026-10-01": {"reach": 5400, "shares": 41, "saves": 88, "follows": 23}}
"""
import json, os, re
from datetime import datetime
from pathlib import Path

import requests

def _env(n, d):
    return os.getenv(n) or d

KEY   = os.getenv("DEEPSEEK_API_KEY")
URL   = _env("DEEPSEEK_URL", "https://api.deepseek.com/chat/completions")
MODEL = _env("DEEPSEEK_MODEL", "deepseek-flash")
TEMP  = float(_env("AI_COPY_TEMP", "1.3"))
ON    = _env("AI_COPY", "1") == "1" and bool(KEY)

STATE      = Path(__file__).parent / "state"
LOG_FILE   = STATE / "copy_log.json"
PERF_FILE  = STATE / "performance.json"
CACHE_FILE = STATE / "copy_cache.json"

# slot -> (max characters, max words, what goes there)
SLOTS = {
    "reel_hook":    (18, 3, "Reel line 1, huge bold caps, read in under 1 second. Must contain "
                            "the price OR the town. Style: '$25K AMINO'."),
    "reel_sub":     (44, 8, "Reel line 2: SIZE • DISTANCE • MONEY joined by ' • '. Keep the "
                            "distance. Style: '4BR • 7 min drive to beach • 18% net'."),
    "s3_headline":  (34, 7, "Slide 3 headline = the angle. The 3 facts under it must pay it off."),
    "s3_seo1":      (46, 10, "Slide 3 search line 1: '[Town] Airbnb' or '[Town] akiya' + access time."),
    "s3_seo2":      (46, 10, "Slide 3 search line 2: '[Town] investment' + the minpaku nights rule."),
    "caption_hook": (125, 22, "Caption line 1 (only ~125 chars show before 'more'). Town + "
                              "akiya/house keyword + one hard number, early."),
    "caption_seo":  (220, 40, "One natural sentence with the words people search: town, "
                              "prefecture, akiya, Airbnb/rental, the attraction. No stuffing."),
    "reel_line1":   (90, 16, "Reel caption line 1: '[Town] akiya' + price -> money result."),
    "reel_dream":   (90, 16, "Reel caption line 2: one sensory line about being there."),
}
POINT_CHARS = 34
MAX_TAGS    = 5                      # Instagram hard cap since Dec 2025
ANGLES = {"A": "THE COMP – price vs ryokan / competition",
          "B": "THE MAP – access, what is nearby",
          "C": "THE MATH LEAK – what the rental math hides",
          "D": "THE TOWN SECRET – why this town, not the famous rival",
          "E": "THE EXIT – plan B if Airbnb fails"}
BANNED = re.compile(r"guarantee|risk[- ]?free|passive income|get rich|no[- ]brainer|"
                    r"can'?t lose|free money|100% safe|once in a lifetime", re.I)

SYSTEM = """You write Instagram copy for @yama.yield: cheap Japanese houses (akiya) near ski
resorts, onsen towns, sights, beaches and nature, with short-term rental numbers.
Audience: people outside Japan searching "akiya", "cheap house Japan", "[town] Airbnb",
"Japan property investment".
How Instagram works (use it):
- Search reads captions and on-screen text: put the town + a search word (akiya, Airbnb,
  house) early, in natural language. No keyword stuffing.
- The reel hook and caption line 1 decide if people stop: be specific (a real number + a
  real place) and open a question the slides answer. Never promise what the slides don't show.
- Shares and saves carry posts furthest: write what someone would send to a friend
  planning a Japan house or ski/onsen trip.
- Max 5 hashtags, specific beats generic.
Hard rules:
- Every number you write must be copied EXACTLY from DATA (same digits). Never invent,
  round, convert, add up or estimate numbers. If unsure, write the line without a number.
- No hype, no guarantees, no investment-advice wording.
- Plain English. No emoji in slide or reel text.
- Stay inside every slot's character and word limit.
Answer ONLY with json."""

EXAMPLE = {"angle": "A", "reel_hook": "...", "reel_sub": "...", "s3_headline": "...",
           "s3_points": [{"key": "ryokan", "text": "..."}, {"key": "...", "text": "..."},
                         {"key": "...", "text": "..."}],
           "s3_seo1": "...", "s3_seo2": "...", "caption_hook": "...", "caption_seo": "...",
           "reel_line1": "...", "reel_dream": "...", "hashtags": ["#akiya", "..."]}


def load(p):
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except ValueError:
        return {}

def save(p, d):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")

def nums(s):
    """All numbers in a text, commas removed: '$1,234 • 18%' -> ['1234', '18']"""
    return re.findall(r"\d+(?:\.\d+)?", str(s).replace(",", ""))

def clean(s):
    return re.sub(r"\s+", " ", str(s or "")).strip().strip('"').strip()


# ─── the facts the model may use ─────────────────────────────────────
def data_for(M, h, l, hooks, usd, e, facts, s3):
    f = facts or {}
    mins, mode = M.trip_parts(h, l)
    d = {
        "town": M.short_name(h["name"]),
        "attraction": M.hook_label(h),
        "attraction_kind": M.KINDS[h["kind"]]["label"],
        "prefecture": M.PREF_EN.get(l["pref"], l["pref"]),
        "price_usd": "FREE" if l["price_yen"] == 0 else M.fmt_usd(usd),
        "price_yen": M.fmt_yen(l["price_yen"]),
        "trip": f"{mins} min {mode}",
        "bedrooms": l.get("bedrooms"),
        "year_built": l.get("year_built"),
        "nightly_rate": f"${e['adr']:,}",
        "occupancy": f"{e['occ'] * 100:.0f}%",
        "rented_nights_per_year": e["nights"],
        "minpaku_rule": "national cap: 180 nights per year",
        "net_per_month": f"${e['monthly']:,}",
        "all_in_cost": M.fmt_k1(e["all_in"]),
        "net_yield": f"{e['roi'] * 100:.0f}%" if M.show_yield(e) else None,
        "payback": (M.fmt_years(e["breakeven_yrs"])
                    if M.show_yield(e) and e.get("breakeven_yrs") else None),
        "also_near": [f"{M.hook_label(x)}: {M.fmt_trip(x, l).lstrip('~')}" for x in hooks[1:3]],
        "known_for": f.get("known_for") or None,
        "visitors": f.get("visitors") or None,
        "famous_rival_town": M.rival_town(h),
        "slide3_facts": s3.get("pool") or {},            # key -> fact text (pick 3)
        "template_version": {"headline": s3["headline"], "seo": s3["lines"]},
    }
    return {k: v for k, v in d.items() if v not in (None, "", [], {})}


# ─── evidence: your own past posts ───────────────────────────────────
def evidence():
    log, perf = load(LOG_FILE), load(PERF_FILE)
    rows = []
    for day, c in log.items():
        p = perf.get(day) or {}
        reach = p.get("reach") or 0
        if reach < 200:
            continue                                     # too small to mean anything
        score = (10 * p.get("follows", 0) + 3 * p.get("shares", 0)
                 + 2 * p.get("saves", 0)) / reach * 1000
        rows.append({"score": round(score, 1), "angle": c.get("angle"),
                     "reel_hook": c.get("reel_hook"), "headline": c.get("s3_headline"),
                     "caption_hook": c.get("caption_hook")})
    rows.sort(key=lambda r: r["score"], reverse=True)
    return {"best": rows[:5], "worst": rows[-3:] if len(rows) >= 8 else []}

def log_post(day, l, copy):
    """Call after a post went out, so it can be matched with Insights later."""
    log = load(LOG_FILE)
    log[day] = {**copy, "url": l["url"]}
    save(LOG_FILE, dict(sorted(log.items())[-200:]))


# ─── DeepSeek call ───────────────────────────────────────────────────
def call(user):
    body = {"model": MODEL, "temperature": TEMP, "max_tokens": 1500,
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": user}]}
    for _ in range(3):
        try:
            r = requests.post(URL, json=body, timeout=90,
                              headers={"Authorization": f"Bearer {KEY}"})
        except requests.RequestException as ex:
            print(f"  DeepSeek error: {ex}")
            continue
        if r.status_code == 400 and "thinking" in body:
            body.pop("thinking")                         # model without the switch
            continue
        if not r.ok:
            print(f"  DeepSeek {r.status_code}: {r.text[:300]}")
            continue
        try:
            txt = r.json()["choices"][0]["message"]["content"] or ""
            v = json.loads(txt)
            if isinstance(v, dict):
                return v
        except (ValueError, KeyError, IndexError, TypeError):
            pass
        print("  DeepSeek: empty / not JSON – retrying")
    return None


# ─── checks ──────────────────────────────────────────────────────────
def check(name, s, allowed):
    s = clean(s)
    chars, words, _ = SLOTS[name]
    if not s:
        return None, "empty"
    if len(s) > chars:
        return None, f"{len(s)} chars > {chars}"
    if len([w for w in s.split() if w not in ("•", "/", "=", "→", "-", "+")]) > words:
        return None, f"more than {words} words"
    if BANNED.search(s):
        return None, "hype word"
    bad = [n for n in nums(s) if n not in allowed]
    if bad:
        return None, f"numbers not in data {bad}"
    return s, None

def validate(raw, data, s3, rot):
    allowed = set(nums(json.dumps(data, ensure_ascii=False)))
    town = data["town"].lower()
    out, why = {}, {}
    for name in SLOTS:
        s, err = check(name, raw.get(name), allowed)
        if s and name == "reel_hook":
            s = s.upper()
            if town.upper() not in s and data["price_usd"].upper() not in s:
                s, err = None, "no price or town"
        if s and name in ("s3_seo1", "s3_seo2", "caption_hook") and town not in s.lower():
            s, err = None, "town keyword missing"
        if s and name == "reel_sub" and nums(data["trip"])[0] not in nums(s):
            s, err = None, "distance dropped"
        if s:
            out[name] = s
        else:
            why[name] = err

    # slide 3 facts: 3 keys from the pool, each keeps ITS OWN numbers only
    pool = data.get("slide3_facts") or {}
    keys, texts = [], []
    for p in raw.get("s3_points") or []:
        if not isinstance(p, dict):
            continue
        k, t = p.get("key"), clean(p.get("text"))
        if (k in pool and k not in keys and t and len(t) <= POINT_CHARS
                and not BANNED.search(t) and set(nums(t)) <= set(nums(pool[k]))):
            keys.append(k)
            texts.append(t)
    recent = [tuple(sorted(c)) for c in (rot.get("s3_points") or [])[:10] if isinstance(c, list)]
    if len(keys) == min(3, len(pool)) and tuple(sorted(keys)) not in recent:
        out["s3_kinds"], out["s3_points"] = keys[:3], texts[:3]
    else:
        why["s3_points"] = "wrong keys / numbers changed / combo used recently"

    last = (rot.get("s3_templates") or [None])[0]
    a = str(raw.get("angle") or "").upper()
    if a in ANGLES and a != last and (a != "D" or data.get("famous_rival_town")):
        out["angle"] = a

    tags = []
    for t in raw.get("hashtags") or []:
        t = str(t).strip().lower()
        if re.fullmatch(r"#\w{2,40}", t) and t not in tags:
            tags.append(t)
    if len(tags) >= 3:
        out["hashtags"] = tags[:MAX_TAGS]

    if why:
        print(f"  AI copy rejected (template used): {why}")
    return out


# ─── entry point ─────────────────────────────────────────────────────
def write(M, h, l, hooks, usd, e, facts, s3, rot):
    """Returns {slot: text} with only the fields that passed every check."""
    if not ON:
        print("AI copy: off (no DEEPSEEK_API_KEY or AI_COPY=0) – template text")
        return {}
    day = datetime.now(M.JST).strftime("%Y-%m-%d")
    cache = load(CACHE_FILE)
    ck = f"{day}|{l['url']}"
    data = data_for(M, h, l, hooks, usd, e, facts, s3)
    if ck in cache:
        raw = cache[ck]                                  # re-run today: no new call
    else:
        last = (rot.get("s3_templates") or [None])[0]
        user = ("SLOTS (max characters, max words, what goes there):\n"
                + json.dumps({k: {"max_chars": c, "max_words": w, "job": j}
                              for k, (c, w, j) in SLOTS.items()}, indent=1)
                + f"\n\ns3_points: pick exactly 3 keys from DATA.slide3_facts, rewrite each "
                  f"in max {POINT_CHARS} chars, keep that fact's numbers exactly."
                + "\nangle: one of " + json.dumps(ANGLES)
                + f" – not '{last}' (used last post). D only if famous_rival_town exists."
                + f"\nhashtags: {MAX_TAGS} max, include #akiya, one town/area tag, "
                  "one attraction-kind tag."
                + "\n\nDATA:\n" + json.dumps(data, ensure_ascii=False, indent=1)
                + "\n\nOUR PAST POSTS (score = follows/shares/saves per reach; "
                  "learn the pattern, don't copy):\n" + json.dumps(evidence(), ensure_ascii=False)
                + "\n\nAnswer in this json shape:\n" + json.dumps(EXAMPLE))
        raw = call(user)
        if raw is None:
            print("AI copy: no answer – template text")
            return {}
        cache = {k: v for k, v in cache.items() if k.startswith(day)}
        cache[ck] = raw
        save(CACHE_FILE, cache)
    out = validate(raw, data, s3, rot)
    print(f"AI copy ({MODEL}): kept {sorted(out)}")
    return out

def apply_s3(s3, c):
    """Puts the accepted AI wording into the slide 3 dict (template text stays otherwise)."""
    s3 = dict(s3)
    if c.get("angle"):
        s3["template"] = c["angle"]
    if c.get("s3_headline"):
        s3["headline"] = c["s3_headline"]
    if c.get("s3_points"):
        s3["points"], s3["kinds"] = c["s3_points"], c["s3_kinds"]
    if c.get("s3_seo1") and c.get("s3_seo2"):
        s3["lines"] = [c["s3_seo1"], c["s3_seo2"]]
    s3["copy"] = c
    return s3
