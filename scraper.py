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
    r = requests.post("https://api.deepseek.com/chat/completions",
        headers={"Authorization": f"Bearer {DEEPSEEK_KEY}", "Content-Type": "application/json"},
        json={"model": "deepseek-chat", "messages": [{"role": "user", "content": p}], "response_format": {"type": "json_object"}}, timeout=30)
    return json.loads(r.json()["choices"][0]["message"]["content"])

def send(msg, link=None):
    kb = [[{"text": "✅ Approve", "callback_data": "a"}, {"text": "❌ Skip", "callback_data": "s"}]]
    if link:
        kb.append([{"text": "🔗 Open", "url": link}])
    requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": msg, "reply_markup": {"inline_keyboard": kb}})

def font(sz):
    for p in ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, sz)
            except:
                pass
    return ImageFont.load_default()

def make_slide(img_url, hook, sub, price):
    W, H = 1080, 1350
    try:
        if not img_url.startswith("http"):
            img_url = urljoin(BASE, img_url)
        d = requests.get(img_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10).content
        im = Image.open(BytesIO(d)).convert("RGB")
        im = im.resize((W, H))
    except:
        im = Image.new("RGB", (W, H), (30, 30, 30))
    dr = ImageDraw.Draw(im, "RGBA")
    dr.rectangle([(0, 0), (W, H)], fill=(0, 0, 0, 90))
    dr.rectangle([(0, 0), (W, 500)], fill=(0, 0, 0, 200))
    dr.rectangle([(0, 500), (W, 506)], fill=(255, 230, 0, 255))
    fb = font(95)
    fm = font(42)
    fs = font(30)
    y = 60
    for ln in hook.split("\n")[:2]:
        dr.text((45, y), ln, font=fb, fill="white", stroke_width=5, stroke_fill="black")
        y += 115
    dr.text((45, y + 5), sub, font=fm, fill="#FFEB3B")
    dr.text((45, y + 60), f"{price} MAN | Nagano Ski", font=fs, fill="#CCCCCC")
    dr.rounded_rectangle([(45, H - 170), (W - 45, H - 70)], radius=45, fill="white")
    dr.text((85, H - 140), f"{price} MAN $20K 11% ROI", font=fm, fill="black")
    path = f"/tmp/s_{os.urandom(2).hex()}.jpg"
    im.save(path, quality=90)
    return path

html = requests.get(BASE, headers={"User-Agent": "Mozilla/5.0"}, timeout=15).text
soup = BeautifulSoup(html, "html.parser")
links = list(set([a["href"] for a in soup.find_all("a", href=True) if "akiya.sumai.biz" in a["href"] and re.search(r"/\d+/?$", a["href"])]))[:8]

for lk in links:
    try:
        pg = requests.get(lk, headers={"User-Agent": "Mozilla/5.0"}, timeout=15).text
        m = re.search(r"(\d+)MAN", pg.replace("万円", "MAN"))
        if not m:
            m2 = re.search(r"(\d+)万円", pg)
            price = int(m2.group(1)) if m2 else 999
        else:
            price = int(m.group(1))
        if price > 800:
            continue
        s2 = BeautifulSoup(pg, "html.parser")
        imgs = [im.get("src") for im in s2.find_all("img") if im.get("src")][:3]

        pr = f"Listing {lk} price {price}MAN. Return JSON: hook 2 lines caps clickbait, sub_hook 5 words, roi, catch, caption"
        data = deepseek(pr)
        hook = data.get("hook", "YEN 3M HOUSE")
        sub = data.get("sub_hook", "WIDE LOT")

        slide = make_slide(imgs[0] if imgs else "", hook, sub, price)
        cap = f"🏔️ {hook}\n💰 {price} MAN | {lk}\n⚠️ {data.get('catch','')}\n{data.get('caption','')}"
        send(cap, lk)
        with open(slide, "rb") as f:
            requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
                data={"chat_id": CHAT_ID, "caption": f"FIXED: {hook}"}, files={"photo": f})
        break
    except Exception as e:
        print(e)
