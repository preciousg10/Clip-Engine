"""CUT + DELIVER stage.

Per selected clip: extract the moment, tighten dead air, render to vertical
1080x1920, burn the flzsh caption at top (safe-zone aware), overlay the mandatory
watermark, and write drafts/NN_score_slug.mp4 (best first) + drafts/manifest.json.

Captions are rendered to a transparent PNG with Pillow (bold white, black outline,
top-center, <=2 lines, auto font-size) and overlaid by ffmpeg — this dodges ffmpeg
drawtext font/escaping issues and is identical across OSes. Watermark is the campaign
PNG from campaign/assets/. Fail loud if anything essential is missing.
"""
import os
import re
import subprocess
import sys

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C

W, H = 1080, 1920
CAPTION_BOX_W = 1000
CAPTION_TOP_Y = 175           # below the top 8% (~154px) TikTok UI safe zone
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}

# --- cold-open restructure (the biggest hook lever) ----------------------------
COLD_OPEN_DUR = 2.0           # target length of the peak teaser (spec: 1.5–2.5s)
COLD_OPEN_MIN = 1.5           # never shorter than this or it reads as a glitch
COLD_OPEN_MIN_SETUP = 3.0     # peak must sit >= this many s past the natural start,
COLD_OPEN_MIN_PAYOFF = 2.0    # and leave >= this much clip after it, else play in order
MOTION_RADIUS = 1.0           # scan ±1s around the peak for the highest-motion frame
FADE_FRAMES = 2               # 2-frame fade on the cold-open->setup cut (reads intentional)
ASSUMED_FPS = 30.0            # fade duration basis when the true fps is unknown
LOUDNORM = "loudnorm=I=-14:TP=-1.5:LRA=11"   # per-clip audio normalization (social target)


# --- fonts / caption rendering -------------------------------------------------
def find_bold_font():
    env = os.environ.get("CLIPPER_FONT")
    cands = [env] if env else []
    cands += [
        r"C:\Windows\Fonts\arialbd.ttf", r"C:\Windows\Fonts\ariblk.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/Library/Fonts/Arial Bold.ttf",
    ]
    for c in cands:
        if c and os.path.exists(c):
            return c
    C.fail("no bold TTF font found for captions. Set CLIPPER_FONT=/path/to/bold.ttf "
           "(Windows has arialbd.ttf; Linux: install fonts-dejavu).")


# Emoji code-point ranges (incl. variation selectors / ZWJ / skin tones).
EMOJI_RE = re.compile(
    "([\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF"
    "\U00002B00-\U00002BFF\U00002190-\U000021FF️‍\U0001F3FB-\U0001F3FF]+)")


def find_emoji_font():
    env = os.environ.get("CLIPPER_EMOJI_FONT")
    cands = [env] if env else []
    cands += [
        r"C:\Windows\Fonts\seguiemj.ttf",                       # Segoe UI Emoji (COLR, scalable)
        "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
        "/System/Library/Fonts/Apple Color Emoji.ttc",
    ]
    for c in cands:
        if not c or not os.path.exists(c):
            continue
        try:                       # bitmap emoji fonts often can't load at our sizes
            ImageFont.truetype(c, 64)
            return c
        except Exception:
            continue
    return None


def strip_emoji(text):
    return re.sub(r"\s{2,}", " ", EMOJI_RE.sub("", text)).strip()


def _segment(text):
    """[(substr, is_emoji), ...] preserving order."""
    parts = EMOJI_RE.split(text)
    return [(p, i % 2 == 1) for i, p in enumerate(parts) if p]


def _cells(text, tf, ef, scratch):
    """Layout cells: words/spaces (text font) and emoji runs (emoji font)."""
    cells = []
    for seg, is_emoji in _segment(text):
        if is_emoji:
            f = ef or tf
            w = scratch.textlength(seg, font=f, embedded_color=bool(ef))
            cells.append({"s": seg, "font": f, "w": w, "emoji": True, "space": False})
        else:
            for tok in re.split(r"(\s+)", seg):
                if not tok:
                    continue
                space = tok.isspace()
                s = " " if space else tok
                cells.append({"s": s, "font": tf, "w": scratch.textlength(s, font=tf),
                              "emoji": False, "space": space})
    return cells


def _wrap_cells(cells, inner, max_lines):
    lines, cur, curw = [], [], 0.0
    for cell in cells:
        if cell["space"] and not cur:
            continue                          # no leading spaces
        if curw + cell["w"] > inner and cur:
            while cur and cur[-1]["space"]:
                curw -= cur[-1]["w"]; cur.pop()
            lines.append(cur)
            cur, curw = [], 0.0
            if cell["space"]:
                continue
        cur.append(cell); curw += cell["w"]
    if cur:
        while cur and cur[-1]["space"]:
            cur.pop()
        lines.append(cur)
    return lines[:max_lines] if lines else [[]]


def render_caption_png(text, out_path, box_w=CAPTION_BOX_W, max_lines=2, stroke=6):
    """Bold white + black outline caption with real emoji glyphs. If no emoji font is
    available, emoji are STRIPPED (never render tofu boxes)."""
    text_font_path = find_bold_font()
    emoji_font_path = find_emoji_font()
    if emoji_font_path is None:
        text = strip_emoji(text)
    text = (text or "clip").strip()

    scratch = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    inner = box_w - 2 * stroke - 20
    size, lines, tf, ef = 84, [[]], None, None
    while size >= 30:
        tf = ImageFont.truetype(text_font_path, size)
        ef = ImageFont.truetype(emoji_font_path, size) if emoji_font_path else None
        lines = _wrap_cells(_cells(text, tf, ef, scratch), inner, max_lines)
        # accept if it fit within max_lines without truncation
        if len(_wrap_cells(_cells(text, tf, ef, scratch), inner, max_lines + 1)) <= max_lines:
            break
        size -= 4

    ascent, descent = tf.getmetrics()
    line_h = ascent + descent + 8
    height = line_h * len(lines) + 2 * stroke + 10
    img = Image.new("RGBA", (box_w, height), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    y = stroke + 5
    for line in lines:
        total = sum(c["w"] for c in line)
        x = (box_w - total) / 2
        for c in line:
            if c["emoji"] and ef is not None:
                try:
                    d.text((x, y), c["s"], font=c["font"], embedded_color=True)
                except Exception:
                    pass
            else:
                d.text((x, y), c["s"], font=c["font"], fill="white",
                       stroke_width=stroke, stroke_fill="black")
            x += c["w"]
        y += line_h
    img.save(out_path)
    return out_path


# --- assets --------------------------------------------------------------------
# Reference images that are NOT overlays: placement examples + safe-zone guides.
REFERENCE_HINTS = ("example", "placement", "safezone", "safe zone", "safe-zone",
                   "safezones", "reference", "guide")


def find_watermark(cfg=None):
    """Pick the watermark PNG. Honors cfg['watermark_file'] (exact or substring match);
    otherwise ignores reference/safezone guides and defaults to the first real
    watermark image (watermark-named PNGs first)."""
    cfg = cfg or {}
    imgs = [p for p in C.ASSETS.glob("*") if p.suffix.lower() in IMAGE_EXTS]
    if not imgs:
        C.fail("no watermark image in campaign/assets/ — a watermark is mandatory on "
               "every clip for this campaign.")

    want = cfg.get("watermark_file")
    if want:
        for p in imgs:
            if p.name.lower() == want.lower() or want.lower() in p.name.lower():
                return p
        C.fail(f"configured watermark_file '{want}' not found in campaign/assets/. "
               f"Available: {[p.name for p in imgs]}")

    cands = [p for p in imgs if not any(h in p.name.lower() for h in REFERENCE_HINTS)]
    if not cands:
        C.fail("campaign/assets/ has only reference images (examples/safezones) — no "
               "actual watermark PNG. Add one or set config watermark_file.")
    # watermark-named PNGs first, then other candidates, alphabetical within each.
    cands.sort(key=lambda p: (
        "watermark" not in p.name.lower(), p.suffix.lower() != ".png", p.name.lower()))
    return cands[0]


# --- bounds + dead air ---------------------------------------------------------
def clip_bounds(m, duration, cmin, cmax, pre=20.0, post=15.0):
    """Expand the moment to a complete beat: back to its setup (up to `pre`) and
    forward to its resolution (up to `post`), then clamp to [cmin, cmax] and the
    source duration."""
    s = float(m["start"]) - pre
    e = float(m["end"]) + post
    if e - s > cmax:                 # too long: keep setup, cap length
        e = s + cmax
    if e - s < cmin:                 # too short: pad symmetrically
        pad = (cmin - (e - s)) / 2
        s, e = s - pad, e + pad
    s = max(0.0, s)
    e = min(duration, e)
    if e - s < 1.0:
        e = min(duration, s + max(cmin, 3))
    return round(s, 2), round(e, 2)


def peak_motion_time(source, peak, start, end, radius=MOTION_RADIUS):
    """Scan ±radius seconds around the audio peak and return the absolute source time
    of the highest-motion frame (max ffmpeg scene score) — so the cold-open opens on
    visible chaos, never a static wide shot. Best-effort: falls back to `peak`."""
    lo = max(start, peak - radius)
    hi = min(end, peak + radius)
    if hi - lo < 0.2:
        return peak
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-ss", f"{lo}", "-t", f"{round(hi - lo, 3)}",
           "-i", str(source), "-an", "-vf", "select='gt(scene,0)',metadata=print",
           "-f", "null", "-"]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    except Exception:
        return peak
    best_t, best_score, cur_t = None, -1.0, None
    for line in (proc.stderr or "").splitlines():
        mt = re.search(r"pts_time:([\d.]+)", line)
        if mt:
            cur_t = float(mt.group(1))
            continue
        ms = re.search(r"scene_score=([\d.]+)", line)
        if ms and cur_t is not None and float(ms.group(1)) > best_score:
            best_score, best_t = float(ms.group(1)), cur_t
    if best_t is None:
        return peak                       # no scene change detected in the window
    return round(lo + best_t, 2)


def cold_open_window(source, peak, start, end):
    """Clip-relative (a, b) for the cold-open teaser opened on the peak's highest-motion
    frame, or None if a clean >=COLD_OPEN_MIN window can't be carved out."""
    open_a = peak_motion_time(source, peak, start, end)
    open_a = max(start, min(open_a, end - COLD_OPEN_MIN))
    open_b = min(end, open_a + COLD_OPEN_DUR)
    if open_b - open_a < COLD_OPEN_MIN:
        open_a = max(start, open_b - COLD_OPEN_DUR)
    if open_b - open_a < COLD_OPEN_MIN:
        return None
    return (round(open_a - start, 2), round(open_b - start, 2))


def dead_air_keeps(source, start, end, min_sil=1.5, noise="-30dB"):
    """Keep-intervals (relative to clip start) after removing internal silences
    longer than min_sil. Returns [(a,b), ...]; a single interval means no trim."""
    length = end - start
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-ss", f"{start}", "-t", f"{round(length, 3)}",
           "-i", str(source), "-af", f"silencedetect=noise={noise}:d={min_sil}",
           "-f", "null", "-"]
    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    sils, cur = [], None
    for line in (proc.stderr or "").splitlines():
        a = re.search(r"silence_start:\s*([\d.]+)", line)
        b = re.search(r"silence_end:\s*([\d.]+)", line)
        if a:
            cur = float(a.group(1))
        elif b and cur is not None:
            sils.append((max(0.0, cur), min(length, float(b.group(1)))))
            cur = None
    keeps, pos = [], 0.0
    for a, b in sils:
        if a - pos > 0.1:
            keeps.append((round(pos, 2), round(a, 2)))
        pos = max(pos, b)
    if length - pos > 0.1:
        keeps.append((round(pos, 2), round(length, 2)))
    return keeps or [(0.0, round(length, 2))]


# --- compose -------------------------------------------------------------------
def has_audio_stream(path):
    """True if the source has at least one audio stream."""
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return bool((proc.stdout or "").strip())


def build_compose_cmd(source, start, end, segments, cold_open, caption_png, watermark_png,
                      out_path, cfg, has_audio):
    """`segments` are clip-relative (a, b) spans played in order. When cold_open is set,
    segment 0 is the peak teaser (opened on the payoff) and segment 1 is the setup —
    a 2-frame fade straddles that cut so it reads as intentional. Remaining segments are
    the dead-air-trimmed body."""
    wm_scale = cfg.get("watermark_scale", 0.18)
    wm_margin = cfg.get("watermark_margin", 40)
    wm_w = int(W * wm_scale)
    trimming = cold_open or len(segments) > 1
    fd = FADE_FRAMES / ASSUMED_FPS

    fc = []
    audio_label = None                        # filter label OR raw stream feeding loudnorm
    if trimming:                              # cold-open and/or dead-air via trim+concat
        for i, (a, b) in enumerate(segments):
            vf = f"[0:v]trim={a}:{b},setpts=PTS-STARTPTS"
            if cold_open and i == 0:          # fade OUT the end of the teaser
                vf += f",fade=t=out:st={max(0.0, (b - a) - fd):.3f}:d={fd:.3f}"
            elif cold_open and i == 1:        # fade IN the start of the setup
                vf += f",fade=t=in:st=0:d={fd:.3f}"
            fc.append(vf + f"[v{i}];")
            if has_audio:
                fc.append(f"[0:a]atrim={a}:{b},asetpts=PTS-STARTPTS[a{i}];")
        n = len(segments)
        if has_audio:
            fc.append("".join(f"[v{i}][a{i}]" for i in range(n))
                      + f"concat=n={n}:v=1:a=1[tv][tacc];")
            audio_label = "[tacc]"
        else:
            fc.append("".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[tv];")
        vsrc = "[tv]"
    else:
        vsrc = "[0:v]"
        if has_audio:
            audio_label = "[0:a]"

    # Per-clip audio peak/loudness normalization to a consistent social target.
    if has_audio:
        fc.append(f"{audio_label}{LOUDNORM}[aout];")

    # CROP-FILL: scale so the source COVERS the whole 1080x1920 frame, then
    # center-crop the overflow — fills edge to edge, zero black bars.
    fc.append(f"{vsrc}scale={W}:{H}:force_original_aspect_ratio=increase,"
              f"crop={W}:{H},setsar=1[base];")
    # eof_action=repeat keeps the single-frame caption/watermark PNGs on-screen for
    # the whole clip (otherwise they'd show for one frame).
    fc.append(f"[base][1:v]overlay=(W-w)/2:{CAPTION_TOP_Y}:eof_action=repeat[cap];")
    fc.append(f"[2:v]scale={wm_w}:-2[wm];")
    fc.append(f"[cap][wm]overlay=W-w-{wm_margin}:H-h-{wm_margin + 20}:eof_action=repeat[vout]")

    dur = round(end - start, 3)
    cmd = ["ffmpeg", "-y", "-v", "error",
           "-ss", f"{start}", "-t", f"{dur}", "-i", str(source),
           "-i", str(caption_png), "-i", str(watermark_png),
           "-filter_complex", "".join(fc), "-map", "[vout]"]
    if has_audio:
        cmd += ["-map", "[aout]", "-c:a", "aac", "-b:a", "160k"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out_path)]
    return cmd


def compose(source, start, end, segments, cold_open, caption_png, watermark_png, out_path,
            cfg, has_audio):
    cmd = build_compose_cmd(source, start, end, segments, cold_open, caption_png,
                            watermark_png, out_path, cfg, has_audio)
    C.run_cmd(cmd, desc=f"cutting {out_path.name}")


# --- misc ----------------------------------------------------------------------
def slugify(text, n=40):
    s = re.sub(r"[^a-z0-9]+", "-", (text or "clip").lower()).strip("-")
    return (s[:n].strip("-") or "clip")


def _durations(sources):
    cache = {}
    for s in sources:
        cache[s["source"]] = s.get("duration_sec") or C.ffprobe_duration(C.ROOT / s["source"])
    return cache


def run(state):
    C.require_exe("ffmpeg")
    caps = C.load_json(C.CAPTIONS_JSON)
    if not caps:
        C.fail("campaign/captions.json missing — run the captions stage first.")
    rules = C.load_json(C.RULES_JSON) or {}
    banned = list(rules.get("banned_words", C.DEFAULT_BANNED_WORDS)) + list(rules.get("banned_topics", []))
    if C.load_knowledge():
        C.log("loaded campaign/knowledge.md for campaign context.")
    moments = C.load_json(C.MOMENTS_JSON) or {"sources": []}
    durations = _durations(moments.get("sources", []))

    cfg = state.get("config", {})
    cmin = float(cfg.get("clip_min_seconds", 15))
    cmax = float(cfg.get("clip_max_seconds", 45))
    pre = float(cfg.get("story_pre_seconds", 20))
    post = float(cfg.get("story_post_seconds", 15))
    watermark = find_watermark(cfg)
    C.log(f"watermark: {watermark.name}")

    C.DRAFTS.mkdir(parents=True, exist_ok=True)
    clips = sorted(caps["clips"], key=lambda c: (c.get("score") or 0), reverse=True)
    audio_cache = {}
    manifest = []
    for rank, c in enumerate(clips, 1):
        # final rules gate (defensive — the gauntlet already filtered)
        for field in ("caption", "tiktok_caption", "shorts_title"):
            from captions import banned_hit
            if banned_hit(c.get(field, ""), banned):
                C.fail(f"banned word slipped into {field} for clip {c['moment_id']} — aborting.")

        src_path = C.ROOT / c["source"]
        duration = durations.get(c["source"]) or C.ffprobe_duration(src_path)
        start, end = clip_bounds(c, duration, cmin, cmax, pre, post)
        keeps = dead_air_keeps(src_path, start, end)

        # COLD-OPEN: if the payoff peak is separable from the setup (sits well past the
        # start and leaves real clip after it), open on the peak, then cut back to the
        # setup. Otherwise the clip plays chronologically.
        cold_rel = None
        peak = c.get("peak")
        if peak is not None:
            try:
                peakf = float(peak)
            except (TypeError, ValueError):
                peakf = None
            if (peakf is not None and (peakf - start) >= COLD_OPEN_MIN_SETUP
                    and (end - peakf) >= COLD_OPEN_MIN_PAYOFF):
                cold_rel = cold_open_window(src_path, peakf, start, end)
        cold_open = cold_rel is not None
        segments = ([cold_rel] if cold_open else []) + keeps

        if c["source"] not in audio_cache:
            audio_cache[c["source"]] = has_audio_stream(src_path)
        has_audio = audio_cache[c["source"]]

        score_i = int(round(c.get("score") or 0))
        name = f"{rank:02d}_{score_i:03d}_{slugify(c['caption'])}.mp4"
        out_path = C.DRAFTS / name
        cap_png = C.DRAFTS / f".cap_{rank:02d}.png"
        render_caption_png(c["caption"], cap_png)
        compose(src_path, start, end, segments, cold_open, cap_png, watermark, out_path,
                cfg, has_audio)
        cap_png.unlink(missing_ok=True)
        C.log(f"  {'cold-open ' if cold_open else ''}cut {name}")

        manifest.append({
            "filename": name, "caption": c["caption"], "variant": c.get("variant"),
            "source": c["source"], "source_start": start, "source_end": end,
            "cold_open": cold_open, "dead_air_trimmed": len(keeps) > 1, "score": c.get("score"),
            "tiktok_caption": c["tiktok_caption"], "shorts_title": c["shorts_title"],
            "reels_hashtags": c["reels_hashtags"],
            "suggested_post_window": c["suggested_post_window"],
        })

    C.save_json(C.DRAFTS_MANIFEST, {"campaign": caps.get("campaign"),
                                    "created_at": C.now_iso(), "clips": manifest})
    C.mark_stage(state, "cut", delivered=len(manifest))
    C.log(f"cut done: {len(manifest)} clip(s) in drafts/ (best first).")


if __name__ == "__main__":
    run(C.load_state())
