"""
Sumai空き家 (akiya.sumai.biz) scraper.
main.py calls scrape() -> list of listing dicts. Nothing runs on import.
"""
import json, os, re, time, unicodedata
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE       = "https://akiya.sumai.biz"
STATE      = Path(__file__).parent / "state"
CACHE_FILE = STATE / "sumai_cache.json"
GEO_FILE   = STATE / "geocode.json"
GSI_URL    = "https://msearch.gsi.go.jp/address-search/AddressSearch"
NS         = "{http://www.sitemaps.org/schemas/sitemap/0.9}"

MAX_FETCH  = int(os.getenv("SUMAI_MAX_FETCH", "150"))   # detail pages per run
DELAY      = 1.0                                         # seconds between pages
UA         = {"User-Agent": "Mozilla/5.0 (compatible; akiya-bot; 1 req/s)"}
SKIP_WORDS = ("受付停止", "交渉中", "商談中", "成約", "契約済", "売約")
PARSE_VERSION = 2      # bump when parsing improves -> old records missing a year get re-checked once

PREFS = ("北海道 青森県 岩手県 宮城県 秋田県 山形県 福島県 茨城県 栃木県 群馬県 埼玉県 "
         "千葉県 東京都 神奈川県 新潟県 富山県 石川県 福井県 山梨県 長野県 岐阜県 静岡県 "
         "愛知県 三重県 滋賀県 京都府 大阪府 兵庫県 奈良県 和歌山県 鳥取県 島根県 岡山県 "
         "広島県 山口県 徳島県 香川県 愛媛県 高知県 福岡県 佐賀県 長崎県 熊本県 大分県 "
         "宮崎県 鹿児島県 沖縄県").split()
PREF_RE = re.compile("(" + "|".join(PREFS) + r")([^\s（(【]+)")

# ── build-year patterns ──
ERAS = {"明治": 1867, "大正": 1911, "昭和": 1925, "平成": 1988, "令和": 2018}   # 昭和1年 = 1926
ERA_RE  = r"(明治|大正|昭和|平成|令和)\s*(\d{1,2}|元)\s*年"
WEST_RE = r"(?<!\d)((?:18|19|20)\d\d)\s*年"
# "...年築", "...年3月築", "...年頃建築", "...年新築", "1926年(大正15年)築"
# (改築 / 増築 / 移築 = renovation or relocation, NOT the build date -> not matched)
BUILT_TAIL = r"\s*(?:\d{1,2}\s*月\s*)?(?:\d{1,2}\s*日\s*)?(?:頃\s*)?\)?\s*(?:新|建)?築"
YEAR_LABEL = (r"(?<![改増移])(?:築年月日|築年月|築年数|築年|建築年月日|建築年月|建築年次|"
              r"建築年|建築時期|竣工年月|竣工)")
FILLER  = r"[^0-9明大昭平令]{0,15}"      # label ... value (stops at a digit or era name)
AGE_RE  = r"(?<![改増移])築(?:年数)?\s*:?\s*(?:約|およそ)?\s*(\d{1,3})\s*年"   # 築45年 / 築年数 約50年


# ─── helpers ─────────────────────────────────────────────────────────
def load(p):
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

def save(p, data):
    STATE.mkdir(exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

def clean(s):
    """Full-width -> half-width (４ＬＤＫ -> 4LDK, ㎡ -> m2), drop 1,000 commas."""
    s = unicodedata.normalize("NFKC", s or "")
    return re.sub(r"(?<=\d),(?=\d)", "", s)

def fetch(url):
    try:
        r = requests.get(url, headers=UA, timeout=30)
        if r.status_code == 200:
            return r
        print(f"  HTTP {r.status_code}: {url}")
    except requests.RequestException as e:
        print(f"  fetch error {url}: {e}")
    key = os.getenv("SCRAPER_API_KEY")
    if key:
        try:
            r = requests.get("https://api.scraperapi.com/",
                             params={"api_key": key, "url": url}, timeout=70)
            if r.status_code == 200:
                return r
            print(f"  ScraperAPI {r.status_code}: {url}")
        except requests.RequestException as e:
            print(f"  ScraperAPI error {url}: {e}")
    return None


# ─── sitemap ─────────────────────────────────────────────────────────
def post_urls():
    """{listing_url: lastmod} from every post-sitemap*.xml"""
    urls = {}
    idx = fetch(f"{BASE}/sitemap.xml")
    if not idx:
        return urls
    subs = [e.text.strip() for e in ET.fromstring(idx.content).iter(NS + "loc")
            if e.text and "post-sitemap" in e.text]
    for sm in subs:
        r = fetch(sm)
        if r:
            for u in ET.fromstring(r.content).iter(NS + "url"):
                loc = u.findtext(NS + "loc")
                if loc:
                    urls[loc.strip()] = (u.findtext(NS + "lastmod") or "").strip()
        time.sleep(DELAY)
    return urls


# ─── parsing ─────────────────────────────────────────────────────────
def parse_price(t):
    if "無償" in t:
        return 0.0                                   # free transfer
    m = re.search(r"【売買】\s*((?:[\d.]+\s*万円\s*→?\s*)+)", t)
    if not m:
        return None                                  # rental-only or no price
    nums = re.findall(r"([\d.]+)\s*万円", m.group(1))
    return float(nums[-1]) * 10_000                  # "20万円→10万円" -> 10万

def parse_address(t):
    m = PREF_RE.search(t)
    return (m.group(1), m.group(1) + m.group(2)) if m else (None, None)

def parse_rooms(t):
    m = re.search(r"(\d+)\s*(?:S?LDK|SDK|DK|K|部屋)", t)
    return int(m.group(1)) if m else None

def _valid(y):
    return y if y and 1868 <= y <= date.today().year else None

def _match_year(m):
    """Era match (昭和, 55) or Western match (1980) -> Western year."""
    if m.group(1) in ERAS:
        return ERAS[m.group(1)] + (1 if m.group(2) == "元" else int(m.group(2)))
    return int(m.group(1))

def parse_year(title, body):
    """Western build year (int) or None. Most reliable patterns first."""
    both = title + " " + body
    for text, pat in (
        (title, ERA_RE + BUILT_TAIL),            # title: 昭和55年築
        (title, WEST_RE + BUILT_TAIL),           # title: 1980年築
        (body, YEAR_LABEL + FILLER + ERA_RE),    # 建築年月: 昭和55年
        (body, YEAR_LABEL + FILLER + WEST_RE),   # 築年: 1980年
        (body, ERA_RE + BUILT_TAIL),             # 昭和55年3月築 anywhere
        (body, WEST_RE + BUILT_TAIL),            # 1980年頃建築 anywhere
    ):
        m = re.search(pat, text)
        if m:
            y = _valid(_match_year(m))
            if y:
                return y
    m = re.search(AGE_RE, both)                  # 築45年 -> this year - 45
    if m:
        return _valid(date.today().year - int(m.group(1)))
    return None

def parse_area(body):
    m = re.search(r"(?:延べ?床面積|建物面積|床面積)[^0-9]{0,15}([\d.]+)\s*(m2|平米|坪)", body)
    if not m:
        return None
    v = float(m.group(1))
    return round(v * 3.3058, 1) if m.group(2) == "坪" else v

def parse_photos(soup, content):
    out = []
    for img in content.find_all("img"):
        src = img.get("data-src") or img.get("data-lazy-src") or img.get("src") or ""
        if "wp-content/uploads" not in src or re.search(r"logo|icon|banner|noimage", src, re.I):
            continue
        src = urljoin(BASE, src)
        out += [re.sub(r"-\d+x\d+(?=\.(?:jpe?g|png|webp)$)", "", src, flags=re.I), src]
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        out.append(og["content"])
    return list(dict.fromkeys(out))

def parse_page(url):
    r = fetch(url)
    if r is None:
        return None                                   # retry next run
    soup = BeautifulSoup(r.content, "html.parser")
    h1 = soup.select_one("h1.entry-title") or soup.find("h1")
    og = soup.find("meta", property="og:title")
    raw = h1.get_text(" ", strip=True) if h1 else (og.get("content", "") if og else "")
    title = clean(re.sub(r"\s*[|｜]\s*Sumai空き家.*$", "", raw))
    content = soup.select_one(".entry-content") or soup.find("article") or soup.body or soup
    body = clean(content.get_text(" ", strip=True))

    rec = {"id": url, "url": url, "title": title[:200], "ok": False, "pv": PARSE_VERSION}
    if any(w in title for w in SKIP_WORDS):
        rec["why"] = "not available"
        return rec
    price = parse_price(title)
    pref, addr = parse_address(title)
    if price is None or not addr:
        rec["why"] = "no sale price" if price is None else "no address"
        return rec
    rec.update(ok=True, price_yen=price, pref=pref, location=addr,
               bedrooms=parse_rooms(title), year_built=parse_year(title, body),
               area_m2=parse_area(body), photos=parse_photos(soup, content)[:8])
    return rec


# ─── geocoding (GSI, free, no key) ───────────────────────────────────
def municipality(addr):
    m = re.match(r"(.+?郡.+?[町村]|.+?[市町村])", addr)
    return m.group(1) if m else None

def geocode(pref, addr, geo):
    if addr in geo:
        return geo[addr]
    result = None
    for q, level in ((addr, "district"), (municipality(addr), "town")):
        if not q:
            continue
        try:
            r = requests.get(GSI_URL, params={"q": q}, timeout=20)
            hits = r.json() if r.ok else []
        except (requests.RequestException, ValueError):
            hits = []
        time.sleep(0.3)
        short = q.replace(pref, "")[:3]
        for h in hits:
            t = h.get("properties", {}).get("title", "")
            if pref in t or short in t:              # reject matches in other prefectures
                lng, lat = h["geometry"]["coordinates"]
                result = [lat, lng, level]
                break
        if result:
            break
    geo[addr] = result
    return result


# ─── entry point used by main.py ─────────────────────────────────────
def scrape():
    cache, geo = load(CACHE_FILE), load(GEO_FILE)
    urls = post_urls()
    print(f"Sitemap: {len(urls)} listing URLs | cache: {len(cache)}")
    if urls:
        cache = {u: v for u, v in cache.items() if u in urls}     # drop removed listings

    by_date = sorted(urls.items(), key=lambda x: x[1], reverse=True)
    new = [u for u, mod in by_date
           if u not in cache or cache[u].get("lastmod") != mod]
    new_set = set(new)
    # saved with the old parser and still missing a year -> re-check once (after new pages)
    reparse = [u for u, _ in by_date
               if u not in new_set and cache[u].get("ok")
               and cache[u].get("year_built") is None
               and cache[u].get("pv", 1) < PARSE_VERSION]
    todo = new + reparse
    print(f"{len(new)} new/updated + {len(reparse)} re-check (missing year), "
          f"fetching up to {MAX_FETCH}")

    try:
        for u in todo[:MAX_FETCH]:
            rec = parse_page(u)
            time.sleep(DELAY)
            if rec is None:
                continue
            rec["lastmod"] = urls[u]
            if rec["ok"]:
                g = geocode(rec["pref"], rec["location"], geo)
                if g:
                    rec["lat"], rec["lng"], rec["geo_level"] = g
            cache[u] = rec
    finally:
        save(CACHE_FILE, cache)
        save(GEO_FILE, geo)

    out = [v for v in cache.values() if v.get("ok") and v.get("lat") is not None]
    skipped = sum(1 for v in cache.values() if not v.get("ok"))
    no_year = sum(1 for v in out if v.get("year_built") is None)
    print(f"Usable listings: {len(out)} (skipped {skipped} rentals/unavailable/unparsed) "
          f"| build year unknown: {no_year}")
    return out


if __name__ == "__main__":        # local test: python scraper.py
    res = scrape()
    for x in res[:5]:
        print(x["location"], x["price_yen"], x["bedrooms"], x["year_built"], x.get("lat"))
