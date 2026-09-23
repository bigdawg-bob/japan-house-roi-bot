import os, requests, re, json
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
DEEPSEEK_KEY = os.getenv("DEEPSEEK_API_KEY")
BASE = "https://akiya.sumai.biz/"

def deepseek(p):
    system = """You are a viral IG copywriter for @japan.house.roi targeting HK investors. ALWAYS ENGLISH. You must return JSON with: hook, sub_hook, caption.
    hook rules: ALL CAPS, max 6 words, super clickbait like "$33K SKI HOUSE" or "CHEAPER THAN HK PARKING"
    caption rules: Use this EXACT template with emojis and line breaks:

🏔️ [HOOK] - [SUB_HOOK]

💰 [PRICE]万円 (~$[USD]K) | [PREFECTURE]
📍 [Location detail e.g. 7km to 5 resorts]
🏠 [Size] | [Land] | [1 cool feature]

📊 ROI BREAKDOWN:
BUY: $[PRICE]K
RENO: $20K
TOTAL IN: $[TOTAL]K
RENT: $140 x 74 nights (60% occ)
GROSS: $10.3K
NET: $6.2K (after costs)
ROI: [X]%

⚠️ CATCH: [1 sentence risk]

🔗 Source: [link]

#japanhouse #akiya #japanrealestate
"""
    r = requests.post("https://api.deepseek.com/chat/completions",
        headers={"Authorization": f"Bearer {DEEPSEEK_KEY}", "Content-Type": "application/json"},
        json={"model":"deepseek-chat","messages":[{"role":"system","content":system},{"role":"user","content":p}],"response_format":{"type":"json_object"}}, timeout=35)
    return json.loads(r.json()["choices"][0]["message"]["content"])

def send(msg, link=None):
    kb = [[{"text":"✅ Approve & Queue","callback_data":"a"},{"text":"❌ Skip","callback_data":"s"}]]
    if link: kb.append([{"text":"🔗 Open Listing","url":link}])
    requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id":CHAT_ID,"text":msg,"reply_markup":{"inline_keyboard":kb}})

def make_hook_slide(hook, sub, price):
    W,H = 1080,1350
    im = Image.new("RGB",(W,H),(10,10,10))
    dr = ImageDraw.Draw(im)
    # Yellow accent
    dr.rectangle([(0,0),(W,18)], fill=(255,235,59))
    dr.rectangle([(0,H-18),(W,H)], fill=(255,235,59))

    # Load font
    try:
        fb = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 108)
        fm = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 44)
        fs = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 34)
    except:
        fb = fm = fs = ImageFont.load_default()

    # Big hook
    y=90
    for line in hook.upper().split("\n")[:2]:
        if line.strip():
            dr.text((50,y), line.strip(), font=fb, fill="white", stroke_width=7, stroke_fill="black")
            y+=125
    dr.line([(50,y+5),(W-50,y+5)], fill="#FFEB3B", width=5)
    y+=25
    dr.text((50,y), sub.upper(), font=fm, fill="#FFEB3B")
    y+=70
    dr.text((50,y), f"{price}万円 | NAGANO SKI | {int(price*67)} USD", font=fs, fill="#AAAAAA")

    # Bottom pill
    dr.rounded_rectangle([(50,H-220),(W-50,H-120)], radius=50, fill="white")
    dr.text((90,H-188), f"{price}万円 ~${int(price*0.067*1000)}K 11% ROI", font=fm, fill="black")

    path = f"/tmp/hook_{os.urandom(3).hex()}.jpg"
    im.save(path,"JPEG",quality=95)
    return path

html = requests.get(BASE, headers={"User-Agent":"Mozilla/5.0"}, timeout=15).text
soup = BeautifulSoup(html, "html.parser")
links = list(set([a["href"] for a in soup.find_all("a", href=True) if "akiya.sumai.biz" in a["href"] and re.search(r"/\d+/?$", a["href"])]))[:10]

for lk in links:
    try:
        pg = requests.get(lk, headers={"User-Agent":"Mozilla/5.0"}, timeout=15).text
        m = re.search(r"(\d+)万円", pg)
        price = int(m.group(1)) if m else 999
        if price>800 or price<10: continue

        prompt = f"Listing URL {lk} price {price}万円 snippet {pg[:3500]}. Generate hook + caption now."
        data = deepseek(prompt)

        hook = data.get("hook","$33K SKI HOUSE")
        sub = data.get("sub_hook","7KM TO 5 RESORTS")
        caption = data.get("caption","No caption")

        # This slide NEVER uses external image, so no rainbow bug
        slide = make_hook_slide(hook, sub, price)

        send(caption, lk)

        with open(slide,"rb") as f:
            requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
                data={"chat_id":CHAT_ID, "caption": f"HOOK SLIDE: {hook} - {sub}"},
                files={"photo": f})
        break
    except Exception as e:
        print(e)
        import traceback; traceback.print_exc()
