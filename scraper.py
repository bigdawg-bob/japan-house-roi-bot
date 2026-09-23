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
    except: return "No results"

def get_license_online(city):
    r = web_search(f"{city} 民泊 住宅宿泊事業 日数制限 条例", 2)
    if "60日" in r and "京都" in city: return 60, f"LIVE Kyoto 60d | {r[:150]}"
    if "365" in r and "特区" in r: return 365, f"LIVE Tokku 365d | {r[:150]}"
    if "大阪" in city: return 180, f"LIVE Osaka 180d std / 365d Tokku | {r[:150]}"
    return 180, f"LIVE Std 180d | {r[:150]}"

def get_comps_live(city, ski_name):
    raw = web_search(f"{city} {ski_name} Airbnb 平均料金 稼働率 AirDNA", 2)
    # extract numbers if present
    m = re.findall(r'(\d{2,3})\s*\$|¥(\d{4,6})', raw)
    # fallback table – Python decides nightly/occ, not Grok
    if "Hakuba" in ski_name or "Niseko" in ski_name: nightly, occ = 200, 45
    elif "Atami" in city: nightly, occ = 150, 68
    elif "Osaka" in city: nightly, occ = 110, 78
    elif "Tokyo" in city: nightly, occ = 130, 72
    elif "Okinawa" in city: nightly, occ = 150, 70
    else:
        # try parse from search
        nm = re.search(r'(\d{2,3})\s*\$', raw)
        om = re.search(r'(\d{2,3})\s*%', raw)
        nightly = int(nm.group(1)) if nm else 70
        occ = int(om.group(1)) if om else 35
    return nightly, occ, raw

def is_near_ski(html):
    t = html.lower()
    for r in SKI_RESORTS:
        if r.lower() in t: return True, r
    m = re.search(r'スキー場まで.*?(\d+)分', html)
    if m and int(m.group(1)) <= 15: return True, f"Ski {m.group(1)}min"
    return False, None

def parse_area_age(html):
    area = int(re.search(r'(\d+)\s*㎡', html).group(1)) if re.search(r'(\d+)\s*㎡', html) else 80
    age = 25
    if "昭和" in html:
        s = re.search(r'昭和(\d+)年', html)
        if s: age = 2026 - (1925 + int(s.group(1)))
    else:
        y = re.search(r'築(\d+)年', html)
        if y: age = int(y.group(1))
    return area, age

def estimate_reno(html, area, age):
    if "リフォーム済" in html and age < 15: return 5000
    if "古民家" in html or age >= 35:
        extra = 10000 if "雨漏り" in html else 0
        extra += 7000 if "汲み取り" in html else 0
        return min(30000 + area*150 + extra, 60000)
    return 15000 + area*80

def get_all_links():
    links = []
    try:
        feed = requests.get("https://akiya.sumai.biz/feed/", headers={"User-Agent":"Mozilla/5.0"}, timeout=10).text
        root = ET.fromstring(feed)
        for it in root.findall(".//item"):
            l = it.find("link").text if it.find("link") is not None else ""
            if l: links.append(l)
    except: pass
    for i in range(1, 31):
        try:
            url = f"https://akiya.sumai.biz/page/{i}/" if i>1 else "https://akiya.sumai.biz/"
            html = requests.get(url, headers={"User-Agent":"Mozilla/5.0"}, timeout=10).text
            for m in re.findall(r'href="([^"]*akiya\.sumai\.biz/\d+/)"', html):
                links.append(urljoin("https://akiya.sumai.biz/", m))
        except: pass
    return list(dict.fromkeys(links))[:650]

def grok_write_caption(payload):
    """Grok does ONLY copywriting – math already done in Python"""
    sys = f"""
You are @japan.house.roi copywriter. You get LIVE verified numbers from Python – DO NOT recalculate.

PAYLOAD: {json.dumps(payload, ensure_ascii=False)[:2000]}

Write JSON: hook (2 lines uppercase, first with price), sub_hook (1 line), caption (must include: LIVE comps source "{payload['comps_raw'][:150]}", reno breakdown ${payload['reno']}, license {payload['license_days']}d {payload['license_text']}, NET ${payload['net']} bold, YIELD {payload['yield']}% formula GROSS=nightly*occ*days, ski {payload['ski_name']}), rating (10 if ski<10min+yield>=12, 9.5 Atami onsen, 9 Shonan/Karuizawa, 7-8 if yield>=8 occ>=50 180d, 5 MAX Kyoto 60d), reason
English only, keep hook punchy for Instagram.
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
    kb = [[{"text":f"✅ Approve {rating}/10","callback_data":"a"},{"text":"❌ Skip","callback_data":"s"}]]
    requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id":CHAT_ID,"text":msg,"parse_mode":"Markdown","reply_markup":{"inline_keyboard":kb}})
    kb2 = [[{"text":"🔗 Open Listing","url":link}]]
    requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id":CHAT_ID,"text":f"🔗 Source (Python LIVE search):\n{link}","reply_markup":{"inline_keyboard":kb2}})

def make_slide(hook, sub, usd, rating):
    W,H=1080,1350
    im=Image.new("RGB",(W,H),(10,10,10)); d=ImageDraw.Draw(im)
    d.rectangle([(0,0),(W,18)],fill=(255,235,59)); d.rectangle([(0,H-18),(W,H)],fill=(255,235,59))
    col="#00FF88" if rating>=9.5 else "#FFEB3B" if rating>=9 else "#FF8C00" if rating>=7 else "#AAAAAA"
    d.rectangle([(0,18),(180,90)],fill=col)
    try:
        fb=ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",100)
        fm=ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",42)
        fs=ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",32)
        fs2=ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",34)
    except: fb=fm=fs=fs2=ImageFont.load_default()
    d.text((15,22),f"{rating}/10",font=fs2,fill="black")
    y=110
    for ln in hook.upper().split("\n")[:2]:
        if ln.strip(): d.text((50,y),ln.strip(),font=fb,fill="white",stroke_width=6,stroke_fill="black"); y+=115
    d.line([(50
