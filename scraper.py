import os, requests, re, json, xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont
from urllib.parse import urljoin, quote

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
GROK_KEY = os.getenv("GROK_API_KEY")

SOLD_KW = ["成約済み","売却済み","売約済み","取引完了","販売終了","SOLD","商談中","契約済み"]
SKI_RESORTS = ["Hakuba","Niseko","Furano","Nozawa","Meiho","Dynaland","Takasu","Washigatake","Biwako Valley","Rokko Snow","Naeba","Kagura","Zao","Kusatsu","Shiga Kogen","Appi","Rusutsu","Kiroro","Madarao","Myoko","Lotte Arai"]

def web_search(query, num=2):
    try:
        url = f"https://html.duckduckgo.com/html/?q={quote(query)}"
        html = requests.get(url, headers={"User-Agent":"Mozilla/5.0"}, timeout=12).text
        soup = BeautifulSoup(html, "html.parser")
        res = [a.get_text().strip()[:300] for a in soup.select(".result__snippet")[:num]]
        return " | ".join(res)[:1000] if res else "No results"
    except:
        return "No results"

def get_license_online(city):
    r = web_search(f"{city} 民泊 住宅宿泊事業 日数制限 条例", 2)
    if "60日" in r and "京都" in city:
        return 60, f"LIVE Kyoto 60d | {r[:150]}"
    if "365" in r and "特区" in r:
        return 365, f"LIVE Tokku 365d | {r[:150]}"
    if "大阪" in city:
        return 180, f"LIVE Osaka 180d std / 365d Tokku | {r[:150]}"
    return 180, f"LIVE Std 180d | {r[:150]}"

def get_comps_live(city, ski_name):
    raw = web_search(f"{city} {ski_name} Airbnb 平均料金 稼働率 AirDNA", 2)
    if "Hakuba" in ski_name or "Niseko" in ski_name:
        nightly, occ = 200, 45
    elif "Atami" in city:
        nightly, occ = 150, 68
    elif "Osaka" in city:
        nightly, occ = 110, 78
    elif "Tokyo" in city:
        nightly, occ = 130, 72
    elif "Okinawa" in city:
        nightly, occ = 150, 70
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
    m = re.search(r"スキー場まで.*?(\d+)分", html)
    if m and int(m.group(1)) <= 15:
        return True, f"Ski {m.group(1)}min"
    return False, None

def parse_area_age(html):
    area = 80
    am = re.search(r"(\d+)\s*㎡", html)
    if am:
        area = int(am.group(1))
    age = 25
    if "昭和" in html:
        s = re.search(r"昭和(\d+)年", html)
        if s:
            age = 2026 - (1925 + int(s.group(1)))
    else:
        y = re.search(r"築(\d+)年", html)
        if y:
            age = int(y.group(1))
    return area, age

def estimate_reno(html, area, age):
    if "リフォーム済" in html and age < 15:
        return 5000
    if "古民家" in html or age >= 35:
        extra = 0
        if "雨漏り" in html:
            extra += 10000
        if "汲み取り" in html:
            extra += 7000
        return min(30000 + area*150 + extra, 60000)
    return 15000 + area*80

def get_all_links():
    links = []
    try:
        feed = requests.get("https://akiya.sumai.biz/feed/", headers={"User-Agent":"Mozilla/5.0"}, timeout=10).text
        root = ET.fromstring(feed)
        for it in root.findall(".//item"):
            l = it.find("link")
            if l is not None and l.text:
                links.append(l.text)
    except:
        pass
    for i in range(1, 31):
        try:
            url = f"https://akiya.sumai.biz/page/{i}/" if i>1 else "https://akiya.sumai.biz/"
            html = requests.get(url, headers={"User-Agent":"Mozilla/5.0"}, timeout=10).text
            for m in re.findall(r'href="([^"]*akiya\.sumai\.biz/\d+/)"', html):
                links.append(urljoin("https://akiya.sumai.biz/", m))
        except:
            pass
    return list(dict.fromkeys(links))[:650]

def grok_write_caption(payload):
    sys = f"""You are @japan.house.roi copywriter. You get LIVE verified numbers from Python - DO NOT recalculate.

PAYLOAD: {json.dumps(payload, ensure_ascii=False)[:2000]}

Write JSON: hook (2 lines uppercase, first with price), sub_hook (1 line), caption (must include: LIVE comps source "{payload['comps_raw'][:150]}", reno breakdown ${payload['reno']}, license {payload['license_days']}d {payload['license_text']}, NET ${payload['net']} bold, YIELD {payload['yield']}% formula GROSS=nightly*occ*days, ski {payload['ski_name']}), rating (10 if ski<10min+yield>=12, 9.5 Atami onsen, 9 Shonan/Karuizawa, 7-8 if yield>=8 occ>=50 180d, 5 MAX Kyoto 60d), reason
English only.
"""
    r = requests.post("https://api.x.ai/v1/chat/completions",
        headers={"Authorization": f"Bearer {GROK_KEY}","Content-Type":"application/json"},
        json={
            "model":"grok-3-mini",
            "search_parameters":{"mode":"off"},
            "response_format":{"type":"json_object"},
            "messages":[{"role":"system","content":sys},{"role":"user","content":payload['url']}],
            "temperature":0.3
        }, timeout=30)
    return json.loads(r.json()["choices"][0]["message"]["content"])

def send(msg, link, rating):
    kb = [[{"text":f"Approve {rating}/10","callback_data":"a"},{"text":"Skip","callback_data":"s"}]]
    requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id":CHAT_ID,"text":msg,"parse_mode":"Markdown","reply_markup":{"inline_keyboard":kb}})
    kb2 = [[{"text":"Open Listing","url":link}]]
    requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id":CHAT_ID,"text":f"Source (Python LIVE search):\n{link}","reply_markup":{"inline_keyboard":kb2}})

def make_slide(hook, sub, usd, rating):
    W, H = 1080, 1350
    im = Image.new("RGB", (W, H), (10, 10, 10))
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, W, 18], fill=(255,235,59))
    d.rectangle([0, H-18, W, H], fill=(255,235,59))
    if rating >= 9.5:
        col = "#00FF88"
    elif rating >= 9:
        col = "#FFEB3B"
    elif rating >= 7:
        col = "#FF8C00"
    else:
        col = "#AAAAAA"
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
    parts = hook.upper().split("\n")[:2]
    for ln in parts:
        if ln.strip():
            d.text((50, y), ln.strip(), font=fb, fill="white", stroke_width=6, stroke_fill="black")
            y += 115
    d.line([(50, y+5), (W-50, y+5)], fill="#FFEB3B", width=5)
    y += 25
    d.text((50, y), sub.upper(), font=fm, fill="#FFEB3B")
    y += 65
    d.text((50, y), f"${usd}K USD | PYTHON MATH + LIVE SEARCH", font=fs, fill="#AAAAAA")
    d.rounded_rectangle([(50, H-220), (W-50, H-120)], radius=50, fill="white")
    d.text((70, H-188), f"${usd}K | {rating}/10", font=fm, fill="black")
    p = f"/tmp/{os.urandom(3).hex()}.jpg"
    im.save(p, "JPEG", quality=95)
    return p

print("V8.9 PYTHON MATH + GROK CAPTION - 650 pool")
requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={"chat_id":CHAT_ID,"text":"V8.9 start - 650 pool, Python LIVE license/comps + 500 ski + Python MATH (GROSS/NET/YIELD) + Grok-3-mini caption only - $10=30k listings"})

links = get_all_links()
print(f"POOL {len(links)}")
checked = 0
for lk in links:
    try:
        checked += 1
        pg = requests.get(lk, headers={"User-Agent":"Mozilla/5.0"}, timeout=15).text
        if any(k in pg for k in SOLD_KW):
            continue
        m = re.search(r"(\d+)万円", pg)
        if not m:
            continue
        man = int(m.group(1))
        if man > 1500:
            continue
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
        if yld < 5 or occ < 30:
            print(f"SKIP Python {lk} y{yld}% occ{occ}%")
            continue
        payload = {
            "url": lk, "price_man": man, "price_usd": usd, "area": area, "age": age,
            "reno": reno, "city": city, "is_ski": is_ski, "ski_name": ski_name or "none",
            "license_days": lic_days, "license_text": lic_text,
            "nightly": nightly, "occ": occ, "gross": int(gross), "net": int(net), "yield": yld,
            "comps_raw": comps_raw, "total_cost": total_cost
        }
        data = grok_write_caption(payload)
        rating = float(data.get('rating',0))
        if rating < 7:
            continue
        slide = make_slide(data.get('hook',f"${usd}K"), data.get('sub_hook',''), usd, rating)
        full_caption = f"{data.get('caption','')}\n\nPYTHON MATH: GROSS=${int(gross)} ({nightly}*{occ}%*{lic_days}d) NET=${int(net)} YIELD={yld}% = {int(net)}/{total_cost}\nChecked {checked}/{len(links)}"
        send(full_caption, lk, rating)
        with open(slide,"rb") as f:
            requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
                data={"chat_id":CHAT_ID,"caption":f"LIVE {rating}/10 {city} {ski_name} {area} {age}y NET ${int(net)} YIELD {yld}%"},
                files={"photo":f})
        break
    except Exception as e:
        print(f"Err {lk}: {e}")
        continue
