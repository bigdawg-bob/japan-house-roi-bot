"""
Preview the cover + numbers slides with made-up data.
No scraping, no AirROI calls, no Telegram.

  python preview.py                  -> plain blue background
  python preview.py house.jpg        -> your own photo
  python preview.py https://...jpg   -> a photo from the web
"""
import sys
from PIL import Image
import main

# ── sample house (change anything you like) ──
FX = 0.0064
listing = {
    "price_yen": 3_000_000, "pref": "佐賀県", "location": "佐賀県嬉野市嬉野町",
    "bedrooms": 6, "year_built": 1984, "area_m2": 140, "geo_level": "district",
    "source": "athome", "url": "https://example.com/listing",
}
hooks = [
    {"kind": "onsen", "name": "Ureshino Onsen", "min": 3, "road_km": 2.5,
     "routed": True, "router": "osrm"},
]
est = {
    "net": 12096, "roi": 0.31, "monthly": 1008,
    "all_in": 39000, "house": 19000, "reno": 15000, "fees": 5000,
    "adr": 120, "occ": 0.80, "nights": 180, "mgmt_pct": 0.30, "breakeven_yrs": 3.2,
}

# ── photo ──
photo = None
if len(sys.argv) > 1:
    src = sys.argv[1]
    photo = main.download(src) if src.startswith("http") else Image.open(src).convert("RGB")
    if photo is None:
        print("Couldn't load that photo, using the plain background")

usd = listing["price_yen"] * FX
main.OUT.mkdir(exist_ok=True)

# 3 versions of the cover: yield shown / only monthly income / no income data
versions = [("yield", True, est), ("income", False, est), ("price_only", True, None)]
for name, yield_on, e in versions:
    main.show_yield = lambda _e, on=yield_on: on      # override just for the preview
    img = main.cover_slide(photo, listing, hooks, usd, e)
    path = main.OUT / f"preview_cover_{name}.jpg"
    img.save(path, "JPEG", quality=90)
    print(f"saved {path}")

stats = main.stats_slide(listing, hooks, usd, est, 0, 0)
stats.save(main.OUT / "preview_stats.jpg", "JPEG", quality=90)
print(f"saved {main.OUT / 'preview_stats.jpg'}")
