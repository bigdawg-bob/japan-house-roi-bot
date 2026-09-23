import os, requests, xml.etree.ElementTree as ET
from datetime import datetime
import time

def fetch_sitemap(url):
    try:
        r = requests.get(url, timeout=30, headers={"User-Agent": "AkiyaBot/1.0 (+contact@yourdomain.com)"})
        print(f"FETCH {url} -> {r.status_code} {len(r.content)} bytes")
        links = set()
        root = ET.fromstring(r.content)
        for elem in root.iter():
            if elem.tag.endswith('loc') and elem.text:
                t = elem.text.strip()
                if '/akiya/' in t or '/bukken/' in t or t.count('/') > 3:
                    links.add(t)
        return list(links)
    except Exception as e:
        print(f"FAIL {url}: {e}")
        return []

def load_seen():
    if not os.path.exists("seen.txt"):
        print("No seen.txt - first run")
        return set()
    with open("seen.txt","r") as f:
        data = [l.strip() for l in f if l.strip()]
    print(f"Loaded seen.txt with {len(data)} lines")
    # Keep only last 300 so it never grows to infinity and blocks everything
    return set(data[-300:])

def save_seen(all_links):
    trimmed = list(all_links)[-300:]
    with open("seen.txt","w") as f:
        f.write("\n".join(trimmed))
    with open("posted.json","w") as f:
        f.write("{}")
    print(f"SAVED seen.txt with {len(trimmed)} lines")

# --- MAIN ---
seen = load_seen()
weekday = datetime.now().weekday()

# ROTATION - you set this once, no daily changes
if weekday in [0,2,4,5,6]: # Mon Wed Fri Sat Sun
    print("TODAY = akiya.sumai.biz day")
    pool = fetch_sitemap("https://akiya.sumai.biz/sitemap.xml")
elif weekday == 1: # Tuesday
    print("TODAY = akiyaathome day - using sumai as fallback for now")
    pool = fetch_sitemap("https://akiya.sumai.biz/sitemap.xml")
    # Later you can add: fetch_sitemap("https://www.akiyaathome.jp/sitemap.xml")
else: # Thursday
    print("TODAY = inakanotane day - using sumai as fallback for now")
    pool = fetch_sitemap("https://akiya.sumai.biz/sitemap.xml")

new_pool = [u for u in pool if u not in seen]
print(f"POOL total {len(pool)} -> after seen filter {len(new_pool)}")

# ALWAYS save, even if 0 - this fixes your 248 Byte empty artifact
save_seen(seen.union(set(pool)))

# Post 1 today with relaxed criteria
if new_pool:
    to_post = new_pool[0]
    print(f"POSTING: {to_post}")
    bot = os.getenv("BOT_TOKEN")
    chat = os.getenv("CHAT_ID")
    if bot and chat:
        try:
            requests.post(f"https://api.telegram.org/bot{bot}/sendMessage",
                          json={"chat_id": chat, "text": f"New Akiya: {to_post}"}, timeout=15)
            print("Telegram sent")
        except Exception as e:
            print(f"Telegram fail: {e}")
else:
    print("No new links today, but seen.txt saved so tomorrow will work")
