"""
AI reel (v2 rules) for ski + onsen houses.
Grok writes the scene -> Grok Imagine still (checked) -> 8 s image-to-video (checked)
-> ffmpeg: exactly 7.0 s seamless loop, 70% black + one line of text 0.5-6.5 s,
silent audio. Anything fails -> make() returns None and main.py uses the photo reel.
reel_caption() builds the reel caption (used for the photo reel too).
"""
import base64, io, os, re, subprocess, time
from datetime import datetime

import requests
from PIL import Image, ImageDraw

def _env(name, default):
    return os.getenv(name) or default

KINDS_ON    = set(_env("AI_REEL_KINDS", "ski,onsen").replace(" ", "").split(","))
IMAGE_MODEL = _env("IMAGE_MODEL", "grok-imagine-image-2.0")
VIDEO_MODEL = _env("VIDEO_MODEL", "grok-imagine-video-1.5")
VIDEO_RES   = _env("VIDEO_RES", "720p")
TRIES       = int(_env("AI_REEL_TRIES", "2"))
MIN_LEFT    = float(_env("AI_REEL_MIN_LEFT", "8")) * 60     # run time needed to start a try
VIDEO_WAIT  = 6 * 60                                        # max wait for one video
RAW_SECS    = 8                                             # ask 8 s, use 0.5-7.5 s
API         = "https://api.x.ai/v1"
CTA         = _env("REEL_CTA", "Full math in yesterday's post — yamayield.com + "
                   "Save this if hunting {pref}.")
BROAD_TAGS  = ["#akiya", "#japanproperty", "#japanhouse"]
INTENT_TAGS = {"ski": ["#rentalyield", "#skihouse"], "onsen": ["#rentalyield", "#onsenhouse"]}
DEFAULT_INTENT = ["#rentalyield", "#airbnbjapan"]
REGIONS = {
    "Hokkaido": "Hokkaido",
    "Tohoku":   "Aomori Iwate Miyagi Akita Yamagata Fukushima",
    "Kanto":    "Ibaraki Tochigi Gunma Saitama Chiba Tokyo Kanagawa",
    "Chubu":    "Niigata Toyama Ishikawa Fukui Yamanashi Nagano Gifu Shizuoka Aichi",
    "Kansai":   "Mie Shiga Kyoto Osaka Hyogo Nara Wakayama",
    "Chugoku":  "Tottori Shimane Okayama Hiroshima Yamaguchi",
    "Shikoku":  "Tokushima Kagawa Ehime Kochi",
    "Kyushu":   "Fukuoka Saga Nagasaki Kumamoto Oita Miyazaki Kagoshima",
    "Okinawa":  "Okinawa",
}
REGION = {p: r for r, ps in REGIONS.items() for p in ps.split()}

IMAGE_SUFFIX = (" Vertical 9:16, photoreal, slightly underexposed. No people, no text, "
                "no signs, no logos, no watermark.")
VIDEO_SUFFIX = (". Continuous even motion for the whole clip, realistic physics, steady light, "
                "no cuts, no people, no text, no morphing.")

PLAN_PROMPT = """You plan ONE 7-second looping Instagram Reel that sells the DREAM of owning an
akiya in rural Japan. FACTS: {facts}

SCENE (use only area_type and season from FACTS):
- Exactly one moment, one mood. No story, no time passing.
- {scene}
- Never name or show a real lift, ryokan, hotel or landmark. No people at all. No animals near camera.
- Never show a house as a listing. Only generic texture if any: snow on an engawa,
  window condensation, fire in a hearth.
- image_prompt: 40-50 words. Vertical 9:16. Main subject centered in the middle 40% of the
  frame; top 30% and bottom 30% simple (sky, snow, steam, water, dark wood). Empty,
  photoreal, slightly underexposed. No text, no signs, no logos, no watermark.
- motion_prompt: 10-15 words. Exactly one natural motion that stays even for the whole clip
  (snow falling, steam rising, water flowing) + "static tripod" (preferred, loops best)
  or "very slow dolly in". No zoom, no pan, no cuts.

TEXT:
- dream_line: one dreamy sentence for the caption, max 20 words. Use ONLY numbers that
  appear in FACTS, or none. No hashtags, no emoji.

Answer ONLY with JSON: {{"image_prompt": "...", "motion_prompt": "...", "dream_line": "..."}}"""

QC_PROMPT = """You check {what} for a 7-second Instagram Reel background (an empty, calm
scene in rural Japan). Answer ONLY with JSON: {{"pass": true/false, "problems": ["..."]}}
Fail if ANY image shows: a person, face or body part; letters, text, signs, logos or a
watermark; melting, warped or duplicated objects; obvious AI glitches; a house that looks
like a real-estate listing; {extra}anything unsafe. Soft focus and grain are OK."""

M = None        # the main.py module (set by make() / reel_caption())


# ─── facts (the only numbers anyone may use) ─────────────────────────
def season():
    m = datetime.now(M.JST).month
    return ("winter" if m in (12, 1, 2, 3) else "spring" if m in (4, 5)
            else "summer" if m in (6, 7, 8) else "autumn")

def scene(kind):
    if kind == "ski":
        return ("Ski town: heavy powder snow falling, flat grey light, no blue sky. "
                "Example: powder falling on an empty chairlift.")
    snow = " Snow on the rocks and ground." if season() == "winter" else ""
    return ("Onsen town at dusk: rising steam, warm light from windows. Example: steam "
            "over an empty outdoor bath at dusk." + snow)

def yield_text(e):
    return f"{e['roi'] * 100:.0f}% net yield" if M.show_yield(e) else f"${e['monthly']:,}/mo net"

def facts_for(h, l, usd, e):
    mins, _ = M.trip_parts(h, l)
    f = {"town": M.short_name(h["name"]), "area_type": h["kind"], "season": season(),
         "house_price": "FREE" if l["price_yen"] == 0 else M.fmt_usd(usd),
         "drive_min_to_town": mins}
    if M.show_yield(e):
        f["net_yield_pct"] = int(f"{e['roi'] * 100:.0f}")
    else:
        f["monthly_net_usd"] = e["monthly"]
    return f

def nums(s):
    return {n.rstrip(".,").replace(",", "") for n in re.findall(r"\d[\d,.]*", str(s))}


# ─── plan (Grok writes the scene + dream line) ───────────────────────
def make_plan(h, l, usd, e):
    facts = facts_for(h, l, usd, e)
    allowed = set().union(*(nums(v) for v in facts.values()))
    prompt = PLAN_PROMPT.format(facts=facts, scene=scene(h["kind"]))
    for _ in range(2):
        p = M.json_from(M.grok(prompt, timeout=120))
        if not isinstance(p, dict):
            continue
        img, mot, dream = (str(p.get(k) or "").strip()
                           for k in ("image_prompt", "motion_prompt", "dream_line"))
        if not 25 <= len(img.split()) <= 70:
            print(f"  AI reel plan: image_prompt {len(img.split())} words, retry")
            continue
        if not 5 <= len(mot.split()) <= 20 or re.search(
                r"\b(zoom\w*|pan|pans|panning|cuts?)\b", mot, re.I):
            print(f"  AI reel plan: bad motion '{mot}', retry")
            continue
        if len(dream.split()) > 25 or not nums(dream) <= allowed:
            print(f"  AI reel plan: dream line dropped (unknown number): {dream}")
            dream = ""
        print(f"  AI reel plan: {img} | {mot} | {dream}")
        return {"image_prompt": img, "motion_prompt": mot, "dream_line": dream}
    return None


# ─── xAI calls ───────────────────────────────────────────────────────
def auth():
    return {"Authorization": f"Bearer {M.GROK_KEY}", "Content-Type": "application/json"}

def post(path, body):
    try:
        r = requests.post(API + path, json=body, headers=auth(), timeout=120)
    except requests.RequestException as ex:
        print(f"  xAI {path} error: {ex}")
        return None
    if not r.ok:
        print(f"  xAI {path} {r.status_code}: {r.text[:300]}")
        return None
    return r.json()

def gen_image(prompt):
    d = post("/images/generations", {"model": IMAGE_MODEL, "prompt": prompt,
                                     "aspect_ratio": "9:16", "n": 1, "response_format": "url"})
    try:
        return d["data"][0]["url"]
    except (TypeError, KeyError, IndexError):
        return None

def gen_video(image_url, prompt, deadline):
    d = post("/videos/generations", {"model": VIDEO_MODEL, "prompt": prompt,
                                     "image": {"url": image_url}, "duration": RAW_SECS,
                                     "resolution": VIDEO_RES})
    rid = (d or {}).get("request_id")
    if not rid:
        return None
    end = min(time.time() + VIDEO_WAIT, deadline)
    while time.time() < end:
        time.sleep(8)
        try:
            res = requests.get(f"{API}/videos/{rid}", headers=auth(), timeout=30).json()
        except (requests.RequestException, ValueError):
            continue
        status = res.get("status")
        if status == "done":
            return (res.get("video") or {}).get("url")
        if status in ("failed", "expired"):
            print(f"  AI reel: video {status}: {str(res)[:300]}")
            return None
    print("  AI reel: video not ready in time")
    return None

def fetch_img(url):
    try:
        r = requests.get(url, timeout=60)
        r.raise_for_status()
        return Image.open(io.BytesIO(r.content)).convert("RGB")
    except (requests.RequestException, OSError) as ex:
        print(f"  AI reel: image download failed: {ex}")
        return None


# ─── quality check (Grok looks at the pictures) ──────────────────────
def data_url(img):
    small = img.copy()
    small.thumbnail((768, 768))
    buf = io.BytesIO()
    small.save(buf, "JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()

def check(images, what, extra):
    parts = [{"type": "input_image", "image_url": data_url(i), "detail": "high"} for i in images]
    parts.append({"type": "input_text", "text": QC_PROMPT.format(what=what, extra=extra)})
    v = M.json_from(M.grok(parts, timeout=180))
    if not isinstance(v, dict):
        return False, "no answer from Grok"
    return M.yes(v.get("pass")), "; ".join(map(str, v.get("problems") or []))[:200]


# ─── ffmpeg ──────────────────────────────────────────────────────────
def duration(exe, path):
    p = subprocess.run([exe, "-i", str(path)], capture_output=True, text=True)
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", p.stderr)
    return int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]) if m else 0.0

def frame_at(exe, video, t, path):
    subprocess.run([exe, "-y", "-loglevel", "error", "-ss", str(t), "-i", str(video),
                    "-frames:v", "1", str(path)], check=True)
    return Image.open(path).convert("RGB")

def overlay_png(fit, path):
    """70% black full screen + the one centred white line (drawn with main.py's put())."""
    s, f, size, track = fit
    img = Image.new("RGBA", (M.REEL_W, M.REEL_H), (0, 0, 0, int(255 * M.REEL_OVERLAY)))
    M.put(ImageDraw.Draw(img), (M.REEL_W / 2, M.REEL_H / 2), s, f,
          (255, 255, 255, 255), "mm", track, 0)
    img.save(path)
    return path

def compose(exe, raw, overlay, out):
    """7.0 s: raw 0.5-7.5 s, last 0.5 s blends into the opening (seamless loop, text is
    already off then). Overlay + text fade in at 0.5 s, out by 6.5 s. Silent audio."""
    W, H, FPS, S = M.REEL_W, M.REEL_H, M.REEL_FPS, M.REEL_SECS
    on, off, fade = M.REEL_TEXT_ON, M.REEL_TEXT_OFF, M.REEL_FADE
    fc = (f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos,"
          f"crop={W}:{H},fps={FPS},setsar=1,split[a][b];"
          f"[a]trim=0.5:{0.5 + S},setpts=PTS-STARTPTS[body];"
          f"[b]trim=0:0.5,setpts=PTS-STARTPTS[head];"
          f"[body][head]xfade=transition=fade:duration=0.5:offset={S - 0.5}[loop];"
          f"[1:v]format=rgba,fade=t=in:st={on}:d={fade}:alpha=1,"
          f"fade=t=out:st={off - fade}:d={fade}:alpha=1[ov];"
          f"[loop][ov]overlay=0:0,format=yuv420p[v]")
    p = subprocess.run([exe, "-y", "-loglevel", "error", "-i", str(raw),
                        "-loop", "1", "-framerate", str(FPS), "-t", str(S), "-i", str(overlay),
                        "-f", "lavfi", "-t", str(S), "-i", "anullsrc=r=44100:cl=stereo",
                        "-filter_complex", fc, "-map", "[v]", "-map", "2:a",
                        "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-r", str(FPS),
                        "-c:a", "aac", "-b:a", "128k", "-t", str(S),
                        "-movflags", "+faststart", str(out)],
                       capture_output=True, text=True)
    if p.returncode != 0 or not out.exists():
        print(f"!! AI reel: ffmpeg failed: {p.stderr[:500]}")
        return None
    return out


# ─── one try: still -> check -> video -> check ───────────────────────
def attempt(plan, exe, deadline, n):
    url = gen_image(plan["image_prompt"] + IMAGE_SUFFIX)
    img = fetch_img(url) if url else None
    if img is None:
        return None
    ok, why = check([img], "one still image", "the main subject not in the middle of the frame; ")
    print(f"  AI reel try {n}: still {'OK' if ok else 'REJECTED'} {why}")
    if not ok:
        return None
    vurl = gen_video(url, plan["motion_prompt"] + VIDEO_SUFFIX, deadline)
    if not vurl:
        return None
    raw = M.OUT / "reel_raw.mp4"
    try:
        raw.write_bytes(requests.get(vurl, timeout=300).content)   # link expires, save now
    except requests.RequestException as ex:
        print(f"  AI reel: video download failed: {ex}")
        return None
    secs = duration(exe, raw)
    if secs < M.REEL_SECS + 0.55:
        print(f"  AI reel try {n}: video only {secs:.1f}s")
        return None
    frames = [frame_at(exe, raw, t, M.OUT / f"reel_qc{i}.jpg")
              for i, t in enumerate((0.5, 2, 3.5, 5, 6.5, 7.4))]
    ok, why = check(frames, "6 frames from ONE video, in time order",
                    "a sudden change of scene, light or camera angle between frames; ")
    print(f"  AI reel try {n}: video {'OK' if ok else 'REJECTED'} {why}")
    return raw if ok else None


# ─── caption + hashtags (all from our own data) ──────────────────────
def tag(s):
    return "#" + re.sub(r"[^a-z0-9]", "", s.lower())

def hashtags(h, l):
    pref = M.PREF_EN.get(l["pref"], l["pref"])
    town = M.seo_label(h) if h["kind"] == "onsen" else M.place_name(h)   # #kinosakionsen
    area = [tag(town), tag(pref)]
    region = tag(REGION[pref]) if pref in REGION else "#visitjapan"
    area.append("#visitjapan" if region in area else region)             # Hokkaido, Okinawa
    tags = list(dict.fromkeys(BROAD_TAGS + area + INTENT_TAGS.get(h["kind"], DEFAULT_INTENT)))
    for extra in ("#ruraljapan", "#akiyabank"):
        if len(tags) < 8 and extra not in tags:
            tags.append(extra)
    return tags[:8]

def fallback_dream(h, l):
    mins, _ = M.trip_parts(h, l)
    start = {"ski": "Fresh powder, ", "onsen": "Evening steam, "}.get(h["kind"], "")
    return f"{start}{mins} min from your own front door."

def reel_caption(m, h, l, usd, e, dream=None):
    global M
    M = m
    price = "FREE" if l["price_yen"] == 0 else M.fmt_usd(usd)
    pref = M.PREF_EN.get(l["pref"], l["pref"])
    return "\n".join([
        f"{M.short_name(h['name'])} Akiya ROI: {price} house → {yield_text(e)}",
        dream or fallback_dream(h, l),
        CTA.format(pref=pref),
        "",
        " ".join(hashtags(h, l)),
    ])


# ─── entry point used by main.build_slides() ─────────────────────────
def make(m, h, l, usd, e, facts):
    """Returns (reel path, on-screen line, caption) or None."""
    global M
    M = m
    if h["kind"] not in KINDS_ON:
        print(f"AI reel: off for '{h['kind']}' (AI_REEL_KINDS={','.join(sorted(KINDS_ON))})")
        return None
    exe = M.ffmpeg_exe()
    if not (M.GROK_KEY and exe):
        print("AI reel: needs GROK_API_KEY and ffmpeg")
        return None
    fit = M.reel_line(ImageDraw.Draw(Image.new("L", (M.REEL_W, M.REEL_H))), h, facts, e, usd, l)
    if not fit:
        print("AI reel: no on-screen line fits")
        return None
    plan = make_plan(h, l, usd, e)
    if not plan:
        print("AI reel: no usable plan from Grok")
        return None

    deadline = M.T0 + M.RUN_LIMIT - 3 * 60          # keep 3 min for Telegram + saving
    raw = None
    for n in range(1, TRIES + 1):
        if deadline - time.time() < MIN_LEFT:
            print(f"AI reel: only {(deadline - time.time()) / 60:.0f} min left, stopping")
            break
        raw = attempt(plan, exe, deadline, n)
        if raw:
            break
    if not raw:
        return None
    out = compose(exe, raw, overlay_png(fit, M.OUT / "reel_overlay.png"), M.OUT / "reel.mp4")
    if not out:
        return None
    print(f"AI reel: {out} | {fit[0]} | {fit[2]}px")
    return out, fit[0], reel_caption(m, h, l, usd, e, plan["dream_line"])
