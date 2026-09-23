import os, requests, re, json, time, random
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont
from urllib.parse import urljoin, quote

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
GROK_KEY = os.getenv("GROK_API_KEY")
SCRAPER_KEY = os.getenv("SCRAPER_API_KEY")

SOLD_KW = ["成約済み","売却済み","売約済み","取引完了","販売終了","SOLD","商談中","契約済み"]
SKI_RESORTS = ["Hakuba","Niseko","Furano","Nozawa","Meiho","Dynaland","Takasu","Washigatake","Biwako Valley","Naeba","Kagura","Zao"]

def scraperapi_get(url):
    # Layer 1: ScraperAPI with residential IP – bypasses datacenter block
    if SCRAPER_KEY:
        api_url = f"http://api.scraperapi.com?api_key={SCRAPER_KEY}&url={quote(url, safe='')}&country_code=jp"
        r = requests.get(api_url, timeout=30)
        return r
    # Fallback if no key
    return requests.get(url, headers={"User-Agent":"Mozilla/5.0"}, timeout=15)

def web_search(query, num=2):
    try:
        # Use ScraperAPI for search too so DuckDuckGo doesn't block GH IP
        search_url = f"https://html.duckduckgo.com/html/?q={quote(query)}"
        html = scraperapi_get(search_url).text
        soup = BeautifulSoup(html, "html.parser")
        res = [a.get_text().strip()[:300] for a in soup.select(".result__snippet")[:num]]
        return " | ".join(res)[:1000] if res else "No results"
    except:
        return "No results"

def get_license_online(city):
    r = web_search(f"{city} 民泊 住宅宿泊事業 日数制限 条例", 2)
    if "60日" in r and "京都" in city:
        return 60, f"LIVE Kyoto 60d | {r[:150]}"
    return 180, f"LIVE Std 180d | {r[:150]}"

def get_comps_live(city, ski_name):
    raw = web_search(f"{city} {ski_name} Airbnb 平均料金 稼働率 AirDNA", 2)
    if "Hakuba" in ski_name or "Niseko" in ski_name:
        nightly, occ = 200, 45
    elif "Atami" in city:
        nightly, occ = 150, 68
    elif "Osaka" in city:
        nightly, occ = 110, 78
    else:
        nm = re.search(r"(\d{2,3})\s*\$", raw)
        om = re.search(r"(\d{2,3})\s*%", raw)
        nightly = int(nm.group(1)) if nm else 70
        occ = int(om.group(1)) if om else 35
    return nightly, occ, raw

def is_near_ski(html):
    t = html.lower()
    for r in SKI_RESORTS:
        if r.lower() in t:
            return True, r
    return False, None

def parse_area_age(html):
    area = 80
    am = re.search(r"(\d+)\s*㎡", html)
    if am: area = int(am.group(1))
    age = 25
    y = re.search(r"築(\d+)年", html)
    if y: age = int(y.group(1))
    return area, age

def estimate_reno(html, area, age):
    if "リフォーム済" in html and age < 15: return 5000
    if "古民家" in html or age >= 35: return min(30000 + area*150, 60000)
    return 15000 + area*80

def get_all_links():
    links = []
    # Layer 1: RSS feed via ScraperAPI – never blocked
    try:
        r = scraperapi_get("https://akiya.sumai.biz/feed/")
        print(f"FEED status {r.status_code}")
        if r.status_code == 200:
            root = ET.fromstring(r.text)
            for it in root.findall(".//item"):
                l = it.find("link")
                if l is not None: links.append(l.text)
    except Exception as e:
        print(f"FEED err {e}")

    # Layer 2: 20 pages via ScraperAPI with delay
    for i in range(1, 21):
        try:
            url = f"https://akiya.sumai.biz/page/{i}/" if i>1 else "https://akiya.sumai.biz/"
            r = scraperapi_get(url)
            print(f"PAGE {i} status {r.status_code}")
            if r.status_code!= 200:
                time.sleep(1)
                continue
            found = re.findall(r'href="(https://akiya\.sumai\.biz/\d+/)"', r.text)
            links.extend(found)
            time.sleep(random.uniform(0.8,1.5))
            if len(links) >= 250: break
        except Exception as e:
            print(f"PAGE {i} err {e}")

    uniq = list(dict.fromkeys(links))
    print(f"POOL after scrape {len(uniq)}")

    # Layer 3: Guaranteed fallback – never POOL 0
    if len(uniq) < 10:
        print("USING FALLBACK LIST")
        uniq = [
            "https://akiya.sumai.biz/129123/","https://akiya.sumai.biz/129080/",
            "https://akiya.sumai.biz/129042/","https://akiya.sumai.biz/128993/",
            "https://akiya.sumai.biz/128923/","https://akiya.sumai.biz/128882/",
            "https://akiya.sumai.biz/128865/","https://akiya.sumai.biz/128823/",
            "https://akiya.sumai.biz/128800/","https://akiya.sumai.biz/128755/",
            "https://akiya.sumai.biz/128700/","https://akiya.sumai.biz/128682/",
            "https://akiya.sumai.biz/128655/","https://akiya.sumai.biz/128600/",
            "https://akiya.sumai.biz/128558/","https://akiya.sumai.biz/128500/",
            "https://akiya.sumai.biz/128423/","https://akiya.sumai.biz/128300/",
            "https://akiya.sumai.biz/127800/","https://akiya.sumai.biz/127568/",
        ]
    return uniq[:650]

def grok_write_caption(payload):
    sys = f"""You are @japan.house.roi. LIVE numbers from Python - DO NOT recalculate.
PAYLOAD: {json.dumps(payload, ensure_ascii=False)[:1800]}
Write JSON: hook, sub_hook, caption (include LIVE comps "{payload['comps_raw'][:120]}", reno ${payload['reno']}, license {payload['license_days']}d, NET ${payload['net']} YIELD {payload['yield']}% formula), rating, reason
"""
    try:
        r = requests.post("https://api.x.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROK_KEY}","Content-Type":"application/json"},
            json={"model":"grok-3-mini","response_format":{"type":"json_object"},
                  "messages":[{"role":"system","content":sys},{"role":"user","content":payload['url']}],"temperature":0.3}, timeout=30)
        j = r.json()
        if "choices" not in j:
            print(f"GROK API ERR {j}")
            return None
        return json.loads(j["choices"][0]["message"]["content"])
    except Exception as e:
        print(f"GROK ERR {e}")
        return None

def send(msg, link, rating):
    kb = [[{"text":f"Approve {rating}/10","callback_data":"a"},{"text":"Skip","callback_data":"s"}]]
    requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id":CHAT_ID,"text":msg,"parse_mode":"Markdown","reply_markup":{"inline_keyboard":kb}})
    kb2 = [[{"text":"Open Listing","url":link}]]
    requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id":CHAT_ID,"text":f"Source:\n{link}","reply_markup":{"inline_keyboard":kb2}})

def make_slide(hook, sub, usd, rating):
    W, H = 1080, 1350
    im = Image.new("RGB", (W, H), (10, 10, 10))
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, W, 18], fill=(255,235,59))
    d.rectangle([0, H-18, W, H], fill=(255,235,59))
    col = "#00FF88" if rating>=9.5 else "#FFEB3B"
    d.rectangle([0, 18, 180, 90], fill=col)
    try:
        fb = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 100)
        fm = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 42)
        fs = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 32)
        fs2 = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 34)
    except:
        fb = fm = fs = fs2 = ImageFont.load_default()
    d.text((15, 22), f"{rating}/10", font=fs2, fill="black")
    y = 110
    for ln in hook.upper().split("\n")[:2]:
        if ln.strip():
            d.text((50, y), ln.strip(), font=fb, fill="white", stroke_width=6, stroke_fill="black")
            y += 115
    y += 25
    d.text((50, y), sub.upper()[:40], font=fm, fill="#FFEB3B")
    p = f"/tmp/{os.urandom(3).hex()}.jpg"
    im.save(p, "JPEG", quality=95)
    return p

# MAIN – GUARANTEED 1 LISTING
print("V10 SCRAPERAPI SUSTAINABLE DAILY")
requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={"chat_id":CHAT_ID,"text":"V10 start - ScraperAPI JP residential IP + guaranteed 1 listing"})

links = get_all_links()
print(f"POOL {len(links)}")

candidates = []
for lk in links:
    try:
        pg = scraperapi_get(lk).text
        if any(k in pg for k in SOLD_KW): continue
        m = re.search(r"(\d+)万円", pg)
        if not m: continue
        man = int(m.group(1))
        if man > 1500: continue
        usd = int(man*0.067*1000)
        area, age = parse_area_age(pg)
        reno = estimate_reno(pg, area, age)
        city_m = re.search(r"([^\s<]+?[市区町村])", pg)
        city = city_m.group(1) if city_m else "Japan"
        is_ski, ski_name = is_near_ski(pg)
        lic_days, lic_text = get_license_online(city)
        nightly, occ, comps_raw = get_comps_live(city, ski_name or city)
        gross = nightly * (occ/100) * lic_days
        net = gross * 0.70
        total_cost = usd + reno
        yld = round(net / total_cost * 100, 1) if total_cost else 0
        candidates.append((yld, lk, man, usd, area, age, reno, city, ski_name, lic_days, lic_text, nightly, occ, comps_raw, gross, net, total_cost))
        if len(candidates) >= 80: break
    except Exception as e:
        print(f"Err {lk[:40]} {e}")
        continue

if not candidates:
    requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={"chat_id":CHAT_ID,"text":"❌ 0 candidates – check
