from pathlib import Path
from jinja2 import Environment, FileSystemLoader
from playwright.sync_api import sync_playwright

TPL = Path(__file__).parent / "templates"
env = Environment(loader=FileSystemLoader(TPL), autoescape=True)

def render(slides, out_dir: Path):
    """slides = [("cover.html", {...data...}), ...] -> list of JPEG paths"""
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1080, "height": 1350})
        for i, (tpl, data) in enumerate(slides, 1):
            tmp = TPL / f"_tmp{i}.html"      # saved here so fonts/ can be found
            tmp.write_text(env.get_template(tpl).render(**data), encoding="utf-8")
            page.goto(tmp.as_uri())
            page.wait_for_load_state("networkidle")
            page.evaluate("() => document.fonts.ready")
            page.wait_for_timeout(100)       # let the shrink script finish
            out = out_dir / f"slide{i}.jpg"
            page.screenshot(path=str(out), type="jpeg", quality=90)
            paths.append(out)
            tmp.unlink()
        browser.close()
    return paths

if __name__ == "__main__":
    render([("cover.html", {
        "photo": "test.jpg",
        "hook": "Ski all day, soak all night",
        "sub_hook": "3 km to Happo-One · Onsen 800 m",
        "price_usd": "$66K", "price_yen": "¥10.0M",
        "specs": "3BR · Built 1990 · 98 m²",
        "location": "Hakuba, Nagano", "handle": "@yourhandle",
    })], Path("out"))
