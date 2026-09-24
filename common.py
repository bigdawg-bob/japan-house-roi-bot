"""
Shared helpers for the extra scrapers (At Home, LIFULL HOME'S).
Reuses fetch / geocode / address parsing from scraper.py, so every site
returns the same listing dicts that main.py already understands.
"""
import re, time
from datetime import date
from urllib import robotparser
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

import scraper as base

# prefecture slugs in the same order as scraper.PREFS (JIS codes 01-47)
SLUGS = ("hokkaido aomori iwate miyagi akita yamagata fukushima ibaraki tochigi gunma "
         "saitama chiba tokyo kanagawa niigata toyama ishikawa fukui yamanashi nagano gifu "
         "shizuoka aichi mie shiga kyoto osaka hyogo nara wakayama tottori shimane okayama "
         "hiroshima yamaguchi tokushima kagawa ehime kochi fukuoka saga nagasaki kumamoto "
         "oita miyazaki kagoshima okinawa").split()

RECHECK_DAYS = 10     # re-read a listing page after this (catches sold / price changes)
KEEP_DAYS    = 21     # forget listings not seen on the list pages for this long
BOT_NAME     = "akiya-bot"


# ─── polite fetching ─────────────────────────────────────────────────
_robots = {}

def allowed(url):
    """True unless the site's robots.txt forbids this page."""
    u = urlparse(url)
    root = f"{u.scheme}://{u.netloc}"
    if root not in _robots:
        rp = None
        try:
            r = requests.get(root + "/robots.txt", headers=base.UA, timeout=15)
            if r.status_code == 200:
                rp = robotparser.RobotFileParser()
                rp.parse(r.text.splitlines())
        except requests.RequestException:
            pass
        _robots[root] = rp
    rp = _robots[root]
    ok = rp is None or rp.can_fetch(BOT_NAME, url)
    if not ok:
        print(f"  robots.txt says no, skipping: {url}")
    return ok

def get(url):
    if not allowed(url):
        return None
    r = base.fetch(url)
    time.sleep(base.DELAY)
    return r

def show_links(r, name):
    """Debug help: print some links when we can't find listings on a page."""
    soup = BeautifulSoup(r.content, "html.parser")
    hrefs = list(dict.fromkeys(a.get("href") for a in soup.find_all("a") if a.get("href")))
    print(f"  [{name} debug] found no listing links on {r.url}. First links on the page:")
    for h in hrefs[:25]:
        print(f"    {h}")


# ─── parsing ─────────────────────────────────────────────────────────
def fields(soup):
    """{label: value} from <th>/<td> and <dt>/<dd> pairs."""
    out = {}
    for th in soup.find_all(["th", "dt"]):
        k = re.sub(r"\s+", "", base.clean(th.get_text(" ", strip=True)))
        td = th.find_next_sibling(["td", "dd"])
        if k and td and k not in out:
            out[k] = base.clean(td.get_text(" ", strip=True))
    return out

def field(f, *names):
    for n in names:
        for k, v in f.items():
            if k.startswith(n):
                return v
    return None

def grab(text, pattern):
    m = re.search(pattern, text)
    return m.group(1) if m else None

def parse_yen(s):
    if not s:
        return None
    s = base.clean(s)
    if "億" in s:
        return 1e9                                   # way over budget
    if "無償" in s or "無料" in s:
        return 0.0
    man = re.findall(r"([\d.]+)\s*万\s*円", s)
    if man:
        return float(man[-1]) * 10_000               # "20万円→10万円" -> 10万
    yen = re.findall(r"(\d+)\s*円", s)
    return float(yen[-1]) if yen else None           # "相談" -> None

def parse_year(s):
    if not s:
        return None
    m = re.search(r"((?:18|19|20)\d\d)\s*年", s)
    if m:
        return int(m.group(1))
    m = re.search(base.ERA_RE, s)
    if m:
        return base.ERAS[m.group(1)] + (1 if m.group(2) == "元" else int(m.group(2)))
    return None

def parse_area(s):
    if not s:
        return None
    m = re.search(r"([\d.]+)\s*(m2|m²|平米|坪)", s)
    if not m:
        return None
    v = float(m.group(1))
    return round(v * 3.3058, 1) if m.group(2) == "坪" else v

def parse_rooms(s):
    m = re.search(r"(\d+)\s*S?L?D?K", s or "")
    return int(m.group(1)) if m else None

def norm_addr(s):
    s = re.sub(r"周辺情報を調べる|地図を見る|地図", "", s or "")
    return re.sub(r"\s+", "", s)

def page_title(soup):
    h1 = soup.find("h1")
    og = soup.find("meta", property="og:title")
    t = h1.get_text(" ", strip=True) if h1 else (og.get("content", "") if og else "")
    if not t and soup.title:
        t = soup.title.get_text(" ", strip=True)
    t = re.sub(r"\s*の物件詳細.*$|\s*[|｜].*$|【(?:アットホーム|ホームズ).*$", "", t)
    return base.clean(t)

def photos(soup, page_url, limit=8):
    out = []
    for img in soup.find_all("img"):
        src = (img.get("data-src") or img.get("data-original") or
               img.get("data-lazy-src") or img.get("src") or "")
        if not re.search(r"\.(jpe?g|png|webp)(\?|$)", src, re.I):
            continue
        if re.search(r"logo|icon|banner|noimage|no_image|btn|button|spacer|sprite|qr", src, re.I):
            continue
        out.append(urljoin(page_url, src))
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        out.append(urljoin(page_url, og["content"]))
    return list(dict.fromkeys(out))[:limit]

def parse_detail(url):
    """One listing page -> listing dict (ok=False with a reason if unusable)."""
    r = get(url)
    if r is None:
        return None                                   # retry next run
    soup = BeautifulSoup(r.content, "html.parser")
    title = page_title(soup)
    f = fields(soup)
    body = base.clean(soup.get_text(" ", strip=True))
    rec = {"id": url, "url": url, "title": title[:200], "ok": False}

    if any(w in title for w in base.SKIP_WORDS):
        rec["why"] = "not available"
        return rec
    price = parse_yen(field(f, "価格", "売買価格", "販売価格") or
                      grab(body, r"価格[^0-9相応無]{0,12}([\d.]+\s*万\s*円|\d+\s*円|無償|応?相談)"))
    addr = field(f, "所在地", "住所") or grab(body, r"所在地\s*[|｜:：]?\s*(\S+(?:\s+\S+){0,2})")
    pref, loc = base.parse_address(norm_addr(addr))
    if loc:
        loc = re.split(r"[0-9\-－−]", loc)[0] or loc    # drop lot numbers
    area  = parse_area(field(f, "建物面積", "延床面積", "延べ床面積", "床面積"))
    rooms = parse_rooms(field(f, "間取り", "間取"))
    year  = parse_year(field(f, "築年月", "建築年月", "築年"))

    if price is None:
        rec["why"] = "no sale price"
    elif not loc:
        rec["why"] = "no address"
    elif not area and not rooms:
        rec["why"] = "land only"
    else:
        rec.update(ok=True, price_yen=price, pref=pref, location=loc, bedrooms=rooms,
                   year_built=year, area_m2=area, photos=photos(soup, url))
    return rec


# ─── cache + run loop shared by all extra sites ──────────────────────
def run(name, cache_file, list_urls, max_fetch):
    cache, geo = base.load(cache_file), base.load(base.GEO_FILE)
    today = date.today()
    seen = list(dict.fromkeys(list_urls()))
    print(f"{name}: {len(seen)} listing links found | cache: {len(cache)}")

    for u in seen:
        cache.setdefault(u, {})["seen"] = today.isoformat()
    if seen:          # only forget old ones if the list pages worked today
        cache = {u: v for u, v in cache.items()
                 if v.get("seen") and (today - date.fromisoformat(v["seen"])).days <= KEEP_DAYS}

    def stale(u):
        c = cache[u].get("checked")
        return not c or (today - date.fromisoformat(c)).days >= RECHECK_DAYS
    todo = [u for u in seen if stale(u)]
    print(f"{name}: {len(todo)} new/stale pages, fetching up to {max_fetch}")

    try:
        for u in todo[:max_fetch]:
            rec = parse_detail(u)
            if rec is None:
                continue
            rec.update(source=name, seen=cache[u].get("seen"), checked=today.isoformat())
            if rec["ok"]:
                g = base.geocode(rec["pref"], rec["location"], geo)
                if g:
                    rec["lat"], rec["lng"], rec["geo_level"] = g
            cache[u] = rec
    finally:
        base.save(cache_file, cache)
        base.save(base.GEO_FILE, geo)

    out = [v for v in cache.values() if v.get("ok") and v.get("lat") is not None]
    why = {}
    for v in cache.values():
        if v.get("why"):
            why[v["why"]] = why.get(v["why"], 0) + 1
    print(f"{name}: usable listings {len(out)} | skipped {why}")
    return out
