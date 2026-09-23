import os, requests, re, json
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont
from io import BytesIO
from urllib.parse import urljoin

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
DEEPSEEK_KEY = os.getenv("DEEPSEEK_API_KEY")
BASE = "https://akiya.sumai.biz/"

def deepseek(p):
    # FORCE ENGLISH
    system = "You are an American IG copywriter. ALWAYS answer in ENGLISH. Never Japanese. Return ONLY valid JSON."
    r = requests.post("https://api.deepseek.com/chat/completions",
        headers={"Authorization": f"Bearer {DEEPSEEK_KEY}", "Content-Type": "application/json"},
        json={"model": "deepseek-chat", "messages": [{"role":"system","content":system},{"role":"user","content":p}], "response_format": {"type": "json_object"}}, timeout=30)
    txt = r.json()["choices"][0]["message"]["content"]
    return json.loads(txt)

def send(msg, link=None):
    kb = [[{"text": "✅ Approve", "callback_data": "a"}, {"text": "❌ Skip", "callback_data": "s"}]]
    if link:
        kb.append([{"text": "🔗 Open Listing", "url": link}])
    requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": msg, "reply_markup": {"inline_keyboard": kb}})

def get_font(sz):
    for p in ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf","/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]:
        if os.path.exists(p):
            try: return ImageFont.truetype(p, sz)
            except: pass
    return ImageFont.load_default()

def make_slide(img_url, hook, sub, price):
    W,H = 1080,1350
    # 1. Download safely
    im = None
    try:
        if img_url:
            if not img_url.startswith("http"):
                img_url = urljoin(BASE, img_url)
            resp = requests.get(img_url, headers={"User-Agent":"Mozilla/5.0"}, timeout=15)
            resp.raise_for_status()
            # Must re-open via BytesIO and convert to RGB immediately
            im = Image.open(BytesIO(resp.content)).convert("RGB")
            im = im.resize((W,H))
    except Exception as e:
        print(f"img fail {e}")

    if im is None:
        im = Image.new("RGB",(W,H),(40,40,40))

    dr = ImageDraw.Draw(im) # NO RGBA HERE - fixes rainbow glitch

    # Top black block - solid, no alpha
    dr.rectangle([(0,0),(W,520)], fill=(0,0,0))
    dr.rectangle([(0,520),(W,526)], fill=(255,230,0))

    fb = get_font(100)
    fm = get_font(44)
    fs = get_font(32)

    y=55
    for ln in hook.upper().split("\n")[:2]: # Force uppercase English
        if not ln.strip(): continue
        # draw stroke manually for safety
        dr.text((45, y), ln, font=fb, fill="white", stroke_width=5, stroke_fill="black")
        y+=115

    dr.text((45, y), sub.upper(), font=fm, fill="#FFEB3B")
    dr.text((45, y+55), f"{price} MAN | NAGANO SKI", font=fs, fill="#CCCCCC")

    # Bottom pill
    dr.rounded_rectangle([(45, H-165), (W-45, H-75)], radius=45, fill="white")
    dr.text((85, H-135), f"{price} MAN ~${int(price*0.067*1000)}K 11% ROI", font=fm, fill="black")

    path = f"/tmp/s_{os.urandom(3).hex()}.jpg"
    im.save(path, "JPEG", quality=90) # Explicit JPEG
    return path

# --- SCRAPE ---
html = requests.get(BASE, headers={"User-Agent":"Mozilla/5.0"}, timeout=15).text
soup = BeautifulSoup(html, "html.parser")
links = list(set([a["href"] for a in soup.find_all("a", href=True) if "akiya.sumai.biz" in a["href"] and re.search(r"/\d+/?$", a["href"])]))[:8]

for lk in links:
    try:
        pg = requests.get(lk, headers={"User-Agent":"Mozilla/5.0"}, timeout=15).text
        m = re.search(r"(\d+)万円", pg)
        price = int(m.group(1)) if m else 999
        if price>800: continue
        s2 = BeautifulSoup(pg, "html.parser")
        imgs = [i.get("src") for i in s2.find_all("img") if i.get("src") and "http" in i.get("src")][:2]

        prompt = f"""
        Listing {lk} price {price}MAN.
        Write JSON ONLY, ENGLISH ONLY:
        {{
          "hook": "2 lines, ALL CAPS, 3 words per line max, e.g. YEN 3M\nFARMHOUSE",
          "sub_hook": "5 words e.g. ALL-ELECTRIC WIDE LOT",
          "roi": 11.5,
          "catch": "short risk in English",
          "caption": "IG caption English, 2 lines"
        }}
        """
        data = deepseek(prompt)
        hook = data.get("hook","YEN 3M HOUSE")
        sub = data.get("sub_hook","WIDE LOT")

        slide = make_slide(imgs[0] if imgs else "", hook, sub, price)

        cap = f"{hook}\n{price} MAN - {lk}\n{data.get('catch','')}\n{data.get('caption','')}"
        send(cap, lk)

        with open(slide,"rb") as f:
            requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
                data={"chat_id": CHAT_ID, "caption": f"FIXED V3 ENGLISH: {hook}"}, files={"photo": f})
        break
    except Exception as e:
        print(e)
        import traceback; traceback.print_exc()
