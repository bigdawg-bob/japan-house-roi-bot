"""
Daily "cheap Japanese house near a ski resort / onsen / sight / beach / nature" bot.
Flow: scrape the enabled sites (SOURCES) -> merge + remove duplicates
      -> keep houses under MAX_PRICE_USD roughly near a hook (straight-line pre-filter)
      -> real ROAD drive times for the best-located houses (Google Routes API if
         GOOGLE_MAPS_KEY is set, otherwise free OSRM / OpenStreetMap), cached
      -> keep houses within MAX_DRIVE_MIN by road, rank by LOCATION first
      -> rank by location points + build-year points (no yield, so no AirROI needed)
      -> walk down that list: yearly fees rule (skip if > 15% of price), then ONE
         AirROI estimate for the top house (MAX_AIRROI_CALLS, default 1; cached = free).
         No AirROI data or no income -> skip.
      -> yield (yield_calc.py) is only shown on the slides/caption, not scored
      -> 3 slides: 1 cover (house photo), 2 the payback (day photo),
         3 why this rents (dusk photo).
         Attraction names always say what they are: "Beppu Onsen", "Rusutsu ski resort".
         Slide 3 wording: 5 rotating headlines (never the same twice in a row) +
         3 town-specific facts (never the same combo as a recent post) +
         2 SEO lines ("[Town] Onsen Airbnb = onsen access..." / "[Town] investment: ...").
         Town facts: towns.json (your checked facts) > Grok web search (sourced only)
         > our own numbers (AirROI occupancy, nearby attractions, build year).
         Area photos: Pexels + Wikimedia Commons checked by Grok -> generic Japan
         -> photo library (any photo from any earlier post) -> unchecked stock
         -> this listing's photos -> fallback_photos/ folder. 75% black overlay.
         No photo at all -> no post today (retry next run).
      -> Reel: AI video (ai_reel.py, optional) or photo reel: 1080x1920, exactly 7.0 s,
         one shot with a slow zoom-in (Ken Burns), 70% black overlay, ONE centred line
         (max 4 words), text fades in at 0.5 s and out at 6.5 s.
         Line: "$15K KINOSAKI" > "19 AIRBNBS HERE" > "25 MIN TO KINOSAKI" > "WHY $203 WORKS".
      -> optional cover.html template ({{ hook }}, {{ location }} ...) -> out/cover.html
      -> 1080x1350 slides -> Telegram (album + copyable caption + reel video).
"""
import faulthandler, signal
RUN_LIMIT = 25 * 60                                         # seconds (workflow step allows 30 min)
faulthandler.dump_traceback_later(RUN_LIMIT, exit=False)    # prints where the bot is at the limit
def _too_long(*_):
    raise SystemExit("!! Run hit the 25-minute limit – stopping (caches are saved)")
signal.signal(signal.SIGALRM, _too_long)
signal.alarm(RUN_LIMIT + 2)

import base64, hashlib, io, itertools, json, math, os, re, shutil, subprocess, sys, time
T0 = time.time()                                  # run start (AI reel checks the time left)
from datetime import datetime, timezone, timedelta
from functools import lru_cache
from html import escape
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont, ImageOps
from PIL import features as pil_features

import scraper
import scraper_athome
import scraper_homes
from yield_calc import (estimate, caption_text, is_renovated, show_yield, fmt_k)

try:
    import ai_reel                                # optional: AI video reel
except ImportError as _ex:
    ai_reel = None
    print(f"!! ai_reel.py not loaded ({_ex}) – photo reel only")


# ─── settings ────────────────────────────────────────────────────────
def _env(name, default):
    return os.getenv(name) or default                 # empty string -> default

HANDLE           = "@yama.yield"                      # top-left text on the slides
MAX_PRICE_USD    = float(_env("MAX_PRICE_USD", "100000"))
MAX_DRIVE_MIN    = float(_env("MAX_DRIVE_MIN", "45"))
MAX_WALK_MIN     = float(_env("MAX_WALK_MIN", "15"))
FEE_LIMIT        = float(_env("FEE_LIMIT_PCT", "15")) / 100   # yearly fees vs price
MAX_AIRROI_CALLS = int(_env("MAX_AIRROI_CALLS", "1"))         # paid calls per run (1 = only the chosen house)
CACHE_DAYS       = 90                                          # re-ask AirROI after this
HOUSE_PHOTOS     = 3                                           # listing photos to download
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

# ── area slides (2 & 3): photos + facts ──
GROK_KEY       = os.getenv("GROK_API_KEY") or os.getenv("XAI_API_KEY")
GROK_MODEL     = _env("GROK_MODEL", "grok-4.6")
GROK_URL       = _env("GROK_URL", "https://api.x.ai/v1/responses")
PEXELS_KEY     = os.getenv("PEXELS_KEY") or os.getenv("PEXELS_API_KEY")
PHOTO_CHECKS   = int(_env("PHOTO_CHECKS", "6"))           # Grok checks per slide (place photos)
GENERIC_CHECKS = int(_env("GENERIC_CHECKS", "4"))         # extra Grok checks for the generic fallback
MIN_PHOTO_W    = 1080                                     # original photo size needed (checked photos)
MIN_PHOTO_H    = 1350
LOOSE_W, LOOSE_H = 800, 1000                              # size for the unchecked last-resort photos
AREA_OVERLAY   = min(1.0, max(0.0, float(_env("AREA_OVERLAY", "0.75"))))  # black overlay on slides 2 & 3
FACTS_DAYS     = 180                                      # re-ask Grok for town facts after this
LIBRARY_MAX    = int(_env("LIBRARY_MAX", "60"))           # photos kept for reuse
SLIDE3_HISTORY = 30                                       # slide 3 fact combos remembered
BOT_UA         = {"User-Agent": "yama-yield-akiya-bot/1.0 (Instagram @yama.yield; GitHub Actions)"}

# ── reel (7 s vertical video, one line of text) ──
REEL_ON      = _env("REEL", "1") == "1"                 # REEL=0 turns the reel off
REEL_W, REEL_H = 1080, 1920
REEL_FPS     = 30
REEL_SECS    = 7.0                                      # exactly 7.0 s = 210 frames
REEL_TEXT_ON, REEL_TEXT_OFF = 0.5, 6.5                  # text visible from 0.5 s to 6.5 s
REEL_OVERLAY = min(1.0, max(0.0, float(_env("REEL_OVERLAY", "0.70"))))   # 70% black
REEL_MAX_PX  = int(_env("REEL_MAX_PX", "130"))          # biggest text size
REEL_MIN_PX  = int(_env("REEL_MIN_PX", "72"))           # smallest text allowed
REEL_WORDS   = 4                                        # v2: max 4 words, one line
REEL_TRACK   = int(_env("REEL_TRACK", "0"))             # letter spacing, 1/1000 em
REEL_MAXW    = 950                                      # ~88% of 1080
REEL_PHOTO   = _env("REEL_PHOTO", "dusk").lower()       # dusk | day | cover
REEL_ZOOM    = max(1.0, float(_env("REEL_ZOOM", "1.12")))  # slow zoom-in: 1.00 -> 1.12 over 7 s
REEL_FADE    = 0.3                                      # text fade in / out, seconds

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
PHOTO_CACHE   = STATE / "photo_cache.json"
FACTS_CACHE   = STATE / "town_facts.json"
LIBRARY_FILE  = STATE / "photo_library.json"
LIBRARY_DIR   = STATE / "photo_library"
TOWNS_FILE    = ROOT / "towns.json"                      # optional: your own checked town facts
FALLBACK_DIR  = ROOT / "fallback_photos"                 # optional: your own backup photos
COVER_TEMPLATE = Path(_env("COVER_TEMPLATE", str(ROOT / "templates" / "cover.html")))  # optional HTML cover
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
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

def hook_label(h):
    """'Rusutsu' -> 'Rusutsu ski resort', 'Beppu' -> 'Beppu Onsen', others unchanged."""
    name = h["name"]
    if h["kind"] == "ski" and "ski" not in name.lower():
        return f"{name} ski resort"
    if h["kind"] == "onsen" and "onsen" not in name.lower():
        return f"{name} Onsen"
    return name


# ─── Grok (xAI) ──────────────────────────────────────────────────────
def grok(content, timeout=120, tools=None):
    """One Grok call (Responses API). content = text or a list of input parts.
    Returns the answer text, or '' if anything failed."""
    if not GROK_KEY:
        return ""
    body = {"model": GROK_MODEL, "input": [{"role": "user", "content": content}]}
    if tools:
        body["tools"] = tools
    try:
        r = requests.post(GROK_URL, json=body, timeout=timeout,
                          headers={"Authorization": f"Bearer {GROK_KEY}",
                                   "Content-Type": "application/json"})
    except requests.RequestException as ex:
        print(f"  Grok error: {ex}")
        return ""
    if not r.ok:
        print(f"  Grok {r.status_code}: {r.text[:300]}")
        return ""
    try:
        data = r.json()
    except ValueError:
        return ""
    if isinstance(data.get("output_text"), str) and data["output_text"]:
        return data["output_text"]
    parts = []
    for item in data.get("output") or []:
        if isinstance(item, dict) and item.get("type") == "message":
            for c in item.get("content") or []:
                if isinstance(c, dict) and c.get("type") == "output_text":
                    parts.append(c.get("text") or "")
    return "\n".join(parts)

def json_from(text):
    """First {...} block in the text as a dict, or None."""
    if not text:
        return None
    text = re.sub(r"```(?:json)?", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except ValueError:
        return None

def yes(v):
    return v is True or (isinstance(v, str) and v.strip().lower() in ("true", "yes"))


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


# ─── town photos: search + Grok check ────────────────────────────────
PEOPLE_WORDS = re.compile(r"\b(wom[ae]n|m[ae]n|people|person|girl|boy|couple|crowd|tourists?|"
                          r"kids?|child|portrait|selfie)\b", re.I)
FREE_LICENCE = re.compile(r"^(CC BY|CC-BY|CC0|Public domain|PD)", re.I)
LIGHT = {
    "day":  "daylight with soft, even light: overcast or no harsh shadows (not night)",
    "dusk": "dusk, blue hour or night with lights on: dark sky, glowing lights, high contrast",
}
# fallback searches: clearly Japan, but not a place anyone could name
GENERIC = {
    "day": {
        "ski":    ["snowy japanese village", "japan snow countryside houses"],
        "onsen":  ["japanese ryokan exterior", "japanese hot spring town street"],
        "sight":  ["old japanese street wooden houses", "japanese countryside village"],
        "beach":  ["japanese fishing village harbor", "japan coast village"],
        "nature": ["japanese rice fields mountains", "japanese forest path"],
        "_all":   ["rural japan", "japanese countryside"],
    },
    "dusk": {
        "ski":    ["snowy japanese village night", "japan snow night lanterns"],
        "onsen":  ["onsen town night lanterns", "ryokan night japan"],
        "sight":  ["japanese alley lanterns night", "old japanese street night"],
        "beach":  ["japanese harbor dusk", "japan coast sunset village"],
        "nature": ["japanese village dusk mountains", "japanese countryside night"],
        "_all":   ["japanese street lanterns night", "japanese village night"],
    },
}

def place_name(h):
    """'Lake Kawaguchiko (Fuji)' -> 'Lake Kawaguchiko', 'Hayama / Zushi' -> 'Hayama'"""
    return re.split(r"\s*[(/]", h["name"])[0].strip()

def photo_queries(h, l, mood):
    name, pref = place_name(h), PREF_EN.get(l["pref"], l["pref"])
    kind = KINDS[h["kind"]]["label"]
    if mood == "dusk":
        return [f"{name} night", f"{name} Japan dusk", f"{pref} Japan {kind} night"]
    return [name, f"{name} Japan", f"{pref} Japan {kind}"]

def generic_queries(h, mood):
    g = GENERIC[mood]
    return g.get(h["kind"], []) + g["_all"]

def pexels_search(q):
    if not PEXELS_KEY:
        return []
    try:
        r = requests.get("https://api.pexels.com/v1/search", timeout=30,
                         headers={"Authorization": PEXELS_KEY},
                         params={"query": q, "per_page": 30})
        if not r.ok:
            print(f"  Pexels error {r.status_code}: {r.text[:200]}")
            return []
        photos = r.json().get("photos", [])
    except (requests.RequestException, ValueError) as ex:
        print(f"  Pexels error: {ex}")
        return []
    return [{"id": f"pexels:{p['id']}", "w": p.get("width", 0), "h": p.get("height", 0),
             "url": f"{p['src']['original']}?auto=compress&cs=tinysrgb&h={min(2000, p.get('height', 0))}",
             "text": p.get("alt") or "",
             "credit": f"Photo: {p.get('photographer') or 'Pexels'} / Pexels"} for p in photos]

def commons_search(q):
    try:
        r = requests.get("https://commons.wikimedia.org/w/api.php", headers=BOT_UA, timeout=30,
                         params={"action": "query", "format": "json", "generator": "search",
                                 "gsrsearch": f"{q} filetype:bitmap", "gsrnamespace": 6,
                                 "gsrlimit": 30, "prop": "imageinfo",
                                 "iiprop": "url|size|extmetadata",
                                 "iiurlwidth": 2400, "iiurlheight": 2000,
                                 "iiextmetadatafilter": "LicenseShortName|Artist"})
        pages = (r.json().get("query") or {}).get("pages", {}) if r.ok else {}
    except (requests.RequestException, ValueError) as ex:
        print(f"  Commons error: {ex}")
        return []
    out = []
    for p in sorted(pages.values(), key=lambda p: p.get("index", 0)):
        info = (p.get("imageinfo") or [{}])[0]
        meta = info.get("extmetadata") or {}
        lic = (meta.get("LicenseShortName") or {}).get("value", "")
        if not FREE_LICENCE.match(lic) or re.search(r"\bNC\b|\bND\b", lic):
            continue
        artist = re.sub(r"<[^>]+>", "", (meta.get("Artist") or {}).get("value", ""))
        artist = re.sub(r"\s+", " ", artist).strip()[:40] or "Unknown"
        out.append({"id": f"commons:{p.get('pageid')}", "w": info.get("width", 0),
                    "h": info.get("height", 0), "url": info.get("thumburl") or info.get("url"),
                    "text": p.get("title", ""),
                    "credit": f"Photo: {artist}, {lic} / Wikimedia Commons"})
    return out

def photo_candidates(queries, skip, min_w=MIN_PHOTO_W, min_h=MIN_PHOTO_H):
    """Photos that pass the size + keyword rules, portrait ones first."""
    seen = set(skip)
    for q in queries:
        batch = pexels_search(q) + commons_search(q)
        batch.sort(key=lambda c: c["h"] < c["w"])          # portrait first
        for c in batch:
            if c["id"] in seen or not c["url"]:
                continue
            seen.add(c["id"])
            if c["w"] < min_w or c["h"] < min_h or PEOPLE_WORDS.search(c["text"]):
                continue
            yield c

def fetch_photo(url, min_w=MIN_PHOTO_W, min_h=MIN_PHOTO_H):
    try:
        r = requests.get(url, headers=BOT_UA, timeout=60)
        if not r.ok:
            return None
        img = Image.open(io.BytesIO(r.content)).convert("RGB")
        return img if img.width >= min_w and img.height >= min_h else None
    except Exception as ex:
        print(f"  photo error {url}: {ex}")
        return None

def check_photo(img, h, l, mood, target):
    """Grok looks at the photo. target = 'place' (must show this area) or
    'generic' (must look like Japan but not a nameable landmark)."""
    small = img.copy()
    small.thumbnail((1024, 1024))
    buf = io.BytesIO()
    small.save(buf, "JPEG", quality=85)
    b64 = base64.b64encode(buf.getvalue()).decode()
    place = f"{place_name(h)}, {PREF_EN.get(l['pref'], l['pref'])}, Japan"
    if target == "place":
        about = f"about {place} ({KINDS[h['kind']]['label']})"
        where = f"japan: looks like Japan and plausibly {place} or its area. "
    else:
        about = "about rural Japan in general"
        where = ("japan: clearly looks like Japan (typical houses, roofs, streets, signs, "
                 "lanterns or landscape). ")
    prompt = (
        f"You check a stock photo for an Instagram slide {about}. "
        "Look carefully. Answer ONLY with JSON:\n"
        '{"people": true/false, "watermark_or_text": true/false, "interior": true/false, '
        '"japan": true/false, "landmark": true/false, "light_ok": true/false, '
        '"score": 0-10, "why": "max 10 words"}\n'
        "people: any person visible, even small. "
        "watermark_or_text: a watermark, logo, or big text/graphics added on the photo "
        "(normal shop signs don't count). interior: indoors, food, or a close-up of an object. "
        + where +
        "landmark: shows a famous place a viewer could name (e.g. Mt Fuji, a well-known "
        "temple, shrine gate, castle, tower or city skyline). "
        f"light_ok: {LIGHT[mood]}. score: how good it is as a calm background for white text.")
    v = json_from(grok([{"type": "input_image", "image_url": f"data:image/jpeg;base64,{b64}",
                         "detail": "high"},
                        {"type": "input_text", "text": prompt}], timeout=120))
    return v if isinstance(v, dict) else None

def hard_ok(v, target):
    if not yes(v.get("japan")):
        return False
    if any(yes(v.get(k)) for k in ("people", "watermark_or_text", "interior")):
        return False
    if target == "generic" and yes(v.get("landmark")):
        return False                                   # generic must not show a nameable place
    return True

def scan_photos(queries, h, l, mood, target, used, checks, limit):
    """Best photo from these searches: right light first, else best-scoring backup."""
    backup, n = None, 0
    for c in photo_candidates(queries, used):
        key = f"{c['id']}|{mood}|{target}"
        v = checks.get(key)
        if v is not None and not hard_ok(v, target):
            continue                                   # known bad, skip for free
        if v is None and n >= limit:
            break
        img = fetch_photo(c["url"])
        if img is None:
            continue
        if v is None:
            n += 1
            v = check_photo(img, h, l, mood, target)
            if v is None:
                continue
            checks[key] = v
            print(f"  photo {target}/{mood} {c['id']}: ok={hard_ok(v, target)} "
                  f"light={v.get('light_ok')} score={v.get('score')} ({v.get('why')})")
        if not hard_ok(v, target):
            continue
        pic = {**c, "img": img, "generic": target == "generic"}
        if yes(v.get("light_ok")):
            return pic
        try:
            s = float(v.get("score") or 0)
        except (TypeError, ValueError):
            s = 0
        if backup is None or s > backup[0]:
            backup = (s, pic)                          # right content, wrong light
    return backup[1] if backup else None

def checked_photo(h, l, mood, target, used):
    """Grok-checked photo of the place ('place') or of generic Japan ('generic').
    Grok verdicts are cached per photo, so the same photo is never paid for twice."""
    cache = load_json(PHOTO_CACHE)
    checks = cache.setdefault("checks", {})
    try:
        if target == "place":
            return scan_photos(photo_queries(h, l, mood), h, l, mood, "place",
                               used, checks, PHOTO_CHECKS)
        return scan_photos(generic_queries(h, mood), h, l, mood, "generic",
                           used, checks, GENERIC_CHECKS)
    finally:
        save_json(PHOTO_CACHE, cache)

def unchecked_stock(h, l, mood, used, tries=8):
    """First stock photo that passes the size + description rules (no Grok needed)."""
    queries = photo_queries(h, l, mood) + generic_queries(h, mood)
    for n, c in enumerate(photo_candidates(queries, used, LOOSE_W, LOOSE_H)):
        if n >= tries:
            break
        img = fetch_photo(c["url"], LOOSE_W, LOOSE_H)
        if img:
            return {**c, "img": img, "generic": True}
    return None


# ─── photo library (reuse photos from any earlier post / slide) ──────
def lib_key(pid):
    return hashlib.sha1(pid.encode("utf-8")).hexdigest()[:16]

def library_add(pic, role, mood=None, kind=None):
    """Saves a 1080x1350 copy of a used photo (so it still works if the original link
    dies). role = 'area' or 'house'. Keeps the LIBRARY_MAX most recently used."""
    if not pic or pic["id"].startswith("file:"):
        return                                        # fallback_photos/ are on disk already
    lib = load_json(LIBRARY_FILE)
    k = lib_key(pic["id"])
    now = datetime.now(JST).isoformat(timespec="seconds")
    if k not in lib or not (LIBRARY_DIR / f"{k}.jpg").exists():
        LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
        ImageOps.fit(pic["img"], (W, H), Image.LANCZOS).save(
            LIBRARY_DIR / f"{k}.jpg", "JPEG", quality=85)
        lib[k] = {"id": pic["id"], "credit": pic.get("credit", ""), "role": role,
                  "mood": mood, "kind": kind, "generic": bool(pic.get("generic")),
                  "added": now, "uses": 0}
    lib[k]["uses"] = lib[k].get("uses", 0) + 1
    lib[k]["last"] = now
    if len(lib) > LIBRARY_MAX:
        for old in sorted(lib, key=lambda x: lib[x].get("last", ""))[:len(lib) - LIBRARY_MAX]:
            (LIBRARY_DIR / f"{old}.jpg").unlink(missing_ok=True)
            del lib[old]
    save_json(LIBRARY_FILE, lib)

def library_pick(h, mood, used):
    """A photo from earlier posts. Prefers area photos with the same mood and kind,
    then any area photo, then house photos; least recently used first."""
    lib = load_json(LIBRARY_FILE)
    def rank(item):
        v = item[1]
        return (v.get("role") != "area", v.get("mood") != mood,
                v.get("kind") != h["kind"], v.get("last", ""))
    for k, v in sorted(lib.items(), key=rank):
        if v.get("id") in used:
            continue
        try:
            img = Image.open(LIBRARY_DIR / f"{k}.jpg").convert("RGB")
        except OSError:
            continue
        return {"id": v["id"], "img": img, "credit": v.get("credit", ""),
                "generic": v.get("generic", True)}
    return None

def folder_photo(used):
    """Your own backup photos in fallback_photos/ (optional)."""
    if not FALLBACK_DIR.exists():
        return None
    files = sorted(p for p in FALLBACK_DIR.iterdir()
                   if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"))
    for p in files:
        pid = f"file:{p.name}"
        if pid in used:
            continue
        try:
            img = Image.open(p).convert("RGB")
        except OSError:
            continue
        return {"id": pid, "img": img, "credit": "", "generic": True}
    return None

def find_photo(h, l, mood, used, house_pics):
    """ALWAYS tries to return a photo for slide 2 ('day') or 3 ('dusk'):
    1 checked photo of the place, 2 checked generic Japan, 3 photo library,
    4 unchecked stock, 5 this listing's photos, 6 fallback_photos/ folder.
    If nothing is left, repeats 3-6 allowing photos already used in this post."""
    steps = []
    if GROK_KEY:
        steps += [("checked photo of the place", True,
                   lambda u: checked_photo(h, l, mood, "place", u)),
                  ("checked generic Japan photo", True,
                   lambda u: checked_photo(h, l, mood, "generic", u))]
    steps += [("photo library", False, lambda u: library_pick(h, mood, u)),
              ("unchecked stock photo", False, lambda u: unchecked_stock(h, l, mood, u)),
              ("house photo", False,
               lambda u: next((p for p in house_pics if p["id"] not in u), None)),
              ("fallback_photos folder", False, lambda u: folder_photo(u))]
    passes = [set(used), set()] if used else [set()]
    for n, u in enumerate(passes):
        for label, paid, fn in steps:
            if n and paid:
                continue                               # don't pay Grok twice
            try:
                pic = fn(u)
            except Exception as ex:                    # one step failing must not stop the rest
                print(f"  {mood} photo, {label} failed: {ex!r}")
                pic = None
            if pic:
                extra = " (reused from this post)" if n else ""
                print(f"  {mood} photo from {label}{extra}: {pic['id']}")
                return pic
            print(f"  {mood} photo: nothing from {label}")
    return None


# ─── town facts for slide 3 (towns.json + Grok web search) ───────────
# Town-specific data points. Each one is a short bullet (max ~30 characters).
POINT_KINDS  = ["ryokan", "competition", "foreign", "stay", "access",
                "occupancy", "trend", "plan_b", "tattoo", "pets"]
NEEDS_NUMBER = {"ryokan", "competition", "foreign", "stay", "access", "occupancy"}

def clean_fact(s):
    s = re.sub(r"\[\[?\d+\]?\]\([^)]*\)", "", str(s))    # citation links
    s = re.sub(r"\[\d+\]", "", s)
    s = re.sub(r"\s+", " ", s).strip().strip('"').strip()
    return s

def grok_town_facts(h, l):
    """Sourced town facts from Grok web search, cached per place.
    {'known_for', 'visitors', 'minpaku_cap', 'points': {kind: text}} or None."""
    cache = load_json(FACTS_CACHE)
    hit = cache.get(h["name"])
    if hit and isinstance(hit.get("v"), dict) and "points" in hit["v"]:   # old-style entries get re-asked
        try:
            if (datetime.now(JST) - datetime.fromisoformat(hit["d"])).days < FACTS_DAYS:
                return hit["v"]
        except (KeyError, ValueError):
            pass
    if not GROK_KEY:
        return None
    place = f"{place_name(h)} ({KINDS[h['kind']]['label']}) in {PREF_EN.get(l['pref'], l['pref'])}, Japan"
    prompt = (
        f"Search the web about {place}. This is for an Instagram slide about short-term "
        "rental investment there. Use ONLY facts you found in a real source (Jalan, Rakuten "
        "Travel, city or prefecture statistics, JNTO, the official tourism site, AirDNA, news). "
        "If you can't find a sourced fact, use an empty string. Never guess or estimate.\n"
        "known_for: what it is best known for, max 5 words, e.g. \"Historic seven-bath onsen town\".\n"
        "visitors: yearly visitor number from a real source, short form like \"750k visitors/yr\".\n"
        "minpaku_cap_days: the yearly day limit for private lodging (minpaku, 住宅宿泊事業) in "
        "this municipality ONLY if it is stricter than the national 180 days; otherwise null.\n"
        "points: facts about THIS town only. Each text: max 30 characters, plain English, "
        "no emoji, no hype words, no hotel names, no citations. Each needs a source (site name).\n"
        "- ryokan: typical ryokan price here, e.g. \"Ryokan: ¥14k–28k per person\"\n"
        "- competition: number of Airbnb / vacation rentals here, e.g. \"Only 12 Airbnbs in town\"\n"
        "- foreign: share of foreign guests, e.g. \"Foreign guests: 18% of stays\"\n"
        "- stay: average nights per stay, e.g. \"Avg stay: 1.2 nights\"\n"
        "- access: travel time from a big city or airport, e.g. \"2.5 hrs from Kyoto by train\"\n"
        "- occupancy: weekend vs weekday occupancy, e.g. \"Weekends 90% full, weekdays 50%\"\n"
        "- trend: population, school closure or tourism trend, e.g. \"Visitors up 20% since 2019\"\n"
        "- plan_b: long-term rent or staff housing demand, e.g. \"Ryokan staff housing shortage\"\n"
        "- tattoo: ONLY if tattoo-friendly baths exist, e.g. \"All 7 baths tattoo-friendly\"\n"
        "- pets: pet-friendly lodging supply, e.g. \"Only 3 pet-friendly ryokan\"\n"
        'Answer ONLY with JSON: {"known_for": "...", "visitors": "...", "minpaku_cap_days": null, '
        '"points": {"ryokan": {"text": "...", "source": "..."}, "competition": {...}, ...}}')
    v = json_from(grok(prompt, timeout=240, tools=[{"type": "web_search"}]))
    if not isinstance(v, dict):
        print(f"  town facts: no usable answer for {h['name']}")
        return None
    known = clean_fact(v.get("known_for") or "")
    known = known if 3 <= len(known) <= 40 else ""
    visits = clean_fact(v.get("visitors") or "")
    visits = visits if re.search(r"\d", visits) and len(visits) <= 24 else ""
    cap = v.get("minpaku_cap_days")
    cap = int(cap) if isinstance(cap, (int, float)) and 0 <= cap < 180 else None
    points, raw = {}, v.get("points") or {}
    for k in POINT_KINDS:
        item = raw.get(k) if isinstance(raw, dict) else None
        if not isinstance(item, dict):
            continue
        text = clean_fact(item.get("text") or "")
        src = clean_fact(item.get("source") or "")
        if not src or not 3 <= len(text) <= 34:
            continue                                  # no source or too long -> not used
        if k in NEEDS_NUMBER and not re.search(r"\d", text):
            continue
        points[k] = text
        print(f"  town fact {k}: {text} ({src})")
    if not known and not points:
        print(f"  town facts: nothing usable for {h['name']}")
        return None
    out = {"known_for": known, "visitors": visits, "minpaku_cap": cap, "points": points}
    cache[h["name"]] = {"v": out, "d": datetime.now(JST).isoformat(timespec="seconds")}
    save_json(FACTS_CACHE, cache)
    print(f"  town facts {h['name']}: {out}")
    return out

def town_facts(h, l):
    """Your own checked facts (towns.json) on top of Grok's. None if neither has anything."""
    manual = load_json(TOWNS_FILE).get(h["name"]) or {}
    auto = grok_town_facts(h, l) or {}
    if not manual and not auto:
        return None
    points = dict(auto.get("points") or {})
    points.update({k: str(t).strip() for k, t in (manual.get("points") or {}).items()
                   if k in POINT_KINDS and t})
    return {"known_for": manual.get("known_for") or auto.get("known_for") or "",
            "visitors": manual.get("visitors") or auto.get("visitors") or "",
            "minpaku_cap": manual.get("minpaku_cap", auto.get("minpaku_cap")),
            "points": points}


# ─── slide 3 wording (headline + 3 facts + 2 SEO lines) ──────────────
# 5 headline templates. prefer = facts that fit this headline, need = must have.
TEMPLATES = {
    "A": {"prefer": ["ryokan", "competition", "occupancy"], "need": []},      # THE COMP
    "B": {"prefer": ["access", "near1", "near2"], "need": []},                # THE MAP
    "C": {"prefer": ["stay", "occupancy", "ryokan", "occ"], "need": []},      # THE MATH LEAK
    "D": {"prefer": ["tattoo", "foreign", "pets", "stay"], "need": []},       # THE TOWN SECRET
    "E": {"prefer": ["plan_b", "trend", "stay"], "need": ["plan_b"]},         # THE EXIT
}
ACCESS_TAG = {"ski": "ski access", "onsen": "onsen access", "sight": "sight access",
              "beach": "beach access", "nature": "trail access"}

def short_name(name):
    """'Kinosaki Onsen' -> 'Kinosaki', 'Lake Kawaguchiko (Fuji)' -> 'Lake Kawaguchiko'"""
    n = re.split(r"\s*[(/]", name)[0].strip()
    return re.sub(r"\s+Onsen$", "", n) or n

def seo_label(h):
    """'Kinosaki Onsen', 'Beppu' -> 'Beppu Onsen', 'Rusutsu' -> 'Rusutsu Ski'"""
    name = place_name(h)
    if h["kind"] == "onsen":
        return name if "onsen" in name.lower() else f"{name} Onsen"
    if h["kind"] == "ski":
        return f"{name} Ski"
    return name

def trip_parts(h, l):
    """(minutes, 'walk' or 'drive') – same numbers as fmt_trip."""
    m = re.search(r"(\d+) min (walk|drive)", fmt_trip(h, l))
    return (int(m.group(1)), m.group(2)) if m else (max(1, round(h["min"])), "drive")

def rival_town(h):
    """Nearest other famous place of the same kind, 15+ km away and not the same town."""
    first = short_name(h["name"]).split()[0].lower()
    opts = []
    for kind, name, a, b in HOOKS:
        if kind != h["kind"] or name == h["name"] or name not in FAMOUS:
            continue
        if short_name(name).split()[0].lower() == first:
            continue                                  # Hakuba vs Hakuba = same town
        km = haversine(h["lat"], h["lng"], a, b)
        if km >= 15:
            opts.append((km, name))
    return short_name(min(opts)[1]) if opts else None

def own_points(l, hooks, e):
    """Backup bullets from our own numbers (never generic 'tourists')."""
    pts = {}
    occ = e.get("occ")
    if isinstance(occ, (int, float)) and occ > 0:
        pts["occ"] = f"AirROI occupancy: {occ * 100:.0f}%"
    for key, hk in zip(("near1", "near2"), hooks[1:3]):
        pts[key] = f"{short_name(hk['name'])}: {fmt_trip(hk, l).lstrip('~')}"
    try:
        year = int(l.get("year_built") or 0)
    except (TypeError, ValueError):
        year = 0
    if year >= 1981:
        pts["built"] = f"Built {year}: post-1981 quake code"
    return pts

def pick_template(pool, rival, rot):
    """Least recently used template, never the same as the last post."""
    hist = [t for t in (rot.get("s3_templates") or []) if t in TEMPLATES]
    last = hist[0] if hist else None

    def ok(t):
        if t == "D" and not rival:
            return False
        return all(k in pool for k in TEMPLATES[t]["need"])

    def key(t):
        fit = sum(k in pool for k in TEMPLATES[t]["prefer"])
        return (t in hist, -hist.index(t) if t in hist else 0, -fit)

    options = sorted((t for t in TEMPLATES if ok(t)), key=key)
    fresh = [t for t in options if t != last]
    return (fresh or options or ["C"])[0]

def pick_points(t, pool, rot):
    """3 facts: town data first, fitting the headline, not recently used,
    and never the exact same combo as the last SLIDE3_HISTORY posts."""
    prefer, need = TEMPLATES[t]["prefer"], TEMPLATES[t]["need"]
    history = [tuple(sorted(c)) for c in (rot.get("s3_points") or []) if isinstance(c, list)]
    used = set(history)

    def age(k):                                        # posts since this fact kind was used
        return next((i for i, c in enumerate(history) if k in c), 3)

    keys = list(pool)
    n = min(3, len(keys))
    combos = list(itertools.combinations(keys, n))
    with_need = [c for c in combos if all(k in c for k in need)]
    combos = with_need or combos
    new = [c for c in combos if tuple(sorted(c)) not in used]

    def score(c):
        return sum(3 * (k in POINT_KINDS) + 2 * (k in prefer) + min(age(k), 3) for k in c)

    best = max(new or combos, key=score) if combos else ()
    return sorted(best, key=lambda k: (k not in need, k not in prefer, keys.index(k)))

def slide3_text(h, l, hooks, facts, e, rot):
    """All slide 3 wording: {'template', 'headline', 'points', 'kinds', 'lines'}."""
    f = facts or {}
    pool = {k: v for k, v in (f.get("points") or {}).items() if k in POINT_KINDS and v}
    for k, v in own_points(l, hooks, e).items():
        pool.setdefault(k, v)
    rival = rival_town(h)
    t = pick_template(pool, rival, rot)
    mins, mode = trip_parts(h, l)

    headline = {
        "A": f"Why ${e['adr']:,}/nt wins here",
        "B": f"What does {mins} min actually get you?",
        "C": "Rental math they don't show",
        "D": f"Why this town isn't {rival}",
        "E": "If Airbnb fails, Plan B is...",
    }[t]
    kinds = pick_points(t, pool, rot)

    # SEO line 1: "[Town] Onsen Airbnb = onsen access in 20 min"
    tag = ACCESS_TAG.get(h["kind"], "easy access")
    trip = f"in {mins} min" if mode == "drive" else f"{mins} min walk"
    line1 = f"{seo_label(h)} Airbnb = {tag} {trip}" if mode == "drive" \
        else f"{seo_label(h)} Airbnb = {tag}, {trip}"
    # SEO line 2: "[Town] investment: 180 days = minpaku cap"
    nights = e.get("nights") or 180
    town = short_name(h["name"])
    line2 = (f"{town} investment: {nights} days = minpaku cap" if nights >= 180
             else f"{town} investment: {nights} days, cap is 180")
    cap = f.get("minpaku_cap")
    if isinstance(cap, int) and cap < nights:
        print(f"!! {h['name']}: local minpaku cap may be {cap} days (< {nights} used) – "
              f"check with the city before posting")

    return {"template": t, "headline": headline, "kinds": list(kinds),
            "points": [pool[k] for k in kinds], "lines": [line1, line2]}


# ─── slides ──────────────────────────────────────────────────────────
FONT_DIR = ROOT / "fonts"
FALLBACK_FONTS = ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                  "DejaVuSans.ttf", "arial.ttf", "Arial.ttf"]
RAQM = pil_features.check("raqm")          # needed for straight (lining) numbers in the serif
_missing = set()

def font_file(style, weight):
    if style == "serif":
        return FONT_DIR / "Serif-Light.ttf"
    if weight >= 700 and (FONT_DIR / "Sans-Bold.ttf").exists():
        return FONT_DIR / "Sans-Bold.ttf"              # slides 2 & 3 only
    if weight >= 500:
        return FONT_DIR / "Sans-Medium.ttf"
    if weight >= 400:
        return FONT_DIR / "Sans-Regular.ttf"
    return FONT_DIR / "Sans-Light.ttf"

@lru_cache(maxsize=256)
def cfont(style, size, weight=500):
    """'serif' = Cormorant Light. 'sans' = Inter: 300 Light, 400 Regular, 500+ Medium, 700 Bold."""
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

def feat(f):
    """Lining numbers (no dropping 3/5/7/9) for the serif, if Pillow supports it."""
    if RAQM and "Serif" in str(getattr(f, "path", "")):
        return {"features": ["lnum"]}
    return {}

def text_width(d, s, f, track=0):
    return d.textlength(s, font=f, **feat(f)) + track * max(0, len(s) - 1)

def fit_font(d, s, size, weight, track=0, maxw=W - 120, style="sans"):
    """Largest font (starting at size) that makes the line fit maxw."""
    while True:
        f = cfont(style, size, weight)
        if text_width(d, s, f, track) <= maxw or size <= 18:
            return f
        size -= 2

def wrap_px(d, s, f, maxw):
    """Splits text into lines that fit maxw pixels."""
    lines, cur = [], ""
    for word in s.split():
        t = f"{cur} {word}".strip()
        if cur and text_width(d, t, f) > maxw:
            lines.append(cur)
            cur = word
        else:
            cur = t
    if cur:
        lines.append(cur)
    return lines

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
    """'13 min to Beppu Onsen' / '5 min walk to Rusutsu ski resort'"""
    t = fmt_trip(h, l).lstrip("~").replace(" drive", "")
    return f"{t} to {hook_label(h)}"

def cover_facts(l):
    """'6BR · 1984' – parts the listing doesn't have are left out."""
    parts = []
    if l.get("bedrooms"):
        parts.append(f"{l['bedrooms']}BR")
    if l.get("year_built"):
        parts.append(str(l["year_built"]))
    return " · ".join(parts)

def cover_fields(l, hooks, usd, e):
    """All cover text in one place (used by slide 1 AND the cover.html template).
    hook = big number, sub_hook = small label under it, detail = monthly line,
    price_usd / price_yen, specs = '5BR · 1960', location = '13 min to Beppu Onsen'."""
    h0 = hooks[0]
    free = l["price_yen"] == 0
    price_usd = "FREE" if free else fmt_usd(usd)
    price_yen = "" if free else fmt_yen(l["price_yen"])
    has_income = bool(e) and e["net"] > 0
    if has_income and show_yield(e):
        hook, sub_hook = f"{e['roi'] * 100:.0f}%", "net yield"
        detail, show_price = f"~ usd ${e['monthly']:,} / month net", True
    elif has_income:
        hook, sub_hook = f"${e['monthly']:,}", "month income (est.)"
        detail, show_price = "usd, after management", True
    else:                                          # no income estimate -> price is the hero
        hook = price_usd
        sub_hook = "house price" if free else price_yen
        detail, show_price = "", False
    return {"hook": hook, "sub_hook": sub_hook, "detail": detail,
            "price_usd": price_usd, "price_yen": price_yen,
            "price": "FREE" if free else f"{price_usd} ({price_yen})",
            "show_price": show_price, "specs": cover_facts(l),
            "location": cover_trip(h0, l), "place": hook_label(h0),
            "kind": KINDS[h0["kind"]]["label"], "handle": HANDLE}

def render_cover_html(fields):
    """Fills cover.html ({{ hook }}, {{ location }} ...) -> out/cover.html.
    Does nothing if there is no template file."""
    if not COVER_TEMPLATE.exists():
        return None
    tpl = COVER_TEMPLATE.read_text(encoding="utf-8")

    def fill(m):
        k = m.group(1)
        return escape(str(fields[k])) if k in fields else m.group(0)
    out = OUT / "cover.html"
    out.write_text(re.sub(r"\{\{\s*(\w+)\s*\}\}", fill, tpl), encoding="utf-8")
    print(f"Cover HTML: {out} | {fields['hook']} | {fields['location']}")
    return out

def cover_slide(photo, l, hooks, usd, e):
    """Slide 1. photo = PIL image (always given)."""
    c = cover_fields(l, hooks, usd, e)
    cx, white = W // 2, (255, 255, 255)
    soft, grey = (230, 230, 230), (200, 200, 200)
    img = shade(ImageOps.fit(photo, (W, H), Image.LANCZOS))
    d = ImageDraw.Draw(img)

    # (x, y, text, style, size, weight, colour, anchor, letter spacing)
    items = [
        (60, 60, HANDLE, "sans", 26, 500, white, "la", 1),
        (cx, 640, c["hook"], "serif", 300, 300, white, "ms", -4),
        (cx, 690, c["sub_hook"], "sans", 40, 300, soft, "mt", 2),
    ]
    if c["detail"]:
        items.append((cx, 760, c["detail"], "sans", 34, 400, soft, "mt", 0))
    if c["show_price"]:
        items.append((cx, H - 330, c["price"], "serif", 88, 300, white, "mt", 0))
    items.append((cx, H - 225, c["location"], "sans", 42, 500, white, "mt", 0))
    if c["specs"]:
        items.append((cx, H - 160, c["specs"], "sans", 30, 400, grey, "mt", 3))

    for x, y, s, style, size, weight, fill, anchor, track in items:
        f = fit_font(d, s, size, weight, track, style=style)
        put(d, (x, y), s, f, fill, anchor, track, shadow=3 if size >= 100 else 2)
    return img

# ── slides 2 & 3 ──
def area_bg(pic):
    """Area photo filling the slide with a black overlay (AREA_OVERLAY, 0.75 = 75%)."""
    img = ImageOps.fit(pic["img"], (W, H), Image.LANCZOS)
    return Image.blend(img, Image.new("RGB", (W, H), (0, 0, 0)), AREA_OVERLAY)

def put_credit(d, pic):
    credit = pic.get("credit") or ""
    if not credit:
        return
    f = fit_font(d, credit, 24, 400)
    put(d, (W - 60, H - 60), credit, f, (190, 190, 190), anchor="rs", shadow=0)

def fmt_years(y):
    """3.46 -> '~3.5 years', 1.0 -> '~1 year', 12.3 -> '~12 years'"""
    n = f"{y:.1f}".removesuffix(".0") if y < 10 else f"{y:.0f}"
    return f"~{n} year{'' if n == '1' else 's'}"

PLAYFAIR = FONT_DIR / "Serif-Playfair.ttf"        # Playfair Display Regular (slide 2)

@lru_cache(maxsize=64)
def pfont(size):
    """Playfair Display Regular; falls back to the normal serif if the file is missing."""
    try:
        return ImageFont.truetype(str(PLAYFAIR), size)
    except OSError:
        if PLAYFAIR not in _missing:
            _missing.add(PLAYFAIR)
            print(f"!! FONT MISSING: {PLAYFAIR} – slide 2 uses Serif-Light instead")
        return cfont("serif", size, 300)

def fit_pfont(d, s, size, maxw=W - 120):
    """Largest Playfair size (starting at size) that fits maxw."""
    while size > 18 and text_width(d, s, pfont(size)) > maxw:
        size -= 2
    return pfont(size)

def fmt_k1(v):
    """0 -> 'FREE', 950 -> '$950', 19000 -> '$19K', 17100 -> '$17.1K'"""
    if v < 1:
        return "FREE"
    if v >= 1000:
        return f"${v / 1000:.1f}".removesuffix(".0") + "K"
    return f"${v:,.0f}"

def _num(e, *names):
    """First of these keys in the estimate that holds a number."""
    for n in names:
        v = e.get(n)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
    return None

def cost_breakdown(e, usd):
    """'($19K + $17.1K reno + $3.5K fees)' = house + reno + one-off buying fees."""
    if usd is None:
        return "(house + reno + fees)"
    rest = max(0.0, e["all_in"] - usd)                  # reno + buying fees together
    reno = _num(e, "reno_usd", "reno", "renovation_usd", "renovation", "reno_cost")
    buy = _num(e, "buy_fees_usd", "buy_fees", "buying_fees", "closing_usd", "closing",
               "fees_once", "one_off_fees", "purchase_fees", "acq_fees")
    if reno is not None and not 0 <= reno <= rest + 1:
        reno = None                                     # wrong unit (yen?) -> ignore
    if buy is not None and not 0 <= buy <= rest + 1:
        buy = None
    if reno is None and buy is not None:
        reno = rest - buy
    elif buy is None and reno is not None:
        buy = rest - reno
    if reno is None:
        print(f"  slide 2: reno / buying-fee keys not found, estimate keys: {sorted(e)}")
        return f"({fmt_k1(usd)} + {fmt_k1(rest)} reno & fees)"
    return f"({fmt_k1(usd)} + {fmt_k1(reno)} reno + {fmt_k1(buy)} fees)"

def area_slide(pic, h, l, e, usd=None):
    """Slide 2: daytime photo + THE PAYBACK (numbers from yield_calc.estimate)."""
    img = area_bg(pic)
    d = ImageDraw.Draw(img)
    cx = W // 2
    white = (255, 255, 255)
    w85, w70, w60, w30 = (217, 217, 217), (179, 179, 179), (153, 153, 153), (77, 77, 77)
    SMALL = 21                                          # size of the 2 bottom lines
    fees_on = bool(e.get("fees_yearly"))
    after = "after management" + (" & fees" if fees_on else "")
    yrs = e.get("breakeven_yrs")
    real = show_yield(e) and yrs                        # same rule as slide 1 (hide if > 60%)

    # top: handle + title on one line
    put(d, (60, 62), HANDLE, cfont("sans", 24, 300), w70, "la", 1, 2)
    put(d, (cx, 62), "THE PAYBACK", cfont("sans", 22, 300), w60, "mt", 6, 0)

    if real:
        big, label = fmt_years(yrs), "to payback"
        sub = f"≈ ${e['monthly']:,} / month net, {after}"
    else:
        big, label = f"${e['monthly']:,}", "per month (est.)"
        sub = f"usd net, {after}"

    # 2 big Playfair lines, same size (both shrink together if one is too wide)
    size = 210
    while size > 80 and max(text_width(d, s, pfont(size)) for s in (big, label)) > W - 120:
        size -= 4
    bf = pfont(size)
    put(d, (cx, 600), big, bf, white, "ms", 0, 3)
    put(d, (cx, 600 + int(size * 0.95)), label, bf, white, "ms", 0, 3)

    put(d, (cx, 900), sub, fit_pfont(d, sub, 36), white, "ms", 0, 2)
    d.line([(cx - 100, 1000), (cx + 100, 1000)], fill=w30, width=1)

    # 2 detail lines, same font (Inter Regular)
    all_in = f"{fmt_k1(e['all_in'])} all-in {cost_breakdown(e, usd)}"
    line_a = f"{e['roi'] * 100:.0f}% net yield • {all_in}" if real else all_in
    mg = _num(e, "mgmt_pct", "management_pct", "mgmt_rate", "mgmt", "management")
    mg = 30 if mg is None else (mg * 100 if mg <= 1 else mg)
    line_b = (f"{hook_label(h)} • ${e['adr']:,}/nt × {e['occ'] * 100:.0f}% × "
              f"{e['nights']} days • After {mg:.0f}% mgmt{' & fees' if fees_on else ''}: "
              f"{fmt_k1(e['net'])}/yr")
    for y, s in ((1085, line_a), (1140, line_b)):
        put(d, (cx, y), s, fit_font(d, s, SMALL, 400), w85, "mm", 0, 2)

    return img

def facts_slide(pic, h, l, hooks, facts, e, s3):
    """Slide 3: dusk photo + WHY THIS RENTS. Same layout as before; wording from s3."""
    img = area_bg(pic)
    d = ImageDraw.Draw(img)
    white, soft = (255, 255, 255), (225, 225, 225)
    X = 190                                            # left edge of the text block
    MAXW = W - X - 130
    f = facts or {}
    name = re.split(r"\s*[(/]", hook_label(h))[0].strip()   # 'Beppu Onsen', 'Rusutsu ski resort'

    put(d, (60, 62), HANDLE, cfont("sans", 26, 500), white, "la", 1, 2)
    put(d, (W // 2, 62), "WHY THIS RENTS", cfont("sans", 22, 500), soft, "mt", 5, 0)

    # town + subtitle + divider
    put(d, (X, 470), name, fit_font(d, name, 130, 300, -1, MAXW, "serif"), white, "ls", -1, 3)
    sub = " • ".join(x for x in (f.get("known_for"), f.get("visitors")) if x)
    if not sub:
        sub = f"{KINDS[h['kind']]['label'].capitalize()} · {PREF_EN.get(l['pref'], l['pref'])}"
    put(d, (X, 510), sub, fit_font(d, sub, 34, 700, 0, MAXW), white, "la", 0, 2)
    d.line([(X, 615), (X + 410, 615)], fill=(170, 170, 170), width=2)

    # rotating headline + 3 town-specific bullets
    head = s3["headline"]
    put(d, (X, 665), head, fit_font(d, head, 56, 700, 0, MAXW), white, "la", 0, 3)
    y = 760
    for g in s3["points"][:3]:
        s = f"• {g}"
        put(d, (X + 6, y), s, fit_font(d, s, 38, 700, 0, MAXW - 6), white, "la", 0, 2)
        y += 54

    # 2 SEO lines (always at the same height, even with fewer than 3 bullets)
    y = 760 + 54 * 3 + 22
    for s in s3["lines"]:
        put(d, (X, y), s, fit_font(d, s, 34, 700, 0, MAXW), white, "la", 0, 2)
        y += 52

    # newsletter box
    cta = "Full breakdown + agent contact → newsletter link in bio"
    cf = fit_font(d, cta, 28, 700, 0, MAXW - 50)
    bw = text_width(d, cta, cf) + 50
    d.rounded_rectangle([X, 1100, X + bw, 1185], radius=6, outline=(235, 235, 235), width=2)
    put(d, (X + bw / 2, 1142), cta, cf, white, "mm", 0, 0)
    return img


# ─── reel (7 s vertical video, one centred line) ─────────────────────
REEL_FONT_FILES = [FONT_DIR / "Arimo-Bold.ttf",
                   FONT_DIR / "Reel-Bold.ttf",
                   "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
                   "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
                   "LiberationSans-Bold.ttf",
                   FONT_DIR / "Sans-Bold.ttf"]

@lru_cache(maxsize=1)
def reel_font_file():
    for p in REEL_FONT_FILES:
        try:
            ImageFont.truetype(str(p), 20)
            return str(p)
        except OSError:
            continue
    return None

@lru_cache(maxsize=128)
def rfont(size):
    p = reel_font_file()
    return ImageFont.truetype(p, size) if p else cfont("sans", size, 700)

def reel_candidates(h, facts, e, usd=None, l=None):
    """v2: max 4 words, [PRICE or NUMBER] + [PLACE or HOOK], all from our own data.
    '$15K KINOSAKI' > '19 AIRBNBS HERE' > '25 MIN TO KINOSAKI' > 'WHY $203 WORKS'"""
    out = []
    town = short_name(h["name"]).upper()
    if usd is not None and l is not None:
        out.append(f"FREE HOUSE, {town}" if l["price_yen"] == 0 else f"{fmt_usd(usd)} {town}")
    comp = ((facts or {}).get("points") or {}).get("competition") or ""
    m = re.search(r"(\d[\d,]*)\s*\+?\s*(?:airbnbs?|vacation rentals?|rentals?|listings?)",
                  comp, re.I)
    if m:
        out.append("1 AIRBNB HERE" if m.group(1) == "1" else f"{m.group(1)} AIRBNBS HERE")
    if l is not None:
        out.append(f"{trip_parts(h, l)[0]} MIN TO {town}")
    adr = e.get("adr")
    if isinstance(adr, (int, float)) and adr > 0:
        out.append(f"WHY ${int(round(adr)):,} WORKS")
    return out

def reel_line(d, h, facts, e, usd=None, l=None):
    """(text, font, size, tracking px) for the first line that fits, or None."""
    for s in reel_candidates(h, facts, e, usd, l):
        words = len(s.split())
        if words > REEL_WORDS:
            print(f"  reel: skip '{s}' ({words} words > {REEL_WORDS})")
            continue
        size = REEL_MAX_PX
        while size >= REEL_MIN_PX:
            f = rfont(size)
            track = size * REEL_TRACK / 1000
            if text_width(d, s, f, track) <= REEL_MAXW:
                return s, f, size, track
            size -= 2
        print(f"  reel: skip '{s}' (would need < {REEL_MIN_PX}px to fit)")
    return None

def ffmpeg_exe():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg")

def build_reel(pic, h, facts, e, usd=None, l=None):
    """out/reel.mp4: 1080x1920, 7.0 s, one shot with a slow zoom-in (Ken Burns),
    70% black, one centred line fading in at 0.5 s and out at 6.5 s.
    Returns (path, line) or (None, None)."""
    exe = ffmpeg_exe()
    if not exe:
        print("!! reel: ffmpeg not found (pip install imageio-ffmpeg) – no reel today")
        return None, None

    # background a bit bigger than the video, so zooming in keeps it sharp
    big_w, big_h = int(REEL_W * REEL_ZOOM), int(REEL_H * REEL_ZOOM)
    bg = ImageOps.fit(pic["img"], (big_w, big_h), Image.LANCZOS)
    bg = Image.blend(bg, Image.new("RGB", bg.size, (0, 0, 0)), REEL_OVERLAY)

    # text is drawn once as a mask; it does NOT zoom (stays sharp and readable)
    mask = Image.new("L", (REEL_W, REEL_H), 0)
    md = ImageDraw.Draw(mask)
    line = reel_line(md, h, facts, e, usd, l)
    if not line:
        print("!! reel: no line fits – no reel today")
        return None, None
    s, f, size, track = line
    put(md, (REEL_W / 2, REEL_H / 2), s, f, 255, "mm", track, 0)
    white = Image.new("RGB", (REEL_W, REEL_H), (255, 255, 255))

    frames = int(round(REEL_SECS * REEL_FPS))
    fade = max(1, round(REEL_FADE * REEL_FPS))
    on, off = round(REEL_TEXT_ON * REEL_FPS), round(REEL_TEXT_OFF * REEL_FPS)

    def text_alpha(i):
        if i < on or i >= off:
            return 0.0
        return min(1.0, (i - on + 1) / fade, (off - i) / fade)

    def frame(i):
        t = i / max(1, frames - 1)                       # 0 -> 1, steady speed
        z = 1 + (REEL_ZOOM - 1) * t                      # zoom factor now
        cw, ch = big_w / z, big_h / z                    # visible part of bg
        mx, my = (big_w - cw) / 2, (big_h - ch) / 2      # free margin
        x0, y0 = mx, my * (1 - 0.5 * t)                  # centred, drifting slightly up
        img = bg.resize((REEL_W, REEL_H), Image.BICUBIC,
                        box=(x0, y0, x0 + cw, y0 + ch))  # sub-pixel crop = smooth
        a = text_alpha(i)
        if a > 0:
            m = mask if a >= 1 else mask.point(lambda v, a=a: int(v * a))
            img.paste(white, (0, 0), m)
        return img

    frame(frames // 2).save(OUT / "reel_frame.jpg", "JPEG", quality=90)   # preview still

    out = OUT / "reel.mp4"
    cmd = [exe, "-y", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{REEL_W}x{REEL_H}",
           "-r", str(REEL_FPS), "-i", "-",
           "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
           "-t", f"{REEL_SECS}", "-c:v", "libx264", "-preset", "medium", "-crf", "18",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", "-movflags", "+faststart",
           str(out)]
    t0 = time.time()
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for i in range(frames):
            proc.stdin.write(frame(i).tobytes())
    except BrokenPipeError:
        pass
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
    err = proc.stderr.read().decode(errors="replace")
    proc.wait()
    if proc.returncode != 0 or not out.exists():
        print(f"!! reel: ffmpeg failed ({proc.returncode}): {err[:500]}")
        return None, None
    print(f"Reel: {out} | {s} | {size}px | {frames} frames, zoom 1.00→{REEL_ZOOM:.2f} | "
          f"{time.time() - t0:.0f}s | font {Path(reel_font_file() or 'fallback').name}")
    return out, s


def build_slides(l, hooks, usd, e, rot=None):
    """3 slides: cover, the payback (day), why this rents (dusk) + the reel.
    Returns (slide paths, area photo credits, slide 3 wording, (reel path, reel line, reel caption)),
    or (None, None, None, None) if no photo at all."""
    OUT.mkdir(exist_ok=True)
    for old in (list(OUT.glob("slide_*.jpg")) + list(OUT.glob("reel*"))
                + list(OUT.glob("cover.html"))):
        old.unlink()
    h0 = hooks[0]

    # listing photos: the cover, and a last-resort backup for slides 2 & 3
    house_pics = []
    for url in l.get("photos", []):
        if len(house_pics) >= HOUSE_PHOTOS:
            break
        img = download(url)
        if img:
            house_pics.append({"id": f"house:{url}", "img": img, "generic": False,
                               "credit": f"Photo: {site_info(l)[1]}"})
    print(f"House photos downloaded: {len(house_pics)}")

    cover = house_pics[0] if house_pics else None
    used = {cover["id"]} if cover else set()
    day = find_photo(h0, l, "day", used, house_pics)
    if day:
        used.add(day["id"])
    dusk = find_photo(h0, l, "dusk", used, house_pics)
    if cover is None:
        cover = day or dusk
        if cover:
            print("  no listing photo – the cover uses the area photo")
    if not (cover and day and dusk):
        print("!! no photo found anywhere – not posting today")
        return None, None, None, None

    facts = None
    try:
        facts = town_facts(h0, l)
    except Exception as ex:
        print(f"!! town facts error: {ex!r}")

    s3 = slide3_text(h0, l, hooks, facts, e, rot or {})
    print(f"Slide 3: template {s3['template']} | {s3['headline']} | "
          f"{' / '.join(s3['points'])} | {' / '.join(s3['lines'])}")

    def tag(p):
        return f"{p['id']}{' (generic)' if p.get('generic') else ''}"
    print(f"Slide photos: cover={tag(cover)} day={tag(day)} dusk={tag(dusk)} | facts: "
          f"{'found' if facts else 'fallback'}")

    slides = [cover_slide(cover["img"], l, hooks, usd, e),
              area_slide(day, h0, l, e, usd),
              facts_slide(dusk, h0, l, hooks, facts, e, s3)]
    paths = []
    for i, s in enumerate(slides, 1):
        p = OUT / f"slide_{i}.jpg"
        s.save(p, "JPEG", quality=90)
        paths.append(p)
    print(f"Cover: {cover_trip(h0, l)}")

    # optional HTML cover template (never stops the post if it fails)
    try:
        render_cover_html(cover_fields(l, hooks, usd, e))
    except Exception as ex:
        print(f"!! cover.html error: {ex!r}")

    # reel: AI video first (ski/onsen), photo reel as backup. Never stops the post.
    reel = (None, None, None)
    if REEL_ON:
        me = sys.modules[__name__]
        if ai_reel:
            try:
                reel = ai_reel.make(me, h0, l, usd, e, facts) or reel
            except Exception as ex:
                print(f"!! AI reel error: {ex!r} – using the photo reel")
        if not reel[0]:
            try:
                pic = {"dusk": dusk, "day": day, "cover": cover}.get(REEL_PHOTO, dusk)
                path, line = build_reel(pic, h0, facts, e, usd, l)
                if path:
                    cap = None
                    if ai_reel:
                        try:
                            cap = ai_reel.reel_caption(me, h0, l, usd, e)
                        except Exception as ex:
                            print(f"!! reel caption error: {ex!r}")
                    reel = (path, line, cap)
            except Exception as ex:
                print(f"!! reel error: {ex!r}")

    # remember every photo used, so later posts can reuse it
    try:
        for pic, mood in ((cover, None), (day, "day"), (dusk, "dusk")):
            role = "house" if pic["id"].startswith("house:") else "area"
            library_add(pic, role, mood if role == "area" else None, h0["kind"])
    except Exception as ex:
        print(f"!! photo library error: {ex!r}")

    credits = list(dict.fromkeys(p["credit"] for p in (day, dusk) if p.get("credit")))
    return paths, credits, s3, reel


# ─── caption ─────────────────────────────────────────────────────────
def build_caption(l, hooks, usd, e, fees_yen, fees_usd, area_credits=()):
    h0 = hooks[0]
    pref = PREF_EN.get(l["pref"], l["pref"])
    price_line = ("💴 Price: FREE 🎉" if l["price_yen"] == 0
                  else f"💴 Price: {fmt_yen(l['price_yen'])} (≈ {fmt_usd(usd)})")
    headline = (f"{KINDS[h0['kind']]['emoji']} {fmt_usd(usd)} house, "
                f"{fmt_trip(h0, l)} to {hook_label(h0)}.")
    roi_block = caption_text(e, headline)
    lines = [roi_block or headline,
             "",
             price_line,
             f"📍 {l['location']} ({pref})"]
    if len(hooks) > 1:
        also = ", ".join(f"{KINDS[h['kind']]['emoji']} {hook_label(h)} ({fmt_trip(h, l)})"
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
              f"Source & house photos: {site_info(l)[0]}"]
    if area_credits:
        lines.append("Area photos: " + "; ".join(c.replace("Photo: ", "", 1)
                                                 for c in area_credits))
    lines += [f"🔗 {l['url']}",
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

def tg_video(path, caption=""):
    """Sends the reel as a normal (streamable) video."""
    if DRY_RUN or not (BOT_TOKEN and CHAT_ID):
        print(f"[telegram skipped] reel {path}")
        return True
    try:
        with open(path, "rb") as fh:
            r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendVideo",
                              data={"chat_id": CHAT_ID, "caption": caption[:1024],
                                    "supports_streaming": "true", "width": REEL_W,
                                    "height": REEL_H, "duration": int(REEL_SECS)},
                              files={"video": fh}, timeout=120)
    except (requests.RequestException, OSError) as ex:
        print(f"Telegram reel error: {ex}")
        return False
    print(f"Telegram reel: {r.status_code} {r.text[:200] if not r.ok else ''}")
    return r.ok


# ─── picking ─────────────────────────────────────────────────────────
def pick(ranked, fx, last_source=None):
    """ranked = [(location_points, listing, hooks)], best location first.
    Score = location points + build-year points (-10..+5). Yield is NOT scored, so
    ranking needs no AirROI. Then walks down the list: fee rule (free page read),
    then an AirROI estimate for the top house. Cached estimates are free; at most
    MAX_AIRROI_CALLS paid calls per run (default 1). A house with no AirROI data
    or no income is skipped (it isn't postable without numbers)."""
    ac, fc = load_json(AIRROI_CACHE), load_json(FEE_CACHE)
    if not AIRROI_KEY:
        print("!! AIRROI_API_KEY missing – only houses already in the AirROI cache can be used")
    budget = MAX_AIRROI_CALLS if AIRROI_KEY else 0

    # 1) free ranking: location + build year
    order = []
    for hp, l, hooks in ranked:
        ap, age_label = age_points(l.get("year_built"))
        order.append((hp + ap, hp, ap, age_label, l, hooks))
    order.sort(key=lambda s: (s[0], s[1], -s[4]["price_yen"]), reverse=True)
    if ROTATE and last_source is not None:
        order.sort(key=lambda s: s[4].get("source") == last_source)   # other sites first (keeps order)
    no_year = sum(1 for s in order if s[3] == "built ?")

    # 2) first house that passes the fee rule gets the AirROI check
    tried = skip_fee = skip_yield = skip_data = skip_budget = 0
    chosen = None
    try:
        for total, hp, ap, age_label, l, hooks in order:
            if tried >= MAX_CHECK:
                break
            found, est = cached_estimate(l, ac)
            if not found and budget <= 0:
                skip_budget += 1                          # would need a paid call, limit reached
                continue
            tried += 1
            fees = yearly_fees(l, fc)
            if fees is not None and fees > FEE_LIMIT * l["price_yen"]:
                skip_fee += 1
                print(f"  skip, fees {fmt_yen(fees)}/yr > {FEE_LIMIT:.0%} of "
                      f"{fmt_yen(l['price_yen'])}: {l['url']}")
                continue
            if not found:
                budget -= 1
                print(f"  AirROI call for: {l['url']}")
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
            h0 = hooks[0]
            how = "road" if h0.get("routed") else "est."
            print(f"  [{l.get('source')}] {l['location']} {fmt_yen(l['price_yen'])} | "
                  f"{h0['name']} {fmt_trip(h0, l)} ({how}) | location {hp:.0f} + "
                  f"{age_label} ({ap:+.0f}) = {total:.0f} | yield {roi_pct:.0f}% on "
                  f"{fmt_k(e['all_in'])} all-in (not scored)")
            chosen = (l, hooks, e, fees)
            break
    finally:
        save_json(AIRROI_CACHE, ac)                       # never pay twice
        save_json(FEE_CACHE, fc)
    used = (MAX_AIRROI_CALLS if AIRROI_KEY else 0) - budget
    print(f"Tried: {tried}  |  fee rule skipped: {skip_fee}  |  no AirROI data: {skip_data}  |  "
          f"no income skipped: {skip_yield}  |  skipped (call limit): {skip_budget}  |  "
          f"build year unknown: {no_year}  |  AirROI calls used: {used}/{MAX_AIRROI_CALLS}  |  "
          f"last source: {last_source}")
    return chosen or (None, None, None, None)


# ─── main ────────────────────────────────────────────────────────────
def main():
    today = datetime.now(JST).strftime("%Y-%m-%d")
    print(f"Fonts folder: {FONT_DIR} | files: "
          f"{sorted(p.name for p in FONT_DIR.glob('*.ttf')) if FONT_DIR.exists() else 'FOLDER NOT FOUND'}"
          f" | raqm (lining numbers): {RAQM}")
    lib_size = len(load_json(LIBRARY_FILE))
    print(f"Area slides: Grok {'on' if GROK_KEY else 'OFF'} ({GROK_MODEL}) | "
          f"Pexels {'on' if PEXELS_KEY else 'OFF'} | overlay {AREA_OVERLAY:.0%} | "
          f"photo library {lib_size} | fallback_photos/ "
          f"{'found' if FALLBACK_DIR.exists() else 'none'} | towns.json "
          f"{'found' if TOWNS_FILE.exists() else 'none'} | cover template "
          f"{'found' if COVER_TEMPLATE.exists() else 'none'}")
    print(f"Reel: {'on' if REEL_ON else 'OFF'} | AI reel {'loaded' if ai_reel else 'not loaded'} | "
          f"font {reel_font_file() or 'MISSING (fallback)'} | "
          f"{REEL_MIN_PX}-{REEL_MAX_PX}px, max {REEL_WORDS} words | photo {REEL_PHOTO} | "
          f"zoom {REEL_ZOOM:.2f} | ffmpeg {'found' if ffmpeg_exe() else 'MISSING'}")
    fx = get_fx()
    max_yen = MAX_PRICE_USD / fx
    listings = gather()
    posted = load_json(POSTED_FILE)
    rot = load_json(ROTATION_FILE)
    last_source = rot.get("last_source")
    recent = rot.get("recent_hooks", [])
    print(f"Posted so far: {sum(1 for k in posted if not k.startswith('fp:'))} urls | "
          f"last slide 3 templates: {rot.get('s3_templates', [])[:5]}")

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
        tg_text(f"No deal today ({today}) – the top house(s) failed the fee "
                f"({FEE_LIMIT:.0%}) rule, had no AirROI data, or wouldn't make money "
                f"(AirROI limit: {MAX_AIRROI_CALLS} call/run). Will try the next one next run.")
        return

    usd = l["price_yen"] * fx
    fees_yen = fees or 0
    fees_usd = fees_yen * fx
    h0 = hooks[0]
    print(f"PICK [{l.get('source')}]: {l['location']} {fmt_yen(l['price_yen'])} "
          f"{fmt_trip(h0, l)} to {hook_label(h0)} ({h0['kind']}, {time_source(hooks)}), "
          f"{age_points(l.get('year_built'))[1]}, {e['roi'] * 100:.0f}% net yield, "
          f"~${e['monthly']:,}/mo\n  {l['url']}")

    paths, area_credits, s3, reel = build_slides(l, hooks, usd, e, rot)
    if paths is None:
        tg_text(f"No post today ({today}) – couldn't find any photo for the slides. "
                f"Will retry next run.")
        return
    caption = build_caption(l, hooks, usd, e, fees_yen, fees_usd, area_credits)
    (OUT / "caption.txt").write_text(caption, encoding="utf-8")

    ok = tg_album(paths, caption) and tg_text(caption)
    reel_path, reel_text, reel_cap = (tuple(reel or ()) + (None, None, None))[:3]
    if ok and reel_path:
        tg_video(reel_path, reel_cap or f"🎬 Reel: {reel_text}")   # a failed reel doesn't block the post
    if ok and not DRY_RUN:
        for u in l.get("all_urls", [l["url"]]):
            posted[u] = today
        posted["fp:" + l["fp"]] = today
        save_json(POSTED_FILE, posted)
        recent = ([h0["name"]] + [h for h in recent if h != h0["name"]])[:RECENT_HOOKS]
        rot.update({"last_source": l.get("source"), "date": today,
                    "recent_hooks": recent,
                    "s3_templates": ([s3["template"]] + (rot.get("s3_templates") or []))[:SLIDE3_HISTORY],
                    "s3_points": ([s3["kinds"]] + (rot.get("s3_points") or []))[:SLIDE3_HISTORY]})
        save_json(ROTATION_FILE, rot)
        print("Saved to state/posted.json + state/rotation.json")
    elif not ok:
        print("Telegram failed – not marking as posted, will retry next run")


if __name__ == "__main__":
    main()
