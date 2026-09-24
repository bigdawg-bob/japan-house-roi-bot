"""
Daily "cheap Japanese house near a ski resort" bot.
Flow: scraper.scrape() -> keep houses within MAX_KM of a ski resort
      -> ask AirROI about the cheapest few (results cached in state/airroi_cache.json)
      -> post the one with the best Airbnb yield (or the cheapest if AirROI has no data)
      -> 1080x1350 slides -> Telegram (album + copyable caption).
"""
import io, json, math, os, textwrap
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont, ImageOps

import scraper

# ─── settings ────────────────────────────────────────────────────────
MAX_KM           = float(os.getenv("MAX_KM", "30"))         # max distance to a resort
MAX_PRICE_YEN    = float(os.getenv("MAX_PRICE_YEN", "5000000"))
MAX_AIRROI_CALLS = int(os.getenv("MAX_AIRROI_CALLS", "15"))  # paid calls per run
CHECK_TOP        = int(os.getenv("CHECK_TOP", "15"))         # cheapest N houses checked
CACHE_DAYS       = 90                                         # re-ask AirROI after this
MAX_PHOTOS       = 5                                          # photo slides after cover
W, H             = 1080, 1350                                 # Instagram portrait
FX_FALLBACK      = 0.0067                                     # USD per JPY if API fails
DRY_RUN          = os.getenv("DRY_RUN") == "1"

ROOT         = Path(__file__).parent
STATE        = ROOT / "state"
POSTED_FILE  = STATE / "posted.json"
AIRROI_CACHE = STATE / "airroi_cache.json"
OUT          = ROOT / "out"
JST          = timezone(timedelta(hours=9))

BOT_TOKEN   = os.getenv("BOT_TOKEN")
CHAT_ID     = os.getenv("CHAT_ID")
AIRROI_KEY  = os.getenv("AIRROI_API_KEY") or os.getenv("AIRROI_KEY")
AIRROI_URL  = os.getenv("AIRROI_URL", "https://api.airroi.com/calculator/estimate")

# (name, lat, lng) – approximate base-area coordinates
SKI_RESORTS = [
    ("Niseko Grand Hirafu", 42.862, 140.698),
    ("Rusutsu",             42.748, 140.555),
    ("Kiroro",              43.075, 140.985),
    ("Furano",              43.332, 142.358),
    ("Hakkoda",             40.656, 140.858),
    ("APPI Kogen",          40.003, 140.966),
    ("Kazuno Hanawa",       40.190, 140.750),
    ("Tazawako",            39.752, 140.726),
    ("Zao Onsen",           38.166, 140.415),
    ("Gassan",              38.528, 140.020),
    ("Aizu Takatsue",       37.117, 139.563),
    ("Oze Iwakura",         36.820, 139.200),
    ("Minakami",            36.830, 138.930),
    ("GALA Yuzawa",         36.947, 138.804),
    ("Naeba",               36.790, 138.760),
    ("Myoko Akakura",       36.887, 138.172),
    ("Madarao Kogen",       36.863, 138.297),
    ("Nozawa Onsen",        36.922, 138.444),
    ("Shiga Kogen",         36.707, 138.508),
    ("Hakuba Happo-one",    36.700, 137.832),
    ("Hakuba Goryu",        36.670, 137.830),
    ("Hakuba Cortina",      36.797, 137.853),
    ("Hida Nagareha",       36.325, 137.330),
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

def nearest_hook(lat, lng):
    return min(((n, haversine(lat, lng, a, b)) for n, a, b in SKI_RESORTS), key=lambda x: x[1])

def fmt_dist(km): return "<1 km" if km < 1 else f"~{km:.0f} km"
def fmt_usd(v):   return "FREE" if v == 0 else (f"${v/1000:.0f}K" if v >= 1000 else f"${v:.0f}")
def fmt_yen(v):   return "FREE" if v == 0 else (f"¥{v/1e6:.1f}M" if v >= 1e6 else f"¥{v/1e3:.0f}K")

def load_json(p):
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except ValueError:
        return {}

def save_json(p, data):
    p.parent.mkdir(exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


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


# ─── AirROI ──────────────────────────────────────────────────────────
def find_num(obj, names):
    """First number whose key is in names; checks the top level before going deeper."""
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
    rooms = l.get("bedrooms")          # "4LDK" -> 4 bedrooms
    return max(1, min(rooms or 3, 5))

def airroi_call(lat, lng, beds):
    """Returns (estimate or None, ok_to_cache)."""
    params = {"lat": lat, "lng": lng, "bedrooms": beds, "baths": 1,
              "guests": beds * 2, "currency": "usd"}
    try:
        r = requests.get(AIRROI_URL, params=params,
                         headers={"X-API-KEY": AIRROI_KEY}, timeout=30)
        print(f"AirROI {r.status_code}: {r.text[:600]}")
        if not r.ok:
            # 4xx other than auth/limit = no data for this spot -> cache it
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

def get_estimate(l, cache, budget):
    beds = bedrooms_for(l)
    key = f"{l['lat']:.3f},{l['lng']:.3f},{beds}"
    hit = cache.get(key)
    if hit:
        age = (datetime.now(JST) - datetime.fromisoformat(hit["date"])).days
        if age < CACHE_DAYS:
            return hit["est"]
    if budget["left"] <= 0:
        return None
    budget["left"] -= 1
    est, cacheable = airroi_call(l["lat"], l["lng"], beds)
    if cacheable:
        cache[key] = {"date": datetime.now(JST).isoformat(timespec="seconds"), "est": est}
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

def draw_text(d, xy, s, size, fill=(255, 255, 255)):
    x, y = xy
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

def yield_text(usd, est):
    if not est or not est.get("revenue"):
        return None
    if usd == 0:
        return "Free house + Airbnb income"
    return f"{est['revenue'] / usd * 100:.0f}% est. Airbnb yield"

def cover_slide(photo, l, resort, km, usd, est):
    img = ImageOps.fit(photo, (W, H), Image.LANCZOS) if photo else Image.new("RGB", (W, H), (24, 44, 70))
    img = darken_bottom(img)
    d = ImageDraw.Draw(img)
    draw_text(d, (60, 60), "JAPAN SKI AKIYA", 44, (180, 220, 255))
    yt = yield_text(usd, est)
    if yt:
        draw_text(d, (60, 125), yt, 48, (140, 255, 170))
    y = H - 480
    y = draw_text(d, (60, y), fmt_usd(usd), 150)
    y = draw_text(d, (60, y + 10), fmt_yen(l["price_yen"]), 56, (230, 230, 230))
    y = draw_text(d, (60, y + 20), f"{fmt_dist(km)} to {resort}", 54)
    draw_text(d, (60, y + 5), f"{PREF_EN.get(l['pref'], l['pref'])}, Japan", 48, (200, 200, 200))
    return img

def photo_slide(photo):
    img = ImageOps.fit(photo, (W, H), Image.LANCZOS)
    d = ImageDraw.Draw(img)
    draw_text(d, (W - 470, H - 70), "Photo: akiya.sumai.biz", 32, (235, 235, 235))
    return img

def stats_slide(l, resort, km, usd, est):
    img = Image.new("RGB", (W, H), (18, 28, 45))
    d = ImageDraw.Draw(img)
    y = draw_text(d, (60, 70), "THE NUMBERS", 64, (180, 220, 255)) + 30
    rows = [("Price", f"{fmt_usd(usd)}  ({fmt_yen(l['price_yen'])})"),
            ("Location", f"{PREF_EN.get(l['pref'], l['pref'])}, Japan"),
            ("Nearest ski resort", f"{resort}, {fmt_dist(km)}")]
    house = []
    if l.get("bedrooms"):
        house.append(f"{l['bedrooms']} rooms")
    if l.get("year_built"):
        house.append(f"built {l['year_built']}")
    if l.get("area_m2"):
        house.append(f"{l['area_m2']:.0f} m²")
    if house:
        rows.append(("House", " · ".join(house)))
    if est:
        if est.get("revenue"):
            rows.append(("Airbnb est. revenue", f"${est['revenue']:,.0f} / year"))
            yt = yield_text(usd, est)
            if yt and usd > 0:
                rows.append(("Gross yield (before costs)", f"{est['revenue'] / usd * 100:.0f}%"))
        if est.get("adr"):
            occ = f", {est['occupancy']:.0f}% booked" if est.get("occupancy") else ""
            rows.append(("Nightly rate est.", f"${est['adr']:,.0f}{occ}"))
    for label, value in rows:
        y = draw_text(d, (60, y), label.upper(), 34, (140, 160, 190))
        for line in textwrap.wrap(value, 30):
            y = draw_text(d, (60, y), line, 52)
        y += 22
    place = "town" if l.get("geo_level") == "town" else "district"
    note = f"Distance measured from the {place} centre, not the exact house."
    yy = H - 160
    for line in textwrap.wrap(note, 48) + ["Link to the listing in the caption."]:
        yy = draw_text(d, (60, yy), line, 30, (150, 150, 150))
    return img

def build_slides(l, resort, km, usd, est):
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
    slides = [cover_slide(photos[0] if photos else None, l, resort, km, usd, est)]
    slides += [photo_slide(p) for p in photos[1:MAX_PHOTOS + 1]]
    slides.append(stats_slide(l, resort, km, usd, est))
    paths = []
    for i, s in enumerate(slides, 1):
        p = OUT / f"slide_{i}.jpg"
        s.save(p, "JPEG", quality=90)
        paths.append(p)
    return paths


# ─── caption ─────────────────────────────────────────────────────────
def build_caption(l, resort, km, usd, est):
    pref = PREF_EN.get(l["pref"], l["pref"])
    lines = [f"🏔 {fmt_usd(usd)} house {fmt_dist(km)} from {resort}",
             "",
             f"💴 Price: {fmt_yen(l['price_yen'])} (≈ {fmt_usd(usd)})",
             f"📍 {l['location']} ({pref})"]
    if l.get("bedrooms"):
        lines.append(f"🛏 Rooms: {l['bedrooms']}")
    if l.get("year_built"):
        lines.append(f"🏗 Built: {l['year_built']}")
    if l.get("area_m2"):
        lines.append(f"📐 Floor area: {l['area_m2']:.0f} m²")
    if est and est.get("revenue"):
        lines.append(f"📈 Airbnb estimate: ${est['revenue']:,.0f}/year (AirROI, rough)")
        if usd > 0:
            lines.append(f"💰 Gross yield: ~{est['revenue'] / usd * 100:.0f}% before costs & renovation")
    lines += ["",
              "Distance is approximate (district centre).",
              "Source & photos: Sumai空き家 / local akiya bank",
              f"🔗 {l['url']}",
              "",
              "#akiya #japanhouse #cheaphouse #skijapan #japow #moveto" + pref.lower()
              + " #japanrealestate #空き家 #古民家"]
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
def pick(cands, fx):
    """Best Airbnb yield among the cheapest CHECK_TOP; cheapest if no AirROI data."""
    if not AIRROI_KEY:
        print("AirROI: no AIRROI_API_KEY secret, posting the cheapest house")
        l, resort, km = cands[0]
        return l, resort, km, None

    cache = load_json(AIRROI_CACHE)
    budget = {"left": MAX_AIRROI_CALLS}
    scored = []
    try:
        for l, resort, km in cands[:CHECK_TOP]:
            est = get_estimate(l, cache, budget)
            if not est or not est.get("revenue"):
                continue
            usd = l["price_yen"] * fx
            yld = est["revenue"] / max(usd, 1)          # free houses rank first
            print(f"  {l['location']} {fmt_yen(l['price_yen'])} -> "
                  f"${est['revenue']:,.0f}/yr = {yld*100:.0f}%")
            scored.append((yld, -l["price_yen"], l, resort, km, est))
    finally:
        save_json(AIRROI_CACHE, cache)                   # never pay twice
    print(f"AirROI calls used this run: {MAX_AIRROI_CALLS - budget['left']}")

    if not scored:
        print("AirROI: no usable estimates, posting the cheapest house")
        l, resort, km = cands[0]
        return l, resort, km, None
    scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
    _, _, l, resort, km, est = scored[0]
    return l, resort, km, est


# ─── main ────────────────────────────────────────────────────────────
def main():
    today = datetime.now(JST).strftime("%Y-%m-%d")
    listings = scraper.scrape()
    posted = load_json(POSTED_FILE)

    cands = []
    for l in listings:
        if l["url"] in posted:
            continue
        if l.get("price_yen") is None or l["price_yen"] > MAX_PRICE_YEN:
            continue
        name, km = nearest_hook(l["lat"], l["lng"])
        if km > MAX_KM:
            continue
        cands.append((l, name, km))
    print(f"Candidates within {MAX_KM:.0f} km of a ski resort: {len(cands)}")

    if not cands:
        tg_text(f"No deal today ({today}) – no new houses within {MAX_KM:.0f} km of a ski resort.")
        return

    cands.sort(key=lambda c: (c[0]["price_yen"], c[2]))       # cheapest, then closest
    fx = get_fx()
    l, resort, km, est = pick(cands, fx)
    usd = l["price_yen"] * fx
    print(f"PICK: {l['location']} {fmt_yen(l['price_yen'])} {fmt_dist(km)} to {resort}\n  {l['url']}")

    paths = build_slides(l, resort, km, usd, est)
    caption = build_caption(l, resort, km, usd, est)
    (OUT / "caption.txt").write_text(caption, encoding="utf-8")

    ok = tg_album(paths, caption) and tg_text(caption)
    if ok and not DRY_RUN:
        posted[l["url"]] = today
        save_json(POSTED_FILE, posted)
        print("Saved to state/posted.json")
    elif not ok:
        print("Telegram failed – not marking as posted, will retry next run")


if __name__ == "__main__":
    main()
