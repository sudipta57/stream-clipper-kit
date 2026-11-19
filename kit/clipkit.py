#!/usr/bin/env python3
"""clipkit: turn a landscape video into vertical 9:16 Shorts with ffmpeg.

Commands
  probe  SRC                              Show duration, size, fps and audio of a video.
  scenes SRC --out DIR [--threshold 0.3]  Find scene cuts. Writes DIR/scenes.json,
                                          one labelled thumbnail per scene (DIR/thumbs/)
                                          and contact sheets (DIR/sheet_01.jpg ...).
  render PLAN.json [--only NAME]          Render the clips listed in a plan file.
  still  PLAN.json --only NAME [--at 2]   Save a preview frame of a rendered clip.
  frame  SRC --at T --out F.jpg [--grid | --layout L.json]
                                          One source frame with a pixel grid (to measure a
                                          facecam box), or rendered in a layout (to check it).
  restack JOB_DIR --source SRC --layout L.json --out DIR
                                          Re-render an OpenShorts job's clips in another layout
                                          (e.g. facecam over gameplay), keeping its captions.

Layouts ("layout" in a plan, or a layout/campaign JSON for frame/restack):
  blur   the whole frame in the middle over a blurred copy (default)
  crop   a 9:16 slice of the frame (crop_x 0..1 picks where)
  stack  facecam on top, gameplay below: face_box "w:h:x:y" (source pixels) is the
         facecam; face_h (output px, default from its shape), game_box "w:h:x:y"
         (default: whole frame), game_x 0..1, game_zoom, face_position top|bottom,
         divider px, caption_y (0..1, where caption lines end).
  keep   an already vertical video (e.g. an OpenShorts render), only fitted to 1080x1920.

Call to action: "cta" (text, in the plan or a clip) is shown for the last
"cta_seconds" (default 3.5) on a red box; layout keys cta_size, cta_y (0..1),
cta_wrap, cta_box (ffmpeg colour), cta_pad.

Paths inside a plan file are relative to the plan file.
Encodes on the GPU (h264_nvenc) when available ("encoder": "x264" forces the CPU).
Only needs Python 3 and ffmpeg/ffprobe (with drawtext; libass for captions).
"""

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

OUT_W, OUT_H = 1080, 1920
DEFAULT_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
KIT_DIR = Path(__file__).resolve().parent
OPENSHORTS_FONTS = KIT_DIR.parent / "openshorts" / "fonts"
_NVENC = None


# ---------- helpers ----------

def die(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def need_ffmpeg():
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            die(f"{tool} not found. Install it with: sudo apt install ffmpeg")


def parse_time(value):
    """'83.5', '1:23.5' or '0:01:23.5' -> seconds (float)."""
    if isinstance(value, (int, float)):
        return float(value)
    parts = str(value).strip().split(":")
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + float(part)
    return seconds


def fmt_time(seconds):
    m, s = divmod(seconds, 60)
    return f"{int(m)}:{s:04.1f}"


def filter_escape(value):
    """Escape a value used inside an ffmpeg filter option."""
    return (str(value).replace("\\", "\\\\").replace(":", "\\:")
            .replace("'", "\\'").replace(",", "\\,"))


def run(cmd, quiet=False):
    if not quiet:
        print("  $ " + " ".join(str(c) for c in cmd[:6]) + (" ..." if len(cmd) > 6 else ""))
    result = subprocess.run([str(c) for c in cmd], capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stderr[-3000:], file=sys.stderr)
        die(f"command failed: {cmd[0]}")
    return result


def nvenc_available():
    """True when this ffmpeg can encode H.264 on the NVIDIA GPU (checked once)."""
    global _NVENC
    if _NVENC is None:
        test = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
             "-i", "color=c=black:s=256x256:d=0.2", "-c:v", "h264_nvenc", "-f", "null", "-"],
            capture_output=True)
        _NVENC = test.returncode == 0
    return _NVENC


def use_gpu(plan):
    encoder = str(plan.get("encoder", "auto")).lower()
    if encoder in ("x264", "cpu", "libx264"):
        return False
    if encoder == "nvenc" and not nvenc_available():
        die("encoder is 'nvenc' but this ffmpeg cannot use the NVIDIA encoder")
    return nvenc_available()


def video_encode_args(plan):
    if use_gpu(plan):
        return ["-c:v", "h264_nvenc", "-preset", "p6", "-tune", "hq", "-rc", "vbr",
                "-cq", str(plan.get("cq", 19)), "-b:v", "0", "-maxrate", "25M",
                "-bufsize", "50M", "-spatial-aq", "1", "-temporal-aq", "1",
                "-profile:v", "high", "-pix_fmt", "yuv420p"]
    return ["-c:v", "libx264", "-preset", "slow", "-crf", str(plan.get("crf", 18)),
            "-profile:v", "high"]


def decode_args(plan):
    """Decode on the GPU too when encoding there (frames come back for the filters)."""
    return ["-hwaccel", "cuda"] if use_gpu(plan) and plan.get("gpu_decode", True) else []


def probe(src):
    out = run(["ffprobe", "-v", "error", "-print_format", "json",
               "-show_format", "-show_streams", src], quiet=True).stdout
    return json.loads(out)


def video_info(src):
    data = probe(src)
    v = next((s for s in data["streams"] if s["codec_type"] == "video"), None)
    if v is None:
        die(f"no video stream in {src}")
    num, den = (v.get("avg_frame_rate") or "0/1").split("/")
    fps = float(num) / float(den) if float(den) else 0.0
    audio = [s for s in data["streams"] if s["codec_type"] == "audio"]
    return {
        "duration": float(data["format"].get("duration", 0)),
        "width": int(v["width"]),
        "height": int(v["height"]),
        "fps": round(fps, 3),
        "video_codec": v.get("codec_name"),
        "audio_streams": [
            {"index": a["index"], "codec": a.get("codec_name"),
             "channels": a.get("channels"), "language": a.get("tags", {}).get("language")}
            for a in audio
        ],
        "size_mb": round(int(data["format"].get("size", 0)) / 1e6, 1),
    }


# ---------- probe ----------

def cmd_probe(args):
    print(json.dumps(video_info(args.src), indent=2))


# ---------- scenes ----------

def detect_cuts(src, threshold):
    """Return the times (s) where ffmpeg's scene score exceeds threshold."""
    print(f"Detecting scene cuts (threshold {threshold}) ...")
    vf = f"scale=320:-2,select='gt(scene\\,{threshold})',showinfo"
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", src, "-an", "-vf", vf, "-f", "null", "-"],
        capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stderr[-3000:], file=sys.stderr)
        die("scene detection failed")
    return [float(m) for m in re.findall(r"pts_time:([0-9.]+)", result.stderr)]


def cmd_scenes(args):
    src = Path(args.src).resolve()
    out = Path(args.out).resolve()
    thumbs = out / "thumbs"
    if thumbs.exists():
        shutil.rmtree(thumbs)
    thumbs.mkdir(parents=True)
    info = video_info(src)
    duration = info["duration"]

    cuts = [c for c in detect_cuts(src, args.threshold) if 0.2 < c < duration - 0.2]
    bounds = [0.0] + cuts + [duration]
    scenes = []
    for start, end in zip(bounds, bounds[1:]):
        if scenes and end - start < args.min_len:
            scenes[-1]["end"] = end          # merge very short flashes into the previous scene
            continue
        scenes.append({"start": start, "end": end})
    for i, sc in enumerate(scenes, 1):
        sc["id"] = i
        sc["length"] = round(sc["end"] - sc["start"], 2)
        sc["start"], sc["end"] = round(sc["start"], 2), round(sc["end"], 2)
        sc["label"] = f"#{i}  {fmt_time(sc['start'])}-{fmt_time(sc['end'])}  ({sc['length']:.1f}s)"

    print(f"{len(scenes)} scenes. Grabbing thumbnails ...")
    font = filter_escape(args.font)
    for sc in scenes:
        mid = sc["start"] + sc["length"] / 2
        label_file = thumbs / f"scene_{sc['id']:03d}.txt"
        label_file.write_text(sc["label"])
        vf = (f"scale=480:270:force_original_aspect_ratio=decrease,"
              f"pad=480:270:(ow-iw)/2:(oh-ih)/2,"
              f"drawtext=fontfile='{font}':textfile='{filter_escape(label_file)}':"
              f"fontsize=20:fontcolor=yellow:box=1:boxcolor=black@0.7:boxborderw=6:x=8:y=8")
        run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{mid:.3f}",
             "-i", src, "-frames:v", "1", "-vf", vf, "-q:v", "3",
             thumbs / f"scene_{sc['id']:03d}.jpg"], quiet=True)
        label_file.unlink()

    for old in out.glob("sheet_*.jpg"):
        old.unlink()
    cols, rows = 4, 5
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-start_number", "1",
         "-i", thumbs / "scene_%03d.jpg",
         "-vf", f"tile={cols}x{rows}:padding=6:margin=6:color=black",
         "-q:v", "3", out / "sheet_%02d.jpg"], quiet=True)

    (out / "scenes.json").write_text(json.dumps(
        {"source": str(src), "info": info, "threshold": args.threshold, "scenes": scenes}, indent=2))
    sheets = sorted(out.glob("sheet_*.jpg"))
    print(f"Wrote {out/'scenes.json'}, {len(scenes)} thumbnails, {len(sheets)} contact sheets.")


# ---------- render ----------

def load_plan(path):
    plan_path = Path(path).resolve()
    plan = json.loads(plan_path.read_text())
    base = plan_path.parent

    def p(key, required=True):
        if key not in plan:
            if required:
                die(f"plan is missing '{key}'")
            return None
        return (base / plan[key]).resolve()

    plan["_source"] = p("source")
    plan["_out_dir"] = p("out_dir")
    plan["_logo"] = p("logo", required=False)
    plan["_base"] = base
    return plan


def trimmed_logo(logo, work):
    """Crop away the transparent border around a PNG logo (cached in work/)."""
    target = work / f"{logo.stem}_trim.png"
    if target.exists():
        return target
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", logo, "-vf",
         "alphaextract,format=gray,cropdetect=limit=24:round=2:reset=0:skip=0", "-f", "null", "-"],
        capture_output=True, text=True)
    found = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", result.stderr)
    if not found:
        return logo                      # no alpha channel or nothing to trim
    w, h, x, y = (int(v) for v in found[-1])
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", logo,
         "-vf", f"crop={w}:{h}:{x}:{y}", target], quiet=True)
    return target


def parse_box(value, what):
    try:
        w, h, x, y = (int(round(float(v))) for v in str(value).split(":"))
    except ValueError:
        die(f"{what} must look like 'w:h:x:y' (source pixels), got {value!r}")
    if w <= 0 or h <= 0 or x < 0 or y < 0:
        die(f"{what} {value!r} has a negative or empty size")
    return w, h, x, y


def stack_geometry(layout, clip=None):
    """Facecam panel height and gameplay panel height for the stack layout."""
    clip = clip or {}
    fw, fh, _, _ = parse_box(clip.get("face_box", layout.get("face_box")), "face_box")
    face_h = int(clip.get("face_h", layout.get("face_h", 0)) or round(OUT_W * fh / fw))
    face_h = max(320, min(1100, face_h)) // 2 * 2
    return face_h, OUT_H - face_h


def avoid_facecam(game_x, face, region, crop_w, explicit):
    """game_x moved so the gameplay crop (crop_w source px wide inside
    region (x, w)) misses the facecam box; unchanged when it already does,
    or when the position was set by hand (then only warn)."""
    fx0, fx1 = face[2] - 16, face[2] + face[0] + 16         # facecam x-range + border/glow
    rx, rw = region
    if rw <= crop_w:
        return game_x
    left = rx + (rw - crop_w) * game_x
    if left + crop_w <= fx0 or left >= fx1:
        return game_x
    if explicit:
        print("  warning: the gameplay crop overlaps the facecam; change game_x")
        return game_x
    options = []
    if fx0 - crop_w >= rx:                                   # crop entirely left of the cam
        options.append((fx0 - crop_w - rx) / (rw - crop_w))
    if fx1 + crop_w <= rx + rw:                              # entirely right of it
        options.append((fx1 - rx) / (rw - crop_w))
    if not options:
        print("  warning: the facecam is in the middle of the frame; the gameplay crop "
              "will show part of it")
        return game_x
    best = min(options, key=lambda g: abs(g - game_x))
    print(f"  gameplay crop moved to game_x={best:.2f} so it does not show the facecam")
    return best


def stack_parts(layout, clip, src_size=None, src="0:v"):
    """Filter chains for the stack layout: the facecam box scaled to fill the
    top panel, the gameplay cropped to fill the bottom one."""
    if not clip.get("face_box", layout.get("face_box")):
        die("the stack layout needs face_box 'w:h:x:y' (measure it with: clipkit.py frame SRC --grid)")
    fw, fh, fx, fy = parse_box(clip.get("face_box", layout.get("face_box")), "face_box")
    face_h, game_h = stack_geometry(layout, clip)
    game_box = clip.get("game_box", layout.get("game_box", layout.get("source_crop")))
    explicit = "game_x" in clip or "game_x" in layout
    game_x = float(clip.get("game_x", layout.get("game_x", 0.5)))
    zoom = float(clip.get("game_zoom", layout.get("game_zoom", 1.0)))
    gw, gh = int(OUT_W * zoom) // 2 * 2, int(game_h * zoom) // 2 * 2
    if src_size:
        rx, ry, rw, rh = 0, 0, src_size[0], src_size[1]
        if game_box:
            rw, rh, rx, ry = parse_box(game_box, "game_box")
        scale = max(gw / rw, gh / rh)
        game_x = avoid_facecam(game_x, (fw, fh, fx, fy), (rx, rw), OUT_W / scale, explicit)
    parts = [f"[{src}]split=2[facesrc][gamesrc]",
             f"[facesrc]crop={fw}:{fh}:{fx}:{fy},"
             f"scale={OUT_W}:{face_h}:force_original_aspect_ratio=increase,"
             f"crop={OUT_W}:{face_h},setsar=1[face]"]
    game = "[gamesrc]"
    if game_box:
        gbw, gbh, gbx, gby = parse_box(game_box, "game_box")
        game += f"crop={gbw}:{gbh}:{gbx}:{gby},"
    parts.append(game + f"scale={gw}:{gh}:force_original_aspect_ratio=increase,"
                 f"crop={OUT_W}:{game_h}:(iw-{OUT_W})*{game_x}:(ih-{game_h})/2,setsar=1[game]")
    face_top = layout.get("face_position", "top") != "bottom"
    top, bottom = ("face", "game") if face_top else ("game", "face")
    parts.append(f"[{top}][{bottom}]vstack=inputs=2[stacked]")
    divider = int(layout.get("divider", 6))
    if divider > 0:
        seam = face_h if face_top else game_h
        parts.append(f"[stacked]drawbox=x=0:y={seam - divider // 2}:w=iw:h={divider}:"
                     f"color=black@0.85:t=fill[base]")
    else:
        parts.append("[stacked]null[base]")
    return parts


HEADLINE_FONT = OPENSHORTS_FONTS / "Montserrat-ExtraBold.ttf"


def headline_geometry(layout, n_lines):
    """(font size, box padding, first line top y, block height) of the headline box:
    centred on the seam of a stack layout (or headline_y, 0..1 of the height)."""
    size = int(layout.get("headline_size", 70))
    pad = int(layout.get("headline_pad", 18))
    step = size + 2 * pad
    block = step * n_lines
    if layout.get("headline_y") is not None:
        center = int(float(layout["headline_y"]) * OUT_H)
    elif layout.get("mode") == "stack":
        face_h, game_h = stack_geometry(layout)
        center = face_h if layout.get("face_position", "top") != "bottom" else game_h
    else:
        center = int(OUT_H * 0.27)
    return size, pad, center - block // 2, block


def balanced_wrap(text, width, max_lines=3):
    """Wrap a headline into the fewest lines that fit `width`, then even the lines out
    (no lonely last word) and never split 'GTA 6' / 'GTA VI'."""
    text = re.sub(r"\bGTA (6|VI)\b", lambda m: "GTA\u00a0" + m.group(1), text)
    best = textwrap.wrap(text, width)
    for w in range(width - 1, 7, -1):
        trial = textwrap.wrap(text, w)
        if len(trial) != len(best):
            break
        best = trial
    return best[:max_lines]


def default_caption_y(layout):
    """Where caption lines end (0..1 of the height): just under the seam of a
    stack layout, the usual lower third otherwise."""
    if layout.get("caption_y") is not None:
        return float(layout["caption_y"])
    if layout.get("mode") == "stack":
        face_h, game_h = stack_geometry(layout)
        seam = face_h if layout.get("face_position", "top") != "bottom" else game_h
        return min(0.9, (seam + 260) / OUT_H)
    return None


def place_ass(src_ass, dst_ass, caption_y):
    """Copy an OpenShorts caption file, moving its lines so they end at
    caption_y (fraction of the height). Unchanged when caption_y is None."""
    text = Path(src_ass).read_text(encoding="utf-8-sig")
    if caption_y is not None:
        m = re.search(r"^PlayResY:\s*(\d+)", text, re.M)
        play_h = int(m.group(1)) if m else 288
        margin_v = max(0, round((1.0 - caption_y) * play_h))
        fmt = re.search(r"^\[V4\+ Styles\]\s*\nFormat:\s*(.+)$", text, re.M)
        fields = [f.strip() for f in fmt.group(1).split(",")] if fmt else []
        if "MarginV" in fields:
            idx = fields.index("MarginV")

            def fix(line):
                head, _, rest = line.group(0).partition(":")
                values = rest.split(",")
                if len(values) > idx:
                    values[idx] = str(margin_v)
                return head + ":" + ",".join(values)
            text = re.sub(r"^Style:.*$", fix, text, flags=re.M)
    Path(dst_ass).write_text(text, encoding="utf-8")
    return dst_ass


def cta_filter(layout, cta_files, start, font):
    """drawtext chain for the call-to-action lines, faded in at ``start``
    seconds (clip time) and kept to the end. Centred on the gameplay panel
    of a stack layout, in the lower part of the frame otherwise."""
    size = int(layout.get("cta_size", 66))
    pad = int(layout.get("cta_pad", 20))
    step = size + 2 * pad
    block = step * len(cta_files)
    if layout.get("cta_y") is not None:
        y0 = int(float(layout["cta_y"]) * OUT_H)
    else:
        if layout.get("mode") == "stack":
            face_h, game_h = stack_geometry(layout)
            top = face_h if layout.get("face_position", "top") != "bottom" else 0
            center = top + game_h // 2
        else:
            center = int(OUT_H * 0.70)
        y0 = center - block // 2
    color = layout.get("cta_box", "0xE62117@0.92")     # YouTube red
    draws = []
    for i, tf in enumerate(cta_files):
        draws.append(
            f"drawtext=fontfile='{filter_escape(font)}':textfile='{filter_escape(tf)}':"
            f"fontsize={size}:fontcolor=white:borderw=3:bordercolor=black@0.6:"
            f"box=1:boxcolor={color}:boxborderw={pad}:y_align=font:"
            f"x=(w-text_w)/2:y={y0 + pad + i * step}:"
            f"enable='gte(t,{start:.3f})':alpha='min(1,(t-{start:.3f})/0.3)'")
    return ",".join(draws)


def build_filter(layout, clip, has_logo, text_files, font, ass_file=None, src_size=None,
                 cta_files=None, cta_start=0.0, pre_trim=0.0, trim_len=None, headline_files=None):
    mode = clip.get("mode", layout.get("mode", "blur"))
    parts = []
    src = "0:v"
    if pre_trim > 0:
        # The input was opened pre_trim seconds early (see render_clip): cut to the
        # exact first and last frame here, so the clip is on time wherever the seek landed.
        # A fixed shift (not PTS-STARTPTS) keeps picture and sound on the same clock.
        dur = f":duration={trim_len:.3f}" if trim_len else ""
        parts.append(f"[0:v]trim=start={pre_trim:.3f}{dur},setpts=PTS-{pre_trim:.3f}/TB[trimmed]")
        src = "trimmed"
    source_crop = clip.get("source_crop", layout.get("source_crop"))   # "w:h:x:y", e.g. to remove black bars
    if source_crop and mode != "stack":
        parts.append(f"[{src}]crop={source_crop}[src]")
        src = "src"
    if mode == "stack":
        parts += stack_parts(layout, clip, src_size, src)
    elif mode == "keep":
        # Already vertical (e.g. an OpenShorts render): only fit it to 1080x1920.
        parts.append(f"[{src}]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=decrease,"
                     f"pad={OUT_W}:{OUT_H}:(ow-iw)/2:(oh-ih)/2:black,setsar=1[base]")
    elif mode == "crop":
        crop_x = float(clip.get("crop_x", 0.5))           # 0 = left edge, 1 = right edge
        parts.append(f"[{src}]scale=-2:{OUT_H},crop={OUT_W}:{OUT_H}:(iw-{OUT_W})*{crop_x}:0,setsar=1[base]")
    else:
        zoom = float(clip.get("zoom", layout.get("zoom", 1.0)))  # >1 makes the middle video bigger
        fg_w = int(OUT_W * zoom) // 2 * 2
        # yuv420p first: GPU-decoded frames arrive as nv12, and boxblur on nv12 at this
        # small size leaves half the background green
        parts.append(f"[{src}]format=yuv420p,split=2[bgsrc][fgsrc]")
        parts.append(f"[bgsrc]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,"
                     f"boxblur=12:2,scale={OUT_W}:{OUT_H},eq=brightness=-0.12,setsar=1[bg]")
        parts.append(f"[fgsrc]scale={fg_w}:-2,setsar=1[fgfull]")
        if fg_w > OUT_W:
            parts.append(f"[fgfull]crop={OUT_W}:ih[fg]")
        else:
            parts.append("[fgfull]null[fg]")
        parts.append("[bg][fg]overlay=(W-w)/2:(H-h)/2[base]")
    last = "base"

    if has_logo:
        logo_w = int(layout.get("logo_width", 420))
        logo_y = layout.get("logo_y", 1330)
        parts.append(f"[1:v]scale={logo_w}:-1[logo]")
        parts.append(f"[{last}][logo]overlay=(W-w)/2:{logo_y}[withlogo]")
        last = "withlogo"

    if headline_files:
        # big headline: one white box per line with bold black text (style of the top
        # GTA 6 clip pages); it gives way to the call to action at the end
        size, pad, y0, _ = headline_geometry(layout, len(headline_files))
        hfont = layout.get("headline_font") or (str(HEADLINE_FONT) if HEADLINE_FONT.exists() else font)
        until = (f":enable='lt(t,{cta_start:.3f})'"
                 if cta_files and layout.get("headline_until_cta", True) else "")
        draws = []
        for i, tf in enumerate(headline_files):
            draws.append(
                f"drawtext=fontfile='{filter_escape(hfont)}':textfile='{filter_escape(tf)}':"
                f"fontsize={size}:fontcolor=black:box=1:boxcolor=white:boxborderw={pad}:"
                f"y_align=font:x=(w-text_w)/2:y={y0 + pad + i * (size + 2 * pad)}{until}")
        parts.append(f"[{last}]" + ",".join(draws) + "[withheadline]")
        last = "withheadline"

    if text_files:
        # One drawtext per line: ffmpeg 8 draws a box glyph for line breaks.
        size = int(layout.get("font_size", 64))
        text_y = int(layout.get("text_y", 300))
        step = size + int(layout.get("line_gap", 16))
        draws = []
        for i, tf in enumerate(text_files):
            draws.append(
                f"drawtext=fontfile='{filter_escape(font)}':textfile='{filter_escape(tf)}':"
                f"fontsize={size}:fontcolor=white:borderw=5:bordercolor=black:"
                f"x=(w-text_w)/2:y={text_y + i * step}")
        parts.append(f"[{last}]" + ",".join(draws) + "[withtext]")
        last = "withtext"

    if ass_file:
        fonts = f":fontsdir='{filter_escape(OPENSHORTS_FONTS)}'" if OPENSHORTS_FONTS.is_dir() else ""
        parts.append(f"[{last}]ass=filename='{filter_escape(ass_file)}'{fonts}[withsubs]")
        last = "withsubs"

    if cta_files:
        parts.append(f"[{last}]" + cta_filter(layout, cta_files, cta_start, font) + "[withcta]")
        last = "withcta"

    parts.append(f"[{last}]format=yuv420p[v]")
    return ";".join(parts)


def measure_lufs(src, ss: float, t: float):
    """Integrated loudness (LUFS) of the source audio in [ss, ss+t], or None."""
    res = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-ss", f"{ss:.3f}", "-t", f"{t:.3f}",
                          "-i", str(src), "-vn", "-af", "loudnorm=print_format=json", "-f", "null", "-"],
                         capture_output=True, text=True)
    m = re.search(r'"input_i"\s*:\s*"(-?[\d.]+)"', res.stderr or "")
    return float(m.group(1)) if m else None


def render_clip(plan, clip):
    layout = plan.get("layout", {})
    name = clip["name"]
    start, end = parse_time(clip["start"]), parse_time(clip["end"])
    length = end - start
    if length <= 0:
        die(f"{name}: end must be after start")
    if length < 5 or length > 60:
        print(f"  warning: {name} is {length:.1f}s (Shorts work best between 5 and 60 s)")

    allowed = plan.get("allowed_text")
    text = clip.get("text", "").strip()
    if text and allowed and text not in allowed:
        print(f"  warning: {name} text is not one of the campaign's approved lines")

    out_dir = plan["_out_dir"]
    work = out_dir / "_work"
    work.mkdir(parents=True, exist_ok=True)
    text_files = []
    if text:
        wrap = int(layout.get("wrap_chars", 22))
        for i, line in enumerate(textwrap.wrap(text, wrap)):
            tf = work / f"{name}_line{i}.txt"
            tf.write_text(line)
            text_files.append(tf)

    # Call to action ("cta" in the clip or plan), shown for the last cta_seconds.
    cta = str(clip.get("cta", plan.get("cta")) or "").strip()
    cta_files = []
    if cta:
        for i, line in enumerate(textwrap.wrap(cta, int(layout.get("cta_wrap", 20)))):
            tf = work / f"{name}_cta{i}.txt"
            tf.write_text(line)
            cta_files.append(tf)
    cta_secs = float(clip.get("cta_seconds", plan.get("cta_seconds", 3.5)))
    cta_start = max(0.0, length - cta_secs)

    headline = str(clip.get("headline") or "").strip()
    headline_files = []
    if headline:
        for i, line in enumerate(balanced_wrap(headline, int(layout.get("headline_wrap", 19)))):
            tf = work / f"{name}_hl{i}.txt"
            tf.write_text(line)
            headline_files.append(tf)

    font = plan.get("font", DEFAULT_FONT)
    logo = plan["_logo"]
    if logo is not None and layout.get("logo_trim", True):
        logo = trimmed_logo(logo, work)
    ass_file = None
    if clip.get("captions"):
        cap_y = clip.get("caption_y", default_caption_y(layout))
        if headline_files and clip.get("caption_y") is None and layout.get("caption_y") is None:
            # captions start just under the headline box
            _, _, hy0, hblock = headline_geometry(layout, len(headline_files))
            cap_y = min(0.9, (hy0 + hblock + 290) / OUT_H)
        ass_file = place_ass(clip["captions"], work / f"{name}.ass", cap_y)
    info = video_info(plan["_source"])
    # seek_margin: open the input that many seconds early and trim to the exact
    # start in the filters. Needed for MPEG-TS recordings (live mode), where a
    # seek can land a keyframe after the requested time.
    margin = float(clip.get("seek_margin", plan.get("seek_margin", 0)))
    seek = max(0.0, start - margin)
    pre = start - seek
    graph = build_filter(layout, clip, logo is not None, text_files, font, ass_file,
                         (info["width"], info["height"]), cta_files, cta_start, pre, length,
                         headline_files)

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-stats", "-y",
           *decode_args(plan),
           "-ss", f"{seek:.3f}", "-t", f"{length + pre:.3f}", "-i", plan["_source"]]
    if logo is not None:
        cmd += ["-i", logo]
    cmd += ["-filter_complex", graph, "-map", "[v]", "-map", "0:a:0?"]

    fade = float(plan.get("audio_fade", 0.15))
    afilters = ([f"atrim=start={pre:.3f}:duration={length:.3f},asetpts=PTS-{pre:.3f}/TB",
                 "aresample=async=1:first_pts=0"] if pre > 0 else [])
    if fade > 0:
        afilters += [f"afade=t=in:d={fade}", f"afade=t=out:st={max(length - fade, 0):.3f}:d={fade}"]
    # loudness: a fixed gain to the target (Shorts/Reels play around -14 LUFS and never turn a
    # quiet clip up) plus a peak limiter; a static gain keeps the stream's own dynamics
    target = plan.get("loudness", layout.get("loudness", -14))
    if target is not None:
        lufs = measure_lufs(plan["_source"], start, length)
        if lufs is not None and lufs > -70:
            gain = max(-20.0, min(20.0, float(target) - lufs))
            if abs(gain) >= 0.5:
                afilters += [f"volume={gain:.2f}dB", "alimiter=limit=0.891:level=false"]
                print(f"  loudness {lufs:.1f} LUFS -> {float(target):.0f} ({gain:+.1f} dB)")
    if afilters:
        cmd += ["-af", ",".join(afilters)]
    fps = plan.get("fps")
    if fps:
        cmd += ["-r", str(fps)]
    cmd += video_encode_args(plan)
    cmd += ["-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            "-movflags", "+faststart", out_dir / f"{name}.mp4"]

    where = "GPU" if use_gpu(plan) else "CPU"
    print(f"Rendering {name}  ({fmt_time(start)} -> {fmt_time(end)}, {length:.1f}s, {where})")
    run(cmd, quiet=True)
    print(f"  -> {out_dir / (name + '.mp4')}")
    return out_dir / f"{name}.mp4"


def cmd_render(args):
    plan = load_plan(args.plan)
    clips = plan.get("clips", [])
    if args.only:
        clips = [c for c in clips if c["name"] == args.only]
        if not clips:
            die(f"no clip named {args.only}")
    if not clips:
        die("the plan has no clips")
    plan["_out_dir"].mkdir(parents=True, exist_ok=True)
    for clip in clips:
        render_clip(plan, clip)
    shutil.rmtree(plan["_out_dir"] / "_work", ignore_errors=True)
    print(f"Done: {len(clips)} clip(s) in {plan['_out_dir']}")


def cmd_still(args):
    plan = load_plan(args.plan)
    video = plan["_out_dir"] / f"{args.only}.mp4"
    if not video.exists():
        die(f"{video} not rendered yet")
    previews = plan["_out_dir"] / "previews"
    previews.mkdir(exist_ok=True)
    target = previews / f"{args.only}_{args.at:g}s.jpg"
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", str(args.at), "-i", video,
         "-frames:v", "1", "-vf", "scale=540:-2", "-q:v", "3", target], quiet=True)
    print(target)


# ---------- frame (measure / preview a layout) ----------

def load_layout(value):
    """A layout from a JSON file (a layout, or a plan/campaign with "layout")
    or an inline JSON string."""
    if not value:
        return {}
    text = value if str(value).strip().startswith("{") else Path(value).read_text()
    data = json.loads(text)
    return data.get("layout", data) if isinstance(data, dict) else {}


def cmd_frame(args):
    src = Path(args.src).resolve()
    out = Path(args.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    layout = load_layout(args.layout)
    if args.grid or not layout:
        # The raw frame at full size with a labelled pixel grid: read the
        # facecam's left/top/right/bottom edges off it for face_box.
        info = video_info(src)
        step = args.step
        font = filter_escape(DEFAULT_FONT)
        draws = [f"drawgrid=w={step}:h={step}:t=1:c=white@0.35",
                 f"drawgrid=w={step * 5}:h={step * 5}:t=3:c=yellow@0.7"]
        for x in range(0, info["width"], step * 2):
            draws.append(f"drawtext=fontfile='{font}':text='{x}':x={x + 4}:y=4:fontsize=22:"
                         f"fontcolor=yellow:box=1:boxcolor=black@0.6")
        for y in range(step, info["height"], step):
            draws.append(f"drawtext=fontfile='{font}':text='{y}':x=4:y={y + 4}:fontsize=22:"
                         f"fontcolor=cyan:box=1:boxcolor=black@0.6")
        if layout.get("face_box"):
            w, h, x, y = parse_box(layout["face_box"], "face_box")
            draws.append(f"drawbox=x={x}:y={y}:w={w}:h={h}:color=red:t=4")
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", str(args.at),
               "-i", src, "-frames:v", "1", "-vf", ",".join(draws), "-q:v", "2", out]
    else:
        info = video_info(src)
        graph = build_filter(layout, {}, False, [], DEFAULT_FONT, None,
                             (info["width"], info["height"])) + ";[v]scale=540:960[small]"
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", str(args.at),
               "-i", src, "-filter_complex", graph, "-map", "[small]", "-frames:v", "1",
               "-q:v", "2", out]
    run(cmd, quiet=True)
    print(out)


# ---------- restack (re-render an OpenShorts job in another layout) ----------

def _words(text):
    return set(re.findall(r"[a-z0-9']+", text.lower()))


def _ass_words(path):
    words = set()
    for line in Path(path).read_text(encoding="utf-8-sig", errors="ignore").splitlines():
        if line.startswith("Dialogue:"):
            body = re.sub(r"\{[^}]*\}", " ", line.split(",", 9)[-1]).replace("\\N", " ")
            words |= _words(body)
    return words


def _spoken_words(meta, short):
    start, end = float(short["start"]), float(short["end"])
    words = set()
    for seg in (meta.get("transcript") or {}).get("segments") or []:
        for w in seg.get("words") or []:
            if w.get("end", 0) > start and w.get("start", 0) < end:
                words |= _words(str(w.get("word", "")))
    return words


def openshorts_clips(job_dir):
    """[(index, short, captions .ass or None)] for an OpenShorts output folder.
    A clip's caption file shares its timestamp with the captioned render
    (subtitled_<ts>_..._clip_<n>.mp4 <-> autosubs_<ts>_<random>.ass). Clips
    finished in the same second share a timestamp, so within such a group each
    caption file goes to the clip whose spoken words it matches best."""
    job_dir = Path(job_dir)
    metas = sorted(job_dir.glob("*_metadata.json"))
    if not metas:
        die(f"no OpenShorts metadata in {job_dir}")
    meta = json.loads(metas[0].read_text())
    shorts = meta.get("shorts") or []
    groups = {}                                    # ts -> [clip index]
    for i in range(1, len(shorts) + 1):
        for rendered in job_dir.glob(f"subtitled_*_clip_{i}.mp4"):
            m = re.match(r"subtitled_(\d+)_", rendered.name)
            if m:
                groups.setdefault(m.group(1), []).append(i)
    chosen = {}
    for ts, idxs in groups.items():
        files = sorted(job_dir.glob(f"autosubs_{ts}_*.ass"))
        if len(idxs) == 1 and len(files) == 1:
            chosen[idxs[0]] = files[0]
            continue
        pairs = []
        for i in idxs:
            said = _spoken_words(meta, shorts[i - 1])
            for f in files:
                cap = _ass_words(f)
                score = len(said & cap) / max(1, len(said | cap))
                pairs.append((score, i, f))
        for score, i, f in sorted(pairs, key=lambda p: -p[0]):
            if i not in chosen and f not in chosen.values():
                chosen[i] = f
                if score < 0.3:
                    print(f"  warning: clip {i}: unsure which caption file is its own "
                          f"(best match {score:.2f}); check the captions")
    return [(i, short, chosen.get(i)) for i, short in enumerate(shorts, 1)]


def restack_job(job_dir, source, layout, out_dir, names=None, plan_extra=None):
    """Render every clip of an OpenShorts job from ``source`` in ``layout``,
    with its captions. Returns [(index, output path)]."""
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    plan = {"_source": Path(source).resolve(), "_out_dir": out_dir, "_logo": None,
            "layout": layout}
    plan.update(plan_extra or {})
    outputs = []
    for i, short, ass in openshorts_clips(job_dir):
        clip = {"name": (names or {}).get(i) or f"{i:02d}_clip",
                "start": float(short["start"]), "end": float(short["end"])}
        if ass:
            clip["captions"] = str(ass)
        else:
            print(f"  note: clip {i} has no OpenShorts captions; rendering without")
        outputs.append((i, render_clip(plan, clip)))
    shutil.rmtree(out_dir / "_work", ignore_errors=True)
    return outputs


def cmd_restack(args):
    layout = load_layout(args.layout)
    if not layout:
        die("--layout is empty")
    outs = restack_job(args.job_dir, args.source, layout, args.out)
    print(f"Done: {len(outs)} clip(s) in {Path(args.out).resolve()}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("probe")
    p.add_argument("src")
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("scenes")
    p.add_argument("src")
    p.add_argument("--out", required=True)
    p.add_argument("--threshold", type=float, default=0.3)
    p.add_argument("--min-len", type=float, default=0.6, help="merge scenes shorter than this (s)")
    p.add_argument("--font", default=DEFAULT_FONT)
    p.set_defaults(func=cmd_scenes)

    p = sub.add_parser("render")
    p.add_argument("plan")
    p.add_argument("--only")
    p.set_defaults(func=cmd_render)

    p = sub.add_parser("still")
    p.add_argument("plan")
    p.add_argument("--only", required=True)
    p.add_argument("--at", type=float, default=2.0)
    p.set_defaults(func=cmd_still)

    p = sub.add_parser("frame")
    p.add_argument("src")
    p.add_argument("--at", default="5", help="time in the source (seconds or m:ss)")
    p.add_argument("--out", required=True)
    p.add_argument("--grid", action="store_true", help="raw frame with a pixel grid")
    p.add_argument("--step", type=int, default=50, help="grid spacing in pixels")
    p.add_argument("--layout", help="layout JSON (file or inline); with --grid draws face_box")
    p.set_defaults(func=cmd_frame)

    p = sub.add_parser("restack")
    p.add_argument("job_dir", help="openshorts/output/<job id>")
    p.add_argument("--source", required=True, help="the footage the job was made from")
    p.add_argument("--layout", required=True, help="layout JSON (file or inline)")
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_restack)

    args = parser.parse_args()
    need_ffmpeg()
    args.func(args)


if __name__ == "__main__":
    main()
