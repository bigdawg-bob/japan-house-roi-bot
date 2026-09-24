"""
Daily "cheap Japanese house near a ski resort / onsen / sight / beach / nature" bot.
Flow: scrape ALL sites (Sumai, At Home, LIFULL HOME'S) -> merge + remove duplicates
      -> keep houses under MAX_PRICE_USD within a ~45-min drive of a hook
      -> read yearly fees from the listing page, skip if > 15% of the price
      -> AirROI estimate (cached) -> post the ONE highest-scoring house
         (prefers a different site than the last post, when one qualifies)
      -> 1080x1350 slides -> Telegram (album + copyable caption).
Score = (Airbnb revenue - yearly fees) / price.  No AirROI data -> cheapest house.
"""
import io, json, math, os, re, textwrap, time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont, ImageOps

import scraper
import scraper_athome
import scraper_homes

# ─── settings ────────────────────────────────────────────────────────
MAX_PRICE_USD    = float(os.getenv("MAX_PRICE_USD", "100000"))
MAX_DRIVE_MIN    = float(os.getenv("MAX_DRIVE_MIN", "45"))
MAX_WALK_MIN     = float(os.getenv("MAX_WALK_MIN", "15"))
FEE_LIMIT        = float(os.getenv("FEE_LIMIT_PCT", "15")) / 100   # yearly fees vs price
MAX_AIRROI_CALLS = int(os.getenv("MAX_AIRROI_CALLS", "15"))        # paid calls per run
CACHE_DAYS       = 90                                               # re-ask AirROI after this
MAX_PHOTOS       = 5
W, H             = 1080, 1350
FX_FALLBACK      = 0.0067
DRY_RUN          = os.getenv("DRY_RUN") == "1"
ENABLED          = set(os.getenv("HOOK_KINDS", "ski,onsen,sight,beach,nature")
                       .replace(" ", "").split(","))
SITES_ON         = [s for s in os.getenv("SOURCES", "sumai,athome,homes")
                    .replace(" ", "").split(",") if s]
ROTATE           = os.getenv("ROTATE_SOURCES", "1") == "1"

# travel-time estimate (no routing API; straight line -> road distance -> minutes)
ROAD_FACTOR = 1.3     # roads are ~30% longer than a straight line
DRIVE_KMH   = 40      # average rural driving speed
WALK_KMH    = 4.8
MAX_KM      = MAX_DRIVE_MIN / 60 * DRIVE_KMH / ROAD_FACTOR   # ≈ 23 km straight line

ROOT          = Path(__file__).parent
STATE         = ROOT / "state"
POSTED_FILE   = STATE / "posted.json"
AIRROI_CACHE  = STATE / "airroi_cache.json"
FEE_CACHE     = STATE / "fees_cache.json"
ROTATION_FILE = STATE / "rotation.json"
OUT           = ROOT / "out"
JST           = timezone(timedelta(hours=9))

BOT_TOKEN   = os.getenv("BOT_TOKEN")
CHAT_ID     = os.getenv("CHAT_ID")
AIRROI_KEY  = os.getenv("AIRROI_API_KEY") or os.getenv("AIRROI_KEY")
AIRROI_URL  = os.getenv("AIRROI_URL", "https://api.airroi.com/calculator/estimate")

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

def drive_min(km): return km * ROAD_FACTOR / DRIVE_KMH * 60
def walk_min(km):  return km * ROAD_FACTOR / WALK_KMH * 60

def nearby_hooks(lat, lng):
    """Enabled attractions within MAX_DRIVE_MIN, closest first: [(kind, name, km)]"""
    hits = []
    for kind, name, a, b in HOOKS:
        if kind not in ENABLED:
            continue
        km = haversine(lat, lng, a, b)
        if drive_min(km) <= MAX_DRIVE_MIN:
            hits.append((kind, name, km))
    return sorted(hits, key=lambda h: h[2])

def fmt_trip(km, l):
    """'~10 min walk' only when we know the town; otherwise a drive estimate."""
    if l.get("geo_level") == "town" and walk_min(km) <= MAX_WALK_MIN:
        return f"~{max(5, round(walk_min(km) / 5) * 5)} min walk"
    return f"~{max(5, round(drive_min(km) / 5) * 5)} min drive"

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


# ─── all sites: gather + remove duplicates ───────────────────────────
def fingerprint(l):
    """Same town/district + same price = same house (across sites)."""
    a = re.sub(r"\s|大字|字", "", l.get("location") or "")
    a = re.split(r"[0-9０-９\-－−]", a)[0]
    return f"{a}|{int(l.get('price_yen') or 0)}"

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
FONT_PATHS = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
              "DejaVuSans-Bold.ttf", "arialbd.ttf", "Arial Bold.ttf"]

def font(size):
    for p in FONT_PATHS:
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()

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

def net_revenue(est, fees_usd):
    return est["revenue"] - fees_usd if est and est.get("revenue") else None

def yield_text(usd, est, fees_usd):
    net = net_revenue(est, fees_usd)
    if net is None:
        return None
    if usd == 0:
        return "Free house + Airbnb income"
    return f"{net / usd * 100:.0f}% est. yield after fees"

def cover_slide(photo, l, hooks, usd, est, fees_usd):
    kind, name, km = hooks[0]
    img = ImageOps.fit(photo, (W, H), Image.LANCZOS) if photo else Image.new("RGB", (W, H), (24, 44, 70))
    img = darken_bottom(img)
    d = ImageDraw.Draw(img)
    draw_text(d, (60, 60), KINDS[kind]["banner"], 44, (180, 220, 255))
    yt = yield_text(usd, est, fees_usd)
    if yt:
        draw_text(d, (60, 125), yt, 48, (140, 255, 170))
    y = H - 480 - (55 if len(hooks) > 1 else 0)
    y = draw_text(d, (60, y), fmt_usd(usd), 150)
    y = draw_text(d, (60, y + 10), fmt_yen(l["price_yen"]), 56, (230, 230, 230))
    y = draw_text(d, (60, y + 20), f"{fmt_trip(km, l)} to {name}", 54)
    if len(hooks) > 1:
        _, n2, km2 = hooks[1]
        y = draw_text(d, (60, y), f"+ {n2}, {fmt_trip(km2, l)}", 44, (200, 230, 255))
    draw_text(d, (60, y + 5), f"{PREF_EN.get(l['pref'], l['pref'])}, Japan", 48, (200, 200, 200))
    return img

def photo_slide(photo, l):
    img = ImageOps.fit(photo, (W, H), Image.LANCZOS)
    d = ImageDraw.Draw(img)
    credit = f"Photo: {site_info(l)[1]}"
    x = W - 60 - int(d.textlength(credit, font=font(32)))
    draw_text(d, (x, H - 70), credit, 32, (235, 235, 235))
    return img

def stats_slide(l, hooks, usd, est, fees_yen, fees_usd):
    kind, name, km = hooks[0]
    img = Image.new("RGB", (W, H), (18, 28, 45))
    d = ImageDraw.Draw(img)
    y = draw_text(d, (60, 70), "THE NUMBERS", 64, (180, 220, 255)) + 30
    price_txt = "FREE" if l["price_yen"] == 0 else f"{fmt_usd(usd)}  ({fmt_yen(l['price_yen'])})"
    rows = [("Price", price_txt),
            ("Location", f"{PREF_EN.get(l['pref'], l['pref'])}, Japan"),
            (f"Nearest {KINDS[kind]['label']}", f"{name}, {fmt_trip(km, l)}")]
    if len(hooks) > 1:
        _, n2, km2 = hooks[1]
        rows.append(("Also nearby", f"{n2}, {fmt_trip(km2, l)}"))
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
    if est:
        if est.get("revenue"):
            rows.append(("Airbnb est. revenue", f"${est['revenue']:,.0f} / year"))
            if usd > 0:
                net = net_revenue(est, fees_usd)
                rows.append(("Yield after fees", f"{net / usd * 100:.0f}% (before tax & renovation)"))
        if est.get("adr"):
            occ = f", {est['occupancy']:.0f}% booked" if est.get("occupancy") else ""
            rows.append(("Nightly rate est.", f"${est['adr']:,.0f}{occ}"))
    for label, value in rows:
        y = draw_text(d, (60, y), label.upper(), 34, (140, 160, 190))
        for line in textwrap.wrap(value, 30):
            y = draw_text(d, (60, y), line, 52)
        y += 16
    place = "town" if l.get("geo_level") == "town" else "district"
    note = f"Travel times are estimates from the {place} centre, not the exact house."
    yy = H - 160
    for line in textwrap.wrap(note, 48) + ["Link to the listing in the caption."]:
        yy = draw_text(d, (60, yy), line, 30, (150, 150, 150))
    return img

def build_slides(l, hooks, usd, est, fees_yen, fees_usd):
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
    slides = [cover_slide(photos[0] if photos else None, l, hooks, usd, est, fees_usd)]
    slides += [photo_slide(p, l) for p in photos[1:MAX_PHOTOS + 1]]
    slides.append(stats_slide(l, hooks, usd, est, fees_yen, fees_usd))
    paths = []
    for i, s in enumerate(slides, 1):
        p = OUT / f"slide_{i}.jpg"
        s.save(p, "JPEG", quality=90)
        paths.append(p)
    return paths


# ─── caption ─────────────────────────────────────────────────────────
def build_caption(l, hooks, usd, est, fees_yen, fees_usd):
    kind, name, km = hooks[0]
    pref = PREF_EN.get(l["pref"], l["pref"])
    price_line = ("💴 Price: FREE 🎉" if l["price_yen"] == 0
                  else f"💴 Price: {fmt_yen(l['price_yen'])} (≈ {fmt_usd(usd)})")
    lines = [f"{KINDS[kind]['emoji']} {fmt_usd(usd)} house, {fmt_trip(km, l)} to {name}",
             "",
             price_line,
             f"📍 {l['location']} ({pref})"]
    if len(hooks) > 1:
        also = ", ".join(f"{KINDS[k]['emoji']} {n} ({fmt_trip(d, l)})" for k, n, d in hooks[1:4])
        lines.append(f"🗺 Also near: {also}")
    if l.get("bedrooms"):
        lines.append(f"🛏 Rooms: {l['bedrooms']}")
    if l.get("year_built"):
        lines.append(f"🏗 Built: {l['year_built']}")
    if l.get("area_m2"):
        lines.append(f"📐 Floor area: {l['area_m2']:.0f} m²")
    if fees_yen:
        lines.append(f"🧾 Yearly fees: {fmt_yen(fees_yen)} (≈ {fmt_usd(fees_usd)})")
    if est and est.get("revenue"):
        lines.append(f"📈 Airbnb estimate: ${est['revenue']:,.0f}/year (AirROI, rough)")
        if usd > 0:
            net = net_revenue(est, fees_usd)
            lines.append(f"💰 Yield after fees: ~{net / usd * 100:.0f}% before tax & renovation")
    place = "town" if l.get("geo_level") == "town" else "district"
    kind_tags = " ".join(dict.fromkeys(KINDS[k]["tags"] for k, _, _ in hooks))
    lines += ["",
              f"Travel times are estimates ({place} centre).",
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
def pick(cands, fx, last_source=None):
    """Highest (revenue - fees) / price. Cached estimates are free; new ones limited
    to MAX_AIRROI_CALLS (cheapest first). Fallback: cheapest house passing the fee rule.
    With ROTATE_SOURCES on, prefers a site different from the last post (if one qualifies)."""
    ac, fc = load_json(AIRROI_CACHE), load_json(FEE_CACHE)
    budget = MAX_AIRROI_CALLS
    rotate = ROTATE and last_source is not None
    scored, fallback, fallback_other, skipped = [], None, None, 0
    try:
        for l, hooks in cands:
            found, est = cached_estimate(l, ac) if AIRROI_KEY else (False, None)
            can_score = bool(AIRROI_KEY) and (found or budget > 0)
            other = l.get("source") != last_source
            need_fb = fallback is None or (rotate and other and fallback_other is None)
            if not can_score and not need_fb:
                continue
            fees = yearly_fees(l, fc)
            if fees is not None and fees > FEE_LIMIT * l["price_yen"]:
                skipped += 1
                print(f"  skip, fees {fmt_yen(fees)}/yr > {FEE_LIMIT:.0%} of "
                      f"{fmt_yen(l['price_yen'])}: {l['url']}")
                continue
            if fallback is None:
                fallback = (l, hooks, None, fees)
            if other and fallback_other is None:
                fallback_other = (l, hooks, None, fees)
            if not can_score:
                continue
            if not found:
                budget -= 1
                est = fetch_estimate(l, ac)
            if not est or not est.get("revenue"):
                continue
            usd = l["price_yen"] * fx
            net = est["revenue"] - (fees or 0) * fx
            score = net / max(usd, 1)                     # free houses rank first
            print(f"  [{l.get('source')}] {l['location']} {fmt_yen(l['price_yen'])} -> "
                  f"${est['revenue']:,.0f}/yr, fees {fmt_yen(fees or 0)} = {score*100:.0f}%")
            scored.append((score, -l["price_yen"], l, hooks, est, fees))
    finally:
        save_json(AIRROI_CACHE, ac)                       # never pay twice
        save_json(FEE_CACHE, fc)
    print(f"Fee rule skipped: {skipped}  |  scored: {len(scored)}  |  "
          f"AirROI calls used: {MAX_AIRROI_CALLS - budget}  |  last source: {last_source}")

    if scored:
        scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
        best = scored[0]
        if rotate:
            best = next((s for s in scored if s[2].get("source") != last_source), best)
        _, _, l, hooks, est, fees = best
        return l, hooks, est, fees
    if fallback:
        print("No AirROI data – posting the cheapest house that passes the fee rule")
        return fallback_other if (rotate and fallback_other) else fallback
    return None, None, None, None


# ─── main ────────────────────────────────────────────────────────────
def main():
    today = datetime.now(JST).strftime("%Y-%m-%d")
    fx = get_fx()
    max_yen = MAX_PRICE_USD / fx
    listings = gather()
    posted = load_json(POSTED_FILE)
    last_source = load_json(ROTATION_FILE).get("last_source")

    cands, per_kind, per_src = [], {}, {}
    for l in listings:
        if already_posted(l, posted):
            continue
        if l.get("price_yen") is None or l["price_yen"] > max_yen:
            continue
        hooks = nearby_hooks(l["lat"], l["lng"])
        if not hooks:
            continue
        cands.append((l, hooks))
        per_kind[hooks[0][0]] = per_kind.get(hooks[0][0], 0) + 1
        per_src[l.get("source")] = per_src.get(l.get("source"), 0) + 1
    print(f"Candidates: {len(cands)} {per_kind} by site {per_src}  "
          f"(≤ ${MAX_PRICE_USD:,.0f} = ¥{max_yen:,.0f}, "
          f"≤ {MAX_DRIVE_MIN:.0f} min drive ≈ {MAX_KM:.0f} km, enabled: {', '.join(sorted(ENABLED))})")

    if not cands:
        tg_text(f"No deal today ({today}) – no new houses near any attraction.")
        return

    cands.sort(key=lambda c: (c[0]["price_yen"], c[1][0][2]))   # cheapest, then closest
    l, hooks, est, fees = pick(cands, fx, last_source)
    if l is None:
        tg_text(f"No deal today ({today}) – every candidate had yearly fees over {FEE_LIMIT:.0%} of the price.")
        return

    usd = l["price_yen"] * fx
    fees_yen = fees or 0
    fees_usd = fees_yen * fx
    kind, name, km = hooks[0]
    print(f"PICK [{l.get('source')}]: {l['location']} {fmt_yen(l['price_yen'])} "
          f"{fmt_trip(km, l)} to {name} ({kind})\n  {l['url']}")

    paths = build_slides(l, hooks, usd, est, fees_yen, fees_usd)
    caption = build_caption(l, hooks, usd, est, fees_yen, fees_usd)
    (OUT / "caption.txt").write_text(caption, encoding="utf-8")

    ok = tg_album(paths, caption) and tg_text(caption)
    if ok and not DRY_RUN:
        for u in l.get("all_urls", [l["url"]]):
            posted[u] = today
        posted["fp:" + l["fp"]] = today
        save_json(POSTED_FILE, posted)
        save_json(ROTATION_FILE, {"last_source": l.get("source"), "date": today})
        print("Saved to state/posted.json")
    elif not ok:
        print("Telegram failed – not marking as posted, will retry next run")


if __name__ == "__main__":
    main()
