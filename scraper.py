import os, requests, xml.etree.ElementTree as ET
from datetime import datetime

def get_links():
    url = "https://akiya.sumai.biz/sitemap.xml"
    try:
        r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        print(f"SITEMAP {r.status_code} {len(r.content)} bytes")
        links = []
        root = ET.fromstring(r.content)
        for el in root.iter():
            if el.tag.endswith('loc') and el.text:
                t = el.text.strip()
                if 'akiya.sumai.biz' in t and t!= 'https://akiya.sumai.biz/':
                    links.append(t)
        # remove duplicates, keep order
        links = list(dict.fromkeys(links))
        print(f"FOUND {len(links)} links")
        return links
    except Exception as e:
        print(f"ERROR fetching sitemap: {e}")
        return []

def load_seen():
    if not os.path.exists("seen.txt"):
        return set()
    with open("seen.txt","r") as f:
        data = [l.strip() for l in f if l.strip()]
    # keep last 300 only - sustainable
    if len(data) > 300:
        data = data[-300:]
    print(f"LOADED seen.txt {len(data)}")
    return set(data)

def save_seen(all_links):
    trimmed = list(all_links)[-300:]
    with open("seen.txt","w") as f:
        f.write("\n".join(trimmed))
    with open("posted.json","w") as f:
        f.write("{}")
    print(f"SAVED seen.txt {len(trimmed)} lines")

# MAIN
seen = load_seen()
pool = get_links()

new_pool = [u for u in pool if u not in seen]
print(f"POOL total {len(pool)} -> after seen filter {len(new_pool)}")

save_seen(seen.union(set(pool)))

if new_pool:
    to_post = new_pool[0]
    print(f"POSTING TODAY: {to_post}")
    bot = os.getenv("BOT_TOKEN")
    chat = os.getenv("CHAT_ID")
    if bot and chat:
        try:
            msg = f"🏠 New Akiya\n{to_post}"
            requests.post(f"https://api.telegram.org/bot{bot}/sendMessage",
                          json={"chat_id": chat, "text": msg}, timeout=15)
            print("TELEGRAM OK")
        except Exception as e:
            print(f"TELEGRAM FAIL {e}")
    else:
        print("No BOT_TOKEN/CHAT_ID - set in secrets")
else:
    print("No new today - but artifact saved for tomorrow")
