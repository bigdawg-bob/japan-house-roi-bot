"""
ai_copy.py – DeepSeek writes the WORDS, our data supplies the NUMBERS.

The model gets every fact we already have + the rules for each line (characters, words,
what must be in it). Inside those rules it is free to choose the angle and the wording.
Every field is checked: a field that breaks a rule is dropped and the template text is
used for it. No key / API down / bad JSON -> {} -> the post goes out exactly as before.

What DeepSeek writes (free wording, always checked):
  reel      line 1 (hook) + line 2          same font, size, spacing and timing as before
  slide 2   the 2 small math lines          numbers ONLY as {placeholders}; main.py puts in
                                            its own numbers, so they can't be wrong
  slide 3   headline, 3 bullets, 2 SEO lines  same layout; each bullet names the DATA fact
                                            it comes from and keeps that fact's numbers
  captions  carousel line 1 + SEO sentence, reel line 1 + dream line, hashtags
Never written by DeepSeek: the big hero numbers on slides 1 + 2, the price and link lines.

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

KEY     = os.getenv("DEEPSEEK_API_KEY")
URL     = _env("DEEPSEEK_URL", "https://api.deepseek.com/chat/completions")
MODEL   = _env("DEEPSEEK_MODEL", "deepseek-flash")
TEMP    = float(_env("AI_COPY_TEMP", "1.3"))
ON      = _env("AI_COPY", "1") == "1" and bool(KEY)
VERSION = "v2"                       # part of the cache key: new rules = new answer

STATE      = Path(__file__).parent / "state"
LOG_FILE   = STATE / "copy_log.json"
PERF_FILE  = STATE / "performance.json"
CACHE_FILE = STATE / "copy_cache.json"

# slot -> (max characters, max words, what goes there)
SLOTS = {
    "reel_hook":    (18, 3, "Reel line 1, huge bold caps, read in under 1 second. Any wording, "
                            "but it must name the town OR show a number from DATA (price, drive "
                            "time, yield...). Example: '$25K AMINO'."),
    "reel_sub":     (44, 8, "Reel line 2 under the hook. Any wording and order, but it must keep "
                            "the drive/walk minutes from DATA.trip. ' • ' may separate parts. "
                            "Example: '4BR • 7 min drive to beach • 18% net'."),
    "s3_headline":  (34, 7, "Slide 3 headline = the angle. The 3 bullets under it must pay it off."),
    "s3_seo1":      (46, 10, "Slide 3 search line 1. Free wording: the town + a search word people "
                             "type (akiya, Airbnb, rental, investment, minpaku, onsen/ski/beach house)."),
    "s3_seo2":      (46, 10, "Slide 3 search line 2. Same rules as s3_seo1, but a different search "
                             "and a different fact."),
    "caption_hook": (125, 22, "Caption line 1 (only ~125 chars show before 'more'). Town + "
                              "akiya/house keyword + one hard number, early."),
    "caption_seo":  (220, 40, "One natural sentence with the words people search: town, "
                              "prefecture, akiya, Airbnb/rental, the attraction. No stuffing."),
    "reel_line1":   (90, 16, "Reel caption line 1: '[Town] akiya' + price -> money result."),
    "reel_dream":   (90, 16, "Reel caption line 2: one sensory line about being there."),
}
POINT_CHARS = 34                     # slide 3 bullet (same limit as the template bullets)
POINT_WORDS = 7
MAX_TAGS    = 5                      # Instagram hard cap since Dec 2025

# slide 2: the 2 small math lines, written with {placeholders} only
MATH = {
    "s2_line_a": {"max_chars": 110, "need": ["all_in", "breakdown"], "need_if_real": ["yield"],
                  "place": False,
                  "job": "Slide 2 math line 1: the cost side – what it costs all-in and its "
                         "parts, plus the yield if {yield} is in DATA.slide2."},
    "s2_line_b": {"max_chars": 140, "need": ["adr", "occ", "nights", "mgmt", "net"],
                  "place": True,
                  "job": "Slide 2 math line 2: the income side – nightly rate × occupancy × "
                         "nights, after management = net per year, with {place} for search."},
}
MATH_MEANS = {
    "yield":     "net yield per year (already says 'net yield')",
    "all_in":    "total cost: house + renovation + buying fees",
    "breakdown": "the parts of the all-in cost, already in brackets",
    "place":     "the attraction name (search keyword)",
    "adr":       "nightly rate (already says '/nt')",
    "occ":       "occupancy = share of nights booked",
    "nights":    "nights rented per year (already says 'days')",
    "mgmt":      "what is taken off before net (already says 'mgmt', and '& fees' if any)",
    "net":       "net income per year (already says '/yr')",
}
PH = re.compile(r"\{(\w+)\}")

ANGLES = {"A": "THE COMP – price vs ryokan / competition",
          "B": "THE MAP – access, what is nearby",
          "C": "THE MATH LEAK – what the rental math hides",
          "D": "THE TOWN SECRET – why this town, not the famous rival",
          "E": "THE EXIT – plan B if Airbnb fails"}
BANNED = re.compile(r"guarantee|risk[- ]?free|passive income|get rich|no[- ]brainer|"
                    r"can'?t lose|free money|100% safe|once in a lifetime", re.I)
SEARCH = re.compile(r"\b(akiya|airbnb|short[- ]term rental|rentals?|minpaku|"
                    r"invest(?:ment|ing|or)?s?|propert(?:y|ies)|houses?|homes?|cabins?|ryokan|"
                    r"onsen|ski|beach|yield|vacation|real estate)\b", re.I)
NAME_STOP = {"lake", "mt", "old", "the", "town", "onsen", "ski", "beach", "bay", "coast"}

SYSTEM = """You write Instagram copy for @yama.yield: cheap Japanese houses (akiya) near ski
resorts, onsen towns, sights, beaches and nature, with short-term rental numbers.
Audience: people outside Japan searching "akiya", "cheap house Japan", "[town] Airbnb",
"Japan property investment".
How Instagram works (use it):
- Search reads captions and on-screen text: put the town + a search word (akiya, Airbnb,
  house, rental, investment) early, in natural language. No keyword stuffing.
- The reel hook and caption line 1 decide if people stop: be specific (a real number + a
  real place) and open a question the slides answer. Never promise what the slides don't show.
- Shares and saves carry posts furthest: write what someone would send to a friend
  planning a Japan house or ski/onsen trip.
- Max 5 hashtags, specific beats generic.
You are free to choose the angle and the wording. The limits are:
- Every number you write must be copied EXACTLY from DATA (same digits). Never invent,
  round, convert, add up or estimate numbers. If unsure, write the line without a number.
- Slide 2 lines (s2_line_a, s2_line_b): never type a digit. Write every number as a
  {placeholder} from DATA.slide2; the code puts in the real value.
- Slide 3 bullets say only what their DATA.facts source says.
- No hype, no guarantees, no investment-advice wording.
- Plain English. No emoji in slide or reel text.
- Stay inside every slot's character and word limit.
Answer ONLY with json."""

EXAMPLE = {"angle": "A", "reel_hook": "...", "reel_sub": "...",
           "s2_line_a": "... {all_in} ... {breakdown} ...",
           "s2_line_b": "{place} ... {adr} ... {occ} ... {nights} ... {mgmt} ... {net}",
           "s3_headline": "...",
           "s3_points": [{"from": ["ryokan"], "text": "..."},
                         {"from": ["..."], "text": "..."},
                         {"from": ["...", "..."], "text": "..."}],
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

def word_count(s):
    return len([w for w in s.split() if w not in ("•", "/", "=", "→", "-", "+", "×")])

def fill(template, values):
    """'{adr} × {occ}' + values -> '$120/nt × 80%' (unknown placeholders stay as they are)."""
    return PH.sub(lambda m: str(values.get(m.group(1), m.group(0))), template)

def town_names(data):
    """Names that count as 'the town' for the SEO checks: full name + its main word."""
    names = {data["town"].lower()}
    words = [w for w in re.split(r"[\s\-]+", data["town"].lower()) if w not in NAME_STOP]
    if words and len(words[0]) >= 4:
        names.add(words[0])
    return names

def has_name(s, names):
    s = s.lower()
    return any(n in s for n in names)


# ─── the facts the model may use ─────────────────────────────────────
def fact_sources(M, h, l, e, f, s3, price_usd, price_yen):
    """key -> fact text. Every slide 3 bullet must name 1-2 of these keys."""
    src = {k: str(v) for k, v in (s3.get("pool") or {}).items() if v}
    mins, mode = M.trip_parts(h, l)
    on = M.show_yield(e)
    rival = M.rival_town(h)
    own = {
        "price":     f"House price {price_usd} ({price_yen})" if price_usd != "FREE" else "Free house",
        "trip":      f"{mins} min {mode} to {M.hook_label(h)}",
        "rate":      f"Nightly rate ${e['adr']:,}",
        "nights":    f"{e['nights']} rented nights per year",
        "minpaku":   "Minpaku national cap: 180 nights per year",
        "net_month": f"Net ${e['monthly']:,} per month",
        "all_in":    f"All-in cost {M.fmt_k1(e['all_in'])}",
        "yield":     f"Net yield {e['roi'] * 100:.0f}%" if on else None,
        "payback":   (f"Payback {M.fmt_years(e['breakeven_yrs'])}"
                      if on and e.get("breakeven_yrs") else None),
        "known_for": f.get("known_for") or None,
        "visitors":  f.get("visitors") or None,
        "bedrooms":  f"{l['bedrooms']} bedrooms" if l.get("bedrooms") else None,
        "rival":     f"Famous rival town: {rival}" if rival else None,
    }
    for k, v in own.items():
        if v:
            src.setdefault(k, v)
    return src

def data_for(M, h, l, hooks, usd, e, facts, s3):
    f = facts or {}
    mins, mode = M.trip_parts(h, l)
    price_usd = "FREE" if l["price_yen"] == 0 else M.fmt_usd(usd)
    price_yen = M.fmt_yen(l["price_yen"])
    vals, real = M.math_values(h, e, usd)
    d = {
        "town": M.short_name(h["name"]),
        "attraction": M.hook_label(h),
        "attraction_kind": M.KINDS[h["kind"]]["label"],
        "prefecture": M.PREF_EN.get(l["pref"], l["pref"]),
        "price_usd": price_usd,
        "price_yen": price_yen,
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
        "facts": fact_sources(M, h, l, e, f, s3, price_usd, price_yen),
        "slide2": {k: {"value": v, "means": MATH_MEANS.get(k, "")} for k, v in vals.items()},
        "template_version": {
            "s3_headline": s3["headline"], "s3_points": s3["points"], "s3_seo": s3["lines"],
            "s2_line_a": M.S2_TEMPLATES["a_real" if real else "a"],
            "s2_line_b": M.S2_TEMPLATES["b"]},
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
    body = {"model": MODEL, "temperature": TEMP, "max_tokens": 2000,
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
    if word_count(s) > words:
        return None, f"more than {words} words"
    if BANNED.search(s):
        return None, "hype word"
    bad = [n for n in nums(s) if n not in allowed]
    if bad:
        return None, f"numbers not in data {bad}"
    return s, None

def check_math(name, t, values, real):
    """Slide 2 line: placeholders only, all needed ones used once, fits after filling."""
    t = clean(t)
    rule = MATH[name]
    if not t:
        return None, "empty"
    used = PH.findall(t)
    unknown = sorted(set(used) - set(values))
    if unknown:
        return None, f"unknown placeholder {unknown}"
    need = set(rule["need"]) | (set(rule.get("need_if_real", [])) if real else set())
    if rule.get("place"):
        need.add("place")                                # SEO: the place name is in the line
    missing = sorted(need - set(used))
    if missing:
        return None, f"missing {missing}"
    if len(used) != len(set(used)):
        return None, "placeholder used twice"
    bare = PH.sub("", t)
    if re.search(r"\d", bare):
        return None, "typed a number (numbers must be placeholders)"
    if "{" in bare or "}" in bare:
        return None, "broken placeholder"
    if BANNED.search(bare):
        return None, "hype word"
    s = fill(t, values)
    if len(s) > rule["max_chars"]:
        return None, f"{len(s)} chars > {rule['max_chars']} after filling"
    return t, None

def check_point(p, src):
    """Slide 3 bullet: free wording, but tied to 1-2 DATA.facts keys and their numbers."""
    if not isinstance(p, dict):
        return None, None, "bullet is not an object"
    keys = p.get("from")
    keys = [keys] if isinstance(keys, str) else keys
    keys = [k for k in (keys or []) if isinstance(k, str)]
    if not keys or len(keys) > 2 or any(k not in src for k in keys):
        return None, None, f"'from' must be 1-2 keys of DATA.facts (got {p.get('from')})"
    t = clean(p.get("text"))
    if not t:
        return None, None, "empty bullet"
    if len(t) > POINT_CHARS:
        return None, None, f"bullet {len(t)} chars > {POINT_CHARS}"
    if word_count(t) > POINT_WORDS:
        return None, None, f"bullet more than {POINT_WORDS} words"
    if BANNED.search(t):
        return None, None, "hype word"
    base = " ".join(src[k] for k in keys)
    have, got = set(nums(base)), set(nums(t))
    if got - have:
        return None, None, f"bullet numbers {sorted(got - have)} not in its source"
    if have and not got:
        return None, None, "bullet dropped the number of its source"
    if not have:                                         # no number: must share a real word
        w = lambda s: {x for x in re.findall(r"[a-z]{4,}", s.lower())}
        if not w(t) & w(base):
            return None, None, "bullet doesn't match its source"
    return t, keys, None

def validate(raw, data, rot):
    allowed = set(nums(json.dumps(data, ensure_ascii=False)))
    names = town_names(data)
    trip_min = (nums(data.get("trip", "")) or [None])[0]
    out, why = {}, {}

    for name in SLOTS:
        s, err = check(name, raw.get(name), allowed)
        if s and name == "reel_hook":
            s = s.upper()
            if not has_name(s, names) and not nums(s):
                s, err = None, "no town and no number"
        if s and name in ("s3_seo1", "s3_seo2", "caption_seo") and not (
                has_name(s, names) and SEARCH.search(s)):
            s, err = None, "needs the town + a search word"
        if s and name in ("caption_hook", "reel_line1") and not has_name(s, names):
            s, err = None, "town keyword missing"
        if s and name == "reel_sub" and trip_min and trip_min not in nums(s):
            s, err = None, "drive time dropped"
        if s:
            out[name] = s
        else:
            why[name] = err
    if out.get("s3_seo1") and out.get("s3_seo2") and \
            out["s3_seo1"].lower() == out["s3_seo2"].lower():
        out.pop("s3_seo2")
        why["s3_seo2"] = "same as s3_seo1"

    # slide 2: the 2 math lines (placeholders only)
    values = {k: v["value"] for k, v in (data.get("slide2") or {}).items()}
    real = "yield" in values
    for name in MATH:
        t, err = check_math(name, raw.get(name), values, real)
        if t:
            out[name] = t
        else:
            why[name] = err

    # slide 3: 3 free bullets, each tied to its DATA.facts source
    src = data.get("facts") or {}
    pts, keys, perr = [], [], None
    for p in (raw.get("s3_points") or [])[:3]:
        t, k, err = check_point(p, src)
        if err:
            perr = err
            break
        if t.lower() in (x.lower() for x in pts):
            perr = "same bullet twice"
            break
        pts.append(t)
        keys += [x for x in k if x not in keys]
    recent = [tuple(sorted(c)) for c in (rot.get("s3_points") or [])[:10] if isinstance(c, list)]
    if not perr and len(pts) < 3:
        perr = "need 3 bullets"
    if not perr and tuple(sorted(keys)) in recent:
        perr = "same facts as a recent post"
    if perr:
        why["s3_points"] = perr
    else:
        out["s3_points"], out["s3_kinds"] = pts, keys

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


# ─── prompt ──────────────────────────────────────────────────────────
def prompt(data, rot):
    last = (rot.get("s3_templates") or [None])[0]
    recent = [c for c in (rot.get("s3_points") or [])[:10] if isinstance(c, list)]
    real = "yield" in (data.get("slide2") or {})
    math_rules = {n: {"must_use": r["need"] + (r["need_if_real"] if real and r.get("need_if_real")
                                               else []) + (["place"] if r.get("place") else []),
                      "max_chars_after_filling": r["max_chars"], "job": r["job"]}
                  for n, r in MATH.items()}
    return (
        "SLOTS (max characters, max words, what goes there):\n"
        + json.dumps({k: {"max_chars": c, "max_words": w, "job": j}
                      for k, (c, w, j) in SLOTS.items()}, indent=1)
        + "\n\nSLIDE 2 (s2_line_a, s2_line_b): rewrite the 2 small math lines in your own "
          "words. NEVER type a digit: write every number as a {placeholder} from DATA.slide2 "
          "(each at most once per line); the code puts in the real value. Only use "
          "placeholders listed in DATA.slide2. Separate parts with ' • ' – the slide breaks "
          "lines there (28 px text, both lines together max 5 rows). SEO: plain words people "
          "search (all-in cost, renovation, nightly rate, occupancy, Airbnb, rental income, "
          "net yield). Current wording: DATA.template_version.\n"
        + json.dumps(math_rules, indent=1)
        + f"\n\nSLIDE 3 UNDER THE HEADLINE: 3 bullets (s3_points) + 2 search lines (s3_seo1, "
          f"s3_seo2), your own wording, same layout. Bullet: max {POINT_CHARS} chars, max "
          f"{POINT_WORDS} words; 'from' = 1-2 keys of DATA.facts it is based on; say only what "
          "those facts say and copy their numbers exactly (if the fact has a number, use it). "
          "The bullets must pay off the headline. Don't use exactly these sets of 'from' keys "
          f"(recent posts): {json.dumps(recent)}. Search lines: the town + a search word people "
          "type, natural language, no stuffing, the two lines on different searches."
        + "\n\nangle: one of " + json.dumps(ANGLES)
        + f" – not '{last}' (used last post). D only if famous_rival_town exists."
        + f"\nhashtags: {MAX_TAGS} max, include #akiya, one town/area tag, "
          "one attraction-kind tag."
        + "\n\nDATA:\n" + json.dumps(data, ensure_ascii=False, indent=1)
        + "\n\nOUR PAST POSTS (score = follows/shares/saves per reach; "
          "learn the pattern, don't copy):\n" + json.dumps(evidence(), ensure_ascii=False)
        + "\n\nAnswer in this json shape:\n" + json.dumps(EXAMPLE, ensure_ascii=False))


# ─── entry point ─────────────────────────────────────────────────────
def write(M, h, l, hooks, usd, e, facts, s3, rot):
    """Returns {slot: text} with only the fields that passed every check."""
    if not ON:
        print("AI copy: off (no DEEPSEEK_API_KEY or AI_COPY=0) – template text")
        return {}
    day = datetime.now(M.JST).strftime("%Y-%m-%d")
    cache = load(CACHE_FILE)
    ck = f"{day}|{l['url']}|{VERSION}"
    data = data_for(M, h, l, hooks, usd, e, facts, s3)
    if ck in cache:
        raw = cache[ck]                                  # re-run today: no new call
    else:
        raw = call(prompt(data, rot))
        if raw is None:
            print("AI copy: no answer – template text")
            return {}
        cache = {k: v for k, v in cache.items() if k.startswith(day)}
        cache[ck] = raw
        save(CACHE_FILE, cache)
    out = validate(raw, data, rot)
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
