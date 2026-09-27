"""
Reel builder for Yama Yield: 1 background video = 1 reel.

Background: ONE Pexels video from bg_videos.txt, downloaded with the Pexels API
(PEXELS_KEY). Nothing is read from a reels/ folder.

bg_videos.txt, one video per line:
    https://www.pexels.com/video/snowy-ski-slope-1234567/
    1234567                        <- the ID alone also works
    1234567  ski                   <- only for ski houses (ski onsen sight beach nature)
    1234567  onsen town=kinosaki   <- preferred for Kinosaki houses
    1234567  crop=0                <- don't cut the bottom (default cuts 10%)
    # lines starting with # are ignored

Reel: 1080x1920, 30 fps, 6.8 s seamless loop, H.264, < 8 MB, no sound.
  0.0-0.3 s  video only (no overlay, no text)
  0.3-0.5 s  black overlay 0% -> 55%
  0.3 s      line 1 (hook) pops: scale 0.9 -> 1.05 -> 1.0, opacity 0 -> 100% in 0.15 s
  1.5 s      line 2 rises 20 px, opacity 0 -> 85% in 0.2 s
  6.5-6.8 s  overlay + both lines fade to 0%, so the last frame matches the first
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
TRIES      = 3                          # videos to try before giving up for today

W, H, FPS  = 1080, 1920, 30
SECS       = 6.8
FRAMES     = round(SECS * FPS)          # 204 frames
LAST       = (FRAMES - 1) / FPS         # time of the final frame
XF         = 0.2                        # loop crossfade (end blends into the start)
WIN        = SECS + XF                  # 7.0 s of footage used
SKIP_START = 3.0                        # avoid the first 3 s of the source if possible
CROP_DEFAULT = 0.10                     # cut the bottom 10% (nets / branding)
STABILIZE  = _env("REEL_STABILIZE", "0") == "1"   # 1 = ffmpeg deshake (shaky clips only)

OV_ALPHA   = min(0.9, max(0.0, float(_env("REEL_OVERLAY", "0.55"))))
OV_ON, OV_IN = 0.3, 0.2                 # overlay fades in 0.3 -> 0.5 s
FADE_OUT   = 6.5                        # overlay + text fade out 6.5 s -> last frame

HOOK_ON, HOOK_POP = 0.3, 0.15
HOOK_PX, HOOK_MIN_PX, HOOK_WORDS = 190, 90, 3
SUB_ON, SUB_POP = 1.5, 0.2
SUB_PX, SUB_ALPHA, SUB_GAP, SUB_RISE, SUB_WORDS = 46, 0.85, 60, 20, 8
MARGIN     = 80
SAFE_TOP, SAFE_BOTTOM = int(H * 0.30), int(H * 0.70)
PAD        = 6
MAX_MB     = 8.0
AUDIO      = _env("REEL_AUDIO", "silent").lower()   # silent (no sound) | none (no track)
KIND_NAMES = {"ski", "onsen", "sight", "beach", "nature"}
STOP       = {"lake", "mt", "old", "town", "the", "castle", "shrine", "grand", "valley",
              "beach", "bay", "coast", "falls", "river", "gorge", "kogen", "onsen",
              "village", "thatched", "taisha", "hongu", "yumoto", "and"}
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
        e = {"id": ids[-1], "kinds": set(), "town": "", "crop": None}
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
    """Best first: named after this town > tagged with this kind > untagged.
    Videos tagged for another kind are never used. Least recently used first."""
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
    if (v.get("duration") or 99) < WIN:
        print(f"  reel bg: video {vid} is only {v.get('duration')}s, need {WIN:.0f}s")
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


# ─── the 2 text lines ────────────────────────────────────────────────
def main_word(name):
    """'Lake Kawaguchiko' -> 'Kawaguchiko', 'Takayama old town' -> 'Takayama'"""
    words = name.split()
    keep = [w for w in words if w.lower() not in STOP]
    return (keep or words)[0]

def hook_line(h, l, usd):
    """Line 1: '$PRICE LOCATION', max 3 words, bold, 190 px, shrinks to fit.
    Width leaves room for the 1.05 pop, so it never crosses the 80 px margins."""
    price = "FREE" if l["price_yen"] == 0 else M.fmt_usd(usd)
    town = M.short_name(h["name"]).upper()
    if len(town.split()) > HOOK_WORDS - 1:
        town = main_word(town)
    options = [f"{price} {town}"]
    if " " in town:
        options.append(f"{price} {main_word(town)}")    # shorter = bigger text
    maxw = (W - 2 * MARGIN) / 1.05
    for i, s in enumerate(options):
        floor = HOOK_MIN_PX if i < len(options) - 1 else 60
        size = HOOK_PX
        while size >= floor:
            f = M.rfont(size)
            if f.getlength(s) <= maxw:
                return s, f, size
            size -= 2
    return None

def sub_font(px):
    for p in SUB_FONTS:
        try:
            return ImageFont.truetype(str(p), px)
        except OSError:
            pass
    return M.cfont("sans", px, 400)

# Edges found in the listing's own text. (reel text, kinds allowed or None = any, words to find)
# Order = priority. Add words here if your listings describe things differently.
EDGE_WORDS = [
    ("ski-in/out",    {"ski"}, ["ski-in", "ski in/out", "スキーイン"]),
    ("private onsen", None,    ["private onsen", "onsen bath", "温泉付き", "温泉引込", "温泉引き込み"]),
    ("onsen source",  None,    ["onsen source", "自家源泉", "源泉かけ流し"]),
    ("Fuji view",     None,    ["fuji view", "view of mt. fuji", "富士山が見え", "富士山眺望", "富士山ビュー"]),
]
CLOSE_MIN = 3                       # only show distance if it's this many minutes or less

def listing_text(l):
    """All text in the listing, lowercased, to search for features."""
    out = []
    def walk(v):
        if isinstance(v, str):
            out.append(v)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, (list, tuple)):
            for x in v:
                walk(x)
    walk(l)
    return " ".join(out).lower()

def num(l, *keys):
    """First positive number found under any of these keys, else None."""
    for k in keys:
        try:
            v = float(str(l.get(k)).replace(",", "").replace("m²", "").replace("㎡", "").strip())
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
    return None

def size_options(l):
    """Most specific first: '127m² / 4BR', '4BR / 2 bath', '4BR', '127m²'."""
    beds = l.get("bedrooms")
    br = f"{beds}BR" if beds else None
    baths = num(l, "bathrooms", "baths", "bath")
    m2 = num(l, "floor_m2", "building_m2", "floor_area", "building_area")   # building, NOT land
    opts = []
    if br and m2:
        opts.append(f"{m2:.0f}m² / {br}")
    if br and baths:
        opts.append(f"{br} / {baths:g} bath")
    if br:
        opts.append(br)
    if m2:
        opts.append(f"{m2:.0f}m²")
    return opts or [None]

def edge(h, l):
    """A real feature from the listing first, then distance if it's 3 min or less, else None."""
    kind = h["kind"]
    text = listing_text(l)
    for label, kinds, words in EDGE_WORDS:
        if (kinds is None or kind in kinds) and any(w in text for w in words):
            return label
    mins, mode = M.trip_parts(h, l)
    try:
        m = float(mins)
    except (TypeError, ValueError):
        return None
    if m > CLOSE_MIN:
        return None                                     # 20 min onsen -> don't say it
    to = {"ski": "lift", "onsen": "onsen", "beach": "beach"}.get(
        kind, main_word(M.short_name(h["name"])))
    return f"steps to {to}" if m <= 1 else f"{mins} min {to}"

def sub_line(h, l, e):
    """Line 2: SIZE • EDGE • MONEY, e.g. '4BR • private onsen • 11% net'."""
    money = f"{e['roi'] * 100:.0f}% net" if M.show_yield(e) else f"${e['monthly']:,}/mo net"
    ed = edge(h, l)
    sizes = size_options(l)
    print(f"  reel line 2: size {sizes}, edge {ed or 'none (not within 3 min, no feature in listing)'}")
    f = sub_font(SUB_PX)
    tries = ([[s, ed, money] for s in sizes]           # full line, shorter size if too wide
             + ([[ed, money]] if ed else [])           # keep the edge over the size
             + [[s, money] for s in sizes] + [[money]])
    for parts in tries:
        parts = [p for p in parts if p]
        s = " • ".join(parts)
        words = sum(len([w for w in p.split() if w != "/"]) for p in parts)
        if words <= SUB_WORDS and f.getlength(s) <= W - 2 * MARGIN:
            return s, f
    return None

def sprite(s, font):
    """White text on a transparent image, trimmed to the text."""
    d = ImageDraw.Draw(Image.new("L", (8, 8)))
    x0, y0, x1, y1 = d.textbbox((0, 0), s, font=font, anchor="ls")
    img = Image.new("RGBA", (x1 - x0 + 2 * PAD, y1 - y0 + 2 * PAD), (255, 255, 255, 0))
    ImageDraw.Draw(img).text((PAD - x0, PAD - y0), s, font=font,
                             fill=(255, 255, 255, 255), anchor="ls")
    return img

def layout(hook, sub):
    """Both lines centred as one block, line 2 60 px below line 1."""
    hs = sprite(hook[0], hook[1])
    ih = hs.height - 2 * PAD
    ss = sprite(sub[0], sub[1]) if sub else None
    ish = ss.height - 2 * PAD if ss else 0
    gap = SUB_GAP if ss else 0
    top = (H - (ih + gap + ish)) // 2
    L = {"hook": hs, "hook_cy": top + ih / 2, "sub": ss, "sub_y": top + ih + gap - PAD}
    hi = top + ih / 2 - ih * 1.05 / 2                   # top of the hook at its biggest
    lo = top + ih + gap + ish + SUB_RISE                # bottom of line 2 at its lowest
    area = (hs.width * hs.height + (ss.width * ss.height if ss else 0)) / (W * H)
    ok = hi >= SAFE_TOP and lo <= SAFE_BOTTOM
    print(f"  reel text: y {hi:.0f}-{lo:.0f} px (safe {SAFE_TOP}-{SAFE_BOTTOM}) "
          f"{'OK' if ok else '!! outside safe zone'} | text area {area:.1%} of frame")
    return L


# ─── overlay frames (black layer + text), drawn in Python ────────────
def ramp(t, start, dur):
    return min(1.0, max(0.0, (t - start) / dur))

def state(t):
    out = 1.0 - ramp(t, FADE_OUT, LAST - FADE_OUT)      # reaches 0 on the final frame
    ov = OV_ALPHA * ramp(t, OV_ON, OV_IN) * out
    u = ramp(t, HOOK_ON, HOOK_POP)
    sc = 0.9 + 0.15 * u / 0.6 if u < 0.6 else 1.05 - 0.05 * (u - 0.6) / 0.4
    v = ramp(t, SUB_ON, SUB_POP)
    dy = SUB_RISE * (1 - v) ** 3                        # eases into place
    return (round(ov, 3), round(u * out, 3), round(sc, 3),
            round(SUB_ALPHA * v * out, 3), round(dy, 1))

def faded(sp, op):
    if op >= 0.999:
        return sp
    sp = sp.copy()
    sp.putalpha(sp.getchannel("A").point(lambda a: round(a * op)))
    return sp

def draw(st, L):
    ov, hop, sc, sop, dy = st
    img = Image.new("RGBA", (W, H), (0, 0, 0, round(255 * ov)))
    if hop > 0:
        sp = L["hook"]
        if abs(sc - 1) > 1e-3:
            sp = sp.resize((max(1, round(sp.width * sc)), max(1, round(sp.height * sc))),
                           Image.LANCZOS)
        sp = faded(sp, hop)
        img.alpha_composite(sp, (round(W / 2 - sp.width / 2), round(L["hook_cy"] - sp.height / 2)))
    if L["sub"] is not None and sop > 0:
        sp = faded(L["sub"], sop)
        img.alpha_composite(sp, (round(W / 2 - sp.width / 2), round(L["sub_y"] + dy)))
    return img.tobytes()

def overlay_frames(L):
    prev = buf = None
    for n in range(FRAMES):
        st = state(n / FPS)
        if st != prev:
            buf, prev = draw(st, L), st
        yield buf


# ─── ffmpeg ──────────────────────────────────────────────────────────
def duration(exe, path):
    p = subprocess.run([exe, "-i", str(path)], capture_output=True, text=True)
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", p.stderr)
    return int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]) if m else 0.0

def crop_filter(crop):
    return f"crop=iw:trunc(ih*{1 - crop:.3f}/2)*2:0:0," if crop > 0 else ""

def analyse(exe, path, crop=0.0):
    """[(time, brightness 0-255, motion)] for every frame."""
    vf = (crop_filter(crop) + "scale=160:-2,signalstats,"
          "metadata=print:key=lavfi.signalstats.YAVG,"
          "metadata=print:key=lavfi.signalstats.YDIF")
    try:
        p = subprocess.run([exe, "-hide_banner", "-nostats", "-i", str(path), "-vf", vf,
                            "-an", "-f", "null", "-"], capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return []
    rows, t = {}, None
    for line in p.stderr.splitlines():
        m = re.search(r"pts_time:([\d.]+)", line)
        if m:
            t = float(m.group(1))
            continue
        m = re.search(r"signalstats\.(YAVG|YDIF)=([\d.]+)", line)
        if m and t is not None:
            rows.setdefault(t, {})[m.group(1)] = float(m.group(2))
    return sorted((t, v.get("YAVG", 0.0), v.get("YDIF", 0.0)) for t, v in rows.items())

def best_start(samples, dur):
    """Start of the best 7 s: steady exposure, no dark flicker, continuous motion,
    and the end looks like the start (loop). Skips the first 3 s when possible."""
    latest = dur - WIN - 0.05
    lo = max(0.0, min(SKIP_START, latest))
    difs = sorted(d for _, _, d in samples if d > 0)
    typical = difs[len(difs) // 2] if difs else 0.0
    best, s = None, lo
    while s <= latest + 1e-6:
        win = [(y, d) for t, y, d in samples if s <= t <= s + WIN]
        ys = [y for y, _ in win]
        if len(ys) >= 10 and sum(ys) > len(ys):
            mean = sum(ys) / len(ys)
            std = (sum((y - mean) ** 2 for y in ys) / len(ys)) ** 0.5 / mean
            med = sorted(ys)[len(ys) // 2]
            dip = max(0.0, (med - min(ys)) / med - 0.10)
            head = [y for t, y, _ in samples if abs(t - (s + XF)) <= 0.05] or ys[:3]
            loop = abs(sum(head) / len(head) - sum(ys[-3:]) / 3) / mean
            still = (sum(1 for _, d in win[1:] if d < 0.25 * typical) / len(win)) if typical else 0
            score = std + 3 * dip + 2 * max(0.0, loop - 0.02) + 1.5 * still
            if best is None or score < best[0]:
                best = (score, s, std, dip, loop, still)
        s += 0.25
    if best:
        print(f"  reel: best 7 s starts at {best[1]:.2f}s (exposure spread {best[2]:.1%}, "
              f"dark dip {best[3]:.1%}, start/end diff {best[4]:.1%}, frozen {best[5]:.0%})")
        return best[1]
    return lo

def encode(exe, src, start, fc, rate, L, out):
    cmd = [exe, "-y", "-hide_banner", "-loglevel", "error",
           "-ss", f"{start:.3f}", "-t", f"{WIN + 0.3:.3f}", "-i", str(src),
           "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{W}x{H}",
           "-framerate", str(FPS), "-i", "-"]
    if AUDIO != "none":
        cmd += ["-f", "lavfi", "-t", str(SECS), "-i", "anullsrc=r=44100:cl=stereo"]
    cmd += ["-filter_complex", fc, "-map", "[v]"]
    cmd += ["-map", "2:a", "-c:a", "aac", "-b:a", "32k"] if AUDIO != "none" else ["-an"]
    cmd += ["-t", str(SECS), "-r", str(FPS),
            "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-maxrate", rate, "-bufsize", f"{int(rate[:-1]) * 2}M", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", str(out)]
    log = M.OUT / "reel_ffmpeg.log"
    with open(log, "wb") as err:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=err)
        try:
            for frame in overlay_frames(L):
                proc.stdin.write(frame)
        except BrokenPipeError:
            pass
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass
        try:
            proc.wait(timeout=600)
        except subprocess.TimeoutExpired:
            proc.kill()
            print("!! reel: ffmpeg took over 10 min")
            return False
    if proc.returncode != 0 or not out.exists():
        print(f"!! reel: ffmpeg failed: {log.read_text(errors='replace')[-800:]}")
        return False
    return True

def build(exe, src, crop, L):
    """One background -> out/reel.mp4 (6.8 s loop). Returns the path or None."""
    out = M.OUT / "reel.mp4"
    dur = duration(exe, src)
    if dur < WIN + 0.05:
        print(f"  reel bg: clip is {dur:.1f}s, need {WIN:.1f}s – trying another")
        return None
    start = best_start(analyse(exe, src, crop), dur)
    pre = crop_filter(crop) + ("deshake," if STABILIZE else "")
    fc = (f"[0:v]setpts=PTS-STARTPTS,fps={FPS},trim=duration={WIN},setpts=PTS-STARTPTS,{pre}"
          f"scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos,"
          f"crop={W}:{H},setsar=1,format=yuv420p,split[a][b];"
          f"[a]trim=start={XF},setpts=PTS-STARTPTS,fps={FPS}[body];"
          f"[b]trim=duration={XF},setpts=PTS-STARTPTS,fps={FPS}[head];"
          f"[body][head]xfade=transition=fade:duration={XF}:offset={SECS - XF:.3f}[xf];"
          f"[xf]setpts=N/({FPS}*TB)[base];"
          f"[1:v]format=rgba,setpts=N/({FPS}*TB)[ov];"
          f"[base][ov]overlay=0:0:shortest=1,format=yuv420p[v]")
    mb = 0.0
    for rate in ("8M", "5M"):                           # 2nd pass only if over 8 MB
        if not encode(exe, src, start, fc, rate, L, out):
            return None
        mb = out.stat().st_size / 1e6
        if mb <= MAX_MB:
            break
        print(f"  reel: {mb:.1f} MB > {MAX_MB:.0f} MB – encoding again smaller")

    s = analyse(exe, out)                               # loop check: first vs last frame
    if s:
        def y(t):
            return min(s, key=lambda r: abs(r[0] - t))[1]
        print(f"  reel timing: brightness 0.0s={y(0):.0f} 0.2s={y(0.2):.0f} "
              f"0.6s={y(0.6):.0f} 1.5s={y(1.5):.0f} 6.3s={y(6.3):.0f} end={s[-1][1]:.0f} "
              f"| first frame at {s[0][0]:.2f}s, length {s[-1][0] - s[0][0] + 1 / FPS:.2f}s")
    if len(s) >= 2:
        a, b = s[0][1], s[-1][1]
        diff = abs(a - b) / max(a, b, 1) * 100
        print(f"  reel loop check: first/last frame brightness differ {diff:.1f}% "
              f"{'(OK)' if diff <= 5 else '(!! over 5%)'}")
    subprocess.run([exe, "-y", "-loglevel", "error", "-ss", "3", "-i", str(out),
                    "-frames:v", "1", str(M.OUT / "reel_frame.jpg")], capture_output=True)
    print(f"  reel: {SECS}s, {len(s) or FRAMES} frames, {mb:.1f} MB, overlay {OV_ALPHA:.0%}, "
          f"crop {crop:.0%}, {'no sound' if AUDIO != 'none' else 'no audio track'}")
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
def make(m, h, l, usd, e, facts=None, rot=None, video=None):
    """Returns (reel path, hook line, caption, 'pexels:ID') or None.
    video = a local mp4 instead of Pexels (only for preview.py)."""
    global M
    M = m
    exe = M.ffmpeg_exe()
    if not exe:
        print("Reel: ffmpeg not found – no reel today")
        return None
    hook = hook_line(h, l, usd)
    if not hook:
        print("Reel: hook line doesn't fit – no reel today")
        return None
    sub = sub_line(h, l, e)
    print(f"Reel text: {hook[0]} ({hook[2]}px) / {sub[0] if sub else '-'} ({SUB_PX}px)")
    L = layout(hook, sub)

    if video:
        out = build(exe, Path(video), CROP_DEFAULT, L)
        return (out, hook[0], reel_caption(m, h, l, usd, e), None) if out else None

    order = candidates(h, rot or {})
    if not order:
        print(f"Reel: no usable video in {BG_LIST.name} – no reel today (post still goes out)")
        return None
    if not PEXELS_KEY:
        print("Reel: PEXELS_KEY missing – no reel today (post still goes out)")
        return None
    tries = 0
    for entry in order:                                 # go through all videos...
        if tries >= TRIES:                              # ...but build at most 3
            break
        got = fetch(entry)
        if not got:                                     # deleted / too short / not vertical:
            continue                                    # skip it, doesn't count as a try
        tries += 1
        src, credit = got
        crop = CROP_DEFAULT if entry["crop"] is None else entry["crop"]
        try:
            out = build(exe, src, crop, L)
        finally:
            src.unlink(missing_ok=True)                 # don't keep the big source file
        if out:
            print(f"Reel: pexels {entry['id']} -> {out}")
            return (out, hook[0], reel_caption(m, h, l, usd, e, credit=credit),
                    f"pexels:{entry['id']}")
    print(f"Reel: {tries} video(s) built and failed, {len(order)} in list – "
          f"no reel today (post still goes out)")
    return None
