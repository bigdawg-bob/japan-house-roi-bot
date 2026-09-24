"""
At Home 空き家バンク (akiya-athome.jp) scraper – national akiya bank.
Reads each prefecture's list sorted cheapest first, then the listing pages.
"""
import os, re
from urllib.parse import urljoin

import common
import scraper as base

BASE       = "https://www.akiya-athome.jp"
CACHE_FILE = base.STATE / "athome_cache.json"
PAGES      = int(os.getenv("ATHOME_PAGES", "2"))        # list pages per prefecture (20 each)
MAX_FETCH  = int(os.getenv("ATHOME_MAX_FETCH", "60"))   # listing pages per run
LINK_RE    = re.compile(r"(?:https?://[a-z0-9-]+\.akiya-athome\.jp)?/bukken/detail/buy/[^\s\"'#?<>]+")


def list_urls():
    urls = []
    for i in range(1, 48):
        cd = f"{i:02d}"
        for p in range(1, PAGES + 1):
            r = common.get(f"{BASE}/buy/{cd}/?br_kbn=buy&item_count=20&page={p}"
                           f"&pref_cd={cd}&search_sort=low_price")
            if not r:
                break
            found = [urljoin(BASE, m) for m in LINK_RE.findall(r.text)]
            if i == 1 and p == 1 and not found:
                common.show_links(r, "athome")
            new = [u for u in dict.fromkeys(found) if u not in urls]
            urls += new
            if not new:
                break
    return urls


def scrape():
    return common.run("athome", CACHE_FILE, list_urls, MAX_FETCH)


if __name__ == "__main__":        # local test: python scraper_athome.py
    for x in scrape()[:5]:
        print(x["location"], x["price_yen"], x["bedrooms"], x["year_built"], x["url"])
