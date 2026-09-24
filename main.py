"""
Daily "cheap Japanese house near a ski resort / onsen / sight / beach / nature" bot.
Flow: scrape the enabled sites (SOURCES) -> merge + remove duplicates
      -> keep houses under MAX_PRICE_USD roughly near a hook (straight-line pre-filter)
      -> real ROAD drive times for the best-located houses (Google Routes API if
         GOOGLE_MAPS_KEY is set, otherwise free OSRM / OpenStreetMap), cached
      -> keep houses within MAX_DRIVE_MIN by road, rank by LOCATION first
      -> for the best-located houses: read yearly fees (skip if > 15% of price)
         and get an AirROI estimate (cached). No AirROI data -> skip.
      -> yield (yield_calc.py): AirROI rate x occupancy x 180 nights, minus 30%
         management and yearly fees, divided by house + reno + buying fees
      -> final score = attraction points + yield points (-10..+15) + build-year points
         (so location always matters most)
      -> 1080x1350 slides -> Telegram (album + copyable caption).
"""
import io, json, math, os, re, textwrap, time
from datetime import datetime, timezone, timedelta
from functools import lru_cache
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont, ImageOps
from PIL import features as pil_features

import scraper
import scraper_athome
import scraper_homes
from yield_calc import (estimate, yield_points, caption_text, is_renovated,
                        show_yield, fmt_k)

# ─── settings ────────────────────────────────────────────────────────
def _env(name, default):
    return os.getenv(name) or default                 # empty string -> default

HANDLE           = "@yama.yield"                      # top-left text on the cover
MAX_PRICE_USD    = float(_env("MAX_PRICE_USD", "100000"))
MAX_DRIVE_MIN    = float(_env("MAX_DRIVE_MIN", "45"))
MAX_WALK_MIN     = float(_env("MAX_WALK_MIN", "15"))
FEE_LIMIT        = float(_env("FEE_LIMIT_PCT", "15")) / 100   # yearly fees vs price
MAX_AIRROI_CALLS = int(_env("MAX_AIRROI_CALLS", "15"))        # paid calls per run
CACHE_DAYS       = 90                                          # re-ask AirROI after this
MAX_PHOTOS       = 5
W, H             = 1080, 1350
FX_FALLBACK      = 0.0067
DRY_RUN          = os.getenv("DRY_RUN") == "1"
ENABLED          = set(_env("HOOK_KINDS", "ski,onsen,sight,beach,nature")
                       .replace(" ", "").split(","))
SITES_ON         = [s for s in _env("SOURCES", "sumai,athome,homes")
                    .replace(" ", "").split(",") if s]
ROTATE           = _env("ROTATE_SOURCES", "1") == "1"

# ranking: location first, yield and build year as adjustments
MIN_YIELD        = float(_env("MIN_YIELD_PCT", "0"))   # skip houses with net yield below this
MAX_CHECK        = int(_env("MAX_CHECK", "40"))        # top-located houses to fully check
RECENT_HOOKS     = int(_env("RECENT_HOOKS", "5"))      # avoid repeating these attractions
REPEAT_PENALTY   = float(_env("REPEAT_PENALTY", "0"))  # points off for a recently used attraction (0 = off)
CLOSE_PTS        = 60                                  # points for a house right next to the hook
FAMOUS_BONUS     = 20
EXTRA_HOOK_PTS   = 5                                   # per extra attraction nearby
MAX_EXTRA_HOOKS  = 4

# build year: 1981 = new earthquake standard (新耐震), 2000 = stricter standard
AGE_BANDS        = [(2000, 5), (1981, 0), (1960, -5), (1940, -8)]   # (built from, points)
AGE_OLDEST_PTS   = -10                                 # built before 1940
AGE_UNKNOWN_PTS  = float(_env("AGE_UNKNOWN_PTS", "-5"))  # no build year in the listing

def _weights(s):
    out = {}
    for part in s.replace(" ", "").split(","):
        k, _, v = part.partition("=")
        try:
            out[k] = float(v)
        except ValueError:
            pass
    return out

KIND_WEIGHT = _weights(_env("HOOK_PRIORITY", "ski=1.0,onsen=1.0,sight=0.9,beach=0.9,nature=0.8"))

# ── travel time ──
# Real road times: Google Routes API (if GOOGLE_MAPS_KEY) or OSRM (free, OpenStreetMap).
ROUTER        = _env("ROUTER", "auto").lower()          # auto | google | osrm | none
GOOGLE_KEY    = os.getenv("GOOGLE_MAPS_KEY")
OSRM_URL      = _env("OSRM_URL", "https://router.project-osrm.org").rstrip("/")
OSRM_SLOWDOWN = float(_env("OSRM_SLOWDOWN", "1.2"))     # OSRM is usually a bit faster than Google
ROUTE_TOP     = int(_env("ROUTE_TOP", "60"))            # new routing requests per run
GOOGLE_CACHE_DAYS = 30                                   # Google terms: don't keep results long
OSRM_CACHE_DAYS   = 365
# Fallback estimate (routing failed / not routed): deliberately cautious for mountain roads.
ROAD_FACTOR   = 1.4       # roads are ~40% longer than a straight line
DRIVE_KMH     = 32        # average speed on rural / mountain roads
WALK_KMH      = 4.8
PREFILTER     = 1.5       # keep hooks up to 1.5 x MAX_DRIVE_MIN (estimate) until routed
WALK_KM       = MAX_WALK_MIN / 60 * WALK_KMH                 # ≈ 1.2 km by road

ROOT          = Path(__file__).parent
STATE         = ROOT / "state"
POSTED_FILE   = STATE / "posted.json"
AIRROI_CACHE  = STATE / "airroi_cache.json"
FEE_CACHE     = STATE / "fees_cache.json"
ROUTE_CACHE   = STATE / "routes_cache.json"
ROTATION_FILE = STATE / "rotation.json"
OUT           = ROOT / "out"
JST           = timezone(timedelta(hours=9))

BOT_TOKEN   = os.getenv("BOT_TOKEN")
CHAT_ID     = os.getenv("CHAT_ID")
AIRROI_KEY  = os.getenv("AIRROI_API_KEY") or os.getenv("AIRROI_KEY")
AIRROI_URL  = _env("AIRROI_URL", "https://api.airroi.com/calculator/estimate")

SITE_MODULES = {"sumai": scraper, "athome": scraper_athome, "homes": scraper_homes}
# source -> (caption text, photo credit)
SITE_INFO = {
    "sumai":  ("Sumai空き家 / local akiya bank", "akiya.sumai.biz"),
    "athome": ("At Home 空き家バンク (national akiya bank)", "akiya-athome.jp"),
    "homes":  ("LIFULL HOME'S 空き家バンク (national akiya bank)", "homes.co.jp"),
}

def site_info(l):
    return SITE_INFO.get(l.get("source"), SITE_INFO["sumai"])

KINDS = {
    "ski":    {"label": "ski resort", "emoji": "🏔", "banner": "JAPAN SKI AKIYA",
               "tags": "#skijapan #japow"},
    "onsen":  {"label": "onsen town", "emoji": "♨️", "banner": "JAPAN ONSEN AKIYA",
               "tags": "#onsen #温泉 #hotspring"},
    "sight":  {"label": "famous sight", "emoji": "⛩", "banner": "RURAL JAPAN AKIYA",
               "tags": "#ruraljapan #visitjapan"},
    "beach":  {"label": "beach", "emoji": "🏖", "banner": "JAPAN BEACH AKIYA",
               "tags": "#japanbeach #beachhouse"},
    "nature": {"label": "nature spot", "emoji": "🌲", "banner": "JAPAN NATURE AKIYA",
               "tags": "#japannature #countryside"},
}

# (kind, name, lat, lng) – approximate centre coordinates (starter list)
HOOKS = [
    # ── ski resorts ──
    ("ski", "Niseko Grand Hirafu", 42.862, 140.698),
    ("ski", "Rusutsu",             42.748, 140.555),
    ("ski", "Kiroro",              43.075, 140.985),
    ("ski", "Furano",              43.332, 142.358),
    ("ski", "Hakkoda",             40.656, 140.858),
    ("ski", "APPI Kogen",          40.003, 140.966),
    ("ski", "Kazuno Hanawa",       40.190, 140.750),
    ("ski", "Tazawako",            39.752, 140.726),
    ("ski", "Zao Onsen",           38.166, 140.415),
    ("ski", "Gassan",              38.528, 140.020),
    ("ski", "Aizu Takatsue",       37.117, 139.563),
    ("ski", "Oze Iwakura",         36.820, 139.200),
    ("ski", "Minakami",            36.830, 138.930),
    ("ski", "GALA Yuzawa",         36.947, 138.804),
    ("ski", "Naeba",               36.790, 138.760),
    ("ski", "Myoko Akakura",       36.887, 138.172),
    ("ski", "Madarao Kogen",       36.863, 138.297),
    ("ski", "Nozawa Onsen",        36.922, 138.444),
    ("ski", "Shiga Kogen",         36.707, 138.508),
    ("ski", "Hakuba Happo-one",    36.700, 137.832),
    ("ski", "Hakuba Goryu",        36.670, 137.830),
    ("ski", "Hakuba Cortina",      36.797, 137.853),
    ("ski", "Hida Nagareha",       36.325, 137.330),
    # ── onsen towns ──
    ("onsen", "Noboribetsu Onsen", 42.495, 141.146),
    ("onsen", "Jozankei Onsen",    42.967, 141.163),
    ("onsen", "Toyako Onsen",      42.567, 140.817),
    ("onsen", "Nyuto Onsen",       39.805, 140.773),
    ("onsen", "Ginzan Onsen",      38.570, 140.530),
    ("onsen", "Nasu Onsen",        37.090, 139.960),
    ("onsen", "Kinugawa Onsen",    36.830, 139.720),
    ("onsen", "Kusatsu Onsen",     36.620, 138.596),
    ("onsen", "Ikaho Onsen",       36.497, 138.922),
    ("onsen", "Shibu Onsen",       36.735, 138.425),
    ("onsen", "Bessho Onsen",      36.356, 138.157),
    ("onsen", "Hakone Yumoto",     35.233, 139.105),
    ("onsen", "Atami",             35.096, 139.071),
    ("onsen", "Shuzenji Onsen",    34.970, 138.927),
    ("onsen", "Gero Onsen",        35.806, 137.244),
    ("onsen", "Okuhida Onsen",     36.230, 137.560),
    ("onsen", "Wakura Onsen",      37.090, 136.915),
    ("onsen", "Yamanaka Onsen",    36.247, 136.374),
    ("onsen", "Kinosaki Onsen",    35.626, 134.810),
    ("onsen", "Arima Onsen",       34.797, 135.247),
    ("onsen", "Shirahama Onsen",   33.680, 135.345),
    ("onsen", "Misasa Onsen",      35.411, 133.881),
    ("onsen", "Tamatsukuri Onsen", 35.420, 133.011),
    ("onsen", "Dogo Onsen",        33.852, 132.786),
    ("onsen", "Beppu",             33.280, 131.500),
    ("onsen", "Yufuin",            33.265, 131.355),
    ("onsen", "Kurokawa Onsen",    33.077, 131.141),
    ("onsen", "Unzen Onsen",       32.760, 130.263),
    ("onsen", "Ureshino Onsen",    33.100, 129.990),
    ("onsen", "Kirishima Onsen",   31.870, 130.850),
    ("onsen", "Ibusuki",           31.230, 130.640),
    # ── famous sights ──
    ("sight", "Kakunodate",              39.595, 140.562),
    ("sight", "Hiraizumi",               38.990, 141.120),
    ("sight", "Nikko",                   36.750, 139.600),
    ("sight", "Karuizawa",               36.343, 138.635),
    ("sight", "Lake Kawaguchiko (Fuji)", 35.500, 138.768),
    ("sight", "Matsumoto Castle",        36.239, 137.969),
    ("sight", "Shirakawa-go",            36.257, 136.906),
    ("sight", "Takayama old town",       36.141, 137.252),
    ("sight", "Tsumago (Kiso Valley)",   35.578, 137.596),
    ("sight", "Kanazawa",                36.560, 136.660),
    ("sight", "Miyama Thatched Village", 35.316, 135.562),
    ("sight", "Amanohashidate",          35.570, 135.190),
    ("sight", "Himeji Castle",           34.839, 134.694),
    ("sight", "Koyasan",                 34.213, 135.586),
    ("sight", "Ise Grand Shrine",        34.455, 136.725),
    ("sight", "Kumano Hongu Taisha",     33.835, 135.772),
    ("sight", "Izumo Taisha",            35.402, 132.685),
    ("sight", "Miyajima",                34.296, 132.320),
    ("sight", "Naoshima",                34.460, 133.995),
    # ── beaches ──
    ("beach", "Kujukuri Beach",           35.530, 140.450),
    ("beach", "Onjuku Beach",             35.183, 140.353),
    ("beach", "Hayama / Zushi",           35.270, 139.580),
    ("beach", "Shirahama Beach (Izu)",    34.690, 138.980),
    ("beach", "Chirihama",                36.900, 136.760),
    ("beach", "Kotohikihama",             35.700, 135.030),
    ("beach", "Takeno Beach",             35.660, 134.760),
    ("beach", "Shirarahama (Wakayama)",   33.679, 135.342),
    ("beach", "Katsurahama",              33.497, 133.575),
    ("beach", "Itoshima",                 33.600, 130.200),
    ("beach", "Aoshima (Miyazaki)",       31.800, 131.470),
    ("beach", "Amami Oshima",             28.400, 129.470),
    ("beach", "Onna coast (Okinawa)",     26.500, 127.850),
    ("beach", "Emerald Beach (Motobu)",   26.694, 127.878),
    ("beach", "Miyakojima",               24.800, 125.280),
    ("beach", "Kabira Bay (Ishigaki)",    24.453, 124.146),
    # ── nature ──
    ("nature", "Shiretoko",                44.070, 145.000),
    ("nature", "Lake Akan",                43.430, 144.090),
    ("nature", "Biei",                     43.590, 142.470),
    ("nature", "Sounkyo (Daisetsuzan)",    43.720, 142.950),
    ("nature", "Lake Towada & Oirase",     40.460, 140.900),
    ("nature", "Shirakami-Sanchi",         40.560, 139.970),
    ("nature", "Urabandai",                37.660, 140.080),
    ("nature", "Oze",                      36.930, 139.260),
    ("nature", "Chichibu / Nagatoro",      36.110, 139.110),
    ("nature", "Kamikochi",                36.250, 137.640),
    ("nature", "Kurobe Gorge",             36.810, 137.580),
    ("nature", "Yoshino",                  34.370, 135.860),
    ("nature", "Nachi Falls",              33.668, 135.890),
    ("nature", "Iya Valley",               33.875, 133.835),
    ("nature", "Shimanto River",           33.000, 132.930),
    ("nature", "Takachiho Gorge",          32.700, 131.300),
    ("nature", "Mt Aso",                   32.950, 131.100),
    ("nature", "Yakushima",                30.350, 130.530),
]

# names people outside Japan already know -> bonus points (must match HOOKS names)
FAMOUS = {
    "Niseko Grand Hirafu", "Furano", "Hakuba Happo-one", "Hakuba Goryu", "Hakuba Cortina",
    "Nozawa Onsen", "Shiga Kogen", "Myoko Akakura", "Zao Onsen", "Naeba", "Rusutsu",
    "Noboribetsu Onsen", "Ginzan Onsen", "Kusatsu Onsen", "Hakone Yumoto", "Kinosaki Onsen",
    "Beppu", "Yufuin", "Kurokawa Onsen", "Arima Onsen", "Dogo Onsen",
    "Nikko", "Karuizawa", "Lake Kawaguchiko (Fuji)", "Matsumoto Castle", "Shirakawa-go",
    "Takayama old town", "Kanazawa", "Himeji Castle", "Koyasan", "Miyajima", "Naoshima",
    "Miyakojima", "Kabira Bay (Ishigaki)", "Onna coast (Okinawa)",
    "Biei", "Kamikochi", "Yakushima", "Shiretoko",
}

PREF_EN = {
    "北海道": "Hokkaido", "青森県": "Aomori", "岩手県": "Iwate", "宮城県": "Miyagi",
    "秋田県": "Akita", "山形県": "Yamagata", "福島県": "Fukushima", "茨城県": "Ibaraki",
    "栃木県": "Tochigi", "群馬県": "Gunma", "埼玉県": "Saitama", "千葉県": "Chiba",
    "東京都": "Tokyo", "神奈川県": "Kanagawa", "新潟県": "Niigata", "富山県": "Toyama",
    "石川県": "Ishikawa", "福井県": "Fukui", "山梨県": "Yamanashi", "長野県": "Nagano",
    "岐阜県": "Gifu", "静岡県": "Shizuoka", "愛知県": "Aichi", "三重県": "Mie",
    "滋賀県": "Shiga", "京都府": "Kyoto", "大阪府": "Osaka", "兵庫県": "Hyogo",
    "奈良県": "Nara", "和歌山県": "Wakayama", "鳥取県": "Tottori", "島根県": "Shimane",
    "岡山県": "Okayama", "広島県": "Hiroshima", "山口県": "Yamaguchi", "徳島県": "Tokushima",
    "香川県": "Kagawa", "愛媛県": "Ehime", "高知県": "Kochi", "福岡県": "Fukuoka",
    "佐賀県": "Saga", "長崎県": "Nagasaki", "熊本県": "Kumamoto", "大分県": "Oita",
    "宮崎県": "Miyazaki", "鹿児島県": "Kagoshima", "沖縄県": "Okinawa",
}


# ─── helpers ─────────────────────────────────────────────────────────
def haversine(lat1, lng1, lat2, lng2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(a))

def est_drive_min(km):
    """Rough fallback: straight line -> road distance -> minutes (cautious)."""
    return km * ROAD_FACTOR / DRIVE_KMH * 60

def fmt_usd(v): return "FREE" if v == 0 else (f"${v/1000:.0f}K" if v >= 1000 else f"${v:.0f}")
def fmt_yen(v): return "FREE" if v == 0 else (f"¥{v/1e6:.1f}M" if v >= 1e6 else f"¥{v/1e3:.0f}K")

def load_json(p):
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except ValueError:
        return {}

def save_json(p, data):
    p.parent.mkdir(exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


# ─── nearby attractions ──────────────────────────────────────────────
def nearby_hooks(lat, lng):
    """Enabled attractions roughly in range (loose pre-filter, refined by road routing).
    Each hook: {kind, name, lat, lng, km (straight), road_km, min, routed}"""
    hits = []
    for kind, name, a, b in HOOKS:
        if kind not in ENABLED:
            continue
        km = haversine(lat, lng, a, b)
        mins = est_drive_min(km)
        if mins <= MAX_DRIVE_MIN * PREFILTER:
            hits.append({"kind": kind, "name": name, "lat": a, "lng": b, "km": km,
                         "road_km": km * ROAD_FACTOR, "min": mins, "routed": False})
    return sorted(hits, key=lambda h: h["min"])

def fmt_trip(h, l):
    """'~8 min walk' only when the house position is precise (district level) and the
    road distance is short; exact minutes for short routed drives, else rounded."""
    if l.get("geo_level") == "district" and h["road_km"] <= WALK_KM:
        m = h["road_km"] / WALK_KMH * 60
        return f"~{max(5, math.ceil(m / 5) * 5)} min walk"
    m = h["min"]
    if h.get("routed"):
        m = max(1, round(m)) if m < 20 else int(round(m / 5) * 5)
        return f"~{m} min drive"
    return f"~{max(5, math.ceil(m / 5) * 5)} min drive"     # estimate: round UP

def time_source(hooks):
    if hooks and hooks[0].get("routed"):
        return "Google Maps" if hooks[0].get("router") == "google" else "OpenStreetMap routing"
    return "rough estimates"


# ─── road routing (real drive times) ─────────────────────────────────
def active_router():
    if ROUTER == "auto":
        return "google" if GOOGLE_KEY else "osrm"
    if ROUTER == "google" and not GOOGLE_KEY:
        print("ROUTER=google but GOOGLE_MAPS_KEY is missing -> using osrm")
        return "osrm"
    return ROUTER if ROUTER in ("google", "osrm") else "none"

def route_google(lat, lng, dests):
    """[(minutes, road_km) or None per destination], or None if the call failed."""
    def wp(a, b):
        return {"waypoint": {"location": {"latLng": {"latitude": a, "longitude": b}}}}
    r = requests.post(
        "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix",
        headers={"X-Goog-Api-Key": GOOGLE_KEY,
                 "X-Goog-FieldMask": "originIndex,destinationIndex,duration,distanceMeters,condition"},
        json={"origins": [wp(lat, lng)], "destinations": [wp(a, b) for a, b in dests],
              "travelMode": "DRIVE", "routingPreference": "TRAFFIC_UNAWARE"},
        timeout=30)
    if not r.ok:
        print(f"  Google routes {r.status_code}: {r.text[:200]}")
        return None
    out = [None] * len(dests)
    for e in r.json():
        if e.get("condition") != "ROUTE_EXISTS":
            continue
        i = e.get("destinationIndex", 0)                 # 0 is omitted in the JSON
        secs = float(str(e.get("duration", "0s")).rstrip("s") or 0)
        out[i] = (secs / 60, e.get("distanceMeters", 0) / 1000)
    return out

def route_osrm(lat, lng, dests):
    """Free OSRM table service (OpenStreetMap data). Public server: max 1 request/second."""
    coords = ";".join(f"{b:.6f},{a:.6f}" for a, b in [(lat, lng)] + list(dests))
    try:
        r = requests.get(f"{OSRM_URL}/table/v1/driving/{coords}",
                         params={"sources": "0", "annotations": "duration,distance"},
                         headers=scraper.UA, timeout=30)
    finally:
        time.sleep(1.1)
    if not r.ok:
        print(f"  OSRM {r.status_code}: {r.text[:200]}")
        return None
    data = r.json()
    if data.get("code") != "Ok":
        print(f"  OSRM: {data.get('code')} {data.get('message', '')}")
        return None
    durs = data["durations"][0][1:]
    dists = (data.get("distances") or [[None] * (len(dests) + 1)])[0][1:]
    return [None if d is None else (d / 60 * OSRM_SLOWDOWN, (m or 0) / 1000)
            for d, m in zip(durs, dists)]

def route_hooks(l, hooks, cache, router, allow_call):
    """Replaces rough estimates with cached/real road times. Returns (made_call, ok)."""
    base = f"{router}|{l['lat']:.4f},{l['lng']:.4f}|"
    max_age = GOOGLE_CACHE_DAYS if router == "google" else OSRM_CACHE_DAYS
    now = datetime.now(JST)

    def fresh(key):
        hit = cache.get(key)
        return hit and (now - datetime.fromisoformat(hit["d"])).days < max_age

    todo = [h for h in hooks if not fresh(base + h["name"])]
    made, ok = False, True
    if todo and allow_call:
        made = True
        try:
            fn = route_google if router == "google" else route_osrm
            res = fn(l["lat"], l["lng"], [(h["lat"], h["lng"]) for h in todo])
        except (requests.RequestException, ValueError, KeyError, IndexError) as e:
            print(f"  routing error {l['url']}: {e}")
            res = None
        if res is None:
            ok = False                                    # keep estimates, retry next run
        else:
            stamp = now.isoformat(timespec="seconds")
            for h, v in zip(todo, res):
                cache[base + h["name"]] = {"v": v and [round(v[0], 1), round(v[1], 2)],
                                           "d": stamp}
    for h in hooks:
        hit = cache.get(base + h["name"])
        if not hit:
            continue
        if hit["v"] is None:
            h["min"] = math.inf                           # no road route (island etc.)
        else:
            h["min"], h["road_km"] = hit["v"]
        h["routed"], h["router"] = True, router
    return made, ok

def add_road_times(ranked):
    """ranked = [(points, listing, hooks)] best first. Routes the best ones (ROUTE_TOP
    new requests per run); cached routes are used for everyone."""
    router = active_router()
    if router == "none":
        print("Road routing off (ROUTER=none) – using rough estimates")
        return
    cache = load_json(ROUTE_CACHE)
    calls = fails = streak = 0
    try:
        for _, l, hooks in ranked:
            allow = calls < ROUTE_TOP and streak < 3       # stop if the service is down
            made, ok = route_hooks(l, hooks, cache, router, allow)
            if made:
                calls += 1
                fails += not ok
                streak = 0 if ok else streak + 1
    finally:
        save_json(ROUTE_CACHE, cache)
    routed = sum(1 for _, _, hs in ranked if hs and hs[0].get("routed"))
    print(f"Road times ({router}): {routed}/{len(ranked)} houses routed | "
          f"{calls} new requests, {fails} failed")


# ─── location score (the main ranking) ───────────────────────────────
def rank_hooks(hooks, recent=()):
    """Scores the location. Returns (points, hooks with the BEST attraction first).
    Best = close + famous + preferred kind; a house near several attractions gets extra."""
    def value(h):
        v = CLOSE_PTS * max(0.0, 1 - h["min"] / MAX_DRIVE_MIN)
        if h["name"] in FAMOUS:
            v += FAMOUS_BONUS
        v *= KIND_WEIGHT.get(h["kind"], 1.0)
        if h["name"] in recent:
            v -= REPEAT_PENALTY                      # variety: not Hakuba every day
        return v
    ordered = sorted(hooks, key=value, reverse=True)
    extra = EXTRA_HOOK_PTS * min(len(hooks) - 1, MAX_EXTRA_HOOKS)
    return round(value(ordered[0]) + extra, 1), ordered

def finalize(rough, recent):
    """Drops attractions beyond MAX_DRIVE_MIN (real road time where known) and re-ranks."""
    out = []
    for _, l, hooks in rough:
        hooks = sorted((h for h in hooks if h["min"] <= MAX_DRIVE_MIN), key=lambda h: h["min"])
        if hooks:
            hp, hooks = rank_hooks(hooks, recent)
            out.append((hp, l, hooks))
    out.sort(key=lambda c: (c[0], -c[1]["price_yen"]), reverse=True)
    return out


# ─── build-year score (adjustment, -10 .. +5) ────────────────────────
def age_points(year):
    """Returns (points, label for the log). 1981 = new earthquake standard (新耐震)."""
    try:
        year = int(year)
    except (TypeError, ValueError):
        year = None
    if not year:
        return AGE_UNKNOWN_PTS, "built ?"
    for start, pts in AGE_BANDS:
        if year >= start:
            return pts, f"built {year}"
    return AGE_OLDEST_PTS, f"built {year}"


# ─── all sites: gather + remove duplicates ───────────────────────────
def fingerprint(l):
    """Same town/district + same price + similar floor area = same house."""
    a = re.sub(r"\s|大字|字", "", l.get("location") or "")
    a = re.split(r"[0-9０-９\-－−]", a)[0]
    area = int(round((l.get("area_m2") or 0) / 10))   # within ~10 m²
    return f"{a}|{int(l.get('price_yen') or 0)}|{area}"

def dedupe(listings):
    groups = {}
    for l in listings:
        if l.get("price_yen") is None:
            continue
        groups.setdefault(fingerprint(l), []).append(l)
    out, dupes = [], 0
    for key, g in groups.items():
        g.sort(key=lambda x: (len(x.get("photos") or []), bool(x.get("year_built")),
                              bool(x.get("area_m2"))), reverse=True)
        best = dict(g[0])
        if not best.get("year_built"):                # borrow the year from a duplicate
            best["year_built"] = next((x["year_built"] for x in g if x.get("year_built")), None)
        best["fp"] = key
        best["all_urls"] = list(dict.fromkeys(x["url"] for x in g))
        dupes += len(g) - 1
        out.append(best)
    print(f"Merged: {len(out)} unique houses ({dupes} duplicates removed)")
    return out

def gather():
    everything = []
    for name in SITES_ON:
        mod = SITE_MODULES.get(name)
        if not mod:
            print(f"Unknown source '{name}', skipping")
            continue
        t = time.time()
        try:
            res = mod.scrape()
        except Exception as e:
            print(f"!! {name} scraper failed, skipping it today: {e!r}")
            continue
        for l in res:
            l.setdefault("source", name)
        print(f"== {name}: {len(res)} usable listings ({time.time() - t:.0f}s)")
        everything += res
    return dedupe(everything)

def already_posted(l, posted):
    return (("fp:" + l["fp"]) in posted
            or any(u in posted for u in l.get("all_urls", [l["url"]])))


# ─── exchange rate ───────────────────────────────────────────────────
def get_fx():
    try:
        r = requests.get("https://open.er-api.com/v6/latest/JPY", timeout=15)
        rate = float(r.json()["rates"]["USD"])
        if 0.003 < rate < 0.02:
            print(f"FX: 1 JPY = {rate:.5f} USD")
            return rate
    except Exception as e:
        print(f"FX error: {e}")
    print(f"FX: using fallback {FX_FALLBACK}")
    return FX_FALLBACK


# ─── yearly fees (management / repair fund / onsen / land rent) ──────
FEE_LABELS = ["管理費", "修繕積立金", "修繕積立費", "共益費",
              "温泉使用料", "温泉利用料", "温泉維持費", "借地料", "地代"]

def parse_fees(html):
    """Returns (yearly_yen, {label: yearly_yen}). Monthly amounts x12 unless '年' is written."""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>|&nbsp;", " ", text)
    text = re.sub(r"\s+", " ", text)
    text = text.translate(str.maketrans("０１２３４５６７８９，．", "0123456789,."))
    items = {}
    for label in FEE_LABELS:
        for m in re.finditer(label, text):
            win = text[m.end(): m.end() + 40]
            cuts = [win.find(o) for o in FEE_LABELS if o != label and o in win]
            if cuts:
                win = win[:min(cuts)]            # don't read the next fee's number
            n = re.search(r"(\d[\d,]*(?:\.\d+)?)\s*(万)?\s*円", win)
            if not n:
                continue
            yen = float(n.group(1).replace(",", "")) * (10000 if n.group(2) else 1)
            items[label] = yen if "年" in win[:n.start()] else yen * 12
            break
    return sum(items.values()), items

def yearly_fees(l, fee_cache):
    """Yearly fees in yen, or None if the page couldn't be read."""
    if l.get("fees_yearly_yen") is not None:
        return l["fees_yearly_yen"]
    if l["url"] in fee_cache:
        return fee_cache[l["url"]]["yearly"]
    try:
        r = requests.get(l["url"], headers=scraper.UA, timeout=30)
        if not r.ok:
            return None
        if not r.encoding or r.encoding.lower() == "iso-8859-1":
            r.encoding = r.apparent_encoding
        yearly, items = parse_fees(r.text)
    except Exception as e:
        print(f"  fee check error {l['url']}: {e}")
        return None
    finally:
        time.sleep(1)                              # be polite to the site
    fee_cache[l["url"]] = {"yearly": yearly, "items": items}
    if items:
        print(f"  fees {l['url']}: {items}")
    return yearly


# ─── AirROI ──────────────────────────────────────────────────────────
def find_num(obj, names):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() in names and isinstance(v, (int, float)) and not isinstance(v, bool):
                return float(v)
        for v in obj.values():
            n = find_num(v, names)
            if n is not None:
                return n
    elif isinstance(obj, list):
        for v in obj:
            n = find_num(v, names)
            if n is not None:
                return n
    return None

def bedrooms_for(l):
    return max(1, min(l.get("bedrooms") or 3, 5))

def airroi_call(lat, lng, beds):
    """Returns (estimate or None, ok_to_cache)."""
    params = {"lat": lat, "lng": lng, "bedrooms": beds, "baths": 1,
              "guests": beds * 2, "currency": "usd"}
    try:
        r = requests.get(AIRROI_URL, params=params,
                         headers={"X-API-KEY": AIRROI_KEY}, timeout=30)
        print(f"AirROI {r.status_code}: {r.text[:600]}")
        if not r.ok:
            return None, r.status_code in (400, 404, 422)
        data = r.json()
    except Exception as e:
        print(f"AirROI error: {e}")
        return None, False
    rev = find_num(data, {"revenue", "annual_revenue", "ltm_revenue", "revenue_ltm",
                          "yearly_revenue", "total_revenue", "projected_revenue"})
    occ = find_num(data, {"occupancy", "occupancy_rate", "ltm_occupancy"})
    adr = find_num(data, {"adr", "average_daily_rate", "ltm_adr", "daily_rate"})
    if occ is not None and occ <= 1:
        occ *= 100
    if rev is None and adr and occ:
        rev = adr * occ / 100 * 365
    if not rev and not adr:
        return None, True
    return {"revenue": rev, "occupancy": occ, "adr": adr}, True

def est_key(l):
    return f"{l['lat']:.3f},{l['lng']:.3f},{bedrooms_for(l)}"

def cached_estimate(l, cache):
    """(found, estimate) – found=True means no paid call needed."""
    hit = cache.get(est_key(l))
    if hit and (datetime.now(JST) - datetime.fromisoformat(hit["date"])).days < CACHE_DAYS:
        return True, hit["est"]
    return False, None

def fetch_estimate(l, cache):
    est, cacheable = airroi_call(l["lat"], l["lng"], bedrooms_for(l))
    if cacheable:
        cache[est_key(l)] = {"date": datetime.now(JST).isoformat(timespec="seconds"), "est": est}
    return est


# ─── slides ──────────────────────────────────────────────────────────
FONT_DIR = ROOT / "fonts"
FALLBACK_FONTS = ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                  "DejaVuSans.ttf", "arial.ttf", "Arial.ttf"]
RAQM = pil_features.check("raqm")          # needed for straight (lining) numbers in the serif
_missing = set()

def font_file(style, weight):
    if style == "serif":
        return FONT_DIR / "Serif-Light.ttf"
    if weight >= 500:
        return FONT_DIR / "Sans-Medium.ttf"
    if weight >= 400:
        return FONT_DIR / "Sans-Regular.ttf"
    return FONT_DIR / "Sans-Light.ttf"

@lru_cache(maxsize=256)
def cfont(style, size, weight=500):
    """'serif' = Cormorant Light. 'sans' = Inter: 300 Light, 400 Regular, 500+ Medium."""
    p = font_file(style, weight)
    try:
        return ImageFont.truetype(str(p), size)
    except OSError:
        if p not in _missing:
            _missing.add(p)
            print(f"!! FONT MISSING: {p} – using a fallback font")
    for fb in FALLBACK_FONTS:
        try:
            return ImageFont.truetype(fb, size)
        except OSError:
            pass
    return ImageFont.load_default()

def font(size):
    return cfont("sans", size, 500)

def feat(f):
    """Lining numbers (no dropping 3/5/7/9) for the serif, if Pillow supports it."""
    if RAQM and "Serif" in str(getattr(f, "path", "")):
        return {"features": ["lnum"]}
    return {}

def draw_text(d, xy, s, size, fill=(255, 255, 255), maxw=W - 120):
    """Draws text with a shadow; shrinks the font if the line is too wide."""
    x, y = xy
    f = font(size)
    while size > 20 and d.textlength(s, font=f) > maxw:
        size -= 2
        f = font(size)
    d.text((x + 3, y + 3), s, font=f, fill=(0, 0, 0))
    d.text((x, y), s, font=f, fill=fill)
    return y + int(size * 1.25)

def text_width(d, s, f, track=0):
    return d.textlength(s, font=f, **feat(f)) + track * max(0, len(s) - 1)

def fit_font(d, s, size, weight, track=0, maxw=W - 120, style="sans"):
    """Largest font (starting at size) that makes the line fit maxw."""
    while True:
        f = cfont(style, size, weight)
        if text_width(d, s, f, track) <= maxw or size <= 18:
            return f
        size -= 2

def put(d, xy, s, f, fill, anchor="la", track=0, shadow=3):
    """Text with a shadow. track = extra letter spacing in px (can be negative).
    All letters sit on ONE shared baseline and keep the font's kerning."""
    x, y = xy
    kw = feat(f)
    if not track:
        if shadow:
            d.text((x + shadow, y + shadow), s, font=f, fill=(0, 0, 0), anchor=anchor, **kw)
        d.text((x, y), s, font=f, fill=fill, anchor=anchor, **kw)
        return
    # work out where the baseline is for the requested anchor (top, middle, ...)
    a_top = d.textbbox((0, 0), s, font=f, anchor="l" + anchor[1], **kw)[1]
    b_top = d.textbbox((0, 0), s, font=f, anchor="ls", **kw)[1]
    base = y + a_top - b_top
    total = text_width(d, s, f, track)
    if anchor[0] == "m":
        x -= total / 2
    elif anchor[0] == "r":
        x -= total
    for i, c in enumerate(s):
        px = x + d.textlength(s[:i], font=f, **kw) + track * i
        if shadow:
            d.text((px + shadow, base + shadow), c, font=f, fill=(0, 0, 0), anchor="ls", **kw)
        d.text((px, base), c, font=f, fill=fill, anchor="ls", **kw)

def shade(img, base=60, top=0.16, bottom=0.55):
    """Darkens the photo: light overall, stronger at the top (handle) and bottom (facts)."""
    mask = Image.new("L", (1, H))
    for y in range(H):
        t = y / H
        a = base
        if t < top:
            a = max(a, int(150 * (1 - t / top)))
        if t > bottom:
            a = max(a, int(base + (230 - base) * (t - bottom) / (1 - bottom)))
        mask.putpixel((0, y), a)
    return Image.composite(Image.new("RGB", (W, H)), img, mask.resize((W, H)))

def darken_bottom(img, start=0.40):
    mask = Image.new("L", (1, H))
    for y in range(H):
        t = max(0.0, (y / H - start) / (1 - start))
        mask.putpixel((0, y), int(235 * t))
    return Image.composite(Image.new("RGB", (W, H)), img, mask.resize((W, H)))

def download(url):
    try:
        r = requests.get(url, headers=scraper.UA, timeout=30)
        if not r.ok:
            return None
        img = Image.open(io.BytesIO(r.content)).convert("RGB")
        return img if img.width >= 300 and img.height >= 200 else None
    except Exception as e:
        print(f"  photo error {url}: {e}")
        return None

def cover_trip(h, l):
    """'3 min to Ureshino Onsen' / '5 min walk to ...'"""
    t = fmt_trip(h, l).lstrip("~").replace(" drive", "")
    return f"{t} to {h['name']}"

def cover_facts(l):
    """'6BR · 1984' – parts the listing doesn't have are left out."""
    parts = []
    if l.get("bedrooms"):
        parts.append(f"{l['bedrooms']}BR")
    if l.get("year_built"):
        parts.append(str(l["year_built"]))
    return " · ".join(parts)

def cover_slide(photo, l, hooks, usd, e):
    h0 = hooks[0]
    cx, white = W // 2, (255, 255, 255)
    soft, grey = (230, 230, 230), (200, 200, 200)
    img = ImageOps.fit(photo, (W, H), Image.LANCZOS) if photo else Image.new("RGB", (W, H), (24, 44, 70))
    img = shade(img)
    d = ImageDraw.Draw(img)

    price = "FREE" if l["price_yen"] == 0 else f"{fmt_usd(usd)} ({fmt_yen(l['price_yen'])})"
    has_income = bool(e) and e["net"] > 0
    if has_income and show_yield(e):
        big, label = f"{e['roi'] * 100:.0f}%", "net yield"
        sub = f"~ usd ${e['monthly']:,} / month net"
        bottom_price = price
    elif has_income:
        big, label = f"${e['monthly']:,}", "month income (est.)"
        sub = "usd, after management"
        bottom_price = price
    else:                                          # no income estimate -> price is the hero
        big = "FREE" if l["price_yen"] == 0 else fmt_usd(usd)
        label = "house price" if l["price_yen"] == 0 else fmt_yen(l["price_yen"])
        sub, bottom_price = None, None

    # (x, y, text, style, size, weight, colour, anchor, letter spacing)
    items = [
        (60, 60, HANDLE, "sans", 26, 500, white, "la", 1),
        (cx, 640, big, "serif", 300, 300, white, "ms", -4),
        (cx, 690, label, "sans", 40, 300, soft, "mt", 2),
    ]
    if sub:
        items.append((cx, 760, sub, "sans", 34, 400, soft, "mt", 0))
    if bottom_price:
        items.append((cx, H - 330, bottom_price, "serif", 88, 300, white, "mt", 0))
    items.append((cx, H - 225, cover_trip(h0, l), "sans", 42, 500, white, "mt", 0))
    facts = cover_facts(l)
    if facts:
        items.append((cx, H - 160, facts, "sans", 30, 400, grey, "mt", 3))

    for x, y, s, style, size, weight, fill, anchor, track in items:
        f = fit_font(d, s, size, weight, track, style=style)
        put(d, (x, y), s, f, fill, anchor, track, shadow=3 if size >= 100 else 2)
    return img

def photo_slide(photo, l):
    img = ImageOps.fit(photo, (W, H), Image.LANCZOS)
    d = ImageDraw.Draw(img)
    credit = f"Photo: {site_info(l)[1]}"
    x = W - 60 - int(d.textlength(credit, font=font(32)))
    draw_text(d, (x, H - 70), credit, 32, (235, 235, 235))
    return img

def stats_slide(l, hooks, usd, e, fees_yen, fees_usd):
    h0 = hooks[0]
    img = Image.new("RGB", (W, H), (18, 28, 45))
    d = ImageDraw.Draw(img)
    y = draw_text(d, (60, 70), "THE NUMBERS", 64, (180, 220, 255)) + 30
    price_txt = "FREE" if l["price_yen"] == 0 else f"{fmt_usd(usd)}  ({fmt_yen(l['price_yen'])})"
    rows = [("Price", price_txt),
            (KINDS[h0["kind"]]["label"].capitalize(), f"{h0['name']}, {fmt_trip(h0, l)}")]
    if e:
        rows += [
            ("All-in cost (est.)", f"{fmt_k(e['all_in'])} = house {fmt_k(e['house'])} + "
                                   f"reno {fmt_k(e['reno'])} + fees {fmt_k(e['fees'])}"),
            ("Airbnb (AirROI)", f"${e['adr']}/night x {e['occ'] * 100:.0f}% x {e['nights']} days"),
            (f"Net after {e['mgmt_pct'] * 100:.0f}% management",
             f"{fmt_k(e['net'])}/yr · ~${e['monthly']:,}/mo"),
        ]
        if e["breakeven_yrs"]:
            rows.append(("Break even", f"{e['breakeven_yrs']:.1f} years"))
    house = []
    if l.get("bedrooms"):
        house.append(f"{l['bedrooms']} rooms")
    if l.get("year_built"):
        house.append(f"built {l['year_built']}")
    if l.get("area_m2"):
        house.append(f"{l['area_m2']:.0f} m²")
    if house:
        rows.append(("House", " · ".join(house)))
    if fees_yen:
        rows.append(("Yearly fees", f"{fmt_yen(fees_yen)} (≈ {fmt_usd(fees_usd)})"))
    rows.append(("Location", f"{PREF_EN.get(l['pref'], l['pref'])}, Japan"))
    if len(hooks) > 1:
        h1 = hooks[1]
        rows.append(("Also nearby", f"{h1['name']}, {fmt_trip(h1, l)}"))
    for label, value in rows:
        lines = textwrap.wrap(value, 30)
        if y + 45 + 65 * len(lines) > H - 190:        # no room left above the note
            break
        y = draw_text(d, (60, y), label.upper(), 34, (140, 160, 190))
        for line in lines:
            y = draw_text(d, (60, y), line, 52)
        y += 16
    place = "town" if l.get("geo_level") == "town" else "district"
    note = (f"Drive times: {time_source(hooks)}, from the {place} centre. "
            f"Estimates, before tax & running costs.")
    yy = H - 170
    for line in textwrap.wrap(note, 48) + ["Link to the listing in the caption."]:
        yy = draw_text(d, (60, yy), line, 30, (150, 150, 150))
    return img

def build_slides(l, hooks, usd, e, fees_yen, fees_usd):
    OUT.mkdir(exist_ok=True)
    for old in OUT.glob("slide_*.jpg"):
        old.unlink()
    photos = []
    for url in l.get("photos", []):
        if len(photos) >= MAX_PHOTOS + 1:
            break
        img = download(url)
        if img:
            photos.append(img)
    print(f"Photos downloaded: {len(photos)}")
    slides = [cover_slide(photos[0] if photos else None, l, hooks, usd, e)]
    slides += [photo_slide(p, l) for p in photos[1:MAX_PHOTOS + 1]]
    slides.append(stats_slide(l, hooks, usd, e, fees_yen, fees_usd))
    paths = []
    for i, s in enumerate(slides, 1):
        p = OUT / f"slide_{i}.jpg"
        s.save(p, "JPEG", quality=90)
        paths.append(p)
    return paths


# ─── caption ─────────────────────────────────────────────────────────
def build_caption(l, hooks, usd, e, fees_yen, fees_usd):
    h0 = hooks[0]
    pref = PREF_EN.get(l["pref"], l["pref"])
    price_line = ("💴 Price: FREE 🎉" if l["price_yen"] == 0
                  else f"💴 Price: {fmt_yen(l['price_yen'])} (≈ {fmt_usd(usd)})")
    headline = (f"{KINDS[h0['kind']]['emoji']} {fmt_usd(usd)} house, "
                f"{fmt_trip(h0, l)} to {h0['name']}.")
    roi_block = caption_text(e, headline)
    lines = [roi_block or headline,
             "",
             price_line,
             f"📍 {l['location']} ({pref})"]
    if len(hooks) > 1:
        also = ", ".join(f"{KINDS[h['kind']]['emoji']} {h['name']} ({fmt_trip(h, l)})"
                         for h in hooks[1:4])
        lines.append(f"🗺 Also near: {also}")
    if l.get("bedrooms"):
        lines.append(f"🛏 Rooms: {l['bedrooms']}")
    if l.get("year_built"):
        lines.append(f"🏗 Built: {l['year_built']}")
    if l.get("area_m2"):
        lines.append(f"📐 Floor area: {l['area_m2']:.0f} m²")
    if fees_yen:
        lines.append(f"🧾 Yearly fees: {fmt_yen(fees_yen)} (≈ {fmt_usd(fees_usd)})")
    place = "town" if l.get("geo_level") == "town" else "district"
    src = time_source(hooks)
    credit = " (© OpenStreetMap contributors)" if src == "OpenStreetMap routing" else ""
    kind_tags = " ".join(dict.fromkeys(KINDS[h["kind"]]["tags"] for h in hooks))
    lines += ["",
              "⚠️ Rough estimates: rental data from AirROI (180 nights max), reno & "
              "buying fees estimated. Before tax & running costs.",
              f"Drive times: {src}{credit}, from the {place} centre.",
              f"Source & photos: {site_info(l)[0]}",
              f"🔗 {l['url']}",
              "",
              f"#akiya #japanhouse #cheaphouse {kind_tags} #moveto{pref.lower()}"
              " #japanrealestate #空き家 #古民家"]
    return "\n".join(lines)


# ─── Telegram ────────────────────────────────────────────────────────
def tg_text(text):
    if DRY_RUN or not (BOT_TOKEN and CHAT_ID):
        print(f"[telegram skipped]\n{text}")
        return True
    r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                      json={"chat_id": CHAT_ID, "text": text[:4096],
                            "disable_web_page_preview": True}, timeout=30)
    print(f"Telegram text: {r.status_code} {r.text[:200] if not r.ok else ''}")
    return r.ok

def tg_album(paths, caption):
    if DRY_RUN or not (BOT_TOKEN and CHAT_ID):
        print(f"[telegram skipped] {len(paths)} slides in {OUT}")
        return True
    files, media = {}, []
    try:
        for i, p in enumerate(paths[:10]):
            key = f"photo{i}"
            files[key] = open(p, "rb")
            item = {"type": "photo", "media": f"attach://{key}"}
            if i == 0:
                item["caption"] = caption[:1024]
            media.append(item)
        r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMediaGroup",
                          data={"chat_id": CHAT_ID, "media": json.dumps(media)},
                          files=files, timeout=120)
    finally:
        for f in files.values():
            f.close()
    print(f"Telegram album: {r.status_code} {r.text[:200] if not r.ok else ''}")
    return r.ok


# ─── picking ─────────────────────────────────────────────────────────
def pick(ranked, fx, last_source=None):
    """ranked = [(location_points, listing, hooks)], best location first.
    Checks the top MAX_CHECK houses (fees + AirROI, spending paid calls on the
    best locations first), then picks the highest
    location points + yield points (-10..+15) + build-year points (-10..+5).
    Houses without AirROI data or with negative income are skipped."""
    ac, fc = load_json(AIRROI_CACHE), load_json(FEE_CACHE)
    if not AIRROI_KEY:
        print("!! AIRROI_API_KEY missing – only houses already in the AirROI cache can be used")
    budget = MAX_AIRROI_CALLS if AIRROI_KEY else 0
    rotate = ROTATE and last_source is not None
    scored, skip_fee, skip_yield, skip_data, no_year = [], 0, 0, 0, 0
    try:
        for hp, l, hooks in ranked:
            if len(scored) >= MAX_CHECK:
                break
            found, est = cached_estimate(l, ac)
            if not found and budget <= 0:
                skip_data += 1                            # no AirROI data, no calls left
                continue
            fees = yearly_fees(l, fc)
            if fees is not None and fees > FEE_LIMIT * l["price_yen"]:
                skip_fee += 1
                print(f"  skip, fees {fmt_yen(fees)}/yr > {FEE_LIMIT:.0%} of "
                      f"{fmt_yen(l['price_yen'])}: {l['url']}")
                continue
            if not found:
                budget -= 1
                est = fetch_estimate(l, ac)
            e = estimate(l["price_yen"], l.get("year_built"), est, fx,
                         floor_m2=l.get("area_m2"), renovated=is_renovated(l),
                         yearly_fees_jpy=fees or 0, rooms=l.get("bedrooms"))
            if e is None:
                skip_data += 1
                print(f"  skip, no AirROI rate/occupancy: {l['url']}")
                continue
            roi_pct = e["roi"] * 100
            if e["net"] <= 0 or roi_pct < MIN_YIELD:
                skip_yield += 1
                print(f"  skip, net {fmt_k(e['net'])}/yr, yield {roi_pct:.0f}%: {l['url']}")
                continue
            yp = yield_points(e)
            ap, age_label = age_points(l.get("year_built"))
            if age_label == "built ?":
                no_year += 1
            total = hp + yp + ap
            h0 = hooks[0]
            how = "road" if h0.get("routed") else "est."
            print(f"  [{l.get('source')}] {l['location']} {fmt_yen(l['price_yen'])} | "
                  f"{h0['name']} {fmt_trip(h0, l)} ({how}) | location {hp:.0f} + "
                  f"yield {roi_pct:.0f}% on {fmt_k(e['all_in'])} all-in ({yp:+.0f}) + "
                  f"{age_label} ({ap:+.0f}) = {total:.0f}")
            scored.append((total, hp, l, hooks, e, fees))
    finally:
        save_json(AIRROI_CACHE, ac)                       # never pay twice
        save_json(FEE_CACHE, fc)
    print(f"Checked: {len(scored)}  |  fee rule skipped: {skip_fee}  |  no AirROI data: "
          f"{skip_data}  |  yield rule skipped: {skip_yield}  |  build year unknown: "
          f"{no_year}  |  AirROI calls used: {(MAX_AIRROI_CALLS if AIRROI_KEY else 0) - budget}"
          f"  |  last source: {last_source}")

    if not scored:
        return None, None, None, None
    scored.sort(key=lambda s: (s[0], s[1], -s[2]["price_yen"]), reverse=True)
    best = scored[0]
    if rotate:
        best = next((s for s in scored if s[2].get("source") != last_source), best)
    _, _, l, hooks, e, fees = best
    return l, hooks, e, fees


# ─── main ────────────────────────────────────────────────────────────
def main():
    today = datetime.now(JST).strftime("%Y-%m-%d")
    print(f"Fonts folder: {FONT_DIR} | files: "
          f"{sorted(p.name for p in FONT_DIR.glob('*.ttf')) if FONT_DIR.exists() else 'FOLDER NOT FOUND'}"
          f" | raqm (lining numbers): {RAQM}")
    fx = get_fx()
    max_yen = MAX_PRICE_USD / fx
    listings = gather()
    posted = load_json(POSTED_FILE)
    rot = load_json(ROTATION_FILE)
    last_source = rot.get("last_source")
    recent = rot.get("recent_hooks", [])

    # 1) rough pre-filter + ranking with straight-line estimates
    rough = []
    for l in listings:
        if already_posted(l, posted):
            continue
        if l.get("price_yen") is None or l["price_yen"] > max_yen:
            continue
        if l.get("lat") is None or l.get("lng") is None:
            continue
        hooks = nearby_hooks(l["lat"], l["lng"])
        if not hooks:
            continue
        hp, hooks = rank_hooks(hooks, recent)
        rough.append((hp, l, hooks))
    rough.sort(key=lambda c: (c[0], -c[1]["price_yen"]), reverse=True)

    # 2) real road times for the best ones, then strict MAX_DRIVE_MIN filter + re-rank
    add_road_times(rough)
    cands = finalize(rough, recent)

    per_kind, per_src = {}, {}
    for _, l, hooks in cands:
        per_kind[hooks[0]["kind"]] = per_kind.get(hooks[0]["kind"], 0) + 1
        per_src[l.get("source")] = per_src.get(l.get("source"), 0) + 1
    print(f"Candidates: {len(cands)} of {len(rough)} pre-filtered {per_kind} by site {per_src}  "
          f"(≤ ${MAX_PRICE_USD:,.0f} = ¥{max_yen:,.0f}, ≤ {MAX_DRIVE_MIN:.0f} min drive, "
          f"enabled: {', '.join(sorted(ENABLED))})")
    if recent and REPEAT_PENALTY:
        print(f"Recently featured (penalised -{REPEAT_PENALTY:.0f}): {', '.join(recent)}")

    if not cands:
        tg_text(f"No deal today ({today}) – no new houses near any attraction.")
        return

    l, hooks, e, fees = pick(cands, fx, last_source)
    if l is None:
        tg_text(f"No deal today ({today}) – every candidate failed the fee "
                f"({FEE_LIMIT:.0%}) rule, had no AirROI data, or wouldn't make money.")
        return

    usd = l["price_yen"] * fx
    fees_yen = fees or 0
    fees_usd = fees_yen * fx
    h0 = hooks[0]
    print(f"PICK [{l.get('source')}]: {l['location']} {fmt_yen(l['price_yen'])} "
          f"{fmt_trip(h0, l)} to {h0['name']} ({h0['kind']}, {time_source(hooks)}), "
          f"{age_points(l.get('year_built'))[1]}, {e['roi'] * 100:.0f}% net yield, "
          f"~${e['monthly']:,}/mo\n  {l['url']}")

    paths = build_slides(l, hooks, usd, e, fees_yen, fees_usd)
    caption = build_caption(l, hooks, usd, e, fees_yen, fees_usd)
    (OUT / "caption.txt").write_text(caption, encoding="utf-8")

    ok = tg_album(paths, caption) and tg_text(caption)
    if ok and not DRY_RUN:
        for u in l.get("all_urls", [l["url"]]):
            posted[u] = today
        posted["fp:" + l["fp"]] = today
        save_json(POSTED_FILE, posted)
        recent = ([h0["name"]] + [h for h in recent if h != h0["name"]])[:RECENT_HOOKS]
        save_json(ROTATION_FILE, {"last_source": l.get("source"), "date": today,
                                  "recent_hooks": recent})
        print("Saved to state/posted.json")
    elif not ok:
        print("Telegram failed – not marking as posted, will retry next run")


if __name__ == "__main__":
    main()
