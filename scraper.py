import os, requests, re, json
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont
from urllib.parse import urljoin

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
DEEPSEEK_KEY = os.getenv("DEEPSEEK_API_KEY")

# 5 SOURCES - solves "same website" problem
SOURCES = [
    "https://akiya.sumai.biz/",
    "https://www.akiya-athome.jp/",
    "https://www.homes.co.jp/akiyabank/",
    "https://www.koryoya.com/",
    "https://www.ieichiba.com/",
]

def deepseek(p):
    system = """You are a viral IG copywriter for @japan.house.roi targeting HK investors who want rental income.
    ALWAYS ENGLISH. Return JSON with: hook, sub_hook, caption, rating, popularity, access, condition, reason.

    HOOK RULES: ALL CAPS, max 6 words, clickbait like "$33K SKI HOUSE" or "CHEAPER THAN HK PARKING"

    RATING RULES - Score 1-10 for RENTAL WORTHINESS:
    Popularity (40% weight): 10=Niseko/Hakuba/Nozawa/Kamakura/Atami/Karuizawa/Furano famous places. 9=30 mins from Tokyo/Osaka CBD (Odawara/Hachioji). 5=decent but unknown town. 2=random village no one knows.
    Access (30%): 10=5 mins to station or ski lift. 9=10 mins to station or 30 mins to Tokyo. 5=20 mins bus. 2=40 mins drive.
    Condition (20%): 10=築浅 <15 years or リフォーム済み or 即入居可. 5=Needs $20K reno. 2=ボロボロ/屋根崩れ.
    Price (10%): 10=<300万円 steal, 5=800万円, 2=1500万円 needs work.

    Final rating = weighted. Only 7+ should be posted. 9-10 is viral.

    CAPTION TEMPLATE EXACTLY:
    🏔 [HOOK] - [SUB_HOOK]

    💰 [PRICE]万円 (~$[USD]K) | [PREFECTURE]
    📍 [Location detail]
    🏠 [Size] | [Land] | [Feature]

    📊 ROI BREAKDOWN:
    BUY: $[PRICE]K
    RENO: $20K
    TOTAL IN: $[TOTAL]K
    RENT: $140 x 74 nights (60% occ)
    GROSS: $10.3K
    NET: $6.2K (after costs)
    ROI: [X]%

    ⭐ RATING: [rating]/10 - [reason]

    ⚠️ CATCH: [1 sentence risk]

    🔗 Source: [link]

    #japanhouse #akiya #japanrealestate

    Return ONLY JSON.
    """
    r = requests.post("https://api.deepseek.com/chat/completions",
        headers={"Authorization": f"Bearer {DEEPSEEK_KEY}", "Content-Type": "application/json"},
        json={"model":"deepseek-chat","messages":[{"role":"system","content":system},{"role":"user","content":p}],"response_format":{"type":"json_object"}}, timeout=40)
    return json.loads(r.json()["choices"][0]["message"]["content"])

def send(msg, link=None, rating=0):
    # Only send if rating >=7
    if rating < 7 and rating!= 0:
        print(f"SKIPPED rating {rating} - too low")
        return False
    kb = [[{"text": f"✅ Approve ({rating}/10)","callback_data":"a"}, {"text":"❌ Skip","callback_data":"s"}]]
    if link: kb.append([{"text":"🔗 Open Listing","url":link}])
    requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id":CHAT_ID,"text":msg,"reply_markup":{"inline_keyboard":kb}})
    return True

def make_hook_slide(hook, sub, price, rating):
    W,H = 1080,1350
    im = Image.new("RGB",(W,H),(10,10,10))
    dr = ImageDraw.Draw(im)
    dr.rectangle([(0,0),(W,18)], fill=(255,235,59))
    dr.rectangle([(0,H-18),(W,H)], fill=(255,235,59))
    # Rating color
    color = "#00FF88" if rating>=9 else "#FFEB3B" if rating>=7 else "#FF5252"
    dr.rectangle([(0,18),(160,90)], fill=color)
    try:
        fb = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 108)
        fm = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 44)
        fs = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 34)
        fs2 = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 36)
    except:
        fb = fm = fs = fs2 = ImageFont.load_default()
    dr.text((20,22), f"{rating}/10", font=fs2, fill="black")
    y=110
    for line in hook.upper().split("\n")[:2]:
        if line.strip():
            dr.text((50,y), line.strip(), font=fb, fill="white", stroke_width=7, stroke_fill="black")
            y+=125
    dr.line([(50,y+5),(W-50,y+5)], fill="#FFEB3B", width=5)
    y+=25
    dr.text((50,y), sub.upper(), font=fm, fill="#FFEB3B")
    y+=70
    dr.text((50,y), f"{price}万円 | {int(price*0.067*1000)} USD | UNDER $100K", font=fs, fill="#AAAAAA")
    dr.rounded_rectangle([(50,H-220),(W-50,H-120)], radius=50, fill="white")
    dr.text((90,H-188), f"{price}万円 ~${int(price*0.067*1000)}K 11% ROI", font=fm, fill="black")
    path = f"/tmp/hook_{os.urandom(3).hex()}.jpg"
    im.save(path,"JPEG",quality=95)
    return path

# --- MAIN MULTI-SITE SCRAPER ---
all_links = []
for base in SOURCES:
    try:
        print(f"Scraping {base}...")
        html = requests.get(base, headers={"User-Agent":"Mozilla/5.0"}, timeout=15).text
        soup = BeautifulSoup(html, "html.parser")
        # Find detail pages - generic
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if any(x in href for x in ["/akiya/","/bukken/","/detail/","/property/"]) or re.search(r"/\d{5,}", href):
                full = urljoin(base, href)
                all_links.append(full)
        # Special for sumai.biz
        if "sumai.biz" in base:
            for a in soup.find_all("a", href=True):
                if "akiya.sumai.biz" in a["href"] and re.search(r"/\d+/?$", a["href"]):
                    all_links.append(a["href"])
        print(f"Found {len(all_links)} so far from {base}")
    except Exception as e:
        print(f"Failed {base}: {e}")

# Dedupe and limit
all_links = list(dict.fromkeys(all_links))[:15]
print(f"Total unique links: {len(all_links)}")

for lk in all_links:
    try:
        pg = requests.get(lk, headers={"User-Agent":"Mozilla/5.0"}, timeout=15).text
        m = re.search(r"(\d+)万円", pg)
        price = int(m.group(1)) if m else 999
        if price > 1500 or price < 10: # UNDER $100K = 1500万円
            continue

        prompt = f"Listing URL {lk} price {price}万円 snippet {pg[:4000]}. Rate it for rental worthiness. Snow town / surf / tourist spots get higher rating. 30 mins from Tokyo higher than random station. Newer/no fix higher."
        data = deepseek(prompt)

        rating = int(data.get("rating",5))
        hook = data.get("hook","$33K HOUSE")
        sub = data.get("sub_hook","7KM TO 5 RESORTS")
        caption = data.get("caption","No caption")

        if rating < 7:
            print(f"Low rating {rating} skip {lk}")
            continue

        slide = make_hook_slide(hook, sub, price, rating)
        ok = send(caption, lk, rating)
        if ok:
            with open(slide,"rb") as f:
                requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
                    data={"chat_id":CHAT_ID, "caption": f"RATING {rating}/10: {hook} - {sub} | {lk}"},
                    files={"photo": f})
            break # one good post per run
    except Exception as e:
        print(f"Error on {lk}: {e}")
        import traceback; traceback.print_exc()
        continue
