"""
Hands the finished post to n8n (the "post package").
  1. Uploads slides + reel to a GitHub Release  -> stable URLs n8n can download
  2. POSTs the package JSON to N8N_WEBHOOK_URL -> Validate Listing -> AI agent -> approval
send() returns True (n8n accepted), False (failed), None (N8N_WEBHOOK_URL not set).
"""
import hashlib, json, math, mimetypes, os, time
from datetime import datetime, timezone
from pathlib import Path

import requests

WEBHOOK  = (os.getenv("N8N_WEBHOOK_URL") or "").strip()
SECRET   = (os.getenv("N8N_SECRET") or "").strip()
GH_TOKEN = os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN")
REPO     = os.getenv("GITHUB_REPOSITORY")
RUN      = f"{os.getenv('GITHUB_RUN_ID', int(time.time()))}-{os.getenv('GITHUB_RUN_ATTEMPT', '1')}"
DRY_RUN  = os.getenv("DRY_RUN") == "1"
ROOT     = Path(__file__).parent
ANALYTICS_FILE = ROOT / "state" / "analytics.json"     # optional, used if it exists

S = requests.Session()
S.trust_env = False            # direct connection: JP_PROXY is only for scraping


def _f(v, nd=1):
    """Finite number rounded, else None (NaN/inf would break the JSON)."""
    if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v):
        return round(float(v), nd)
    return None


def build_package(m, l, hooks, usd, e, fees_yen, caption, s3=None,
                  reel_path=None, reel_line=None, reel_caption=None, reel_clip=None):
    """Everything n8n needs. m = the main.py module (for its helpers)."""
    h0 = hooks[0]
    pref = l.get("pref") or ""
    loc = l.get("location") or ""
    city = loc[len(pref):] if pref and loc.startswith(pref) else loc
    cover = m.cover_fields(l, hooks, usd, e)
    analytics = m.load_json(ANALYTICS_FILE) or None
    return {
        "listing_id": f"{l.get('source') or 'sumai'}-"
                      f"{hashlib.sha1(l['url'].encode()).hexdigest()[:12]}",
        "source_url": l["url"],
        "scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "dry_run": DRY_RUN,
        "post_type": "reel" if reel_path else "carousel",
        "facts": {
            "price_jpy": int(l["price_yen"]),
            "prefecture": m.PREF_EN.get(pref, pref),
            "city": city or loc,
            "bedrooms": l.get("bedrooms"),
            "building_m2": _f(l.get("area_m2")),
            "year_built": l.get("year_built"),
        },
        "themes": list(dict.fromkeys(h["kind"] for h in hooks)),
        "photos": list(l.get("photos") or []),
        "extras": {
            "hook": m.hook_label(h0),
            "hook_kind": h0["kind"],
            "trip": m.fmt_trip(h0, l),
            "nearby": ", ".join(f"{m.hook_label(h)} ({m.fmt_trip(h, l)})" for h in hooks[1:4]),
            "price_usd": round(usd),
            "net_yield_pct": _f(e["roi"] * 100),
            "yield_ok": bool(m.show_yield(e)),
            "monthly_usd": _f(e.get("monthly"), 0),
            "adr_usd": _f(e.get("adr"), 0),
            "occupancy_pct": _f((e.get("occ") or 0) * 100, 0),
            "fees_yearly_jpy": round(fees_yen or 0),
            "caption": caption,
        },
        "media": {},                                   # filled in by send()
        "draft": {
            "caption": caption,
            "reel_caption": reel_caption or "",
            "reel_hook_line": reel_line or "",
            "reel_clip": reel_clip,
            "cover_hook": f"{cover['hook']} {cover['sub_hook']}".strip(),
            "cover_location": cover["location"],
            "slide3": s3 or {},
        },
        "analytics": analytics,
        "analytics_status": "measured" if analytics else "none_use_hypotheses",
    }


def upload_media(files, listing_id):
    """Uploads files to a new GitHub Release. Returns ({name: url}, release page) or ({}, None)."""
    if not (GH_TOKEN and REPO):
        print("!! n8n: GH_TOKEN / GITHUB_REPOSITORY missing – can't publish media")
        return {}, None
    h = {"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json",
         "X-GitHub-Api-Version": "2022-11-28"}
    tag = f"post-{datetime.now(timezone.utc):%Y%m%d}-{RUN}"
    r = S.post(f"https://api.github.com/repos/{REPO}/releases", headers=h, timeout=60,
               json={"tag_name": tag, "name": f"Post media {tag}", "body": listing_id,
                     "prerelease": True, "make_latest": "false"})
    if not r.ok:
        print(f"!! n8n: release create {r.status_code}: {r.text[:300]}")
        return {}, None
    rel = r.json()
    upload = rel["upload_url"].split("{")[0]
    urls = {}
    for p in files:
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        with open(p, "rb") as fh:
            u = S.post(upload, params={"name": p.name}, data=fh, timeout=300,
                       headers={**h, "Content-Type": ctype})
        if not u.ok:
            print(f"!! n8n: upload {p.name} {u.status_code}: {u.text[:300]}")
            return {}, None
        urls[p.name] = u.json()["browser_download_url"]
    print(f"n8n: media published ({len(urls)} files): {rel['html_url']}")
    return urls, rel["html_url"]


def send(pkg, slide_paths, reel_path=None):
    if not WEBHOOK:
        print("n8n: N8N_WEBHOOK_URL not set – handoff off")
        return None
    if "/webhook-test/" in WEBHOOK:
        print("!! n8n: N8N_WEBHOOK_URL is the TEST url (/webhook-test/). "
              "It only works while n8n is listening in the editor. Use /webhook/.")
    slides = [Path(p) for p in slide_paths]
    files = slides + ([Path(reel_path)] if reel_path else [])
    urls, release = upload_media(files, pkg["listing_id"])
    if not urls:
        return False
    pkg["media"] = {"slides": [urls[p.name] for p in slides],
                    "cover": urls[slides[0].name],
                    "reel": urls.get(Path(reel_path).name) if reel_path else None,
                    "release_url": release}
    (ROOT / "out" / "package.json").write_text(
        json.dumps(pkg, ensure_ascii=False, indent=1), encoding="utf-8")

    headers = {"Content-Type": "application/json"}
    if SECRET:
        headers["X-Webhook-Secret"] = SECRET
    for attempt in range(1, 4):
        try:
            r = S.post(WEBHOOK, json=pkg, headers=headers, timeout=120)
            print(f"n8n: {r.status_code} {r.text[:300]}")
            if r.ok:
                print(f"n8n: post package accepted ({pkg['listing_id']})")
                return True
            if r.status_code == 404:
                print("!! n8n: 404 = workflow not published or wrong URL (must be /webhook/...)")
                break
            if r.status_code in (400, 401, 403, 422):
                break                                   # retrying won't help
        except requests.RequestException as ex:
            print(f"n8n: attempt {attempt} error: {ex}")
        time.sleep(5 * attempt)
    return False
