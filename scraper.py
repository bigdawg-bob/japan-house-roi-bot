import os, requests, re, json
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont
from io import BytesIO
from urllib.parse import urljoin

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
DEEPSEEK_KEY = os.getenv("DEEPSEEK_API_KEY")
BASE_URL = "https://akiya.sumai.biz/"

def deepseek(prompt):
    r = requests.post("https://api.deepseek.com/chat/completions",
        headers={"Authorization": f"Bearer {DEEPSEEK_KEY}", "Content-Type": "application/json"},
        json={
            "model": "deepseek-chat",
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"}
        }, timeout=40)
    return json.loads(r.json()["choices"][0]["message"]["content"])

def send_text(msg, link=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    kb = [[{"text": "✅ Approve & Queue", "callback_data": "approve"}, {"text": "❌ Skip", "callback_data": "skip"}]]
    if link:
        kb.append([{"text": "🔗 Open Listing", "url": link}])
    requests.post(url, json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown", "reply_markup": {"inline_keyboard": kb}})

def load_font(size):
    # GitHub Actions ubuntu-latest has DejaVu bold
    paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for p in paths:
        if os.path.exists(p):
            try: return ImageFont.truetype(p, size)
            except: pass
    return ImageFont.load_default()

def make
