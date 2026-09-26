"""
Sumai空き家 (akiya.sumai.biz) scraper.
main.py calls scrape() -> list of listing dicts. Nothing runs on import.
Progress is saved every few pages, and scraping stops after a time limit,
so a slow run still moves forward and the next run continues where it stopped.
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

MAX_FETCH     = int(os.getenv("SUMAI_MAX_FETCH") or "150")              # detail pages per run
TIME_BUDGET   = float(os.getenv("SUMAI_TIME_BUDGET_MIN") or "8") * 60   # stop scraping after this
SAVE_EVERY    = 10                                   # save progress every N pages
DELAY         = 1.0                                  # seconds between pages
PAGE_TIMEOUT  = (10, 25)                             # (connect, read) seconds
GEO_TIMEOUT   = (5, 10)
GEO_FAIL_STOP = 3                                    # map service fails 3x in a row -> stop asking this run
GEO_RETRY_MAX = 300                                  # old listings without a location, retried per run
UA         = {"User-Agent": "Mozilla/5.0 (compatible; akiya-bot; 1 req/s)"}
SKIP_WORDS = ("受付停止", "交渉中", "商談中", "成約", "契約済", "売約")
PARSE_VERSION = 2      # bump when parsing improves -> old records missing a year get re-checked once

SESSION = requests.Session()                         # reuses connections = much faster
SESSION.headers.update(UA)

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
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except ValueError:
        print(f"  !! {p.name} was unreadable – starting it fresh")
        return {}

def save(p, data):
    STATE.mkdir(exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)                                    # never leaves a half-written file

def clean(s):
    """Full-width -> half-width (４ＬＤＫ -> 4LDK, ㎡ -> m2), drop 1,000 commas."""
    s = unicodedata.normalize("NFKC", s or "")
    return re.sub(r"(?<=\d),(?=\d)", "", s)

def fetch(url):
    try:
        r = SESSION.get(url, timeout=PAGE_TIMEOUT)
        if r.status_code == 200:
            return r
        print(f"  HTTP {r.status_code}: {url}")
    except requests.RequestException as e:
        print(f"  fetch error {url}: {e.__class__.__name__}")
    key = os.getenv("SCRAPER_API_KEY")
    if key:
        try:
            r = requests.get("https://api.scraperapi.com/",
                             params={"api_key": key, "url": url}, timeout=(10, 60))
            if r.status_code == 200:
                return r
            print(f"  ScraperAPI {r.status_code}: {url}")
        except requests.RequestException as e:
            print(f"  ScraperAPI error {url}: {e.__class__.__name__}")
    return None


# ─── sitemap ─────────────────────────────────────────────────────────
def post_urls():
    """({listing_url: lastmod}, complete) from every post-sitemap*.xml.
    complete=False if any part failed (then no listings are dropped from the cache)."""
    urls, complete = {}, True
    idx = fetch(f"{BASE}/sitemap.xml")
    if not idx:
        return urls, False
    try:
        subs = [e.text.strip() for e in ET.fromstring(idx.content).iter(NS + "loc")
                if e.text and "post-sitemap" in e.text]
    except ET.ParseError as e:
        print(f"  sitemap unreadable: {e}")
        return urls, False
    for sm in subs:
        r = fetch(sm)
        if not r:
            complete = False
            continue
        try:
            for u in ET.fromstring(r.content).iter(NS + "url"):
                loc = u.findtext(NS + "loc")
                if loc:
                    urls[loc.strip()] = (u.findtext(NS + "lastmod") or "").strip()
        except ET.ParseError as e:
            print(f"  sitemap part unreadable {sm}: {e}")
            complete = False
        time.sleep(DELAY)
    return urls, complete


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
    try:
        v = float(m.group(1))
    except ValueError:
        return None
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

def _gsi(q):
    """GSI hits for q ([] = answered, nothing found), or None if GSI didn't answer."""
    try:
        r = SESSION.get(GSI_URL, params={"q": q}, timeout=GEO_TIMEOUT)
        if not r.ok:
            print(f"  map lookup HTTP {r.status_code}: {q}")
            return None
        hits = r.json()
        return hits if isinstance(hits, list) else []
    except (requests.RequestException, ValueError) as e:
        print(f"  map lookup error ({q}): {e.__class__.__name__}")
        return None
    finally:
        time.sleep(0.3)

def geocode(pref, addr, geo):
    """Returns (result, failed).
    result: [lat, lng, level] = found, False = GSI answered but found nothing.
    failed=True: GSI didn't answer -> nothing is remembered, retried next run."""
    hit = geo.get(addr)
    if isinstance(hit, list) or hit is False:
        return hit, False                            # old None entries are asked again
    result = False
    for q, level in ((addr, "district"), (municipality(addr), "town")):
        if not q:
            continue
        key = "town:" + q if level == "town" else None
        if key:                                      # many houses share a town -> ask once
            cached = geo.get(key)
            if isinstance(cached, list):
                result = cached
                break
            if cached is False:
                continue
        hits = _gsi(q)
        if hits is None:
            return None, True
        found = False
        short = q.replace(pref, "")[:3]
        for h in hits:
            try:
                t = h.get("properties", {}).get("title", "")
                if pref in t or short in t:          # reject matches in other prefectures
                    lng, lat = h["geometry"]["coordinates"][:2]
                    found = [lat, lng, level]
                    break
            except (AttributeError, KeyError, TypeError, ValueError):
                continue
        if key:
            geo[key] = found
        if found:
            result = found
            break
    geo[addr] = result
    return result, False


# ─── entry point used by main.py ─────────────────────────────────────
def scrape():
    t0 = time.time()
    deadline = t0 + TIME_BUDGET
    cache, geo = load(CACHE_FILE), load(GEO_FILE)
    urls, complete = post_urls()
    print(f"Sitemap: {len(urls)} listing URLs{'' if complete else ' (INCOMPLETE)'} | "
          f"cache: {len(cache)}")
    if urls and complete:
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
    todo = (new + reparse)[:MAX_FETCH]
    print(f"{len(new)} new/updated + {len(reparse)} re-check (missing year), "
          f"fetching up to {MAX_FETCH} (time limit {TIME_BUDGET / 60:.0f} min)")

    geo_state = {"fails": 0, "down": False}

    def locate(rec):
        """Adds lat/lng. If the map service is down, leaves it for the next run."""
        if geo_state["down"]:
            return
        g, failed = geocode(rec["pref"], rec["location"], geo)
        if failed:
            geo_state["fails"] += 1
            if geo_state["fails"] >= GEO_FAIL_STOP:
                geo_state["down"] = True
                print("  !! map service (GSI) not answering – no more location lookups "
                      "this run, they will be retried next run")
            return
        geo_state["fails"] = 0
        rec.pop("geo_miss", None)
        if g:
            rec["lat"], rec["lng"], rec["geo_level"] = g
        else:
            rec["geo_miss"] = True                   # GSI really doesn't know this address

    def checkpoint():
        save(CACHE_FILE, cache)
        save(GEO_FILE, geo)

    fetched = usable = 0
    stop_note = ""
    try:
        for i, u in enumerate(todo, 1):
            if time.time() > deadline:
                stop_note = f" – stopped at the {TIME_BUDGET / 60:.0f}-min limit, rest next run"
                break
            rec = parse_page(u)
            time.sleep(DELAY)
            if rec is not None:
                rec["lastmod"] = urls[u]
                if rec["ok"]:
                    locate(rec)
                    usable += 1
                cache[u] = rec
                fetched += 1
            if i % SAVE_EVERY == 0:
                checkpoint()
                print(f"  {i}/{len(todo)} pages | {usable} usable | "
                      f"{time.time() - t0:.0f}s | saved")
        print(f"Pages done: {fetched}/{len(todo)} ({time.time() - t0:.0f}s){stop_note}")

        # listings saved earlier without a location (map service was down) -> try again
        retry = [v for v in cache.values()
                 if v.get("ok") and v.get("lat") is None and not v.get("geo_miss")]
        if retry and not geo_state["down"] and time.time() < deadline:
            print(f"Location retry: {len(retry)} listings without a location "
                  f"(up to {GEO_RETRY_MAX} this run)")
            for n, rec in enumerate(retry[:GEO_RETRY_MAX], 1):
                if time.time() > deadline or geo_state["down"]:
                    break
                locate(rec)
                if n % 25 == 0:
                    checkpoint()
    finally:
        checkpoint()

    out = [v for v in cache.values() if v.get("ok") and v.get("lat") is not None]
    skipped = sum(1 for v in cache.values() if not v.get("ok"))
    no_loc = sum(1 for v in cache.values() if v.get("ok") and v.get("lat") is None)
    no_year = sum(1 for v in out if v.get("year_built") is None)
    print(f"Usable listings: {len(out)} (skipped {skipped} rentals/unavailable/unparsed, "
          f"{no_loc} without a location yet) | build year unknown: {no_year} | "
          f"scraping took {time.time() - t0:.0f}s")
    return out


if __name__ == "__main__":        # local test: python scraper.py
    res = scrape()
    for x in res[:5]:
        print(x["location"], x["price_yen"], x["bedrooms"], x["year_built"], x.get("lat"))
