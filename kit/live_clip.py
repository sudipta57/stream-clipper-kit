#!/usr/bin/env python3
"""Clip a live stream while it is still going (live mode).

    # wait for a streamer to go live, then make a batch of clips every hour
    python3 kit/live_clip.py campaigns/example_streamer_gta6.json \
        --channel https://www.youtube.com/@SomeStreamer --wait \
        --layout layouts/example_stack_cam_right.json --cookies secrets/youtube_cookies.txt

    # a stream that is live right now
    python3 kit/live_clip.py campaigns/<campaign>.json --url "https://www.youtube.com/watch?v=..."

How it works:
  1. --wait checks the channel's live page every --poll minutes until it is live.
  2. The stream is recorded as it plays (video up to --height, plus audio) into
     footage/<campaign>_live_<date>/rec_NN.ts with ffmpeg; the stream's own
     timestamps are kept, so picture and sound stay in sync.
  3. Every --every minutes of recording, that stretch goes through the same
     pipeline as campaign_clip.py: its audio to OpenShorts (Whisper on the GPU,
     Claude picks the moments with the campaign brief, captions), then clipkit
     renders the picks from the recording in the layout, with the campaign's
     call to action, on the GPU. Each batch lands in
     clips/<campaign>_live_<date>/batch_NN_HHMM-HHMM/ with its POSTING.md, and a
     desktop notification says it is ready.
  4. When YouTube's stream link expires the recorder reconnects (new rec_NN.ts).
     When the stream ends, or on Ctrl+C, or after --max-hours, the rest of the
     recording becomes a last batch.

Recording 1080p60 needs a steady connection (about 1.5 MB/s) and about 5 GB of disk
per hour; --height 720 halves both. Recordings are kept until you delete them.
Nothing is posted anywhere.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import clipkit  # noqa: E402
import campaign_clip as cc  # noqa: E402


def log(msg: str) -> None:
    print(f"[{datetime.datetime.now():%H:%M:%S}] {msg}", flush=True)


def notify(title: str, body: str) -> None:
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", "-a", "clipkit", title, body], check=False)


def stream_gap(path) -> float:
    """Seconds between the earliest and latest stream start in a rendered clip
    (0 when picture and sound both start at the beginning)."""
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=start_time",
                          "-of", "json", str(path)], capture_output=True, text=True).stdout
    starts = [float(s.get("start_time") or 0) for s in json.loads(out or "{}").get("streams", [])]
    return (max(starts) - min(starts)) if starts else 0.0


def live_info(url: str, cookies: str | None) -> tuple[str, dict | None]:
    """("live" | "not_live" | "error", yt-dlp info) for a channel /live page or a video."""
    mounts, flags = cc.cookie_parts(cookies)
    try:
        res = cc.compose_run(["yt-dlp", "--no-warnings", "-J", *flags, url], mounts,
                             capture=True, check=False, timeout=300)
    except subprocess.TimeoutExpired:
        return "error", None
    if res.returncode != 0:
        lines = (res.stderr or "").strip().splitlines()
        err = lines[-1] if lines else "unknown error"
        if "confirm your age" in err or "Sign in to confirm" in err:
            sys.exit("This stream is age-restricted. Export a cookies.txt from a browser signed "
                     "in to an 18+ YouTube account and pass --cookies <file>.")
        if any(k in err for k in ("not currently live", "will begin", "Premieres", "live event")):
            return "not_live", None
        log(f"could not read {url}: {err}")
        return "error", None
    info = json.loads(res.stdout)
    return ("live" if info.get("live_status") == "is_live" else "not_live"), info


def pick_formats(info: dict, height: int) -> list:
    """Best HLS video (<= height) + best HLS audio, or one muxed HLS format."""
    hls = [f for f in info.get("formats") or [] if str(f.get("protocol", "")).startswith("m3u8")]
    has_v = lambda f: f.get("vcodec") not in (None, "none")          # noqa: E731
    has_a = lambda f: f.get("acodec") not in (None, "none")          # noqa: E731
    vkey = lambda f: ((f.get("height") or 0), (f.get("fps") or 0),   # noqa: E731
                      str(f.get("vcodec", "")).startswith("avc1"), (f.get("tbr") or 0))
    video = [f for f in hls if has_v(f) and not has_a(f) and (f.get("height") or 0) <= height]
    # YouTube live lists its audio-only playlists with vcodec "none" and no acodec
    audio = [f for f in hls if f.get("vcodec") == "none" or (has_a(f) and not has_v(f))]
    muxed = [f for f in hls if has_v(f) and has_a(f) and (f.get("height") or 0) <= height]
    if video and audio:
        best_a = max(audio, key=lambda f: ("high" in str(f.get("format_note", "")).lower(),
                                           f.get("abr") or f.get("tbr") or 0))
        return [max(video, key=vkey), best_a]
    if muxed:
        return [max(muxed, key=vkey)]
    sys.exit("This stream has no HLS formats yt-dlp can hand to ffmpeg")


class Recorder:
    """ffmpeg copying the live HLS stream(s) into one growing MPEG-TS file.
    -copyts keeps the stream's own clock, so audio and video stay aligned even
    when the two playlists start at different segments."""

    def __init__(self, path: str, formats: list):
        self.path = path
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostats",
               "-progress", "pipe:1"]
        for f in formats:
            headers = dict(f.get("http_headers") or {})
            agent = headers.pop("User-Agent", None)
            if agent:
                cmd += ["-user_agent", agent]
            if headers:
                cmd += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in headers.items())]
            cmd += ["-rw_timeout", "30000000", "-seg_max_retry", "5", "-i", f["url"]]
        if len(formats) == 2:
            cmd += ["-map", "0:v:0", "-map", "1:a:0"]
        else:
            cmd += ["-map", "0:v:0", "-map", "0:a:0?"]
        cmd += ["-c", "copy", "-copyts", "-f", "mpegts", path]
        self.errlog = open(path + ".log", "a")
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=self.errlog, text=True)
        self.first = None
        self.seconds = 0.0
        threading.Thread(target=self._progress, daemon=True).start()

    def _progress(self) -> None:
        for line in self.proc.stdout:
            if line.startswith("out_time_us="):
                try:
                    t = int(line.split("=", 1)[1]) / 1e6
                except ValueError:
                    continue
                if self.first is None:
                    self.first = t
                self.seconds = max(self.seconds, t - self.first)

    def running(self) -> bool:
        return self.proc.poll() is None

    def stop(self) -> None:
        if self.running():
            try:
                self.proc.stdin.write("q")
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
            try:
                self.proc.wait(30)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                self.proc.wait(10)
        self.errlog.close()


class LiveJob:
    def __init__(self, args, campaign: dict, layout: dict, url: str):
        self.args, self.campaign, self.layout, self.url = args, campaign, layout, url
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
        name = cc.slug(campaign.get("name", "campaign"), 24)
        root = os.path.normpath(os.path.join(HERE, ".."))
        self.work = os.path.abspath(args.work or os.path.join(root, "footage", f"{name}_live_{stamp}"))
        self.out_root = os.path.abspath(args.out or os.path.join(root, "clips", f"{name}_live_{stamp}"))
        os.makedirs(self.work, exist_ok=True)
        os.makedirs(self.out_root, exist_ok=True)
        self.batch_no = 0
        self.layout_checked = False

    def batch(self, rec_path: str, rec_wall: float, b0: float, b1: float,
              stream_start: float | None) -> None:
        """One stretch [b0, b1) of a recording -> clips. Errors are reported, not fatal,
        so the recording keeps going."""
        self.batch_no += 1
        n = self.batch_no
        w0 = datetime.datetime.fromtimestamp(rec_wall + b0)
        w1 = datetime.datetime.fromtimestamp(rec_wall + b1)
        log(f"batch {n}: {cc.fmt_time(b1 - b0)} recorded {w0:%H:%M}-{w1:%H:%M} -> OpenShorts")
        try:
            self._batch(n, rec_path, rec_wall, b0, b1, w0, w1, stream_start)
        except KeyboardInterrupt:
            raise
        except BaseException as exc:  # clipkit.die() raises SystemExit
            log(f"batch {n} failed: {exc!r}; recording continues")
            notify("Live clips: batch failed", f"Batch {n} ({w0:%H:%M}-{w1:%H:%M}) failed: {exc}")

    def _batch(self, n, rec_path, rec_wall, b0, b1, w0, w1, stream_start) -> None:
        bdir = os.path.join(self.work, f"batch_{n:02d}")
        os.makedirs(bdir, exist_ok=True)
        audio = os.path.join(bdir, "audio.m4a")
        # Same exact cut as the clip renders: open 10 s early and trim on the
        # recording's own clock, so the OpenShorts timeline (and its captions) is the
        # recording's. The sound can start a few seconds after the picture at the
        # very beginning of a recording; that gap is filled with silence.
        seek = max(0.0, b0 - 10.0)
        pre = b0 - seek
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                        "-ss", f"{seek:.3f}", "-t", f"{b1 - b0 + pre + 2:.3f}", "-i", rec_path,
                        "-map", "0:a:0", "-vn",
                        "-af", f"atrim=start={pre:.3f}:duration={b1 - b0:.3f},"
                               f"asetpts=PTS-{pre:.3f}/TB,aresample=async=1:first_pts=0",
                        "-c:a", "aac", "-b:a", "160k", audio], check=True)
        proxy = cc.make_proxy(audio, bdir)
        job_id = cc.submit(self.args.server, self.campaign, proxy)
        log(f"batch {n}: OpenShorts job {job_id}")
        if cc.follow(self.args.server, job_id) != "completed":
            raise RuntimeError(f"OpenShorts job {job_id} did not complete")
        job_dir, meta = cc.load_meta(job_id)
        shorts = meta.get("shorts") or []
        if not shorts:
            log(f"batch {n}: no clip-worthy moments in this stretch")
            notify("Live clips", f"Batch {n} ({w0:%H:%M}-{w1:%H:%M}): no clips this time")
            return
        out = os.path.join(self.out_root, f"batch_{n:02d}_{w0:%H%M}-{w1:%H%M}")
        os.makedirs(out, exist_ok=True)
        notes = [f"Batch {n}: recorded {w0:%d %b %H:%M}-{w1:%H:%M} (your clock). "
                 "Stream times below are approximate (about +-30 s).", ""]
        if self.layout.get("mode") == "stack" and not self.layout_checked:
            for grid, fname in ((True, "_layout_check_grid.jpg"), (False, "_layout_check.jpg")):
                clipkit.cmd_frame(argparse.Namespace(
                    src=rec_path, at=str(b0 + min(30.0, (b1 - b0) / 2)), grid=grid, step=50,
                    out=os.path.join(out, fname), layout=json.dumps({"layout": self.layout})))
            notes += ["Layout check: `_layout_check_grid.jpg` (red box = facecam) and "
                      "`_layout_check.jpg`.", ""]
            self.layout_checked = True
        names = cc.clip_names(shorts)
        cta = (self.campaign.get("cta_text") or "").strip()
        rendered = {}
        for i, short, ass in clipkit.openshorts_clips(job_dir):
            plan = {"_source": Path(rec_path), "_out_dir": Path(out), "_logo": None,
                    "layout": self.layout}
            if cta:
                plan["cta"] = cta
            clip = {"name": names[i], "start": b0 + float(short["start"]),
                    "end": b0 + float(short["end"])}
            if ass:
                clip["captions"] = str(ass)
            # Seeking in the growing .ts can land past the start: open 10 s early and
            # trim exactly; if the picture still starts late, retry with 30 s.
            plan["seek_margin"] = 10.0
            path = clipkit.render_clip(plan, clip)
            gap = stream_gap(path)
            if gap > 0.1:
                log(f"  clip {i}: picture started {gap:.2f} s after the sound; re-rendering")
                plan["seek_margin"] = 30.0
                path = clipkit.render_clip(plan, clip)
                gap = stream_gap(path)
                if gap > 0.1:
                    log(f"  warning: clip {i} still starts {gap:.2f} s late; check it")
            rendered[i] = path
        shutil.rmtree(os.path.join(out, "_work"), ignore_errors=True)
        offset = (rec_wall - stream_start + b0) if stream_start else b0
        cc.collect(job_id, self.campaign, out, max(0.0, offset), self.url, self.layout,
                   rendered, notes)
        log(f"batch {n}: {len(rendered)} clip(s) ready in {out}")
        notify("Live clips ready", f"Batch {n} ({w0:%H:%M}-{w1:%H:%M}): {len(rendered)} clip(s)\n{out}")

    def run(self) -> None:
        args = self.args
        wait_until = time.time() + args.max_wait * 3600
        while True:
            status, info = live_info(self.url, args.cookies)
            if status == "live":
                break
            if not args.wait:
                sys.exit("Nothing is live there right now (use --wait to wait for it).")
            if time.time() > wait_until:
                sys.exit("Gave up waiting (--max-wait).")
            log(f"not live yet; checking again in {args.poll:g} min")
            time.sleep(args.poll * 60)
        deadline = time.time() + args.max_hours * 3600
        title = info.get("title")
        stream_start = info.get("release_timestamp") or info.get("timestamp")
        log(f"LIVE: {title}")
        notify("Live clips", f"Recording started: {title}")

        part, failures, stop = 0, 0, False
        every = args.every * 60
        while not stop:
            part += 1
            formats = pick_formats(info, args.height)
            rec_path = os.path.join(self.work, f"rec_{part:02d}.ts")
            desc = " + ".join(f"{f.get('format_id')} ({f.get('resolution') or f.get('format_note')})"
                              for f in formats)
            rec = Recorder(rec_path, formats)
            rec_wall = time.time()
            log(f"recording part {part} [{desc}] -> {rec_path}")
            b0 = 0.0
            try:
                while rec.running():
                    time.sleep(10)
                    if rec.seconds >= b0 + every + 20:
                        self.batch(rec_path, rec_wall, b0, b0 + every, stream_start)
                        b0 += every
                    if time.time() > deadline:
                        log("reached --max-hours; stopping")
                        stop = True
                        break
            except KeyboardInterrupt:
                log("Ctrl+C: stopping the recording and clipping what is left ...")
                stop = True
            rec.stop()
            total = rec.seconds
            log(f"recording part {part} ended after {cc.fmt_time(total)}")
            if total < 30:
                failures += 1
                if failures >= 5:
                    sys.exit("The recorder keeps failing; see the rec_*.ts.log files.")
            else:
                failures = 0
            if total - b0 >= args.min_last * 60:
                self.batch(rec_path, rec_wall, b0, max(b0, total - 2), stream_start)
            if stop:
                break
            status, info = live_info(self.url, args.cookies)
            if status != "live":
                log("the stream has ended")
                break
            log("recording stopped but the stream is still live; reconnecting")
            time.sleep(min(60, 5 * failures))
        log(f"done: {self.batch_no} batch(es) in {self.out_root}")
        notify("Live clips", f"Finished: {self.batch_no} batch(es) in {self.out_root}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("campaign", help="campaign JSON (campaigns/*.json)")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--channel", help="channel link, e.g. https://www.youtube.com/@SomeStreamer")
    src.add_argument("--url", help="link of a stream that is live now")
    ap.add_argument("--layout", help="layout JSON (file or inline), e.g. layouts/example_stack_cam_right.json")
    ap.add_argument("--cookies", help="cookies.txt for age-restricted streams")
    ap.add_argument("--wait", action="store_true", help="wait until the channel goes live")
    ap.add_argument("--poll", type=float, default=3, help="minutes between live checks (--wait)")
    ap.add_argument("--every", type=float, default=60, help="minutes of stream per batch")
    ap.add_argument("--min-last", type=float, default=5,
                    help="shortest leftover (minutes) still clipped when the stream ends")
    ap.add_argument("--height", type=int, default=1080, help="recording height (720 saves half)")
    ap.add_argument("--max-hours", type=float, default=14,
                    help="stop recording after this many hours")
    ap.add_argument("--max-wait", type=float, default=48,
                    help="with --wait, give up after this many hours without a live stream")
    ap.add_argument("--out", help="folder for the batches (default clips/<campaign>_live_<date>)")
    ap.add_argument("--work", help="folder for recordings (default footage/<campaign>_live_<date>)")
    ap.add_argument("--server", default="http://localhost:8000")
    args = ap.parse_args()

    campaign = json.load(open(args.campaign))
    layout = campaign.get("layout") or {}
    if args.layout:
        layout = clipkit.load_layout(args.layout)
    if not layout:
        log("note: no --layout; clips use the blurred-background layout")
        layout = {"mode": "blur"}
    url = args.url or args.channel.rstrip("/") + "/live"
    LiveJob(args, campaign, layout, url).run()


if __name__ == "__main__":
    main()
