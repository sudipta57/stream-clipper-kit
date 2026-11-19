#!/usr/bin/env python3
"""Run one campaign job through the local OpenShorts and collect the clips.

    python3 kit/campaign_clip.py campaigns/example_sponsor.json \
        footage/my_stream_from_2h46m29s.mp4 \
        --out clips/example_sponsor --offset 2:46:29

    # gameplay stream: facecam on top, gameplay below (layout per streamer)
    python3 kit/campaign_clip.py campaigns/<gta campaign>.json footage/<x>/<file>.mp4 \
        --layout layouts/<streamer>.json

    # long-stream mode: straight from a stream URL, only the picked parts in 1080p
    python3 kit/campaign_clip.py campaigns/example_streamer_gta6.json \
        --url "https://www.youtube.com/watch?v=..." --layout layouts/example_stack_cam_right.json \
        [--from 1:00:00 --to 3:00:00] [--cookies secrets/youtube_cookies.txt]

What it does:
  1. uploads the footage to OpenShorts (http://localhost:8000) with the
     campaign's brief, must-say words, clip count/length and caption settings;
  2. follows the job until it finishes, printing the important log lines;
  3. copies the finished clips (captioned versions when captions are on) to
     --out with readable names, or, with a layout (--layout, or "layout" in the
     campaign file), re-renders them on the GPU in that layout with clipkit
     (e.g. "stack": facecam over gameplay) keeping OpenShorts' captions, and adds
     the campaign's on-screen call to action ("cta_text") at the end of each
     clip; then writes POSTING.md next to them: Claude's titles, hooks and
     descriptions, where each clip sits in the stream, and a check that each
     clip really says the must-say words.

Long-stream mode (--url) is for streams too long to download in full. It
downloads only the audio (about 60 MB per hour), lets OpenShorts and Claude pick
the moments from it (OpenShorts gets a black stand-in video with that audio),
then downloads just the picked parts in 1080p, lines each one up with the audio
to the millisecond, and renders them with clipkit. yt-dlp runs inside the
OpenShorts container. Age-restricted videos need --cookies (a cookies.txt from
a signed-in browser); the file is only handed to yt-dlp.

Nothing is posted anywhere. Without --url, footage must already be cut to the
campaign's allowed window (yt-dlp --download-sections).
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.request
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
OPENSHORTS = os.path.normpath(os.path.join(HERE, "..", "openshorts"))
FOOTAGE = os.path.normpath(os.path.join(HERE, "..", "footage"))
DONE = {"completed", "failed", "unknown", "ok", "ready"}
QUIET = re.compile(r"^\[\d|Analyzing Scenes|Downloading https|W0000|INFO:|WARNING: All log|"
                   r"UserWarning|tensor = torch|\x1b")

sys.path.insert(0, HERE)
import clipkit  # noqa: E402  (same folder)


def parse_time(text: str) -> float:
    parts = [float(p) for p in str(text).split(":")]
    total = 0.0
    for p in parts:
        total = total * 60 + p
    return total


def fmt_time(sec: float) -> str:
    sec = max(0.0, float(sec))
    h, rem = divmod(int(round(sec)), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def slug(text: str, limit: int = 40) -> str:
    text = re.sub(r"[^a-z0-9]+", "_", (text or "clip").lower()).strip("_")
    return text[:limit].rstrip("_") or "clip"


def get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.load(resp)


def submit(server: str, campaign: dict, video: str) -> str:
    fields = {
        "acknowledged": "true",
        "output_format": "vertical",
        "target_clips": str(campaign.get("clips", 3)),
        "clip_min_seconds": str(campaign.get("min_seconds", 20)),
        "clip_max_seconds": str(campaign.get("max_seconds", 60)),
        "captions": "true" if campaign.get("captions", True) else "false",
        "auto_hook": "true" if campaign.get("auto_hook", False) else "false",
        "brief": campaign.get("brief", ""),
        "must_say": ",".join(campaign.get("must_say") or []),
    }
    cmd = ["curl", "-sS", "-X", "POST", f"{server}/api/process",
           "-F", f"file=@{video};type=video/mp4"]
    for key, value in fields.items():
        cmd += ["--form-string", f"{key}={value}"]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    data = json.loads(out)
    if "job_id" not in data:
        sys.exit(f"OpenShorts refused the job: {out[:400]}")
    return data["job_id"]


def follow(server: str, job_id: str) -> str:
    shown = 0
    while True:
        st = get_json(f"{server}/api/status/{job_id}")
        logs = st.get("logs") or []
        for line in logs[shown:]:
            if line.strip() and not QUIET.search(line):
                print("   " + line[:200], flush=True)
        shown = len(logs)
        if st.get("status") in DONE:
            return st.get("status")
        time.sleep(5)


def clip_text(segments: list, start: float, end: float) -> str:
    return " ".join(s.get("text", "").strip() for s in segments
                    if s.get("end", 0) > start and s.get("start", 0) < end)


def load_meta(job_id: str) -> tuple[str, dict]:
    job_dir = os.path.join(OPENSHORTS, "output", job_id)
    meta_files = glob.glob(os.path.join(job_dir, "*_metadata.json"))
    if not meta_files:
        sys.exit(f"No metadata in {job_dir}")
    return job_dir, json.load(open(meta_files[0]))


def clip_names(shorts: list) -> dict:
    return {i: f"{i:02d}_{slug(s.get('video_title_for_youtube_short', ''))}"
            for i, s in enumerate(shorts, 1)}


def cta_on_openshorts(job_dir: str, shorts: list, names: dict, out_dir: str, cta: str,
                      campaign: dict = None) -> dict:
    """OpenShorts' own renders with the call to action drawn on top (GPU)."""
    done = {}
    for i, _ in enumerate(shorts, 1):
        found = (glob.glob(os.path.join(job_dir, f"subtitled_*_clip_{i}.mp4"))
                 or [p for p in glob.glob(os.path.join(job_dir, f"*_clip_{i}.mp4"))
                     if not os.path.basename(p).startswith("subtitled_")])
        if not found:
            continue
        logo, lay = campaign_watermark(campaign or {}, {"mode": "keep"})
        plan = {"_source": found[0], "_out_dir": clipkit.Path(out_dir), "_logo": logo,
                "layout": lay, "cta": cta}
        length = clipkit.video_info(found[0])["duration"]
        done[i] = clipkit.render_clip(plan, {"name": names[i], "start": 0, "end": length})
    shutil.rmtree(os.path.join(out_dir, "_work"), ignore_errors=True)
    return done


def campaign_watermark(campaign: dict, layout: dict) -> tuple:
    """The campaign's required logo ("watermark": path from the project folder) and the
    layout with its size and position, shown for the whole clip. Default spot: top
    centre (Shorts/Reels cover the bottom with their own text), in the blurred band
    above the video, or over the top of the facecam panel in a stack layout."""
    wm = campaign.get("watermark")
    if not wm:
        return None, layout
    path = wm if os.path.isabs(wm) else os.path.join(HERE, "..", wm)
    if not os.path.isfile(path):
        sys.exit(f"Watermark file not found: {path}")
    lay = dict(layout or {})
    lay.setdefault("logo_width", int(campaign.get("watermark_width", 560)))
    if "logo_y" not in lay:
        default = 110 if lay.get("mode") == "stack" else 470
        lay["logo_y"] = int(campaign.get("watermark_y", default))
    return clipkit.Path(os.path.abspath(path)), lay


SWEAR = re.compile(r"\b(mother)?(fuck|shit|bitch|cunt|dick|pussy|nigg)(\w*)", re.I)


def clean_text(text: str) -> str:
    """Star out swear words in titles and captions (f*ck, sh*t, ...)."""
    def mask(m):
        word = m.group(2)
        k = next((n for n in range(1, len(word)) if word[n].lower() in "aeiou"), 1)
        return (m.group(1) or "") + word[:k] + "*" + word[k + 1:] + m.group(3)
    return SWEAR.sub(mask, text or "").strip()


def with_required(desc: str, campaign: dict) -> str:
    """Append whatever the campaign requires in every caption and is missing.
    "caption_required": ["#tag", {"text": "kick.com/x", "add": "Watch x on Kick: kick.com/x"}]"""
    adds = []
    for item in campaign.get("caption_required") or []:
        text, add = (item, item) if isinstance(item, str) else (item["text"], item.get("add", item["text"]))
        if text.lower() not in desc.lower():
            adds.append(add)
    return (desc.rstrip() + " " + " ".join(adds)).strip() if adds else desc


def load_overrides(out_dir: str) -> dict:
    """posting_overrides.json next to the clips: reviewed text per clip number,
    {"1": {"title": "...", "description": "...", "instagram": "...", "hold": "why",
    "note": "..."}}. Kept when the clips are rendered again."""
    path = os.path.join(out_dir, "posting_overrides.json")
    return json.load(open(path)) if os.path.exists(path) else {}


def collect(job_id: str, campaign: dict, out_dir: str, offset: float,
            video: str = "", layout: dict = None, rendered: dict = None,
            notes: list = None, shorts: list = None) -> None:
    job_dir, meta = load_meta(job_id)
    segments = (meta.get("transcript") or {}).get("segments") or []
    must = campaign.get("must_say") or []
    cta = (campaign.get("cta_text") or "").strip()
    os.makedirs(out_dir, exist_ok=True)

    shorts = shorts or meta.get("shorts") or []
    names = clip_names(shorts)
    if rendered is not None:
        restacked = rendered
    elif layout:
        print(f"\nRe-rendering {len(shorts)} clip(s) in the '{layout.get('mode')}' layout ...")
        logo, lay = campaign_watermark(campaign, layout)
        extra = {"_logo": logo}
        if cta:
            extra["cta"] = cta
        restacked = dict(clipkit.restack_job(job_dir, video, lay, out_dir, names, extra))
    elif cta or campaign.get("watermark"):
        print(f"\nAdding the call to action to {len(shorts)} clip(s) ...")
        restacked = cta_on_openshorts(job_dir, shorts, names, out_dir, cta, campaign)
    else:
        restacked = {}

    lines = [f"# {campaign.get('name', 'Campaign')}: clips from job {job_id}", "",
             f"Rules: {campaign.get('rules_url', '-')}", ""]
    if campaign.get("audience_rule"):
        lines += [f"Audience rule: {campaign['audience_rule']}", ""]
    if cta:
        lines += [f"On-screen call to action (last 3.5 s of every clip): \"{cta}\"", ""]
    if campaign.get("watermark"):
        lines += [f"Required watermark on screen for the whole clip: `{campaign['watermark']}`", ""]
    lines += notes or []
    overrides = load_overrides(out_dir)
    platforms = [p.lower() for p in campaign.get("platforms") or ["youtube", "instagram"]]
    for i, short in enumerate(shorts, 1):
        captioned = glob.glob(os.path.join(job_dir, f"subtitled_*_clip_{i}.mp4"))
        plain = [p for p in glob.glob(os.path.join(job_dir, f"*_clip_{i}.mp4"))
                 if not os.path.basename(p).startswith("subtitled_")]
        src = (captioned or plain or [None])[0]
        name = names[i] + ".mp4"
        if i in restacked:
            src = str(restacked[i])
        elif rendered is not None:
            src = None
        elif src:
            shutil.copy2(src, os.path.join(out_dir, name))
        start, end = float(short["start"]), float(short["end"])
        said = clip_text(segments, start, end).lower()
        checks = ", ".join(f"{w}: {'yes' if w.lower() in said else 'NO'}" for w in must) or "-"
        ov = overrides.get(str(i), {})
        title = ov.get("title") or clean_text(short.get("video_title_for_youtube_short", ""))
        desc = with_required(ov.get("description") or clean_text(
            short.get("video_description_for_tiktok") or short.get("video_description_for_instagram", "")),
            campaign)
        head = f"## {i}. `{name}`" + ("" if src else " (file missing)")
        if ov.get("hold"):
            head += "  **HOLD: do not post**"
        lines += [head, ""]
        if ov.get("hold"):
            lines += [f"> Why on hold: {ov['hold']}", ""]
        if ov.get("note"):
            lines += [f"> Note: {ov['note']}", ""]
        if "youtube" in platforms:
            lines += ["**YouTube Shorts title**", "```", title[:100], "```",
                      "**YouTube description**", "```", desc, "```", ""]
        if "instagram" in platforms:
            ig = with_required(ov.get("instagram") or clean_text(
                short.get("video_description_for_instagram") or desc), campaign)
            lines += ["**Instagram caption**", "```", ig, "```", ""]
        if short.get("parts") and len(short["parts"]) > 1:
            when = (" + ".join(f"{fmt_time(offset + a)}-{fmt_time(offset + b)}" for a, b in short["parts"])
                    + f" ({sum(b - a for a, b in short['parts']):.0f} s, parts joined in this order)")
        else:
            when = (f"{fmt_time(offset + start)} - {fmt_time(offset + end)} ({end - start:.0f} s)"
                    + (" (stretched to include the must-say line)" if short.get("brief_adjusted") else ""))
        lines += [
            f"- Stream time: {when}",
            f"- Claude's score: {short.get('predicted_score')} | Says required words: {checks}"
            + (" | text reviewed by Claude Code" if ov else ""),
            f"- Why: {ov.get('why') or short.get('why', '')}",
        ] + ([f"- Hook idea: {clean_text(short['viral_hook_text'])}"]
             if short.get("viral_hook_text") else []) + [""]
    if campaign.get("posting_notes"):
        lines += ["## When and how to post", ""] + list(campaign["posting_notes"]) + [""]
    if campaign.get("posting_checklist"):
        lines += ["## Before posting", ""] + [f"- [ ] {c}" for c in campaign["posting_checklist"]]
    with open(os.path.join(out_dir, "POSTING.md"), "w") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"\nClips and POSTING.md are in {out_dir}")


# ---------- hand-picked moments (--pick) ----------

PICK_ASS_HEAD = """[Script Info]
ScriptType: v4.00+
PlayResY: 288
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Anton,34,&H00FFFFFF,&H00FFFFFF,&H00000000,&HFF000000,1,0,0,0,100,100,0,0,1,2,0,2,10,10,43,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
PICK_HI = r"{\c&H00E5FF&\fscx90\fscy90\t(0,110,\fscx108\fscy108)}"


def parse_picks(values: list, window_start: float) -> list:
    """--pick "H:MM:SS-H:MM:SS" (stream time), or several ranges joined with "+"
    (setup + payoff, played one after the other in one clip) -> one list of
    (start, end) parts in window seconds per clip."""
    picks = []
    for v in values or []:
        parts = []
        for r in v.split("+"):
            a, _, b = r.strip().partition("-")
            s0, s1 = parse_time(a) - window_start, parse_time(b) - window_start
            if not b or s0 < 0 or s1 <= s0:
                sys.exit(f"--pick {v!r}: give stream times inside the window, start-end[+start-end]")
            parts.append((s0, s1))
        picks.append(parts)
    return picks


def concat_parts(parts: list, target: str) -> str:
    """Join rendered parts (same size, codecs and frame rate) into one clip."""
    lst = target + ".parts.txt"
    with open(lst, "w") as fh:
        fh.writelines(f"file '{os.path.abspath(p)}'\n" for p in parts)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat",
                    "-safe", "0", "-i", lst, "-c", "copy", "-movflags", "+faststart", target],
                   check=True)
    os.remove(lst)
    for p in parts:
        os.remove(p)
    return target


def parse_fixes(values: list, window_start: float) -> list:
    """--fix-word "OLD=NEW" (every OLD word) or "H:MM:SS.ss=NEW" (the word starting
    then, stream time) -> [(word or window seconds, NEW)] for pick captions."""
    fixes = []
    for v in values or []:
        old, _, new = v.partition("=")
        if not _:
            sys.exit(f"--fix-word {v!r}: use OLD=NEW or H:MM:SS.ss=NEW")
        key = parse_time(old) - window_start if ":" in old else old.strip().lower()
        fixes.append((key, new.strip()))
    return fixes


def word_fixes(campaign: dict, values: list, window_start: float) -> list:
    """--fix-word values plus the campaign's "fix_words" ({"clicks": "Clix"}): names
    Whisper keeps mishearing, corrected in every clip of the campaign."""
    return parse_fixes(values, window_start) + [
        (old.strip().lower(), new.strip()) for old, new in (campaign.get("fix_words") or {}).items()]


def fix_ass_words(src: str, dst: str, fixes: list) -> str:
    """Corrects misheard words (the OLD=NEW word fixes) in an OpenShorts caption file;
    only the spoken text changes, never the timing or the highlight tags."""
    words = [(k, v) for k, v in fixes if isinstance(k, str)]

    def fix_text(text):
        bits = re.split(r"(\{[^}]*\})", text)
        for n, bit in enumerate(bits):
            if bit.startswith("{"):
                continue
            for old, new in words:
                bit = re.sub(rf"(?<![\w']){re.escape(old)}(?![\w'])",
                             lambda m: new.upper() if m.group(0).isupper() else new,
                             bit, flags=re.I)
            bits[n] = bit
        return "".join(bits)

    lines = []
    with open(src, encoding="utf-8") as fh:
        for line in fh.read().splitlines():
            if line.startswith("Dialogue:"):
                fields = line.split(",", 9)
                if len(fields) == 10:
                    fields[9] = fix_text(fields[9])
                    line = ",".join(fields)
            lines.append(line)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(dst, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return dst


def clip_headline(campaign: dict, ov: dict, short: dict) -> str:
    """The big headline box text: the reviewed "headline" in posting_overrides.json, else
    the clip title without hashtags/emoji, in capitals. "headline": false in the campaign
    file turns it off."""
    if ov.get("headline") is not None:
        text = str(ov["headline"])
    elif campaign.get("headline", True) is False:
        return ""
    else:
        text = ov.get("title") or short.get("video_title_for_youtube_short") or ""
        text = re.sub(r"#\S+", "", text)
    text = re.sub(r"[^\w\s'’!?.,:;&%$\-–—…\"“”()/+]", "", text)
    return re.sub(r"\s+", " ", clean_text(text)).strip().upper()


def pick_captions(meta: dict, start: float, end: float, path: str, fixes: list = None) -> str:
    """OpenShorts-style word-by-word captions (.ass, clip time) for a hand-picked
    range, from the job's word timings; swear words are starred; misheard words
    can be corrected with fixes (see parse_fixes)."""
    words = [w for seg in (meta.get("transcript") or {}).get("segments") or []
             for w in seg.get("words") or []
             if w.get("start", 0) >= start - 0.05 and w.get("end", 0) <= end + 0.05]
    groups, cur = [], []
    for w in words:
        raw = str(w.get("word", "")).strip()
        bare = re.sub(r"[^\w']", "", raw).lower()
        for key, new in fixes or []:
            if (isinstance(key, float) and abs(w.get("start", 0) - key) < 0.06) or key == bare:
                raw = re.sub(r"[\w']+", new, raw, count=1)
        text = clean_text(raw).upper()
        if not text:
            continue
        if cur and (len(cur) == 3 or sum(len(t) + 1 for t, _ in cur) + len(text) > 16
                    or w["start"] - cur[-1][1]["end"] > 0.8):
            groups.append(cur)
            cur = []
        cur.append((text, w))
        if text[-1] in ".?!,":
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)

    def ts(t):
        t = max(0.0, t - start)
        return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"
    lines = []
    for g in groups:
        for k, (text, w) in enumerate(g):
            stop = g[k + 1][1]["start"] if k + 1 < len(g) else w["end"]
            body = " ".join((PICK_HI + t + "{\\r}") if j == k else t for j, (t, _) in enumerate(g))
            lines.append(f"Dialogue: 0,{ts(w['start'])},{ts(max(stop, w['start'] + 0.05))},"
                         f"Default,,0,0,0,,{body}")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(PICK_ASS_HEAD + "\n".join(lines) + "\n")
    return path


# ---------- long-stream mode (--url) ----------

def compose_run(argv: list, mounts: list, capture: bool = False, check: bool = True,
                timeout: float = None) -> subprocess.CompletedProcess:
    """Run a command in a throwaway OpenShorts backend container (it has
    yt-dlp, ffmpeg and numpy) with host paths mounted (host, inside)."""
    cmd = ["docker", "compose", "run", "--rm", "--no-deps", "-T"]
    for host, inside in mounts:
        cmd += ["-v", f"{host}:{inside}"]
    cmd += ["backend", "sh", "-c", "cd /tmp && " + shlex.join(argv)]
    res = subprocess.run(cmd, cwd=OPENSHORTS, text=True, capture_output=capture,
                         timeout=timeout)
    if check and res.returncode != 0:
        tail = (res.stderr or "").strip().splitlines()[-3:] if capture else []
        sys.exit(f"{argv[0]} failed in the container: " + " | ".join(tail))
    return res


def cookie_parts(cookies: str | None) -> tuple[list, list]:
    if not cookies:
        return [], []
    path = os.path.abspath(cookies)
    if not os.path.isfile(path):
        sys.exit(f"Cookies file not found: {path}")
    # yt-dlp may refresh the file, so it is mounted writable (never printed).
    return [(path, "/cookies.txt")], ["--cookies", "/cookies.txt"]


def stream_info(url: str, work: str, cookies: str | None) -> dict:
    mounts, flags = cookie_parts(cookies)
    res = compose_run(["yt-dlp", "--no-warnings", "-J", *flags, url], mounts,
                      capture=True, check=False, timeout=300)
    if res.returncode != 0:
        lines = (res.stderr or "").strip().splitlines()
        err = lines[-1] if lines else "unknown error"
        if "confirm your age" in err or "Sign in" in err:
            sys.exit("This video is age-restricted or needs sign-in. Export a cookies.txt from a "
                     "browser signed in to an 18+ YouTube account and pass --cookies <file>.")
        sys.exit(f"yt-dlp could not read {url}: {err}")
    info = json.loads(res.stdout)
    if info.get("extractor_key") == "Youtube" and info.get("live_status") == "is_live":
        # a running YouTube stream: these formats list its numbered 5 s pieces from the start
        res = compose_run(["yt-dlp", "--no-warnings", "-J", "--live-from-start", *flags, url],
                          mounts, capture=True, check=False, timeout=300)
        if res.returncode == 0:
            info = json.loads(res.stdout)
    with open(os.path.join(work, "info.json"), "w") as fh:
        json.dump(info, fh)
    return info


def ytdlp_source(work: str, url: str) -> list:
    """Reuse the saved info (no second page fetch) while its links are fresh."""
    info = os.path.join(work, "info.json")
    if os.path.exists(info) and time.time() - os.path.getmtime(info) < 3 * 3600:
        return ["--load-info-json", "/dl/info.json"]
    return [url]


def http_get(url: str, tries: int = 4) -> bytes:
    import urllib.request
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read()
        except Exception:
            if attempt == tries - 1:
                raise
            time.sleep(2 * (attempt + 1))


def youtube_live_formats(work: str) -> list | None:
    """Formats of a still-live YouTube stream read with --live-from-start (each a link to
    its numbered pieces), or None for anything else."""
    try:
        with open(os.path.join(work, "info.json")) as fh:
            info = json.load(fh)
    except (OSError, ValueError):
        return None
    if info.get("extractor_key") != "Youtube" or info.get("live_status") != "is_live":
        return None
    return [f for f in info.get("formats") or []
            if f.get("protocol") == "http_dash_segments_generator" and f.get("url")] or None


def youtube_live_audio(formats: list) -> dict:
    return next((f for f in formats if f.get("format_id") == "140"), None) or max(
        (f for f in formats if f.get("vcodec") == "none"), key=lambda f: f.get("abr") or 0)


def youtube_live_head(url: str) -> tuple[float, float]:
    """(seconds streamed so far, length of one piece) from YouTube's piece headers."""
    import urllib.request
    req = urllib.request.Request(url + "&sq=0", headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        head_t = float(resp.headers.get("X-Head-Time-Sec") or 0)
        head_n = int(resp.headers.get("X-Head-Seqnum") or 0)
    if not head_t or not head_n:
        sys.exit("YouTube did not say how far the live stream has got")
    return head_t, head_t / head_n


def youtube_live_window(audio_url: str, video_url: str | None, start: float, end: float,
                        out: str) -> None:
    """A still-live YouTube stream keeps every 5 s piece from its start (numbered sq=0, 1, ...).
    Download only the pieces covering [start, end] and join them: audio-only is cut exact to
    the packet; with video, whole pieces are kept (audio and video stay in sync, the section
    starts up to one piece early and the alignment step finds the exact spot)."""
    from concurrent.futures import ThreadPoolExecutor
    _, piece = youtube_live_head(audio_url)
    n0, n1 = int(start // piece), int(-(-end // piece))

    def pieces(url, target):
        with ThreadPoolExecutor(8) as pool:
            data = list(pool.map(lambda n: http_get(f"{url}&sq={n}"), range(n0, n1)))
        with open(target, "wb") as fh:
            for chunk in data:
                fh.write(chunk)
        return target

    a_raw = pieces(audio_url, out + ".a.mp4")
    base = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    if video_url:
        v_raw = pieces(video_url, out + ".v.mp4")
        cmd = base + ["-i", v_raw, "-i", a_raw, "-map", "0:v", "-map", "1:a", "-c", "copy",
                      "-avoid_negative_ts", "make_zero", "-movflags", "+faststart", out]
    else:
        v_raw = None
        cmd = base + ["-ss", f"{start - n0 * piece:.3f}", "-i", a_raw, "-t", f"{end - start:.3f}",
                      "-vn", "-c:a", "copy", out]
    subprocess.run(cmd, check=True)
    for f in (a_raw, v_raw):
        if f:
            os.remove(f)


def twitch_vod_formats(work: str) -> list | None:
    """The formats of a Twitch VOD (each an HLS playlist link) from the saved info,
    or None for any other site."""
    try:
        with open(os.path.join(work, "info.json")) as fh:
            info = json.load(fh)
    except (OSError, ValueError):
        return None
    if info.get("extractor_key") != "TwitchVod":
        return None
    return [f for f in info.get("formats") or [] if f.get("url")] or None


def hls_window(m3u8: str, start: float, end: float, out: str, audio_only: bool) -> None:
    """Download only the HLS pieces covering stream seconds [start, end] straight from
    the playlist and cut them (audio exact to the packet, video from the keyframe at or
    before start). yt-dlp's ffmpeg seek hangs on very long Twitch VODs (a 20-hour,
    still-growing playlist gave 0 bytes in 5 minutes); this takes seconds."""
    import urllib.request
    from concurrent.futures import ThreadPoolExecutor

    def get(url):
        for attempt in range(4):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=60) as resp:
                    return resp.read()
            except Exception:
                if attempt == 3:
                    raise
                time.sleep(2 * (attempt + 1))

    base = m3u8.rsplit("/", 1)[0] + "/"
    absolute = lambda uri: uri if "://" in uri else base + uri
    pieces, t, dur, init = [], 0.0, None, None
    for line in get(m3u8).decode("utf-8", "replace").splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-MAP:"):
            # fragmented-MP4 pieces need their header (init) segment in front
            m = re.search(r'URI="([^"]+)"', line)
            init = absolute(m.group(1)) if m else None
        elif line.startswith("#EXTINF:"):
            dur = float(line[8:].split(",")[0])
        elif line and not line.startswith("#") and dur is not None:
            pieces.append((t, dur, absolute(line), init))
            t, dur = t + dur, None
    pick = [p for p in pieces if p[0] + p[1] > start and p[0] < end]
    if not pick:
        sys.exit(f"The stream playlist ({fmt_time(t)} long) has nothing at {fmt_time(start)}")
    with ThreadPoolExecutor(8) as pool:
        data = list(pool.map(lambda p: get(p[2]), pick))
        inits = {u: get(u) for u in {p[3] for p in pick if p[3]}}
    joined = out + (".part.mp4" if inits else ".ts")
    with open(joined, "wb") as fh:
        last = None
        for p, chunk in zip(pick, data):
            if p[3] and p[3] != last:
                fh.write(inits[p[3]])
                last = p[3]
            fh.write(chunk)
    off = max(0.0, start - pick[0][0])
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{off:.3f}", "-i", joined,
           "-t", f"{end - start:.3f}"]
    cmd += (["-vn", "-c:a", "copy", "-bsf:a", "aac_adtstoasc"] if audio_only else
            ["-c", "copy", "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart"])
    subprocess.run(cmd + [out], check=True)
    os.remove(joined)


def download_audio(url: str, work: str, start: float, end: float, duration: float,
                   cookies: str | None) -> str:
    stamp = {"url": url, "start": start, "end": end}
    stamp_file = os.path.join(work, "audio_window.json")
    old = glob.glob(os.path.join(work, "audio.*"))
    if old and os.path.exists(stamp_file) and json.load(open(stamp_file)) == stamp:
        print(f"Audio already downloaded: {os.path.basename(old[0])}")
        return old[0]
    for f in old:
        os.remove(f)
    ytlive = youtube_live_formats(work)
    if ytlive:
        print(f"Downloading the audio of {fmt_time(start)}-{fmt_time(end)} straight from the "
              "live YouTube stream's pieces ...", flush=True)
        target = os.path.join(work, "audio.m4a")
        youtube_live_window(youtube_live_audio(ytlive)["url"], None, start, end, target)
        json.dump(stamp, open(stamp_file, "w"))
        return target
    twitch = twitch_vod_formats(work)
    audio_fmt = next((f for f in twitch or [] if f.get("format_id") == "Audio_Only"), None)
    if audio_fmt:
        print(f"Downloading the audio of {fmt_time(start)}-{fmt_time(end)} straight from the "
              "Twitch playlist ...", flush=True)
        target = os.path.join(work, "audio.m4a")
        hls_window(audio_fmt["url"], start, end, target, audio_only=True)
        json.dump(stamp, open(stamp_file, "w"))
        return target
    mounts, flags = cookie_parts(cookies)
    argv = ["yt-dlp", "--newline", "--progress-delta", "20", "--no-warnings",
            # audio-only stream if there is one (YouTube), else the smallest stream
            # that carries sound (Kick has no audio-only stream)
            "-f", "ba[ext=m4a]/ba/wa*", *flags, "-o", "/dl/audio.%(ext)s"]
    if start > 0.5 or end < duration - 0.5:
        argv += ["--download-sections", f"*{start:.2f}-{end:.2f}",
                 "--downloader-args", "ffmpeg:-nostats -loglevel warning"]
    argv += ytdlp_source(work, url)
    print(f"Downloading the audio of {fmt_time(start)}-{fmt_time(end)} "
          f"(about {(end - start) / 3600 * 60:.0f} MB) ...")
    compose_run(argv, mounts + [(work, "/dl")])
    found = glob.glob(os.path.join(work, "audio.*"))
    if not found:
        sys.exit("The audio download produced no file")
    json.dump(stamp, open(stamp_file, "w"))
    return found[0]


def make_proxy(audio: str, work: str) -> str:
    """A black 640x360, 5 fps video carrying the stream audio, for OpenShorts."""
    proxy = os.path.join(work, "proxy_black.mp4")
    if os.path.exists(proxy) and os.path.getmtime(proxy) > os.path.getmtime(audio):
        return proxy
    enc = (["-c:v", "h264_nvenc", "-preset", "p1"] if clipkit.nvenc_available()
           else ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "stillimage"])
    print("Making the stand-in video for OpenShorts ...")
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "color=c=black:s=640x360:r=5", "-i", audio,
                    "-map", "0:v", "-map", "1:a", "-shortest", *enc, "-c:a", "copy", proxy],
                   check=True)
    return proxy


def download_sections(url: str, work: str, specs: list, height: int,
                      cookies: str | None) -> None:
    """specs: [(file name, start, end)] in stream seconds; 1080p (or --height)."""
    mounts, flags = cookie_parts(cookies)
    fmt = (f"bv*[height<={height}][vcodec^=avc1]+ba[ext=m4a]/"
           f"bv*[height<={height}]+ba/b[height<={height}]/b")
    os.makedirs(os.path.join(work, "sections"), exist_ok=True)
    twitch = [f for f in twitch_vod_formats(work) or []
              if (f.get("height") or 0) and f["height"] <= height]
    twitch.sort(key=lambda f: (f["height"], f.get("fps") or 0))
    ytlive = youtube_live_formats(work)
    yt_video = sorted((f for f in ytlive or [] if (f.get("vcodec") or "").startswith("avc1")
                       and (f.get("height") or 0) <= height),
                      key=lambda f: (f.get("height") or 0, f.get("fps") or 0))
    for name, a, b in specs:
        target = os.path.join(work, "sections", name)
        if os.path.exists(target):
            continue
        if ytlive and yt_video:
            print(f"Downloading {name}: stream {fmt_time(a)}-{fmt_time(b)} in "
                  f"{yt_video[-1].get('height')}p from the live YouTube stream's pieces ...", flush=True)
            youtube_live_window(youtube_live_audio(ytlive)["url"], yt_video[-1]["url"], a, b, target)
            continue
        if twitch:
            print(f"Downloading {name}: stream {fmt_time(a)}-{fmt_time(b)} in "
                  f"{twitch[-1].get('format_id')} straight from the Twitch playlist ...", flush=True)
            hls_window(twitch[-1]["url"], a, b, target, audio_only=False)
            continue
        print(f"Downloading {name}: stream {fmt_time(a)}-{fmt_time(b)} in {height}p ...", flush=True)
        argv = ["yt-dlp", "--newline", "--progress-delta", "30", "--no-warnings", "-f", fmt,
                *flags, "--download-sections", f"*{a:.2f}-{b:.2f}",
                "--downloader-args", "ffmpeg:-nostats -loglevel warning",
                "--merge-output-format", "mp4",
                "-o", "/dl/sections/" + name.replace(".mp4", ".%(ext)s"),
                *ytdlp_source(work, url)]
        res = compose_run(argv, mounts + [(work, "/dl")], check=False)
        if res.returncode != 0 or not os.path.exists(target):
            # saved links may have expired: try once more from the page itself
            argv = argv[:-2] + [url] if argv[-2] == "--load-info-json" else argv
            compose_run(argv, mounts + [(work, "/dl")])
        if not os.path.exists(target):
            sys.exit(f"Section download failed: {name}")


ALIGN_PY = r"""
import json, subprocess, sys
import numpy as np
SR = 8000
def pcm(path, ss, t):
    cmd = ["ffmpeg", "-v", "error", "-ss", f"{max(ss, 0):.3f}", "-i", path, "-t", f"{t:.3f}",
           "-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.int16).astype(np.float64)
def best(seg, tpl):
    n, m = len(seg), len(tpl)
    tpl = tpl - tpl.mean()
    size = 1 << (n + m).bit_length()
    corr = np.fft.irfft(np.fft.rfft(seg, size) * np.conj(np.fft.rfft(tpl, size)), size)[: n - m + 1]
    c2 = np.concatenate([[0.0], np.cumsum(seg * seg)])
    c1 = np.concatenate([[0.0], np.cumsum(seg)])
    energy = (c2[m:] - c2[:-m]) - (c1[m:] - c1[:-m]) ** 2 / m
    score = corr / np.sqrt(np.maximum(energy, 1e-9) * max((tpl * tpl).sum(), 1e-9))
    i = int(np.argmax(score))
    return i, float(score[i])
out = {}
for name, expected in json.loads(sys.argv[1]):
    tpl = pcm(f"/dl/sections/{name}", 1.0, 10.0)
    lo = max(0.0, expected - 10.0)
    seg = pcm(sys.argv[2], lo, 32.0)
    if len(tpl) < SR or len(seg) <= len(tpl):
        out[name] = [expected, 0.0]
        continue
    i, score = best(seg, tpl)
    out[name] = [lo + i / SR - 1.0, score]
print(json.dumps(out))
"""


def align_sections(work: str, audio: str, expected: list) -> dict:
    """{name: (window time of the section's first frame, match score)}, found by
    matching each section's audio against the full stream audio."""
    res = compose_run(["python3", "-c", ALIGN_PY, json.dumps(expected),
                       "/dl/" + os.path.basename(audio)], [(work, "/dl")], capture=True)
    return {k: tuple(v) for k, v in json.loads(res.stdout.strip().splitlines()[-1]).items()}


FACE_PY = r"""
import cv2, json, sys
import numpy as np
# Structural match: each candidate box and the reference camera crop are shrunk to
# the same small greyscale picture and compared by normalised cross-correlation.
# The streamer's room (lights, door, posters) only lines up in the real camera box.
def feat(img):
    g = cv2.cvtColor(cv2.resize(img, (96, 77), interpolation=cv2.INTER_AREA),
                     cv2.COLOR_BGR2GRAY).astype(np.float32)
    g -= g.mean()
    return g / (np.linalg.norm(g) + 1e-6)
fref = feat(cv2.imread(sys.argv[1]))
boxes = json.loads(sys.argv[2])
out = {}
for path in sys.argv[3:]:
    img = cv2.imread(path)
    scores = []
    for b in boxes:
        w, h, x, y = (int(v) for v in b.split(":"))
        scores.append(round(float((feat(img[y:y + h, x:x + w]) * fref).sum()), 3))
    out[path.rsplit("/", 1)[-1]] = scores
print(json.dumps(out))
"""


def pick_facecams(work: str, items: list, layout: dict) -> dict:
    """For streams whose facecam moves between known spots ("face_candidates"),
    pick each clip's spot by structurally matching 3 frames (at 25/50/75%) against a crop of
    the streamer's camera ("face_reference"). {clip index: face_box or None};
    None when no spot looks like the camera or the camera moves within the clip."""
    cands = layout["face_candidates"]
    ref = layout.get("face_reference")
    ref = ref if os.path.isabs(ref or "") else os.path.join(HERE, "..", ref or "")
    if not os.path.isfile(ref):
        print(f"  warning: face_reference {ref} not found; using {cands[0]} for every clip")
        return {i: cands[0] for i, *_ in items}
    fdir = os.path.join(work, "facecheck")
    os.makedirs(fdir, exist_ok=True)
    shutil.copy2(ref, os.path.join(fdir, "ref.png"))
    frames = []
    for i, src, t0, t1 in items:
        for k, frac in enumerate((0.25, 0.5, 0.75)):
            f = os.path.join(fdir, f"c{i:02d}_{k}.png")
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                            "-ss", f"{t0 + (t1 - t0) * frac:.3f}", "-i", src,
                            "-frames:v", "1", f], check=True)
            frames.append(f)
    res = compose_run(["python3", "-c", FACE_PY, "/dl/ref.png", json.dumps(cands),
                       *["/dl/" + os.path.basename(f) for f in frames]],
                      [(fdir, "/dl")], capture=True)
    scores = json.loads(res.stdout.strip().splitlines()[-1])
    picks = {}
    for i, *_ in items:
        tops, sure = [], []
        for k in range(3):
            s = scores[f"c{i:02d}_{k}.png"]
            j = max(range(len(s)), key=lambda n: s[n])
            tops.append(j)
            # a confident frame: the box lines up with the reference and clearly
            # better than every other box (tested: right box 0.4-0.98, wrong < 0.3)
            if s[j] >= 0.3 and s[j] - sorted(s)[-2] >= 0.2:
                sure.append(j)
        # all three frames rank the same box first and at least two are confident;
        # otherwise the camera is missing or moves during the clip
        same = len(set(tops)) == 1 and len(sure) >= 2
        picks[i] = cands[tops[0]] if same else None
        print(f"  clip {i}: facecam " + (f"at {picks[i]}" if same else
              f"unclear or moving ({[scores[f'c{i:02d}_{k}.png'] for k in range(3)]}); full-frame layout"))
    return picks


def long_stream(args, campaign: dict, layout: dict, out_dir: str) -> None:
    server = args.server
    tmp_work = args.work or os.path.join(FOOTAGE, "_incoming")
    os.makedirs(tmp_work, exist_ok=True)
    info = stream_info(args.url, tmp_work, args.cookies)
    vid = info.get("id") or "stream"
    work = os.path.abspath(args.work or os.path.join(
        FOOTAGE, f"{slug(campaign.get('name', 'campaign'), 24)}_{slug(vid, 20)}"))
    os.makedirs(work, exist_ok=True)
    if os.path.abspath(tmp_work) != work:
        shutil.move(os.path.join(tmp_work, "info.json"), os.path.join(work, "info.json"))
        shutil.rmtree(tmp_work, ignore_errors=True)
    duration = float(info.get("duration") or 0)
    if (info.get("extractor_key") or "").startswith("Twitch") and args.pad < 12:
        args.pad = 12.0      # Twitch sections can start several seconds after the asked time
    ytlive = youtube_live_formats(work)
    if ytlive:
        # a running YouTube stream: its recording so far can be clipped, piece by piece
        duration = youtube_live_head(youtube_live_audio(ytlive)["url"])[0] - 120
        print(f"note: still live on YouTube; using the {fmt_time(duration)} streamed so far")
    elif info.get("live_status") == "is_live":
        if not (info.get("extractor_key") or "").startswith("TwitchVod") or not duration:
            sys.exit(f"'{info.get('title')}' is still live. Run again once the stream has ended "
                     "(the full recording appears on the same link), or use kit/live_clip.py.")
        # A Twitch VOD of a running stream: its recording so far can be clipped.
        duration -= 120                   # stay clear of the newest, still-growing part
        print(f"note: still live; using the {fmt_time(duration)} recorded so far")
    start = parse_time(args.start)
    end = parse_time(args.end) if args.end else duration
    if not duration or end <= start:
        sys.exit("Could not work out the stream length; pass --from and --to")
    print(f"Stream: {info.get('title')} ({fmt_time(duration)}), "
          f"using {fmt_time(start)}-{fmt_time(end)}. Work folder: {work}")
    if not layout:
        print("note: no --layout given; clips use the blurred-background layout")
        layout = {"mode": "blur"}

    audio = download_audio(args.url, work, start, end, duration, args.cookies)
    if args.job:
        job_id = args.job                      # re-render an earlier job (e.g. new layout)
    else:
        proxy = make_proxy(audio, work)
        print(f"Submitting the audio of '{info.get('title')}' to OpenShorts for "
              f"'{campaign.get('name')}' ...")
        job_id = submit(server, campaign, proxy)
        print(f"Job {job_id}")
        if follow(server, job_id) != "completed":
            sys.exit(f"Job {job_id} did not complete")

    job_dir, meta = load_meta(job_id)
    shorts = meta.get("shorts") or []
    if args.scan:
        # find the best moments without rendering anything: list OpenShorts' picks in stream
        # time and save them (with the transcript) for comparing windows
        found = [{"start": start + float(sh["start"]), "end": start + float(sh["end"]),
                  "score": sh.get("predicted_score"),
                  "title": sh.get("video_title_for_youtube_short", ""),
                  "why": sh.get("why") or sh.get("reason") or ""} for sh in shorts]
        scan = {"job": job_id, "url": args.url, "from": start, "to": end, "picks": found}
        with open(os.path.join(work, "scan.json"), "w") as fh:
            json.dump(scan, fh, indent=1)
        print(f"Scan of {fmt_time(start)}-{fmt_time(end)} (job {job_id}):")
        for f in found:
            print(f"  {fmt_time(f['start'])}-{fmt_time(f['end'])}  score {f['score']}  {f['title']}")
        print(f"Saved {os.path.join(work, 'scan.json')}; nothing rendered.")
        return
    picks = parse_picks(args.pick, start) if args.pick else None
    if picks:
        # moments chosen by hand from the job's transcript replace OpenShorts' picks;
        # titles come from posting_overrides.json in --out (if it is there yet)
        ov = load_overrides(out_dir)
        shorts = [{"start": parts[0][0], "end": parts[-1][1], "parts": parts, "manual": True,
                   "video_title_for_youtube_short": ov.get(str(i), {}).get("title", f"pick {i}"),
                   "predicted_score": "hand-picked from the transcript"}
                  for i, parts in enumerate(picks, 1)]
    if not shorts:
        sys.exit("OpenShorts found no clips in this stream")
    window = end - start
    # every clip is one or more parts (hand picks can join a setup and its payoff)
    parts = {i: sh.get("parts") or [(float(sh["start"]), float(sh["end"]))]
             for i, sh in enumerate(shorts, 1)}
    specs, expected, part_of = [], [], []
    for i, plist in parts.items():
        for k, (ps, pe) in enumerate(plist):
            a = max(0.0, ps - args.pad)
            b = min(window, pe + args.pad)
            # the stream range is part of the name: a cached section is reused only for
            # the very same range (a new --pick for clip 1 must not get the old clip 1)
            name = (f"{job_id[:8]}_clip_{i:02d}" + (f"_p{k + 1}" if len(plist) > 1 else "")
                    + f"_{start + a:.1f}-{start + b:.1f}.mp4")
            specs.append((name, start + a, start + b))
            expected.append((name, a))
            part_of.append((i, k))
    download_sections(args.url, work, specs, args.height, args.cookies)
    offsets = align_sections(work, audio, expected)

    os.makedirs(out_dir, exist_ok=True)
    notes = []
    if layout.get("mode") == "stack":
        first = os.path.join(work, "sections", specs[0][0])
        for grid, out_name in ((True, "_layout_check_grid.jpg"), (False, "_layout_check.jpg")):
            clipkit.cmd_frame(SimpleNamespace(src=first, at=str(args.pad + 2), grid=grid, step=50,
                                              out=os.path.join(out_dir, out_name),
                                              layout=json.dumps({"layout": layout})))
        notes += ["Layout check: `_layout_check_grid.jpg` (red box = facecam) and "
                  "`_layout_check.jpg` (how the clips are framed).", ""]

    names = clip_names(shorts)
    fixes = word_fixes(campaign, args.fix_word, start)
    if picks:
        os.makedirs(os.path.join(work, "picks"), exist_ok=True)
        caps = {(i, k): pick_captions(meta, ps, pe, os.path.join(
                    work, "picks", f"pick_{i:02d}_p{k + 1}.ass"), fixes)
                for i, plist in parts.items() for k, (ps, pe) in enumerate(plist)}
    else:
        found = {i: ass for i, _, ass in clipkit.openshorts_clips(job_dir)}
        if any(isinstance(k, str) for k, _ in fixes):
            found = {i: fix_ass_words(str(a), os.path.join(work, "picks", f"auto_{i:02d}.ass"),
                                      fixes) if a else a for i, a in found.items()}
        caps = {(i, 0): found.get(i) for i in parts}
    cta = (campaign.get("cta_text") or "").strip()
    starts = {}
    for n, (name, a, b) in enumerate(specs):
        i, k = part_of[n]
        label = f"clip {i}" + (f" part {k + 1}" if len(parts[i]) > 1 else "")
        sec_start, score = offsets.get(name, (expected[n][1], 0.0))
        if score < 0.5:
            print(f"  warning: {label}: weak audio match ({score:.2f}); using the requested "
                  "start time, check the captions are in sync")
            sec_start = expected[n][1]
        print(f"  {label}: section starts at {fmt_time(start + sec_start)} (match {score:.2f})")
        starts[n] = sec_start
    boxes = {}
    if layout.get("face_candidates"):
        items = [(n, os.path.join(work, "sections", specs[n][0]),
                  parts[part_of[n][0]][part_of[n][1]][0] - starts[n],
                  parts[part_of[n][0]][part_of[n][1]][1] - starts[n])
                 for n in starts]
        boxes = pick_facecams(work, items, layout)
    rendered, pieces = {}, {}
    all_ov = load_overrides(out_dir)
    logo, layout = campaign_watermark(campaign, layout)
    for n, (name, a, b) in enumerate(specs):
        i, k = part_of[n]
        ps, pe = parts[i][k]
        last = k == len(parts[i]) - 1
        plan = {"_source": clipkit.Path(work, "sections", name),
                "_out_dir": clipkit.Path(out_dir), "_logo": logo, "layout": layout}
        if cta and last:
            plan["cta"] = cta              # the call to action closes the whole clip
        clip = {"name": names[i] + (f"_part{k + 1}" if len(parts[i]) > 1 else ""),
                "start": ps - starts[n], "end": pe - starts[n]}
        if clip["start"] < 0:
            sys.exit(f"clip {i}: its 1080p section starts {-clip['start']:.1f} s too late. Delete "
                     f"{os.path.join(work, 'sections', name)} and run again with a bigger --pad.")
        if layout.get("face_candidates"):
            if boxes.get(n):
                clip["face_box"] = boxes[n]
            else:
                clip["mode"] = "blur"      # facecam not found or moving: show the whole frame
        if caps.get((i, k)):
            clip["captions"] = str(caps[(i, k)])
        headline = clip_headline(campaign, all_ov.get(str(i), {}), shorts[i - 1])
        if headline:
            clip["headline"] = headline
        pieces.setdefault(i, []).append(str(clipkit.render_clip(plan, clip)))
    for i, outs in pieces.items():
        rendered[i] = outs[0] if len(outs) == 1 else concat_parts(
            outs, os.path.join(out_dir, names[i] + ".mp4"))
    shutil.rmtree(os.path.join(out_dir, "_work"), ignore_errors=True)
    collect(job_id, campaign, out_dir, start, args.url, layout, rendered, notes,
            shorts if picks else None)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("campaign", help="campaign JSON (campaigns/*.json)")
    ap.add_argument("video", nargs="?", help="footage already cut to the allowed window")
    ap.add_argument("--out", help="folder for the finished clips (default clips/<campaign>)")
    ap.add_argument("--offset", default="0",
                    help="where the footage starts in the original stream, e.g. 2:46:29")
    ap.add_argument("--layout", help="layout JSON (file or inline) for clipkit, e.g. "
                    "layouts/<streamer>.json; overrides the campaign's \"layout\"")
    ap.add_argument("--server", default="http://localhost:8000")
    long = ap.add_argument_group("long-stream mode")
    long.add_argument("--url", help="stream/VOD link instead of a video file")
    long.add_argument("--from", dest="start", default="0", help="window start, e.g. 1:00:00")
    long.add_argument("--to", dest="end", help="window end (default: end of the stream)")
    long.add_argument("--cookies", help="cookies.txt for age-restricted videos")
    long.add_argument("--height", type=int, default=1080, help="video height for the clips")
    long.add_argument("--pad", type=float, default=4.0,
                      help="extra seconds downloaded around each clip")
    long.add_argument("--work", help="folder for audio and sections (default footage/<campaign>_<id>)")
    long.add_argument("--pick", action="append", metavar="START-END",
                      help="with --job: render this hand-picked moment (stream time, e.g. "
                      "10:33:10-10:33:52; join ranges with + for setup+payoff in one clip; "
                      "repeatable) instead of OpenShorts' picks, with captions from the "
                      "job's transcript")
    long.add_argument("--fix-word", action="append", metavar="OLD=NEW",
                      help="with --pick: correct a misheard caption word, everywhere "
                      "(Clicks=Clix) or only the word at a stream time (10:40:14.18=out)")
    long.add_argument("--scan", action="store_true",
                      help="transcribe the window and list OpenShorts' picks only (no clips "
                           "rendered), to find the best moments of a long stream first")
    long.add_argument("--job", help="reuse this OpenShorts job's picks (no new job), e.g. to "
                      "re-render with a corrected layout")
    args = ap.parse_args()
    if bool(args.video) == bool(args.url):
        ap.error("give either a video file or --url")

    campaign = json.load(open(args.campaign))
    out_dir = os.path.abspath(args.out or os.path.join(HERE, "..", "clips",
                                                       slug(campaign.get("name", "campaign"))))
    layout = campaign.get("layout")
    if args.layout:
        text = args.layout if args.layout.strip().startswith("{") else open(args.layout).read()
        data = json.loads(text)
        layout = data.get("layout", data)

    if args.url:
        long_stream(args, campaign, layout, out_dir)
        return

    print(f"Submitting {os.path.basename(args.video)} for '{campaign.get('name')}'...")
    job_id = submit(args.server, campaign, os.path.abspath(args.video))
    print(f"Job {job_id}")
    status = follow(args.server, job_id)
    if status != "completed":
        sys.exit(f"Job ended with status {status}")
    collect(job_id, campaign, out_dir, parse_time(args.offset),
            os.path.abspath(args.video), layout)


if __name__ == "__main__":
    main()
