"""
Reel builder for Yama Yield: 1 background = 1 reel.

Background: ONE Pexels video from bg_videos.txt, downloaded with the Pexels API
(PEXELS_KEY). Nothing is read from a reels/ folder.

bg_videos.txt, one video per line:
    https://www.pexels.com/video/snowy-ski-slope-1234567/
    1234567                        <- the ID alone also works
    1234567  ski                   <- only for ski houses (ski onsen sight beach nature)
    1234567  onsen town=kinosaki   <- preferred for Kinosaki houses
    1234567  crop=10               <- cut off the bottom 10% (e.g. a logo or net)
    # lines starting with # are ignored

Reel: 1080x1920, 30 fps, 6.8 s seamless loop, max 8 MB.
  0.0-0.3 s  full bright video (+5%), no overlay, no text
  0.3-0.5 s  black overlay fades in to 55%
  0.3 s      line 1 (hook) fades in (0.15 s)          } both gone by 6.5 s
  1.5 s      line 2 (cheat sheet) fades in (0.15 s)   }
  6.5-6.8 s  overlay fades out, so the last frame matches the first
  Speed 100% -> 103%. Audio: soft generated wind, about -28 LUFS, loops.
"""
import os, random, re, subprocess
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont

def _env(name, default):
    return os.getenv(name) or default

ROOT       = Path(__file__).parent
BG_LIST    = ROOT / _env("BG_LIST", "bg_videos.txt")
PEXELS_KEY = os.getenv("PEXELS_KEY") or os.getenv("PEXELS_API_KEY")
TRIES      = 3                         # videos to try before giving up for today

W, H, FPS  = 1080, 1920, 30
SECS       = 6.8                       # final length
XF         = 0.2                       # loop crossfade (end blends into the start)
WIN        = SECS + XF                 # 7.0 s of footage used
RAMP       = 0.03                      # speed 100% -> 103%
NEED       = WIN * (1 + RAMP / 2)      # source seconds needed (a bit more due to the ramp)
SKIP_START = 3.0                       # avoid the first 3 s of the source if possible
FLASH_END, FLASH_GAIN = 0.3, 1.05      # +5% brightness for the first 0.3 s
OV_ON, OV_FADE = 0.3, 0.2
OV_ALPHA   = min(0.9, max(0.0, float(_env("REEL_OVERLAY", "0.55"))))
OV_OUT     = 6.5                       # overlay fades out 6.5 s -> last frame
HOOK_ON, SUB_ON, TEXT_FADE, TEXT_OFF = 0.3, 1.5, 0.15, 6.5
HOOK_PX, HOOK_MIN_PX = 200, 90
SUB_PX, SUB_ALPHA, LINE_SPACING = 48, 0.85, 1.4
MARGIN     = 80                        # left/right
SAFE_TOP, SAFE_BOTTOM = int(H * 0.30), int(H * 0.70)   # text only in the middle 40%
MAX_MB     = 8.0
AUDIO      = _env("REEL_AUDIO", "wind").lower()        # wind | silent
WIND_LUFS  = float(_env("REEL_WIND_LUFS", "-28"))
STABILIZE  = _env("REEL_STABILIZE", "0") == "1"        # 1 = ffmpeg deshake (for shaky clips)
KIND_NAMES = {"ski", "onsen", "sight", "beach", "nature"}
SUB_FONTS  = [ROOT / "fonts" / "Arimo-Regular.ttf",
              "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
              "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"]

CTA = _env("REEL_CTA", "Full math in yesterday's post — yamayield.com + "
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

M = None        # the main.py module (set by make() / reel_caption())

def slug(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


# ─── bg_videos.txt -> which Pexels video ─────────────────────────────
def read_list():
    try:
        lines = BG_LIST.read_text(encoding="utf-8").splitlines()
    except OSError:
        print(f"  reel bg: {BG_LIST.name} not found")
        return []
    out, seen = [], set()
    for n, raw in enumerate(lines, 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        ids = re.findall(r"\d{5,}", parts[0])
        if not ids:
            print(f"  reel bg: line {n} has no Pexels video ID, skipped: {raw[:60]}")
            continue
        e = {"id": ids[-1], "kinds": set(), "town": "", "crop": 0.0}
        for p in (x.lower() for x in parts[1:]):
            if p in KIND_NAMES:
                e["kinds"].add(p)
            elif p.startswith("town="):
                e["town"] = slug(p[5:])
            elif p.startswith("crop="):
                try:
                    e["crop"] = min(0.3, max(0.0, float(p[5:].rstrip("%")) / 100))
                except ValueError:
                    pass
        if e["id"] not in seen:
            seen.add(e["id"])
            out.append(e)
    return out

def candidates(h, rot):
    """Videos to try, best first: named after this town > tagged with this kind >
    untagged. Videos tagged for another kind or town are never used.
    Least recently used first (history = rotation.json 'reel_clips')."""
    entries = read_list()
    kind, town = h["kind"], slug(M.place_name(h))
    hist = [c for c in (rot.get("reel_clips") or []) if isinstance(c, str)]

    def lru(e):
        k = f"pexels:{e['id']}"
        return (k in hist, -hist.index(k) if k in hist else 0, random.random())

    fits   = [e for e in entries if not e["kinds"] or kind in e["kinds"]]
    named  = [e for e in fits if e["town"] and e["town"] in town]
    tagged = [e for e in fits if not e["town"] and kind in e["kinds"]]
    plain  = [e for e in fits if not e["town"] and not e["kinds"]]
    order = []
    for pool in (named, tagged, plain):
        order += sorted(pool, key=lru)
    print(f"  reel bg: {len(entries)} videos in {BG_LIST.name}, {len(order)} usable for "
          f"{kind} ({len(named)} named after this town)")
    return order

def fetch(entry):
    """Downloads the vertical mp4 of this Pexels video. Returns (path, credit) or None."""
    if not PEXELS_KEY:
        print("  reel bg: PEXELS_KEY missing")
        return None
    vid = entry["id"]
    try:
        r = requests.get(f"https://api.pexels.com/videos/videos/{vid}",
                         headers={"Authorization": PEXELS_KEY}, timeout=30)
        if r.status_code == 404:
            print(f"  reel bg: video {vid} no longer on Pexels – remove it from {BG_LIST.name}")
            return None
        r.raise_for_status()
        v = r.json()
    except (requests.RequestException, ValueError) as ex:
        print(f"  reel bg: Pexels error for {vid}: {ex}")
        return None
    if (v.get("duration") or 99) < NEED - 0.5:
        print(f"  reel bg: video {vid} is only {v.get('duration')}s, need {NEED:.1f}s")
        return None
    files = [f for f in v.get("video_files") or []
             if f.get("file_type") == "video/mp4" and f.get("link")
             and (f.get("height") or 0) > (f.get("width") or 0)]
    if not files:
        print(f"  reel bg: video {vid} has no vertical version – pick a portrait video")
        return None
    big = [f for f in files if f["height"] >= H]
    best = min(big, key=lambda f: f["height"]) if big else max(files, key=lambda f: f["height"])
    path = M.OUT / "reel_src.mp4"
    try:
        with requests.get(best["link"], stream=True, timeout=120) as dl:
            dl.raise_for_status()
            with open(path, "wb") as fh:
                for chunk in dl.iter_content(1 << 20):
                    fh.write(chunk)
    except (requests.RequestException, OSError) as ex:
        print(f"  reel bg: download failed for {vid}: {ex}")
        return None
    name = (v.get("user") or {}).get("name") or "Pexels"
    print(f"  reel bg: pexels {vid} {best['width']}x{best['height']} by {name}")
    return path, f"Video: {name} / Pexels"


# ─── text (2 lines, middle 40% only) ─────────────────────────────────
def sub_font(px):
    for p in SUB_FONTS:
        try:
            return ImageFont.truetype(str(p), px)
        except OSError:
            pass
    return M.cfont("sans", px, 400)

def hook_line(h, l, e, usd, facts):
    """Line 1: bold, 200 px, shrinks (min 90 px) to fit 80 px margins."""
    d = ImageDraw.Draw(Image.new("L", (8, 8)))
    maxw = W - 2 * MARGIN
    for s in M.reel_candidates(h, facts, e, usd, l):
        s = s.upper()
        if len(s.split()) > M.REEL_WORDS:
            continue
        size = HOOK_PX
        while size >= HOOK_MIN_PX:
            f = M.rfont(size)
            if M.text_width(d, s, f) <= maxw:
                return s, f, size
            size -= 4
        print(f"  reel: skip '{s}' (would need < {HOOK_MIN_PX}px)")
    return None

def sub_line(h, l, e):
    """Line 2 (cheat sheet): '4BR • 12 min to slopes • 15% net'."""
    d = ImageDraw.Draw(Image.new("L", (8, 8)))
    mins, mode = M.trip_parts(h, l)
    to = {"ski": "slopes", "onsen": "onsen", "beach": "beach"}.get(h["kind"],
                                                                  M.short_name(h["name"]))
    trip = f"{mins} min walk to {to}" if mode == "walk" else f"{mins} min to {to}"
    money = f"{e['roi'] * 100:.0f}% net" if M.show_yield(e) else f"${e['monthly']:,}/mo net"
    beds = f"{l['bedrooms']}BR" if l.get("bedrooms") else None
    f = sub_font(SUB_PX)
    for parts in ([beds, trip, money], [trip, money], [beds, money], [money]):
        s = " • ".join(p for p in parts if p)
        if s and M.text_width(d, s, f) <= W - 2 * MARGIN:
            return s, f
    return None

def text_pngs(hook, sub):
    """Two transparent 1080x1920 PNGs, the text block centred vertically."""
    s1, f1, _ = hook
    d0 = ImageDraw.Draw(Image.new("L", (8, 8)))
    b1 = d0.textbbox((0, 0), s1, font=f1, anchor="ls")
    h1 = b1[3] - b1[1]
    if sub:
        s2, f2 = sub
        b2 = d0.textbbox((0, 0), s2, font=f2, anchor="ls")
        h2, gap = b2[3] - b2[1], round(SUB_PX * (LINE_SPACING - 1)) + 30
    else:
        h2 = gap = 0
    top = (H - (h1 + gap + h2)) // 2
    if top < SAFE_TOP or top + h1 + gap + h2 > SAFE_BOTTOM:
        print("  reel: !! text leaves the middle 40% safe zone")

    p1, p2 = M.OUT / "reel_hook.png", M.OUT / "reel_sub.png"
    img = Image.new("RGBA", (W, H), (255, 255, 255, 0))
    M.put(ImageDraw.Draw(img), (W / 2, top - b1[1]), s1, f1, (255, 255, 255, 255), "ms", 0, 0)
    img.save(p1)
    img = Image.new("RGBA", (W, H), (255, 255, 255, 0))
    if sub:
        M.put(ImageDraw.Draw(img), (W / 2, top + h1 + gap - b2[1]), s2, f2,
              (255, 255, 255, int(255 * SUB_ALPHA)), "ms", 0, 0)
    img.save(p2)
    return p1, p2


# ─── ffmpeg ──────────────────────────────────────────────────────────
def duration(exe, path):
    p = subprocess.run([exe, "-i", str(path)], capture_output=True, text=True)
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", p.stderr)
    return int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]) if m else 0.0

def crop_filter(crop):
    return f"crop=iw:trunc(ih*{1 - crop:.3f}/2)*2:0:0," if crop > 0 else ""

def brightness(exe, path, crop=0.0):
    """[(time, average brightness 0-255)] for every frame."""
    vf = crop_filter(crop) + "scale=160:-2,signalstats,metadata=print:key=lavfi.signalstats.YAVG"
    try:
        p = subprocess.run([exe, "-hide_banner", "-nostats", "-i", str(path), "-vf", vf,
                            "-an", "-f", "null", "-"], capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return []
    out, t = [], None
    for line in p.stderr.splitlines():
        m = re.search(r"pts_time:([\d.]+)", line)
        if m:
            t = float(m.group(1))
            continue
        m = re.search(r"YAVG=([\d.]+)", line)
        if m and t is not None:
            out.append((t, float(m.group(1))))
    return out

def best_start(samples, dur):
    """Start of the steadiest window: even exposure, no dark flash, and the end
    looks like the start (for the loop). Skips the first 3 s when the clip allows."""
    latest = dur - NEED - 0.05
    lo = max(0.0, min(SKIP_START, latest))
    best, s = None, lo
    while s <= latest + 1e-6:
        win = [y for t, y in samples if s <= t <= s + NEED]
        if len(win) >= 10 and sum(win) > len(win):
            mean = sum(win) / len(win)
            std = (sum((y - mean) ** 2 for y in win) / len(win)) ** 0.5 / mean
            med = sorted(win)[len(win) // 2]
            dip = max(0.0, (med - min(win)) / med - 0.10)
            head = [y for t, y in samples if abs(t - (s + XF)) <= 0.05] or win[:3]
            loop = abs(sum(head) / len(head) - sum(win[-3:]) / 3) / mean
            score = std + 3 * dip + 2 * max(0.0, loop - 0.02)
            if best is None or score < best[0]:
                best = (score, s, std, dip, loop)
        s += 0.25
    if best:
        print(f"  reel: best 7 s starts at {best[1]:.2f}s (exposure spread {best[2]:.1%}, "
              f"dark dip {best[3]:.1%}, start/end diff {best[4]:.1%})")
        return best[1]
    return lo

def build(exe, src, crop, hook_png, sub_png):
    """One background -> out/reel.mp4 (6.8 s loop). Returns the path or None."""
    out = M.OUT / "reel.mp4"
    dur = duration(exe, src)
    if dur < NEED + 0.05:
        print(f"  reel bg: clip is {dur:.1f}s, need {NEED:.1f}s – trying another")
        return None
    start = best_start(brightness(exe, src, crop), dur)
    k = RAMP / WIN
    last = SECS - 1 / FPS                                # time of the final frame
    pre = crop_filter(crop) + ("deshake," if STABILIZE else "")
    fc = (f"[0:v]trim=duration={NEED:.3f},setpts=PTS-STARTPTS,{pre}"
          f"scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos,"
          f"crop={W}:{H},setsar=1,"
          f"setpts='(sqrt(1+2*{k:.6f}*T)-1)/{k:.6f}/TB',fps={FPS},"
          f"trim=duration={WIN},setpts=PTS-STARTPTS,split[a][b];"
          f"[a]trim=start={XF},setpts=PTS-STARTPTS[body];"
          f"[b]trim=duration={XF},setpts=PTS-STARTPTS[head];"
          f"[body][head]xfade=transition=fade:duration={XF}:offset={SECS - XF:.3f},"
          f"colorchannelmixer=rr={FLASH_GAIN}:gg={FLASH_GAIN}:bb={FLASH_GAIN}"
          f":enable='lt(t,{FLASH_END})'[base];"
          f"[1:v]format=rgba,colorchannelmixer=aa={OV_ALPHA},"
          f"fade=t=in:st={OV_ON}:d={OV_FADE}:alpha=1,"
          f"fade=t=out:st={OV_OUT}:d={last - OV_OUT:.3f}:alpha=1[ov];"
          f"[2:v]format=rgba,fade=t=in:st={HOOK_ON}:d={TEXT_FADE}:alpha=1,"
          f"fade=t=out:st={TEXT_OFF - TEXT_FADE}:d={TEXT_FADE}:alpha=1[t1];"
          f"[3:v]format=rgba,fade=t=in:st={SUB_ON}:d={TEXT_FADE}:alpha=1,"
          f"fade=t=out:st={TEXT_OFF - TEXT_FADE}:d={TEXT_FADE}:alpha=1[t2];"
          f"[base][ov]overlay=0:0[v1];[v1][t1]overlay=0:0[v2];"
          f"[v2][t2]overlay=0:0,format=yuv420p[v];")
    if AUDIO == "silent":
        asrc = "anullsrc=r=44100:cl=mono:d=12"
        fc += f"[4:a]atrim=duration={SECS},pan=stereo|c0=c0|c1=c0[a]"
    else:                                                # soft wind, loops like the video
        asrc = "anoisesrc=d=12:c=brown:r=44100:a=0.5"
        fc += (f"[4:a]highpass=f=60,lowpass=f=500,loudnorm=I={WIND_LUFS}:TP=-10:LRA=5,"
               f"aresample=44100,atrim=start=4:duration={WIN},asetpts=PTS-STARTPTS,"
               f"asplit[x][y];[x]atrim=start={XF},asetpts=PTS-STARTPTS[ab];"
               f"[y]atrim=duration={XF},asetpts=PTS-STARTPTS[ah];"
               f"[ab][ah]acrossfade=d={XF}:c1=qsin:c2=qsin,pan=stereo|c0=c0|c1=c0[a]")

    mb = 0.0
    for rate in ("8M", "5M"):                            # 2nd pass only if over 8 MB
        cmd = [exe, "-y", "-hide_banner", "-loglevel", "error",
               "-ss", f"{start:.3f}", "-t", f"{NEED + 0.3:.3f}", "-i", str(src),
               "-f", "lavfi", "-i", f"color=c=black:s={W}x{H}:r={FPS}:d={SECS}",
               "-loop", "1", "-framerate", str(FPS), "-t", str(SECS), "-i", str(hook_png),
               "-loop", "1", "-framerate", str(FPS), "-t", str(SECS), "-i", str(sub_png),
               "-f", "lavfi", "-i", asrc,
               "-filter_complex", fc, "-map", "[v]", "-map", "[a]",
               "-t", str(SECS), "-r", str(FPS),
               "-c:v", "libx264", "-preset", "medium", "-crf", "20",
               "-maxrate", rate, "-bufsize", f"{int(rate[:-1]) * 2}M", "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(out)]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        except subprocess.TimeoutExpired:
            print("!! reel: ffmpeg took over 10 min")
            return None
        if p.returncode != 0 or not out.exists():
            print(f"!! reel: ffmpeg failed: {p.stderr[-800:]}")
            return None
        mb = out.stat().st_size / 1e6
        if mb <= MAX_MB:
            break
        print(f"  reel: {mb:.1f} MB > {MAX_MB:.0f} MB – encoding again smaller")

    s = brightness(exe, out)                             # loop check: first vs last frame
    if len(s) >= 2:
        a, b = s[0][1], s[-1][1]
        diff = abs(a - b) / max(a, b, 1) * 100
        print(f"  reel loop check: first/last frame brightness differ {diff:.1f}% "
              f"{'(OK)' if diff <= 5 else '(!! over 5%)'}")
    subprocess.run([exe, "-y", "-loglevel", "error", "-ss", "3", "-i", str(out),
                    "-frames:v", "1", str(M.OUT / "reel_frame.jpg")], capture_output=True)
    print(f"  reel: {SECS}s, {mb:.1f} MB, overlay {OV_ALPHA:.0%}, audio {AUDIO}")
    return out


# ─── caption + hashtags (all from our own data) ──────────────────────
def yield_text(e):
    return f"{e['roi'] * 100:.0f}% net yield" if M.show_yield(e) else f"${e['monthly']:,}/mo net"

def tag(s):
    return "#" + slug(s)

def hashtags(h, l):
    pref = M.PREF_EN.get(l["pref"], l["pref"])
    town = M.seo_label(h) if h["kind"] == "onsen" else M.place_name(h)
    area = [tag(town), tag(pref)]
    region = tag(REGION[pref]) if pref in REGION else "#visitjapan"
    area.append("#visitjapan" if region in area else region)
    tags = list(dict.fromkeys(BROAD_TAGS + area + INTENT_TAGS.get(h["kind"], DEFAULT_INTENT)))
    for extra in ("#ruraljapan", "#akiyabank"):
        if len(tags) < 8 and extra not in tags:
            tags.append(extra)
    return tags[:8]

def fallback_dream(h, l):
    mins, _ = M.trip_parts(h, l)
    start = {"ski": "Fresh powder, ", "onsen": "Evening steam, ", "beach": "Sea breeze, ",
             "nature": "Forest air, ", "sight": "Quiet old streets, "}.get(h["kind"], "")
    return f"{start}{mins} min from your own front door."

def reel_caption(m, h, l, usd, e, dream=None, credit=""):
    global M
    M = m
    price = "FREE" if l["price_yen"] == 0 else M.fmt_usd(usd)
    pref = M.PREF_EN.get(l["pref"], l["pref"])
    lines = [f"{M.short_name(h['name'])} Akiya ROI: {price} house → {yield_text(e)}",
             dream or fallback_dream(h, l),
             CTA.format(pref=pref)]
    if credit:
        lines.append(f"🎥 {credit}")
    return "\n".join(lines + ["", " ".join(hashtags(h, l))])


# ─── entry point used by main.build_slides() ─────────────────────────
def make(m, h, l, usd, e, facts, rot=None, video=None):
    """Returns (reel path, hook line, caption, 'pexels:ID') or None.
    video = a local mp4 instead of Pexels (only for preview.py)."""
    global M
    M = m
    exe = M.ffmpeg_exe()
    if not exe:
        print("Reel: ffmpeg not found – no reel today")
        return None
    hook = hook_line(h, l, e, usd, facts)
    if not hook:
        print("Reel: no hook line fits – no reel today")
        return None
    sub = sub_line(h, l, e)
    hook_png, sub_png = text_pngs(hook, sub)
    print(f"Reel text: {hook[0]} ({hook[2]}px) / {sub[0] if sub else '-'}")

    if video:
        out = build(exe, Path(video), 0.0, hook_png, sub_png)
        return (out, hook[0], reel_caption(m, h, l, usd, e), None) if out else None

    order = candidates(h, rot or {})
    if not order:
        print(f"Reel: no usable video in {BG_LIST.name} – no reel today (post still goes out)")
        return None
    for entry in order[:TRIES]:
        got = fetch(entry)
        if not got:
            continue
        src, credit = got
        try:
            out = build(exe, src, entry["crop"], hook_png, sub_png)
        finally:
            src.unlink(missing_ok=True)                  # don't keep the big source file
        if out:
            print(f"Reel: pexels {entry['id']} -> {out}")
            return (out, hook[0], reel_caption(m, h, l, usd, e, credit=credit),
                    f"pexels:{entry['id']}")
    print(f"Reel: none of the first {TRIES} videos worked – no reel today (post still goes out)")
    return None
