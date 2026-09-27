"""
Own-video reel. Picks one of YOUR mp4 clips from reels/<kind>/ (or reels/any/)
-> ffmpeg: exactly 7.0 s seamless loop, 70% black + one line of text 0.5-6.5 s,
silent audio. No AI images or AI video. No clip found -> make() returns None.
reel_caption() builds the reel caption.
"""
import json, os, random, re, subprocess
from pathlib import Path

from PIL import Image, ImageDraw

def _env(name, default):
    return os.getenv(name) or default

REEL_DIR  = Path(_env("REEL_DIR", "reels"))                  # your videos live here
USED_FILE = Path(_env("REEL_USED_FILE", "state/reel_used.json"))
NO_REPEAT = int(_env("REEL_NO_REPEAT", "5"))                 # don't reuse the last N clips
CTA       = _env("REEL_CTA", "Full math in yesterday's post — yamayield.com + "
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


# ─── choosing one of your clips ──────────────────────────────────────
def slug(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())

def clips_for(kind):
    for folder in (REEL_DIR / kind, REEL_DIR / "any"):
        if folder.is_dir():
            files = sorted(p for p in folder.iterdir()
                           if p.is_file() and p.suffix.lower() == ".mp4")
            if files:
                return files
    return []

def load_used():
    try:
        return json.loads(USED_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []

def save_used(clip):
    if os.getenv("DRY_RUN") == "1":            # test runs don't count as "used"
        return
    used = [u for u in load_used() if u != str(clip)] + [str(clip)]
    USED_FILE.parent.mkdir(parents=True, exist_ok=True)
    USED_FILE.write_text(json.dumps(used[-50:], ensure_ascii=False, indent=1), encoding="utf-8")

def pick_clip(h):
    files = clips_for(h["kind"])
    if not files:
        return None
    try:
        town = slug(M.place_name(h))
    except Exception:
        town = ""
    named = [p for p in files if town and town in slug(p.stem)]
    if named:                                  # e.g. reels/onsen/kinosaki.mp4
        return random.choice(named)
    used = load_used()
    fresh = [p for p in files if str(p) not in used[-NO_REPEAT:]]
    if fresh:
        return random.choice(fresh)
    return min(files, key=lambda p: used.index(str(p)) if str(p) in used else -1)  # oldest


# ─── ffmpeg ──────────────────────────────────────────────────────────
def duration(exe, path):
    p = subprocess.run([exe, "-i", str(path)], capture_output=True, text=True)
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", p.stderr)
    return int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]) if m else 0.0

def overlay_png(fit, path):
    """70% black full screen + the one centred white line (drawn with main.py's put())."""
    s, f, size, track = fit
    img = Image.new("RGBA", (M.REEL_W, M.REEL_H), (0, 0, 0, int(255 * M.REEL_OVERLAY)))
    M.put(ImageDraw.Draw(img), (M.REEL_W / 2, M.REEL_H / 2), s, f,
          (255, 255, 255, 255), "mm", track, 0)
    img.save(path)
    return path

def compose(exe, raw, overlay, out):
    """7.0 s: clip 0.5-7.5 s, last 0.5 s blends into the opening (seamless loop).
    Short clips repeat to fill. No zoom or pan: scaled to fill 9:16 and centre-cropped.
    Overlay + text fade in at 0.5 s, out by 6.5 s. Silent audio."""
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
    p = subprocess.run([exe, "-y", "-loglevel", "error",
                        "-stream_loop", "-1", "-i", str(raw),
                        "-loop", "1", "-framerate", str(FPS), "-t", str(S), "-i", str(overlay),
                        "-f", "lavfi", "-t", str(S), "-i", "anullsrc=r=44100:cl=stereo",
                        "-filter_complex", fc, "-map", "[v]", "-map", "2:a",
                        "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-r", str(FPS),
                        "-c:a", "aac", "-b:a", "128k", "-t", str(S),
                        "-movflags", "+faststart", str(out)],
                       capture_output=True, text=True)
    if p.returncode != 0 or not out.exists():
        print(f"!! Own reel: ffmpeg failed: {p.stderr[:500]}")
        return None
    return out


# ─── caption + hashtags (all from our own data) ──────────────────────
def yield_text(e):
    return f"{e['roi'] * 100:.0f}% net yield" if M.show_yield(e) else f"${e['monthly']:,}/mo net"

def tag(s):
    return "#" + slug(s)

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
    start = {"ski": "Fresh powder, ", "onsen": "Evening steam, ", "beach": "Sea breeze, ",
             "nature": "Forest air, ", "sight": "Quiet old streets, "}.get(h["kind"], "")
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
    exe = M.ffmpeg_exe()
    if not exe:
        print("Own reel: ffmpeg not found")
        return None
    clip = pick_clip(h)
    if not clip:
        print(f"Own reel: no .mp4 in {REEL_DIR / h['kind']} or {REEL_DIR / 'any'} – no reel")
        return None
    secs = duration(exe, clip)
    if secs < 1:
        print(f"Own reel: can't read {clip}")
        return None
    if secs < M.REEL_SECS + 0.5:
        print(f"  note: {clip.name} is only {secs:.1f}s, it will repeat to fill {M.REEL_SECS}s")
    fit = M.reel_line(ImageDraw.Draw(Image.new("L", (M.REEL_W, M.REEL_H))), h, facts, e, usd, l)
    if not fit:
        print("Own reel: no on-screen line fits")
        return None
    out = compose(exe, clip, overlay_png(fit, M.OUT / "reel_overlay.png"), M.OUT / "reel.mp4")
    if not out:
        return None
    save_used(clip)
    print(f"Own reel: {clip} -> {out} | {fit[0]}")
    return out, fit[0], reel_caption(m, h, l, usd, e)
