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
    if url in SOURCES:
        return False
    if not re.search(r"(/listing/|/bukken/|/detail/|/property/|/\d{5,}|akiya/)", url):
        if "sumai.biz" not in url or not re.search(r"/\d+/?$", url):
            print(f"SKIP not detail: {url}")
            return False
    for kw in SOLD_KEYWORDS:
        if kw.lower() in html.lower():
            print(f"SKIP sold keyword {kw} in {url}")
            return False
    if not re.search(r"(\d+万円|¥\s*\d+[,\d]*)", html):
        print(f"SKIP no price: {url}")
        return False
    return True

def deepseek(p):
    system = """You are @japan.house.roi IG copywriter for HK investors.
    ENGLISH ONLY. No Japanese words like machiya/kominka – say "Townhouse".
    Return SINGLE JSON object only: hook, sub_hook, caption, rating, reason, reno_cost, nightly_rate, occupancy, net_income, yield, license_type, license_days.
    HARD RULES - MUST APPLY:
    - Hakuba/Happo-One/Nozawa/Niseko/Furano/Myoko ski <15 mins to lift + under $100K = 10/10 holy grail.
    - Atami Onsen + onsen included + <15 mins walk to Atami Station + famous tourist = 9.5/10.
    - Shonan/Kamakura surf or Karuizawa = 9/10.
    - Kyoto residential = MAX 6/10 because Minpaku 60 days/year and Jan15-Mar15 only per Kyoto city rule. Must mention restriction.
    - Random unknown town = MAX 5/10 SKIP.
    - 30 mins from Tokyo/Osaka CBD > 5 mins from random small station. Popularity > yield.
    WEIGHTS: Popularity 50%, Access 30%, Condition 10%, Price 10%. Yield NOT in rating.
    HOOK simple: "$45K HAKUBA SKI HOUSE - 8 MINS TO LIFTS" not "Fushimi Inari Neighbor". Always "X MINS TO [JR STATION / CBD / LIFTS / BEACH]".
    RENO: reform/new = $5K, average = $20K, old>35y = $35K.
    RENT: Use AirDNA comps – Kyoto $138/night 83%, Hakuba $180/night 65% winter, Atami $160/night 68% onsen demand.
    LICENSE: New Minpaku 180 days max, Tokku 365 days (2 night min), Kan'i Shukusho 365 days. Kyoto residential 60 days.
    CAPTION USD ONLY, NO YEN:
    🏔 [HOOK]
    💰 $[PRICE]K USD | [Location simple]
    📍 [X mins to JR/lifts/beach]
    🏠 [Size] | [Feature in English]
    📊 ROI (Based on AirDNA: $[rate]/night [occ]%)
    BUY: $[PRICE]K
    RENO: $[RENO]K
    TOTAL: $[TOTAL]K
    **Net Rental Income: $[NET]K / year ([YIELD]% yield)**
    📜 LICENSE: [type] - [days] days/year – [note]
    ⭐ RATING: [rating]/10 - [reason]
    ⚠️ CATCH: [risk]
    🔗 Source: [link]
    """
    for attempt in range(3):
        try:
            r = requests.post("https://api.deepseek.com/chat/completions",
                headers={"Authorization": f"Bearer {DEEPSEEK_KEY}","Content-Type":"application/json"},
                json={"model":"deepseek-chat","messages":[{"role":"system","content":system},{"role":"user","content":p}],"response_format":{"type":"json_object"}}, timeout=40)
            content = r.json()["choices"][0]["message"]["content"]
            try:
                return json.loads(content)
            except json.JSONDecodeError as je:
                print(f"JSON fix attempt {attempt}: {je}")
                m = re.search(r'\{.*\}', content, re.DOTALL)
                if m:
                    return json.loads(m.group(0))
                raise
        except Exception as e:
            print(f"DeepSeek retry {attempt} failed: {e}")
            if attempt == 2:
                print(f"Raw content: {content[:800] if 'content' in locals()
