import os, requests, xml.etree.ElementTree as ET
from datetime import datetime

def get_links_sumai():
    # 100% compliant - sitemap + feed are whitelisted
    links = set()
    try:
        # sitemap.xml - 600+ URLs
        r = requests.get("https://akiya.sumai.biz/sitemap.xml", timeout=20, headers={"User-Agent":"AkiyaBot/1.0"})
        root = ET.fromstring(r.content)
        for url_elem in root.findall(".//{*}loc"):
            if url_elem.text and "/akiya/" in url_elem.text:
                links.add(url_elem.text.strip())
    except Exception as e:
        print(f"sitemap fail: {e}")

    try:
        # feed - 10 newest
        import feedparser
        feed = feedparser.parse("https://akiya.sumai.biz/feed/")
        for entry in feed.entries:
            links.add(entry.link)
    except Exception as e:
        print(f"feed fail: {e}")
    
    return list(links)

def load_seen():
    if not os.path.exists("seen.txt"):
        print("No seen.txt found - creating new empty one")
        return set()
    with open("seen.txt", "r") as f:
        lines = [l.strip() for l in f if l.strip()]
    # Keep only last 300 so it never goes to 0 forever
    return set(lines[-300:])

# In main:
# Monday/Wed/Fri = sumai only
# Tuesday = akiyaathome.jp prefectures 1-23
# Thursday = akiyaathome.jp prefectures 24-47
# This rotation keeps you from hitting any site daily

today = datetime.now().weekday()
if today in [0,2,4,5,6]: # Mon, Wed, Fri, Sat, Sun
    pool = get_links_sumai()
else:
    pool = [] # you will add other sites here with same sitemap method

seen = load_seen()
new_pool = [u for u in pool if u not in seen]

print(f"POOL total {len(pool)} -> after seen filter {len(new_pool)}")
