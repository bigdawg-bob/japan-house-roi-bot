"""
Daily akiya bot:
scrape -> closest to ski resorts -> AirROI revenue -> ROI -> Grok copy
-> render slides -> send to Telegram

Local run:  python main.py
Test mode:  TEST_RUN=true python main.py  (sends, but doesn't mark the house as posted)
"""
import json, math, os, re, sys, time
from datetime import date
from pathlib import Path

import requests

import scraper
from render import render

# ─── Settings you can change ─────────────────────────────────────────
MAX_PRICE_YEN       = 15_000_000   # ignore houses above this
MIN_YIELD           = 0.08         # post nothing if the best deal is under 8% net
NIGHTS_CAP          = 180          # minpaku law: max 180 nights/year
EXPENSE_RATIO       = 0.40         # cleaning, platform fees, utilities, mgmt, tax
RENO_YEN_PER_M2     = 50_000       # renovation guess, built 1981 or later
RENO_YEN_PER_M2_OLD = 80_000       # built before 1981 (old earthquake code)
DEFAULT_AREA_M2     = 90
TOP_N_FOR_AIRROI    = 10           # only the 10 closest houses use AirROI credits
CACHE_DAYS          = 30
GROK_MODEL          = os.getenv("GROK_MODEL", "grok-3-mini")
HANDLE              = os.getenv("IG_HANDLE", "@yourhandle")
TEST_RUN            = os.getenv("TEST_RUN", "false").lower() == "true"

# (name, lat, lng) - approximate resort base locations. Add your own.
HOOKS = [
    ("Happo-One",     36.698, 137.832),
    ("Hakuba Goryu",  36.667, 137.829),
    ("Niseko Hirafu", 42.859, 140.704),
    ("Rusutsu",       42.748, 140.898),
    ("Nozawa Onsen",  36.923, 138.447),
    ("Myoko Akakura", 36.883, 138.180),
    ("Shiga Kogen",   36.720, 138.500),
    ("Furano",        43.335, 142.354),
    ("Zao Onsen",     38.166, 140.400),
]

ROOT      = Path(__file__).parent
STATE     = ROOT / "state"
TEMPLATES = ROOT / "templates"
OUT       = ROOT / "out" / date.today().isoformat()
AIRROI_URL = "https://api.airroi.com/calculator/estimate"


# ─── Small helpers ───────────────────────────────────────────────────
def load(name, default):
    p = STATE / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default

def save(name, data):
    STATE.mkdir(exist_ok=True)
    (STATE / name).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

def to_num(v):
    """'1,000万円' -> 10000000, '3LDK' -> 3, 98.5 -> 98.5"""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace(",", "")
    m = re.search(r"\d+(\.\d+)?", s)
    if not m:
        return None
    n = float(m.group(0))
    if "万" in s:
        n *= 10_000
    return n

def pick(d, *keys):
    for k in keys:
        if d.get(k) not in (None, "", []):
            return d[k]
    return None

def km_between(lat1, lng1, lat2, lng2):
    r = math.radians
    a = (math.sin(r(lat2 - lat1) / 2) ** 2
         + math.cos(r(lat1)) * math.cos(r(lat2)) * math.sin(r(lng2 - lng1) / 2) ** 2)
    return 6371 * 2 * math.asin(math.sqrt(a))

def nearest_hook(lat, lng):
    return min(((name, km_between(lat, lng, hl, hg)) for name, hl, hg in HOOKS),
               key=lambda x: x[1])

def fmt_usd(v):  return f"${v/1000:.0f}K" if v >= 1000 else f"${v:.0f}"
def fmt_yen(v):  return f"¥{v/1e6:.1f}M" if v >= 1e6 else f"¥{v/1e3:.0f}K"
def fmt_dist(km): return f"{km*1000:.0f} m" if km < 1 else f"{km:.0f} km"

def usd_per_yen():
    try:
        r = requests.get("https://open.er-api.com/v6/latest/JPY", timeout=15)
        return float(r.json()["rates"]["USD"])
    except Exception:
        print("FX lookup failed, using 1 USD = 150 JPY")
        return 1 / 150


# ─── Scraper adapter ─────────────────────────────────────────────────
def normalize(d):
    if not isinstance(d, dict):
        d = vars(d)
    photos = pick(d, "photos", "images", "image_urls", "photo_urls", "photo", "image", "thumbnail", "img")
    if isinstance(photos, str):
        photos = [photos]
    year = to_num(pick(d, "year_built", "built", "year", "built_year"))
    beds = to_num(pick(d, "bedrooms", "rooms", "layout", "madori"))
    return {
        "id":        str(pick(d, "id", "listing_id", "url", "link")),
        "url":       pick(d, "url", "link", "detail_url") or "",
        "price_yen": to_num(pick(d, "price_yen", "price", "price_jpy")),
        "lat":       to_num(pick(d, "lat", "latitude")),
        "lng":       to_num(pick(d, "lng", "lon", "longitude")),
        "bedrooms":  int(min(max(beds or 2, 1), 8)),
        "area_m2":   to_num(pick(d, "area_m2", "floor_area", "building_area", "area", "size")),
        "year":      int(year) if year and 1900 < year < 2100 else None,
        "town":      str(pick(d, "location", "town", "address", "city", "prefecture") or "Japan"),
        "photos":    [p for p in (photos or []) if isinstance(p, str) and p.startswith("http")],
    }

def get_listings():
    for fn in ("scrape", "get_listings", "run", "main"):
        if hasattr(scraper, fn):
            try:
                data = getattr(scraper, fn)()
            except TypeError as e:
                sys.exit(f"scraper.{fn}() needs arguments ({e}). Send me scraper.py and I'll wire it up.")
            return [normalize(x) for x in (data or [])]
    sys.exit("scraper.py has no scrape() / get_listings() function. Send it to me and I'll wire it up.")


# ─── AirROI ──────────────────────────────────────────────────────────
_printed_raw = False

def find_num(obj, names):
    """Find the first number under any of `names`, searching nested JSON."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() in names:
                if isinstance(v, (int, float)):
                    return float(v)
                if isinstance(v, dict):
                    for sub in ("p50", "median", "avg", "mean", "value"):
                        if isinstance(v.get(sub), (int, float)):
                            return float(v[sub])
        for v in obj.values():
            f = find_num(v, names)
            if f is not None:
                return f
    elif isinstance(obj, list):
        for v in obj:
            f = find_num(v, names)
            if f is not None:
                return f
    return None

def airroi(l, cache, fx):
    global _printed_raw
    key = f'{l["lat"]:.3f},{l["lng"]:.3f},{l["bedrooms"]}'
    hit = cache.get(key)
    if hit and time.time() - hit["t"] < CACHE_DAYS * 86400:
        return hit["adr"], hit["occ"]

    r = requests.get(
        AIRROI_URL,
        headers={"x-api-key": os.environ["AIRROI_API_KEY"]},
        params={"lat": l["lat"], "lng": l["lng"], "bedrooms": l["bedrooms"],
                "baths": 1, "guests": max(2, l["bedrooms"] * 2), "currency": "usd"},
        timeout=30,
    )
    if r.status_code != 200:
        print(f"AirROI error {r.status_code}: {r.text[:400]}")
        return None
    data = r.json()
    if not _printed_raw:
        print("AirROI raw response (first call):", json.dumps(data)[:1200])
        _printed_raw = True

    adr = find_num(data, {"average_daily_rate", "adr", "avg_daily_rate", "daily_rate"})
    occ = find_num(data, {"occupancy", "occupancy_rate", "avg_occupancy"})
    if adr is None or occ is None:
        print("AirROI: couldn't find ADR/occupancy in the response above")
        return None
    if occ > 1:
        occ /= 100                 # 55 -> 0.55
    if adr > 3000:
        adr *= fx                  # looks like yen, not USD -> convert
    cache[key] = {"t": time.time(), "adr": adr, "occ": occ}
    return adr, occ


# ─── ROI ─────────────────────────────────────────────────────────────
def roi(l, adr, occ, fx):
    nights = min(occ * 365, NIGHTS_CAP)
    gross = adr * nights
    net = gross * (1 - EXPENSE_RATIO)
    per_m2 = RENO_YEN_PER_M2_OLD if (l["year"] or 1970) < 1981 else RENO_YEN_PER_M2
    reno_yen = (l["area_m2"] or DEFAULT_AREA_M2) * per_m2
    total = (l["price_yen"] + reno_yen) * fx
    return {"adr": adr, "occ": occ, "nights": round(nights), "gross": gross, "net": net,
            "price_usd": l["price_yen"] * fx, "reno_usd": reno_yen * fx,
            "total_usd": total, "yield": net / total if total else 0}


# ─── Grok ────────────────────────────────────────────────────────────
def grok_copy(facts, fallback_hook):
    key = os.getenv("GROK_API_KEY")
    if not key:
        return fallback_hook, "", ""
    prompt = f"""You write Instagram copy for a page about cheap Japanese akiya
(vacant houses) that could become Airbnbs. Use ONLY these facts, invent nothing:
{facts}

Reply with JSON only:
{{"hook": "max 28 characters, punchy, no numbers or prices",
  "caption": "3-5 short lines, casual, 1-2 emojis",
  "hashtags": "8 hashtags on one line"}}"""
    try:
        r = requests.post(
            "https://api.x.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": GROK_MODEL, "temperature": 0.8,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=90,
        )
        if r.status_code != 200:
            print(f"Grok error {r.status_code}: {r.text[:300]}")
            return fallback_hook, "", ""
        text = r.json()["choices"][0]["message"]["content"]
        data = json.loads(re.search(r"\{.*\}", text, re.S).group(0))
        hook = (data.get("hook") or "").strip()
        if not hook or len(hook) > 40:
            hook = fallback_hook
        return hook, (data.get("caption") or "").strip(), (data.get("hashtags") or "").strip()
    except Exception as e:
        print(f"Grok failed ({e}), using fallback text")
        return fallback_hook, "", ""


# ─── Photo + Telegram ────────────────────────────────────────────────
def download_photo(urls):
    for u in urls[:5]:
        try:
            r = requests.get(u, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
            if r.ok and r.headers.get("content-type", "").startswith("image"):
                p = TEMPLATES / "_photo.jpg"
                p.write_bytes(r.content)
                return p
        except Exception:
            pass
    return None

def tg(method, **kw):
    r = requests.post(f"https://api.telegram.org/bot{os.environ['BOT_TOKEN']}/{method}",
                      timeout=120, **kw)
    if not r.ok:
        print(f"Telegram {method} error: {r.text[:300]}")
    r.raise_for_status()

def send_to_telegram(paths, caption):
    chat = os.environ["CHAT_ID"]
    if len(paths) == 1:
        with open(paths[0], "rb") as f:
            tg("sendPhoto", data={"chat_id": chat}, files={"photo": f})
    else:
        files = {f"p{i}": open(p, "rb") for i, p in enumerate(paths[:10])}
        media = [{"type": "photo", "media": f"attach://p{i}"} for i in range(len(files))]
        try:
            tg("sendMediaGroup", data={"chat_id": chat, "media": json.dumps(media)}, files=files)
        finally:
            for f in files.values():
                f.close()
    tg("sendMessage", data={"chat_id": chat, "text": caption[:4000]})


# ─── Main ────────────────────────────────────────────────────────────
def main():
    for var in ("AIRROI_API_KEY", "BOT_TOKEN", "CHAT_ID"):
        if not os.getenv(var):
            sys.exit(f"Missing secret: {var}")

    posted = load("posted.json", [])
    cache = load("airroi_cache.json", {})
    fx = usd_per_yen()

    listings = get_listings()
    print(f"Scraped {len(listings)} listings{' (TEST RUN)' if TEST_RUN else ''}")

    cands = []
    for l in listings:
        if l["id"] in posted or not l["price_yen"] or l["price_yen"] > MAX_PRICE_YEN:
            continue
        if l["lat"] is None or l["lng"] is None:
            continue
        name, km = nearest_hook(l["lat"], l["lng"])
        cands.append((km, name, l))
    cands.sort(key=lambda c: c[0])
    print(f"{len(cands)} candidates with price + coordinates")

    scored = []
    try:
        for km, name, l in cands[:TOP_N_FOR_AIRROI]:
            est = airroi(l, cache, fx)
            if not est:
                continue
            r = roi(l, *est, fx)
            print(f"  {l['town'][:24]:24} {fmt_yen(l['price_yen']):>7}  "
                  f"{fmt_dist(km):>6} to {name:14} ADR ${r['adr']:.0f}  "
                  f"occ {r['occ']:.0%}  yield {r['yield']:.1%}")
            scored.append((r["yield"], km, name, l, r))
    finally:
        save("airroi_cache.json", cache)

    scored.sort(key=lambda s: s[0], reverse=True)
    for y, km, name, l, r in scored:
        if y < MIN_YIELD:
            break
        photo = download_photo(l["photos"])
        if not photo:
            print(f"No usable photo for {l['id']}, trying next")
            continue

        specs = " · ".join(x for x in [
            f"{l['bedrooms']}BR",
            f"Built {l['year']}" if l["year"] else "",
            f"{l['area_m2']:.0f} m²" if l["area_m2"] else "",
        ] if x)
        facts = (f"- Location: {l['town']}\n- Price: {fmt_usd(r['price_usd'])} ({fmt_yen(l['price_yen'])})\n"
                 f"- {fmt_dist(km)} to {name} ski resort\n- {specs}\n"
                 f"- Estimated net yield {y:.1%} (180-night cap)")
        hook, cap, tags = grok_copy(facts, f"Ski house near {name}")

        data = {
            "photo": photo.name, "hook": hook,
            "sub_hook": f"{fmt_dist(km)} to {name} · {y:.0%} yield est.",
            "price_usd": fmt_usd(r["price_usd"]), "price_yen": fmt_yen(l["price_yen"]),
            "specs": specs, "location": l["town"], "handle": HANDLE,
            "roi": r, "km": km, "hook_name": name, "listing": l,
        }
        slides = [(t, data) for t in ("cover.html", "location.html", "roi.html", "cta.html")
                  if (TEMPLATES / t).exists()]
        try:
            paths = render(slides, OUT)
        finally:
            photo.unlink(missing_ok=True)

        caption = "\n\n".join(x for x in [
            cap,
            f"📍 {l['town']}\n💴 {fmt_yen(l['price_yen'])} (~{fmt_usd(r['price_usd'])})\n"
            f"🛠 Reno est. {fmt_usd(r['reno_usd'])}\n"
            f"📈 Est. net {fmt_usd(r['net'])}/yr · {y:.1%} yield "
            f"({r['nights']} nights, {EXPENSE_RATIO:.0%} costs)\n🔗 {l['url']}",
            "Estimates only, not financial advice.",
            tags,
        ] if x)
        (OUT / "caption.txt").write_text(caption, encoding="utf-8")

        send_to_telegram(paths, ("🧪 TEST\n\n" if TEST_RUN else "") + caption)
        if not TEST_RUN:
            posted.append(l["id"])
            save("posted.json", posted)
        print(f"Sent {len(paths)} slides for {l['id']}")
        return

    msg = f"No deal ≥ {MIN_YIELD:.0%} yield today ({len(scored)} checked)."
    print(msg)
    tg("sendMessage", data={"chat_id": os.environ["CHAT_ID"], "text": msg})


if __name__ == "__main__":
    main()
