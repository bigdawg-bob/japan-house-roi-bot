"""
Preview every slide (and optionally the reel) with made-up data.
No scraping, no AirROI, no Grok, no Pexels, no Telegram.

  python preview.py                    -> plain blue background
  python preview.py house.jpg          -> your own photo
  python preview.py https://...jpg     -> a photo from the web
  python preview.py house.jpg --reel   -> also make the 7 s reel (needs ffmpeg)

Everything is saved in out/ as preview_*.jpg (+ preview_reel.mp4).
"""
import faulthandler, signal, sys
from PIL import Image

import main

# importing main starts its 25-min run timer – not needed for a preview
faulthandler.cancel_dump_traceback_later()
if hasattr(signal, "alarm"):
    signal.alarm(0)

# ── sample house (change anything you like) ──
FX = 0.0064
LISTING = {
    "price_yen": 3_000_000, "pref": "佐賀県", "location": "佐賀県嬉野市嬉野町",
    "bedrooms": 6, "year_built": 1984, "area_m2": 140, "geo_level": "district",
    "source": "athome", "url": "https://example.com/listing",
}
HOOKS = [
    {"kind": "onsen", "name": "Ureshino Onsen", "lat": 33.100, "lng": 129.990,
     "min": 3, "road_km": 2.5, "routed": True, "router": "osrm"},
]
EST = {
    "net": 12096, "roi": 0.31, "monthly": 1008,
    "all_in": 39000, "house": 19000, "reno": 15000, "fees": 5000,
    "adr": 120, "occ": 0.80, "nights": 180, "mgmt_pct": 0.30, "breakeven_yrs": 3.2,
    "fees_yearly": 0,
}
# MADE-UP sample text for layout testing only – not real facts about the town
FACTS = {
    "known_for": "Silky-water onsen town",
    "visitors": "2M visitors/yr",
    "minpaku_cap": None,
    "points": {
        "ryokan":      "Ryokan: ¥15k–30k per person",
        "competition": "Only 12 Airbnbs in town",
        "access":      "30 min from Nagasaki Airport",
        "stay":        "Avg stay: 1.3 nights",
        "foreign":     "Foreign guests: 20% of stays",
        "plan_b":      "Ryokan staff housing shortage",
    },
}

# ── arguments ──
args = [a for a in sys.argv[1:] if not a.startswith("--")]
MAKE_REEL = "--reel" in sys.argv

def load_photo(src):
    try:
        if src.startswith("http"):
            return main.download(src)
        return Image.open(src).convert("RGB")
    except OSError as ex:
        print(f"Couldn't open {src}: {ex}")
        return None

photo = load_photo(args[0]) if args else None
if photo is None:
    if args:
        print("Couldn't load that photo, using the plain background")
    photo = Image.new("RGB", (main.W, main.H), (38, 64, 104))      # plain blue
pic = {"id": "preview", "img": photo, "credit": "Photo: preview sample"}

usd = LISTING["price_yen"] * FX
h0 = HOOKS[0]
main.OUT.mkdir(exist_ok=True)

def save(img, name):
    path = main.OUT / f"preview_{name}.jpg"
    img.save(path, "JPEG", quality=90)
    print(f"saved {path}")

# ── slides 1 + 2 (with the yield shown / hidden) ──
real_show_yield = main.show_yield
try:
    # slide 1: yield shown / only monthly income / no income data
    for name, yield_on, e in (("yield", True, EST), ("income", False, EST),
                              ("price_only", True, None)):
        main.show_yield = lambda _e, on=yield_on: on
        save(main.cover_slide(photo, LISTING, HOOKS, usd, e), f"1_cover_{name}")

    # slide 2: years to payback / monthly income only
    for name, yield_on in (("payback", True), ("monthly", False)):
        main.show_yield = lambda _e, on=yield_on: on
        save(main.area_slide(pic, h0, LISTING, EST, usd), f"2_{name}")
finally:
    main.show_yield = real_show_yield                 # put the real rule back

# ── slide 3: one version per headline template (A–E) ──
for t in main.TEMPLATES:
    rot = {"s3_templates": [x for x in main.TEMPLATES if x != t]}   # makes t the "unused" one
    s3 = main.slide3_text(h0, LISTING, HOOKS, FACTS, EST, rot)
    note = "" if s3["template"] == t else \
        f"  (template {t} not possible with this data -> used {s3['template']})"
    print(f"slide 3 {t}: {s3['headline']} | {' / '.join(s3['points'])}{note}")
    save(main.facts_slide(pic, h0, LISTING, HOOKS, FACTS, EST, s3), f"3_facts_{t}")

# ── optional cover.html template ──
try:
    main.render_cover_html(main.cover_fields(LISTING, HOOKS, usd, EST))
except Exception as ex:
    print(f"cover.html skipped: {ex!r}")

# ── optional reel ──
if MAKE_REEL:
    path, line = main.build_reel(pic, h0, FACTS, EST)
    if path:
        dest = main.OUT / "preview_reel.mp4"
        path.replace(dest)
        frame = main.OUT / "reel_frame.jpg"
        if frame.exists():
            frame.replace(main.OUT / "preview_reel_frame.jpg")
        print(f"saved {dest} ({line})")
else:
    print("(add --reel to also make the 7 s reel)")
