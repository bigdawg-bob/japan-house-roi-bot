"""
LIFULL HOME'S 空き家バンク (homes.co.jp/akiyabank) scraper – national akiya bank.
Listing pages look like /akiyabank/b-12345/.
"""
import os, re
from urllib.parse import urljoin

import common
import scraper as base

BASE       = "https://www.homes.co.jp"
CACHE_FILE = base.STATE / "homes_cache.json"
PAGES      = int(os.getenv("HOMES_PAGES", "2"))
MAX_FETCH  = int(os.getenv("HOMES_MAX_FETCH", "60"))
LINK_RE    = re.compile(r"/akiyabank/b-\d+")
TEMPLATES  = [t for t in [os.getenv("HOMES_LIST_URL")] if t] or [
    BASE + "/akiyabank/{slug}/list/?page={page}",
    BASE + "/akiyabank/{slug}/?page={page}",
    BASE + "/akiyabank/farmlands/{slug}/?page={page}",
]


def links(r):
    return list(dict.fromkeys(urljoin(BASE, m) + "/" for m in LINK_RE.findall(r.text)))

def find_template():
    """Try the list-page URL styles on Nagano; use the first that shows listings."""
    last = None
    for tpl in TEMPLATES:
        r = common.get(tpl.format(slug="nagano", page=1))
        if r:
            last = r
            if len(links(r)) >= 3:
                print(f"homes: using list pages like {tpl}")
                return tpl
    print("homes: no prefecture list page found – using new listings from the home page only")
    if last:
        common.show_links(last, "homes")
    return None

def list_urls():
    urls = []
    r = common.get(BASE + "/akiyabank/")
    if r:
        urls += links(r)
    tpl = find_template()
    if tpl:
        for slug in common.SLUGS:
            for p in range(1, PAGES + 1):
                r = common.get(tpl.format(slug=slug, page=p))
                if not r:
                    break
                new = [u for u in links(r) if u not in urls]
                urls += new
                if not new:
                    break
    return urls


def scrape():
    return common.run("homes", CACHE_FILE, list_urls, MAX_FETCH)


if __name__ == "__main__":        # local test: python scraper_homes.py
    for x in scrape()[:5]:
        print(x["location"], x["price_yen"], x["bedrooms"], x["year_built"], x["url"])
