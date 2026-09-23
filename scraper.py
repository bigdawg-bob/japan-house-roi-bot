import os, requests, re, json
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont
from urllib.parse import urljoin

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
DEEPSEEK_KEY = os.getenv("DEEPSEEK_API_KEY")

SOURCES = [
    "https://akiya.sumai.biz/",
    "https://www.akiya-athome.jp/",
    "https://www.homes.co.jp/akiyabank/",
    "https://www.koryoya.com/",
    "https://www.ieichiba.com/",
]

SOLD_KEYWORDS = ["成約済み","売却済み","売約済み","取引完了","販売終了","SOLD","商談中","契約済み","売り切れ","成約"]

def is_live_listing(html, url):
    # Rule 1: Must be detail page, not homepage
    if url in SOURCES: return False
    if not re.search(r"(/listing/|/bukken/|/detail/|/property/|/\d{5,})", url):
        # For sumai.biz allow /number pattern
        if "sumai.biz" not in url or not re.search(r"/\d+/?$", url):
            print(f"SKIP not detail page: {url}")
            return False
    # Rule 2: Sold keywords?
    for kw in SOLD_KEYWORDS:
        if kw.lower() in html.lower():
            print(f"SKIP sold keyword {kw} in {url}")
            return False
    # Rule 3: Must still have price
    if not re.search(r"(\d+万円|¥\s*\d+[,\d]*)", html):
        print(f"SKIP no price found: {url}")
        return False
    return True

def deepseek(p):
    system = """You are @japan.house.roi IG copywriter.
    ENGLISH ONLY. Return JSON: hook, sub_hook, caption, rating, reason, reno_cost, nightly_rate, occupancy, net_income, yield, license_type, license_days.
    HARD RULES: Hakuba/Nozawa/Niseko/Furano ski <15 mins to lift + under $100K = 10/10. Atami Onsen + <15 mins walk to Atami Station + onsen = 9.5/10. Kyoto residential MAX 6/10 because Minpaku 60 days/year Jan15-Mar15 only. Random unknown town MAX 5/10 SKIP.
    WEIGHTS: Popularity 50%, Access 30%, Condition 10%, Price 10%. Yield NOT in rating.
    HOOK simple: "$45K HAKUBA SKI HOUSE - 8 MINS TO LIFTS" not Japanese ward names.
    RENO: reform= $5K, avg= $20K, old= $35K.
    RENT: AirDNA comps – Kyoto $138/night 83%, Hakuba $180/night 65%, Atami $160/night 68%.
    LICENSE: New Minpaku 180 days, Tokku 365 days, Kan'i 365 days. Kyoto 60 days.
    CAPTION USD ONLY:
    🏔 [HOOK]
    💰 $[PRICE]K USD | [Location]
    📍 [X mins to JR/lifts]
    🏠 [Size] | [Feature]
    📊 ROI (Based on AirDNA: $[rate]/night [occ]%)
    BUY: $[PRICE]K
    RENO: $[RENO]K
    TOTAL: $[TOTAL]K
    **Net Rental Income: $[NET]K / year ([YIELD]% yield)**
    📜 LICENSE: [type] - [days] days – [Kyoto 60 days note if Kyoto]
    ⭐ RATING: [rating]/10 - [reason]
    ⚠️ CATCH: [risk]
    🔗 Source: [link]
    """
    r = requests.post("https://api.deepseek.com/chat/completions",
        headers={"Authorization": f"Bearer {DEEPSEEK_KEY}","Content-Type":"application/json"},
        json={"model":"deepseek-chat","messages":[{"role":"system","content":system},{"role":"user","content":p}],"response_format":{"type":"json_object"}}, timeout=40)
    return json.loads(r.json()["choices"][0]["message"]["content"])

def send(msg, link=None, rating=0):
    if rating < 6: return False
    kb = [[{"text": f"✅ Approve ({rating}/10)","callback_data":"a"},{"text":"❌ Skip","callback_data":"s"}]]
    if link: kb.append([{"text":"🔗 Open Listing","url":link}])
    requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id":CHAT_ID,"text":msg,"parse_mode":"Markdown","reply_markup":{"inline_keyboard":kb}})
    return True

def make_hook_slide(hook, sub, price_usd, rating):
    W,H = 1080,1350
    im = Image.new("RGB",(W,H),(10,10,10))
    dr = ImageDraw.Draw(im)
    dr.rectangle([(0,0),(W,18)], fill=(255,235,59))
    dr.rectangle([(0,H-18),(W,H)], fill=(255,235,59))
    color = "#00FF88" if rating>=9.5 else "#FFEB3B" if rating>=9 else "#FF8C00"
    dr.rectangle([(0,18),(180,90)], fill=color)
    try:
        fb = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 100)
        fm = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 42)
        fs = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 32)
        fs2 = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 34)
    except:
        fb = fm = fs = fs2 = ImageFont.load_default()
    dr.text((15,22), f"{rating}/10", font=fs2, fill="black")
    y=110
    for line in hook.upper().split("\n")[:2]:
        if line.strip():
            dr.text((50,y), line.strip(), font=fb, fill="white", stroke_width=6, stroke_fill="black")
            y+=115
    dr.line([(50,y+5),(W-50,y+5)], fill="#FFEB3B", width=5)
    y+=25
    dr.text((50,y), sub.upper(), font=fm, fill="#FFEB3B")
    y+=65
    dr.text((50,y), f"${price_usd}K USD | UNDER $100K", font=fs, fill="#AAAAAA")
    dr.rounded_rectangle([(50,H-220),(W-50,H-120)], radius=50, fill="white")
    dr.text((70,H-188), f"${price_usd}K USD | {rating}/10 LIVE", font=fm, fill="black")
    path = f"/tmp/hook_{os.urandom(3).hex()}.jpg"
    im.save(path,"JPEG",quality=95)
    return path

all_links = []
for base in SOURCES:
    try:
        html = requests.get(base, headers={"User-Agent":"Mozilla/5.0"}, timeout=15).text
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            full = urljoin(base, href)
            # Only collect detail-like URLs
            if re.search(r"(/listing/|/bukken/|/detail/|akiya/|\d{6,})", href) or ("sumai.biz" in base and re.search(r"/\d+/?$", href)):
                if full not in all_links:
                    all_links.append(full)
    except Exception as e:
        print(f"Fail {base}: {e}")

all_links = all_links[:20]
print(f"Checking {len(all_links)} links for live status...")

live_count = 0
for lk in all_links:
    try:
        resp = requests.get(lk, headers={"User-Agent":"Mozilla/5.0"}, timeout=15)
        html = resp.text
        if not is_live_listing(html, lk):
            continue
        live_count+=1
        m = re.search(r"(\d+)万円", html)
        price_man = int(m.group(1)) if m else 999
        if price_man > 1500: continue
        price_usd = int(price_man * 0.067 * 1000)
        data = deepseek(f"URL {lk} price {price_man}万円 ({price_usd}K USD) LIVE listing snippet {html[:4000]} Apply HARD RULES: Hakuba=10, Atami=9.5, Kyoto max 6.")
        rating = float(data.get("rating",5))
        if rating < 6: continue
        hook = data.get("hook",f"${price_usd}K HOUSE")
        sub = data.get("sub_hook","5 MINS TO JR STATION")
        caption = data.get("caption","")
        # Add LIVE verification note
        caption += f"\n\n✅ Verified LIVE on {base} – no sold keywords found"
        slide = make_hook_slide(hook, sub, price_usd, rating)
        if send(caption, lk, rating):
            with open(slide,"rb") as f:
                requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
                    data={"chat_id":CHAT_ID, "caption": f"LIVE {rating}/10: {hook}"},
                    files={"photo": f})
            break
    except Exception as e:
        print(f"Error {lk}: {e}")
        continue

print(f"Live listings checked: {live_count}")
