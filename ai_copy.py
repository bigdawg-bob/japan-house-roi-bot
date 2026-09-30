"""
ai_copy.py – DeepSeek writes the WORDS, our data supplies the NUMBERS.

The model gets every fact we already have + the rules for each line (words, characters,
what must be in it). Inside those rules it is free to choose the angle and the wording.
Every field is checked: a field that breaks a rule is dropped and the template text is
used for it. No key / API down / bad JSON -> {} -> the post goes out exactly as before.

Goal of every line: stop the scroll (hook) and/or be found in search (SEO), ideally both.

What DeepSeek writes (free wording, always checked):
  reel      line 1 (hook) + line 2          same font, size, spacing and timing as before
  slide 2   the 2 small lines under the big number: plain English, max 3 numbers each,
            numbers ONLY as {placeholders}; this file fills them in (so they can't be wrong)
            and returns finished text
  slide 3   headline, 3 bullets, 2 SEO lines, CTA button
            each bullet names the DATA fact it comes from
  captions  carousel line 1 + SEO sentence, reel line 1 + dream line, hashtags
Never written by DeepSeek: the big hero numbers on slides 1 + 2, the price and link lines.

How we hit the length (a model can't count characters – it reads word pieces, not letters):
  - max_words is the rule it follows; the word limits are set so a line within max_words
    normally fits max_chars
  - every example is shown with its REAL length (measured by Python, not typed by hand)
  - short on-image lines come as 3 options, longest -> shortest; the first that passes wins
  - fields that still fail get ONE repair round: DeepSeek is told exactly why, rewrites
    only those, at a lower temperature. Whatever still fails -> template.

v3 checks (kept): no repeat of the slide 3 subtitle, headline must name the rival (no "the
famous rival"), headline must be a hook (name or number), only places that are in DATA,
never "near <rival>" unless DATA says so, one SEO line keeps the drive time, STR -> Airbnb.

v5 changes:
  - CTA: no template example in the prompt; a copy of the template CTA (or any
    "Full breakdown ...") is rejected
  - headline / search lines / bullets: a copy of the template text is rejected, and the
    template text is no longer sent to the model (it copied it)
  - nights: DATA tells open nights (legal cap) and booked nights (open × occupancy) apart
  - slide 2: plain lines, max 3 numbers, no × / = formulas, new clear placeholders
  - English: native-copywriter rules with bad/good examples, bullets up to 7 words,
    a percentage may be said as '1 in 3' / 'a third', clumsy patterns rejected,
    facts written as sentences
  - optional editor pass (AI_EDIT=1): one extra call smooths the English; an edited
    field is used only if it still passes every check

Env: DEEPSEEK_API_KEY (required), DEEPSEEK_MODEL (default deepseek-flash),
     AI_COPY=0 turns it off, AI_COPY_TEMP (default 0.8), AI_EDIT=1 turns on the editor pass.
Evidence loop: state/performance.json, filled from Instagram Insights:
     {"2026-10-01": {"reach": 5400, "shares": 41, "saves": 88, "follows": 23}}
"""
import json, os, re
from datetime import datetime
from pathlib import Path

import requests

def _env(n, d):
    return os.getenv(n) or d

KEY       = os.getenv("DEEPSEEK_API_KEY")
URL       = _env("DEEPSEEK_URL", "https://api.deepseek.com/chat/completions")
MODEL     = _env("DEEPSEEK_MODEL", "deepseek-flash")
TEMP      = float(_env("AI_COPY_TEMP", "0.8"))  # 1.3 made it lose the thread mid-answer
FIX_TEMP  = 0.5                                  # repair + editor round: stricter
ON        = _env("AI_COPY", "1") == "1" and bool(KEY)
EDIT      = _env("AI_EDIT", "0") == "1"          # v5: optional editor pass
VERSION   = "v5"                     # part of the cache key: new rules = new answer
N_OPTIONS = 3                        # options per short line, longest -> shortest

STATE      = Path(__file__).parent / "state"
LOG_FILE   = STATE / "copy_log.json"
PERF_FILE  = STATE / "performance.json"
CACHE_FILE = STATE / "copy_cache.json"

CTA_DEFAULT = "Full breakdown + agent contact → newsletter link in bio"   # template only

# slot -> (max characters, max words, what goes there, example or None)
# Word limits are the ones the model can follow: ~6 characters per word incl. the space.
SLOTS = {
    "reel_hook":    (18, 3, "Reel line 1, huge bold caps, read in under 1 second. It must "
                            "name the town OR show a number from DATA (price, drive time, "
                            "yield...).",
                     "$25K AMINO"),
    "reel_sub":     (44, 8, "Reel line 2 under the hook. Any wording and order, but it must "
                            "keep the drive/walk minutes from DATA.trip. ' • ' may separate "
                            "parts.",
                     "4BR • 7 min drive to beach • 18% net"),
    "s3_headline":  (34, 5, "Slide 3 headline = the HOOK for your angle. Be specific: name the "
                            "rival town, the attraction, or use a number from DATA. Never write "
                            "'the famous rival' / 'its rival' without the name. Don't repeat "
                            "DATA.slide3_subtitle. The 3 bullets under it must pay it off.",
                     "Niseko views, Kutchan price"),
    "s3_seo1":      (46, 8, "Slide 3 search line 1: the town + a search word people type "
                            "(akiya, Airbnb, rental, investment, minpaku, onsen/ski/beach "
                            "house), written as a natural phrase – never 'label: number' "
                            "(bad: 'Yufuin Airbnb investment: 180 nights'). At least ONE of "
                            "s3_seo1/s3_seo2 keeps the drive minutes from DATA.trip.",
                     "Amino akiya, 7 min drive to the beach"),
    "s3_seo2":      (46, 8, "Slide 3 search line 2. Same rules as s3_seo1, but a different "
                            "search and a different fact.",
                     "A Kyoto beach house to Airbnb for $25K"),
    "s3_cta":       (56, 10, "Button text at the bottom of slide 3. Give ONE reason to tap "
                             "that fits THIS house (meet its agent, see the full numbers, the "
                             "renovation plan...) and send people to the newsletter link in "
                             "bio. Must contain the word 'bio'. No numbers, no links, no "
                             "hashtags. Never write 'Full breakdown + agent contact'.",
                     "Want the agent for this Amino house? Link in bio"),
    "caption_hook": (125, 22, "Caption line 1 (only ~125 chars show before 'more'). Town + "
                              "akiya/house keyword + one hard number, early.", None),
    "caption_seo":  (220, 40, "One natural sentence with the words people search: town, "
                              "prefecture, akiya, Airbnb/rental, the attraction. No stuffing.",
                     None),
    "reel_line1":   (90, 16, "Reel caption line 1: '[Town] akiya' + price -> money result.", None),
    "reel_dream":   (90, 16, "Reel caption line 2: one sensory line about being there.", None),
}
# short lines on the image: 3 options each (captions have room, one string is enough)
OPTION_SLOTS = {"reel_hook", "reel_sub", "s3_headline", "s3_seo1", "s3_seo2", "s3_cta"}
# v5: slot -> which template text it must not copy
TEMPLATE_OF = {"s3_headline": "s3_headline", "s3_seo1": "s3_seo", "s3_seo2": "s3_seo",
               "s3_cta": "s3_cta"}

POINT_CHARS   = 34                   # slide 3 bullet (same limit as the template bullets)
POINT_WORDS   = 7                    # v5: was 5 – too tight for natural English
POINT_EXAMPLE = "Legal to rent 180 nights a year"
MAX_TAGS      = 5                    # Instagram hard cap since Dec 2025

# slide 2: the 2 small lines under the big payback number, {placeholders} only
MAX_S2_NUMS = 3
MATH = {
    "s2_line_a": {"max_chars": 64, "need": ["all_in"],
                  "job": "Slide 2 small line 1, the cost side: what {all_in} pays for, in "
                         "plain words (house, renovation, fees). A phrase, not a formula."},
    "s2_line_b": {"max_chars": 64, "need": ["net_year"],
                  "job": "Slide 2 small line 2, the income side: how the rent turns into "
                         "{net_year} a year. A phrase, not a formula."},
}
MATH_MEANS = {
    "all_in":        "total cost: house + renovation + buying fees",
    "house":         "house price only",
    "rate":          "nightly rate (just the money – add 'a night' yourself)",
    "occ":           "occupancy = share of the open nights that get booked",
    "open_nights":   "nights a year the house MAY be rented (legal cap) – NOT nights booked",
    "booked_nights": "nights actually booked a year = open_nights × occupancy",
    "mgmt":          "management fee share taken off the rent",
    "net_year":      "net income per year after management",
    "net_month":     "net income per month after management",
    "yield":         "net yield per year",
    "place":         "the attraction name (search keyword)",
}
PH = re.compile(r"\{(\w+)\}")

ANGLES = {"A": "THE COMP – price vs ryokan / competition",
          "B": "THE MAP – access, what is nearby",
          "C": "THE MATH LEAK – what the rental math hides",
          "D": "THE TOWN SECRET – why this town, not the famous rival (name the rival!)",
          "E": "THE EXIT – plan B if Airbnb fails"}
BANNED = re.compile(r"guarantee|risk[- ]?free|passive income|get rich|no[- ]brainer|"
                    r"can'?t lose|free money|100% safe|once in a lifetime", re.I)
SEARCH = re.compile(r"\b(akiya|airbnb|short[- ]term rental|rentals?|minpaku|"
                    r"invest(?:ment|ing|or)?s?|propert(?:y|ies)|houses?|homes?|cabins?|ryokan|"
                    r"onsen|ski|beach|yield|vacation|real estate)\b", re.I)
NAME_STOP = {"lake", "mt", "old", "the", "town", "onsen", "ski", "beach", "bay", "coast"}

# "the famous rival", "its bigger neighbour", "the popular one" ... without a name
VAGUE = re.compile(r"\b(?:famous|big|bigger|popular|well[- ]known|pricier|crowded)\s+"
                   r"(?:rival|neighbou?r|town|one|sister)\b|\b(?:the|its|a)\s+rival\b", re.I)
NEAR_WORDS = (r"(?:near|nearby|close to|next to|beside|steps from|around the corner from|"
              r"min(?:ute)?s? from|walk from|drive from|neighbou?ring)")
# capitalised words that are never a (made-up) place
COMMON_CAPS = {"airbnb", "airbnbs", "japan", "japanese", "akiya", "akiyas", "minpaku",
               "onsen", "ryokan", "ski", "beach", "english", "instagram", "google", "maps",
               "bio", "newsletter", "link", "dm", "mt", "mount", "lake", "i", "want", "see",
               "get", "meet", "tap", "read"}

# v5: English patterns we know are broken
CLUMSY = [
    (re.compile(r"^[A-Za-z]+\s+\d[\d.,]*%\s+of\b"),
     "number crammed into a noun phrase – write e.g. '33% of visitors come from abroad'"),
    (re.compile(r":\s*~?\d[\d.,]*\s+nights?\s*$", re.I),
     "'…: 180 nights' says nothing – say what the nights are (open to rent / booked)"),
    (re.compile(r"\b(the|a|an)\s+(the|a|an)\b", re.I), "double article"),
]
# v5: a bullet may say a percentage from its fact in words
FRACTIONS = [("1 in 2", 50), ("half", 50), ("1 in 3", 33), ("a third", 33),
             ("one third", 33), ("2 in 3", 67), ("two thirds", 67), ("1 in 4", 25),
             ("a quarter", 25), ("1 in 5", 20), ("1 in 10", 10)]

SYSTEM = """You write Instagram copy for @yama.yield: cheap Japanese houses (akiya) near ski
resorts, onsen towns, sights, beaches and nature, with short-term rental numbers.
Audience: people outside Japan searching "akiya", "cheap house Japan", "[town] Airbnb",
"Japan property investment". Goal: grow the audience. Every line should stop the scroll
(hook) or be found in search (SEO) – ideally both.
How Instagram works (use it):
- Search reads captions and on-screen text: put the town + a search word (akiya, Airbnb,
  house, rental, investment) early, in natural language. No keyword stuffing.
- The reel hook, slide 3 headline and caption line 1 decide if people stop: be specific
  (a real number + a real place name) and open a question the slides answer. Vague lines
  ("the famous rival", "this town") don't stop anyone. Never promise what the slides don't show.
- Shares and saves carry posts furthest: write what someone would send to a friend
  planning a Japan house or ski/onsen trip.
- Max 5 hashtags, specific beats generic.
How to write (most important):
- Write like a native English Instagram copywriter. Every line must read as natural
  spoken English – something a person would say out loud. Short is fine; broken is not.
- DATA gives you FACTS, not wording. Some fact texts are rough notes: never paste them,
  rewrite them as natural English.
- Never drop a number into a noun phrase, never write 'label: number' fragments.
  Bad: "Foreign 33% of visitors"          Good: "33% of visitors come from abroad"
  Bad: "Yufuin Airbnb investment: 180 nights"  Good: "Yufuin Airbnb, open 180 nights a year"
  Bad: "{place} • {rate} × {occ} × {open_nights} • {mgmt}: {net_year}"
  Good: "~{booked_nights} booked nights a year → {net_year} after management"
- Nights: open nights (the nights it may legally be rented) are NOT booked nights.
  Booked nights = open nights × occupancy. Never call open nights 'booked' or 'rented'.
You are free to choose the angle and the wording. The limits are:
- Every number you write must be copied EXACTLY from DATA (same digits). Never invent,
  round, convert, add up or estimate numbers. If unsure, write the line without a number.
  One exception: in a slide 3 bullet, a percentage from its fact may be said as
  "1 in 3", "a third", "half", "a quarter".
- Slide 2 lines (s2_line_a, s2_line_b): never type a digit. Write every number as a
  {placeholder} from DATA.slide2; the code puts in the real value. Max 3 per line.
- Slide 3 bullets say only what their DATA.facts source says – in your own words.
- Places: only name places that appear in DATA. Only say the house is near / close to a
  place if DATA.trip or DATA.also_near says so. famous_rival_town is a COMPARISON
  ("instead of Kurokawa", "Kurokawa-style onsen for less"), never a neighbour.
- Slide 3 sits under a subtitle (DATA.slide3_subtitle): don't repeat its facts or numbers.
- Say "Airbnbs" or "short-term rentals", never "STR" / "STRs" (the audience doesn't know it).
- Sentence case: capitals only at the start and for names.
- No hype, no guarantees, no investment-advice wording.
- No emoji in slide or reel text.
- Short is the rule: stay inside every max_words. Where a list of options is asked, give
  them longest to shortest, each one a complete line that follows every rule.
Answer ONLY with json."""

EXAMPLE = {"angle": "A",
           "reel_hook": ["...", "...", "..."], "reel_sub": ["...", "...", "..."],
           "s2_line_a": ["... {all_in} ...", "...", "..."],
           "s2_line_b": ["... {net_year} ...", "...", "..."],
           "s3_headline": ["longest ...", "...", "short"],
           "s3_points": [{"from": ["ryokan"], "text": ["...", "...", "short"]},
                         {"from": ["..."], "text": ["...", "...", "short"]},
                         {"from": ["...", "..."], "text": ["...", "...", "short"]}],
           "s3_seo1": ["...", "...", "..."], "s3_seo2": ["...", "...", "..."],
           "s3_cta": ["... bio", "... bio", "... bio"],
           "caption_hook": "...", "caption_seo": "...",
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

def options(v):
    """A field may be one string or a list of options -> list of non-empty options."""
    vs = v if isinstance(v, list) else [v]
    return [clean(x) for x in vs if isinstance(x, (str, int, float)) and clean(x)]

def fill(template, values):
    """'{rate} a night' + values -> '$181 a night' (unknown placeholders stay as they are)."""
    return PH.sub(lambda m: str(values.get(m.group(1), m.group(0))), template)

def no_str(s):
    """'200 STRs' -> '200 Airbnbs' (the audience doesn't know 'STR')."""
    s = re.sub(r"\bSTRS\b", "AIRBNBS", str(s))
    s = re.sub(r"\bSTRs\b", "Airbnbs", s)
    return re.sub(r"\bSTR\b", "Airbnb", s)

def no_str_all(v):
    if isinstance(v, str):
        return no_str(v)
    if isinstance(v, list):
        return [no_str_all(x) for x in v]
    if isinstance(v, dict):
        return {k: no_str_all(x) for k, x in v.items()}
    return v

def norm(s):
    return re.sub(r"\W+", " ", str(s or "")).strip().lower()

def is_copy(s, templates):
    """v5: same text as a template line (ignoring case and punctuation)."""
    return any(norm(s) == norm(t) for t in templates or [] if t)

def clumsy(s):
    """v5: -> why, if the line matches a known broken-English pattern."""
    for rx, why in CLUMSY:
        if rx.search(s):
            return why
    return None

def fraction_hits(t, base):
    """v5: '1 in 3 visitors...' for a 33% fact -> ({'1', '3'}, True)"""
    pcts = [float(x) for x in re.findall(r"(\d+(?:\.\d+)?)\s*%", str(base).replace(",", ""))]
    extra, hit, low = set(), False, t.lower()
    for phrase, v in FRACTIONS:
        if any(abs(p - v) <= 2 for p in pcts) and re.search(
                r"\b" + re.escape(phrase) + r"\b", low):
            hit = True
            extra |= set(nums(phrase))
    return extra, hit

def booked_nights(e):
    """Open nights × occupancy, e.g. 180 × 45% -> 81."""
    return round(e["nights"] * e["occ"])

def names_of(name):
    """'Yufuin Onsen' -> {'yufuin onsen', 'yufuin'}"""
    name = str(name or "").lower().strip()
    if not name:
        return set()
    names = {name}
    words = [w for w in re.split(r"[\s\-]+", name) if w not in NAME_STOP]
    if words and len(words[0]) >= 4:
        names.add(words[0])
    return names

def town_names(data):
    """Names that count as 'the town' for the SEO checks: full name + its main word."""
    return names_of(data["town"])

def has_name(s, names):
    s = s.lower()
    return any(n in s for n in names)

def subtitle_of(s3, f):
    """The line under the slide 3 title, e.g. 'Onsen town at Mt Yufu • 4.3M visitors/yr'."""
    for k in ("subtitle", "sub"):
        if s3.get(k):
            return clean(s3[k])
    return " • ".join(clean(x) for x in (f.get("known_for"), f.get("visitors")) if x)

def known_words(data):
    """Every word that appears anywhere in DATA (+ a few safe ones)."""
    return set(re.findall(r"[a-z]+", json.dumps(data, ensure_ascii=False).lower())) | COMMON_CAPS

def unknown_names(s, known):
    """Capitalised words (not at the start of a part) that appear nowhere in DATA."""
    bad = []
    for seg in re.split(r"[•:.!?|/–—()\"]+", s):
        for w in re.findall(r"[A-Za-z][A-Za-z']*", seg)[1:]:
            if not w[0].isupper() or (w.isupper() and len(w) <= 4):
                continue
            lw = re.sub(r"'s?$", "", w.lower())
            if lw in known or (lw.endswith("s") and lw[:-1] in known):
                continue
            bad.append(w)
    return bad

def place_problem(s, known, rival, rival_near):
    bad = unknown_names(s, known)
    if bad:
        return f"names a place not in the facts {bad}"
    if rival and not rival_near:
        for r in rival:
            if re.search(NEAR_WORDS + r"\s+(?:\w+\s+){0,2}" + re.escape(r), s, re.I):
                return f"says the house is near {r.title()} (not in the facts)"
    return None

def templates_of(s3):
    """v5: the template text per slot – used only to reject copies, never sent to the model."""
    return {"s3_headline": [no_str(s3.get("headline", ""))],
            "s3_points": [no_str(p) for p in s3.get("points", []) if isinstance(p, str)],
            "s3_seo": [no_str(x) for x in s3.get("lines", []) if isinstance(x, str)],
            "s3_cta": [CTA_DEFAULT]}


# ─── the facts the model may use ─────────────────────────────────────
def fact_sources(M, h, l, e, f, s3, price_usd, price_yen):
    """key -> fact text (full sentences). Every slide 3 bullet must name 1-2 of these keys."""
    src = {k: no_str(v) for k, v in (s3.get("pool") or {}).items() if v}
    mins, mode = M.trip_parts(h, l)
    on = M.show_yield(e)
    rival = M.rival_town(h)
    occ = f"{e['occ'] * 100:.0f}%"
    own = {
        "price":     (f"The house costs {price_usd} ({price_yen})" if price_usd != "FREE"
                      else "The house is free"),
        "trip":      f"It is a {mins} min {mode} to {M.hook_label(h)}",
        "rate":      f"Guests pay about ${e['adr']:,} a night",
        "open_nights": f"It may be rented {e['nights']} nights a year",
        "booked":    f"About {booked_nights(e)} nights a year get booked ({occ} occupancy)",
        "minpaku":   (f"This town caps rentals at {f['minpaku_cap']} nights a year"
                      if isinstance(f.get("minpaku_cap"), int) and f["minpaku_cap"] < 180
                      else "The national minpaku rule caps rentals at 180 nights a year"),
        "net_month": f"It nets ${e['monthly']:,} a month after management",
        "all_in":    f"{M.fmt_k1(e['all_in'])} all-in pays for the house, renovation and fees",
        "yield":     f"The net yield is {e['roi'] * 100:.0f}% a year" if on else None,
        "payback":   (f"It pays for itself in {M.fmt_years(e['breakeven_yrs'])}"
                      if on and e.get("breakeven_yrs") else None),
        "known_for": f.get("known_for") or None,
        "visitors":  f.get("visitors") or None,
        "bedrooms":  f"The house has {l['bedrooms']} bedrooms" if l.get("bedrooms") else None,
        "rival":     (f"{rival} is the famous rival town (a comparison, not nearby)"
                      if rival else None),
    }
    for k, v in own.items():
        if v:
            src.setdefault(k, no_str(v))
    return src

def s2_values(M, h, e, usd, price_usd):
    """v5: plain values for the slide 2 placeholders. Only the place name and the yearly
    net come from main.py's math values, so they match the slide exactly."""
    vals, real = M.math_values(h, e, usd)
    m = re.search(r"\$[\d.,]+[KkMm]?", str(vals.get("net", "")))
    net_year = m.group(0) if m else M.fmt_k1(e["monthly"] * 12)
    mg = re.search(r"(\d+(?:\.\d+)?)\s*%", str(vals.get("mgmt", "")))
    v = {
        "all_in":        M.fmt_k1(e["all_in"]),
        "house":         price_usd if price_usd != "FREE" else None,
        "rate":          f"${e['adr']:,}",
        "occ":           f"{e['occ'] * 100:.0f}%",
        "open_nights":   str(e["nights"]),
        "booked_nights": str(booked_nights(e)),
        "mgmt":          f"{mg.group(1)}%" if mg else None,
        "net_year":      net_year,
        "net_month":     f"${e['monthly']:,}",
        "yield":         f"{e['roi'] * 100:.0f}%" if real else None,
        "place":         vals.get("place") or M.hook_label(h),
    }
    return {k: x for k, x in v.items() if x}

def data_for(M, h, l, hooks, usd, e, facts, s3):
    f = facts or {}
    mins, mode = M.trip_parts(h, l)
    price_usd = "FREE" if l["price_yen"] == 0 else M.fmt_usd(usd)
    price_yen = M.fmt_yen(l["price_yen"])
    s2 = s2_values(M, h, e, usd, price_usd)
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
        "open_nights_per_year": e["nights"],                 # v5: was 'rented_nights'
        "booked_nights_per_year": booked_nights(e),          # v5
        "nights_note": ("open nights = nights it may legally be rented; booked nights = "
                        "open nights × occupancy. Never call open nights 'booked'."),
        "minpaku_rule": (f"this town's own cap: {f['minpaku_cap']} nights a year (national "
                         f"cap 180) – never say 180 nights for this town"
                         if isinstance(f.get("minpaku_cap"), int) and f["minpaku_cap"] < 180
                         else "national cap: 180 nights per year"),
        "net_per_month": f"${e['monthly']:,}",
        "all_in_cost": M.fmt_k1(e["all_in"]),
        "net_yield": f"{e['roi'] * 100:.0f}%" if M.show_yield(e) else None,
        "payback": (M.fmt_years(e["breakeven_yrs"])
                    if M.show_yield(e) and e.get("breakeven_yrs") else None),
        "also_near": [f"{M.hook_label(x)}: {M.fmt_trip(x, l).lstrip('~')}" for x in hooks[1:3]],
        "known_for": no_str(f.get("known_for") or "") or None,
        "visitors": no_str(f.get("visitors") or "") or None,
        "famous_rival_town": M.rival_town(h),
        "slide3_subtitle": no_str(subtitle_of(s3, f)),
        "facts": fact_sources(M, h, l, e, f, s3, price_usd, price_yen),
        "slide2": {k: {"value": v, "means": MATH_MEANS.get(k, "")} for k, v in s2.items()},
        # v5: no 'template_version' – the model copied it word for word
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
def call(messages, temp=None):
    body = {"model": MODEL, "temperature": TEMP if temp is None else temp,
            "max_tokens": 3000,                          # 3 options per line need room
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
            "messages": messages}
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
            ch = r.json()["choices"][0]
            if ch.get("finish_reason") == "length":
                print("  DeepSeek: answer cut off (max_tokens) – retrying")
                continue
            v = json.loads(ch["message"]["content"] or "")
            if isinstance(v, dict):
                return v
        except (ValueError, KeyError, IndexError, TypeError):
            pass
        print("  DeepSeek: empty / not JSON – retrying")
    return None


# ─── checks ──────────────────────────────────────────────────────────
def check_slot(name, s, ctx):
    """One option of one slot -> (text, None) if it passes every rule, else (None, why)."""
    chars, words, _, _ = SLOTS[name]
    if not s:
        return None, "empty"
    if len(s) > chars:
        return None, f"{len(s)} chars > {chars}"
    n = word_count(s)
    if n > words:
        return None, f"{n} words > {words}"
    if BANNED.search(s):
        return None, "hype word"
    bad = [x for x in nums(s) if x not in ctx["allowed"]]
    if bad:
        return None, f"numbers not in data {bad}"

    if name == "reel_hook":                              # all caps: can't spot names
        s = s.upper()
        if not has_name(s, ctx["names"]) and not nums(s):
            return None, "no town and no number"
        return s, None

    if name in TEMPLATE_OF and is_copy(s, ctx["tmpl"].get(TEMPLATE_OF[name])):
        return None, "copied the template text – write your own line"       # v5
    err = clumsy(s)                                                          # v5
    if err:
        return None, err
    err = place_problem(s, ctx["known"], ctx["rival"], ctx["rival_near"])
    if err:
        return None, err
    if name == "s3_headline":
        if VAGUE.search(s) and not (ctx["rival"] and has_name(s, ctx["rival"])):
            return None, f"vague rival – must name {ctx['rival_name'] or 'a real place'}"
        if not (has_name(s, ctx["hook_names"]) or nums(s)):
            return None, "no name and no number (not a hook)"
        if ctx["angle"] == "D" and not has_name(s, ctx["rival"]):
            return None, f"angle D but {ctx['rival_name']} isn't named"
    if name == "s3_cta":
        if "bio" not in s.lower() or re.search(r"\d|[#@]|https?://", s):
            return None, "must point to the bio, no numbers / links / hashtags"
        if re.search(r"full breakdown", s, re.I):                          # v5
            return None, "reuses the old template wording – give a reason about THIS house"
    if name in ("s3_seo1", "s3_seo2", "caption_seo") and not (
            has_name(s, ctx["names"]) and SEARCH.search(s)):
        return None, "needs the town + a search word"
    if name in ("caption_hook", "reel_line1") and not has_name(s, ctx["names"]):
        return None, "town keyword missing"
    if name == "reel_sub" and ctx["trip_min"] and ctx["trip_min"] not in nums(s):
        return None, f"drive time ({ctx['trip_min']} min) dropped"
    return s, None

def passing(name, v, ctx):
    """All options of a slot -> ([(option no., text) that pass], 'why the others failed')."""
    opts = options(v)[:N_OPTIONS]
    ok, errs = [], []
    for i, o in enumerate(opts, 1):
        s, err = check_slot(name, o, ctx)
        if s:
            ok.append((i, s))
        else:
            errs.append(f"#{i} {err}: {o!r}" if len(opts) > 1 else f"{err}: {o!r}")
    return ok, (" | ".join(errs) or "empty")

def check_math(name, t, values):
    """Slide 2 line: placeholders only, the needed one used, max 3 numbers, fits."""
    t = clean(t)
    rule = MATH[name]
    if not t:
        return None, "empty"
    used = PH.findall(t)
    unknown = sorted(set(used) - set(values))
    if unknown:
        return None, f"unknown placeholder {unknown}"
    missing = sorted(set(rule["need"]) - set(used))
    if missing:
        return None, f"missing {missing}"
    if len(used) != len(set(used)):
        return None, "placeholder used twice"
    bare = PH.sub("", t)
    if re.search(r"\d", bare):
        return None, "typed a number (numbers must be placeholders)"
    if "{" in bare or "}" in bare:
        return None, "broken placeholder"
    if re.search(r"[×=]", bare):
        return None, "a formula – write it as a plain phrase"
    if BANNED.search(bare):
        return None, "hype word"
    s = fill(t, values)
    if len(s) > rule["max_chars"]:
        return None, f"{len(s)} chars > {rule['max_chars']} after filling"
    if len(nums(s)) > MAX_S2_NUMS:
        return None, f"{len(nums(s))} numbers > {MAX_S2_NUMS} – keep it simple"
    err = clumsy(s)
    if err:
        return None, err
    return t, None

def bullet_problem(t, base, ctx):
    """One text option of a bullet -> None if fine, else why."""
    if len(t) > POINT_CHARS:
        return f"{len(t)} chars > {POINT_CHARS}"
    n = word_count(t)
    if n > POINT_WORDS:
        return f"{n} words > {POINT_WORDS}"
    if BANNED.search(t):
        return "hype word"
    if is_copy(t, ctx["tmpl"].get("s3_points")):
        return "copied a template bullet – write your own"
    err = clumsy(t)
    if err:
        return err
    extra, frac = fraction_hits(t, base)                 # '1 in 3' for a 33% fact
    got = set(nums(t)) - extra
    if ctx["sub"] and got & set(nums(ctx["sub"])):
        return "repeats a number of the subtitle"
    err = place_problem(t, ctx["known"], ctx["rival"], ctx["rival_near"])
    if err:
        return err
    have = set(nums(base))
    if got - have:
        return f"numbers {sorted(got - have)} not in its source"
    if have and not got and not frac:
        return "dropped the number of its source"
    if not have:                                         # no number: must share a real word
        w = lambda s: set(re.findall(r"[a-z]{4,}", s.lower()))
        if not w(t) & w(base):
            return "doesn't match its source"
    return None

def check_point(p, src, ctx):
    """Slide 3 bullet: tied to 1-2 DATA.facts keys; the first text option that passes wins.
    -> (text, keys, None) or (None, None, why)"""
    if not isinstance(p, dict):
        return None, None, "bullet is not an object"
    keys = p.get("from")
    keys = [keys] if isinstance(keys, str) else keys
    keys = [k for k in (keys or []) if isinstance(k, str)]
    if not keys or len(keys) > 2 or any(k not in src for k in keys):
        return None, None, f"'from' must be 1-2 keys of DATA.facts (got {p.get('from')})"
    sub = ctx["sub"]
    if sub:
        sub_n = set(nums(sub))
        if any(clean(src[k]).lower() in sub.lower() or set(nums(src[k])) & sub_n for k in keys):
            return None, None, f"based on a fact already in the subtitle ({keys})"
    base = " ".join(src[k] for k in keys)
    opts = options(p.get("text"))[:N_OPTIONS]
    if not opts:
        return None, None, "empty bullet"
    errs = []
    for i, t in enumerate(opts, 1):
        err = bullet_problem(t, base, ctx)
        if not err:
            return t, keys, None
        errs.append(f"#{i} {err}: {t!r}" if len(opts) > 1 else f"{err}: {t!r}")
    return None, None, " | ".join(errs)

def validate(raw, data, rot, tmpl=None):
    """-> (out, why, notes)
    out   = fields that passed (what main.py uses)
    why   = field -> why it failed (template used; also sent to the repair round)
    notes = which option was used when it wasn't the 1st"""
    raw = no_str_all(raw if isinstance(raw, dict) else {})   # STR -> Airbnb before checks
    rival = names_of(data.get("famous_rival_town"))
    near = [str(x).split(":")[0] for x in data.get("also_near") or []]
    names = town_names(data)
    last = (rot.get("s3_templates") or [None])[0]
    a = str(raw.get("angle") or "").strip().upper()
    angle = a if (a in ANGLES and a != last
                  and (a != "D" or data.get("famous_rival_town"))) else None
    ctx = {
        "allowed":    set(nums(json.dumps(data, ensure_ascii=False))),
        "names":      names,
        "rival":      rival,
        "rival_name": data.get("famous_rival_town"),
        "rival_near": bool(rival) and any(has_name(x, rival) for x in near),
        "hook_names": names | rival | names_of(data.get("attraction"))
                      | set().union(*[names_of(x) for x in near]),
        "known":      known_words(data),
        "sub":        data.get("slide3_subtitle", ""),
        "trip_min":   (nums(data.get("trip", "")) or [None])[0],
        "angle":      angle,
        "tmpl":       tmpl or {},
    }
    out, why, notes = {}, {}, []

    # single lines (first option that passes wins)
    for name in SLOTS:
        if name in ("s3_seo1", "s3_seo2"):
            continue                                     # checked as a pair below
        ok, errs = passing(name, raw.get(name), ctx)
        if ok:
            i, s = ok[0]
            out[name] = s
            if i > 1:
                notes.append(f"{name}: option {i} used")
        else:
            why[name] = errs

    # slide 3 search lines: a pair that differs and keeps the drive time once
    t = ctx["trip_min"]
    has_t = lambda s: not t or t in nums(s)
    ok1, e1 = passing("s3_seo1", raw.get("s3_seo1"), ctx)
    ok2, e2 = passing("s3_seo2", raw.get("s3_seo2"), ctx)
    pair = next(((x, y) for x in ok1 for y in ok2
                 if x[1].lower() != y[1].lower() and (has_t(x[1]) or has_t(y[1]))), None)
    if pair:
        for n, (i, s) in zip(("s3_seo1", "s3_seo2"), pair):
            out[n] = s
            if i > 1:
                notes.append(f"{n}: option {i} used")
    elif ok1 and ok2:
        msg = ("no pair works: the 2 lines must differ"
               + (f" and one must keep '{t} min'" if t else ""))
        why["s3_seo1"] = why["s3_seo2"] = msg
    else:
        # v5b: use one AI line next to the other template line, if the pair still works
        tl = (list(ctx["tmpl"].get("s3_seo") or []) + ["", ""])[:2]
        for idx, (n, ok, e) in enumerate((("s3_seo1", ok1, e1), ("s3_seo2", ok2, e2))):
            other = tl[1 - idx]                              # the template line it sits with
            pick = next(((i, s) for i, s in ok
                         if s.lower() != other.lower() and (has_t(s) or has_t(other))), None)
            if pick:
                out[n] = pick[1]
                notes.append(f"{n}: AI line used, the other search line is template")
            elif ok:
                why[n] = (f"no option works next to the template line {other!r}"
                          + (f" – keep '{t} min'" if t and not has_t(other) else ""))
            else:
                why[n] = e

    # slide 2: the 2 small lines – filled in HERE, main.py gets finished text
    values = {k: v["value"] for k, v in (data.get("slide2") or {}).items()}
    for name in MATH:
        errs = []
        for o in options(raw.get(name))[:N_OPTIONS]:
            ok, err = check_math(name, o, values)
            if ok:
                out[name] = fill(ok, values)
                break
            errs.append(f"{err}: {o!r}")
        else:
            why[name] = " | ".join(errs) or "empty"

    # slide 3: 3 bullets, each tied to its DATA.facts source (all 3 or none)
    src = data.get("facts") or {}
    pts, keys, perr = [], [], None
    raw_pts = raw.get("s3_points") if isinstance(raw.get("s3_points"), list) else []
    for n, p in enumerate(raw_pts[:3], 1):
        txt, k, err = check_point(p, src, ctx)
        if err:
            perr = f"bullet {n}: {err}"
            break
        if txt.lower() in (x.lower() for x in pts):
            perr = f"bullet {n}: same bullet twice: {txt!r}"
            break
        pts.append(txt)
        keys += [x for x in k if x not in keys]
    recent = [tuple(sorted(c)) for c in (rot.get("s3_points") or [])[:10] if isinstance(c, list)]
    if not perr and len(pts) < 3:
        perr = f"need 3 bullets (got {len(pts)})"
    if not perr and tuple(sorted(keys)) in recent:
        perr = f"same facts as a recent post {sorted(keys)} – pick other 'from' keys"
    if perr:
        why["s3_points"] = perr
    else:
        out["s3_points"], out["s3_kinds"] = pts, keys

    # angle only together with its headline
    if angle and "s3_headline" in out:
        out["angle"] = angle
    elif angle:
        why["angle"] = "dropped together with the headline (template angle kept)"
    elif a:
        notes.append(f"angle {a!r} not allowed (used last post / no rival) – template angle")

    tags = []
    for tg in raw.get("hashtags") or []:
        tg = str(tg).strip().lower()
        if re.fullmatch(r"#\w{2,40}", tg) and tg not in tags:
            tags.append(tg)
    if len(tags) >= 3:
        out["hashtags"] = tags[:MAX_TAGS]
    else:
        why["hashtags"] = f"need 3-{MAX_TAGS} hashtags (got {len(tags)})"
    return out, why, notes


# ─── rules as the model sees them ────────────────────────────────────
def slot_rule(k):
    c, w, job, ex = SLOTS[k]
    r = {"max_words": w, "max_chars": c, "job": job}
    if ex:
        r["example"], r["example_chars"] = ex, len(ex)   # real length, measured here
    r["answer"] = (f"list of {N_OPTIONS} options, longest to shortest"
                   if k in OPTION_SLOTS else "one string")
    return r

def point_rule():
    return {"bullets": 3, "max_words_each": POINT_WORDS, "max_chars_each": POINT_CHARS,
            "from": "1-2 keys of DATA.facts the bullet is based on",
            "text": f"list of {N_OPTIONS} options, longest to shortest",
            "example": POINT_EXAMPLE, "example_chars": len(POINT_EXAMPLE),
            "tip": "say the fact the way a person would say it out loud – a short, "
                   "complete phrase, not a label. A percentage may become '1 in 3' / 'a third'."}

def math_rule(n):
    r = MATH[n]
    return {"must_use": r["need"], "max_numbers": MAX_S2_NUMS,
            "max_chars_after_filling": r["max_chars"], "job": r["job"],
            "answer": f"list of {N_OPTIONS} options, numbers only as {{placeholders}}"}


# ─── prompt ──────────────────────────────────────────────────────────
def prompt(data, rot):
    last = (rot.get("s3_templates") or [None])[0]
    recent = [c for c in (rot.get("s3_points") or [])[:10] if isinstance(c, list)]
    return (
        "HOW TO HIT THE LENGTH (characters are hard to count, so do this):\n"
        "- max_words is the rule to follow. A line within max_words fits max_chars if the "
        "words are short: prefer 'min', 'a year', '$19K', drop 'very'. But keep the words "
        "that make it proper English.\n"
        f"- Fields marked 'list of {N_OPTIONS} options': {N_OPTIONS} complete versions, "
        "longest to shortest; the last one clearly short. We use the first one that passes, "
        "so EVERY option must follow all rules and read as natural English.\n"
        "- 'example' + 'example_chars' show what fits. They are from other houses: learn the "
        "length and the tone, don't copy the words.\n\n"
        "SLOTS:\n"
        + json.dumps({k: slot_rule(k) for k in SLOTS}, ensure_ascii=False, indent=1)
        + "\n\nSLIDE 2 (s2_line_a, s2_line_b): above these 2 small lines the slide already "
          "shows the payback years and the net per month in big type. The 2 lines explain "
          "it in plain English: line a what the money buys, line b how the rent becomes a "
          "yearly net. Write phrases a person would say – NOT the calculation (no ×, no =, "
          "no list of every number; the full math is in the newsletter). NEVER type a digit: "
          "every number is a {placeholder} from DATA.slide2 (its value and meaning are shown "
          "there), each at most once, max 3 per line. Good shapes (other house, don't copy): "
          "'{house} for the house, {all_in} once it's fixed up' / 'Books ~{booked_nights} "
          "nights a year, {net_month} a month net'.\n"
        + json.dumps({n: math_rule(n) for n in MATH}, ensure_ascii=False, indent=1)
        + "\n\nSLIDE 3: the title (town name) and subtitle (DATA.slide3_subtitle) are fixed "
          "and already shown. Under them: headline (the hook), 3 bullets (s3_points), 2 search "
          "lines (s3_seo1, s3_seo2) and the button (s3_cta), all in your own words. Bullets say "
          "only what their DATA.facts say and keep their number (copied exactly, or a "
          "percentage as '1 in 3' / 'a third'). Never base a bullet on a fact that is already "
          "in DATA.slide3_subtitle. The bullets must pay off the headline. Don't use exactly "
          f"these sets of 'from' keys (recent posts): {json.dumps(recent)}. Search lines: the "
          "town + a search word people type, as a natural phrase, no stuffing, the two lines "
          "on different searches, and at least one keeps the drive minutes from DATA.trip. "
          "Only name places that are in DATA; the rival town is a comparison, never 'near'. "
          "The button gives one reason to tap that fits THIS house.\n"
          "BULLETS:\n" + json.dumps(point_rule(), ensure_ascii=False, indent=1)
        + "\n\nangle: one of " + json.dumps(ANGLES)
        + f" – not '{last}' (used last post). D only if famous_rival_town exists, and then "
          "the headline must name it."
        + f"\nhashtags: {MAX_TAGS} max, include #akiya, one town/area tag, "
          "one attraction-kind tag."
        + "\n\nDATA:\n" + json.dumps(data, ensure_ascii=False, indent=1)
        + "\n\nOUR PAST POSTS (score = follows/shares/saves per reach; "
          "learn the pattern, don't copy):\n" + json.dumps(evidence(), ensure_ascii=False)
        + "\n\nAnswer in this json shape:\n" + json.dumps(EXAMPLE, ensure_ascii=False))


# ─── repair round ────────────────────────────────────────────────────
FIXABLE = set(SLOTS) | set(MATH) | {"s3_points", "hashtags"}

def repair(user, raw, why, data):
    """2nd call: DeepSeek rewrites ONLY the fields that failed, told exactly why."""
    bad = {k: v for k, v in why.items() if k in FIXABLE}
    if not bad:
        return None
    rules = {}
    for k in bad:
        if k in SLOTS:
            rules[k] = slot_rule(k)
        elif k in MATH:
            rules[k] = math_rule(k)
        elif k == "s3_points":
            rules[k] = point_rule()
        else:
            rules[k] = {"count": f"3-{MAX_TAGS}",
                        "include": "#akiya, one town/area tag, one attraction-kind tag"}
    ask = ("Our checker rejected these fields of your answer. Rewrite ONLY them, same DATA "
           "and rules, in natural native English. Stay under max_words; in lists make the "
           "last option clearly short.\n"
           "WHY REJECTED:\n" + json.dumps(bad, ensure_ascii=False, indent=1)
           + "\n\nRULES:\n" + json.dumps(rules, ensure_ascii=False, indent=1)
           + ("\n\nWith a new s3_headline you may also give a new 'angle'."
              if "s3_headline" in bad else "")
           + "\n\nAnswer ONLY json with these keys: " + json.dumps(sorted(bad)))
    new = call([{"role": "system", "content": SYSTEM},
                {"role": "user", "content": user},
                {"role": "assistant", "content": json.dumps(raw, ensure_ascii=False)},
                {"role": "user", "content": ask}], temp=min(TEMP, FIX_TEMP))
    if not new:
        print("  AI copy: repair round got no answer")
        return None
    keep = set(bad) | ({"angle"} if "s3_headline" in bad else set())
    return {**raw, **{k: v for k, v in new.items() if k in keep}}


# ─── v5: editor pass (AI_EDIT=1) ─────────────────────────────────────
def polish(user, raw, data, rot, tmpl):
    """One extra call: DeepSeek re-reads its answer as a native English editor.
    An edited field is used only if it still passes every check; else the old one stays."""
    ask = ("Now act as a native English Instagram editor. Read every line of your answer "
           "out loud. Rewrite any line that doesn't sound like natural spoken English "
           "(odd word order, missing little words, numbers crammed into noun phrases, "
           "'label: number' fragments). Keep lines that already read well exactly as they "
           "are. Keep every number, {placeholder}, place name and 'from' key exactly, and "
           "stay within the same limits. Return the SAME json shape with the same keys.")
    new = call([{"role": "system", "content": SYSTEM},
                {"role": "user", "content": user},
                {"role": "assistant", "content": json.dumps(raw, ensure_ascii=False)},
                {"role": "user", "content": ask}], temp=FIX_TEMP)
    if not new:
        print("  AI copy: editor pass got no answer – kept the first version")
        return raw
    new = {**raw, **{k: v for k, v in new.items() if k in raw}}
    out2, _, _ = validate(new, data, rot, tmpl)
    merged, changed = dict(raw), []
    for k in out2:
        if k in ("s3_kinds", "s3_seo1", "s3_seo2", "angle"):
            continue
        if k in new and new[k] != raw.get(k):
            merged[k] = new[k]
            changed.append(k)
    if "s3_headline" in changed:
        merged["angle"] = new.get("angle", raw.get("angle"))
    if "s3_seo1" in out2 and "s3_seo2" in out2:          # both pass = a valid pair
        for k in ("s3_seo1", "s3_seo2"):
            if new.get(k) != raw.get(k):
                merged[k] = new[k]
                changed.append(k)
    print(f"  AI copy: editor pass changed {sorted(set(changed)) or 'nothing'}")
    return merged


# ─── entry point ─────────────────────────────────────────────────────
def write(M, h, l, hooks, usd, e, facts, s3, rot):
    """Returns {slot: text} with only the fields that passed every check."""
    if not ON:
        print("AI copy: off (no DEEPSEEK_API_KEY or AI_COPY=0) – template text")
        return {}
    rot = rot or {}
    day = datetime.now(M.JST).strftime("%Y-%m-%d")
    cache = load(CACHE_FILE)
    ck = f"{day}|{l['url']}|{VERSION}"
    data = data_for(M, h, l, hooks, usd, e, facts, s3)
    tmpl = templates_of(s3)
    if ck in cache:
        raw = cache[ck]                                  # re-run today: no new call
        print("  AI copy: cached answer from today (no new call)")
        out, why, notes = validate(raw, data, rot, tmpl)
    else:
        user = prompt(data, rot)
        raw = call([{"role": "system", "content": SYSTEM},
                    {"role": "user", "content": user}])
        if raw is None:
            print("AI copy: no answer – template text")
            return {}
        out, why, notes = validate(raw, data, rot, tmpl)
        bad = sorted(k for k in why if k in FIXABLE)
        if bad:
            print(f"  AI copy: 1st answer failed {bad} – asking DeepSeek to fix them")
            fixed = repair(user, raw, why, data)
            if fixed:
                raw = fixed
                out, why, notes = validate(raw, data, rot, tmpl)
        if EDIT:
            raw = polish(user, raw, data, rot, tmpl)
            out, why, notes = validate(raw, data, rot, tmpl)
        cache = {k: v for k, v in cache.items() if k.startswith(day)}
        cache[ck] = raw                                  # the final answer is cached
        save(CACHE_FILE, cache)

    for n in notes:
        print(f"  AI copy: {n}")
    for k, v in why.items():
        print(f"  AI copy: {k} dropped – template used ({v})")
    print(f"AI copy ({MODEL}): kept {sorted(out)}")
    seo = "/".join("AI" if k in out else "template" for k in ("s3_seo1", "s3_seo2"))
    print("  slide 3 source: "
          f"headline={'AI' if 's3_headline' in out else 'template'} | "
          f"bullets={'AI' if 's3_points' in out else 'template'} | search lines={seo} | "
          f"button={'AI' if 's3_cta' in out else 'template'}")
    print("  slide 2 source: "
          f"line a={'AI' if 's2_line_a' in out else 'template'} | "
          f"line b={'AI' if 's2_line_b' in out else 'template'}")
    for k in ("s2_line_a", "s2_line_b", "s3_cta"):
        if k in out:
            print(f"    {k}: {out[k]}")
    return out

def apply_s3(s3, c):
    """Puts the accepted AI wording into the slide 3 dict (template text stays otherwise).
    STR(s) -> Airbnb(s) is applied to the template text too."""
    s3 = dict(s3)
    s3["headline"] = no_str(s3.get("headline", ""))
    s3["points"] = [no_str(p) if isinstance(p, str) else p for p in s3.get("points", [])]
    s3["lines"] = [no_str(x) if isinstance(x, str) else x for x in s3.get("lines", [])]
    if c.get("angle"):
        s3["template"] = c["angle"]
    if c.get("s3_headline"):
        s3["headline"] = c["s3_headline"]
    if c.get("s3_points"):
        s3["points"], s3["kinds"] = c["s3_points"], c["s3_kinds"]
    if c.get("s3_seo1") or c.get("s3_seo2"):
        lines = (list(s3["lines"]) + ["", ""])[:2]
        if c.get("s3_seo1"):
            lines[0] = c["s3_seo1"]
        if c.get("s3_seo2"):
            lines[1] = c["s3_seo2"]
        s3["lines"] = lines
    s3["copy"] = c                                       # main.py pick_cta reads c["s3_cta"]
    return s3
