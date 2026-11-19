# Usage

Three scripts in `kit/`:

- **`campaign_clip.py`**: the pipeline. A stream link (or a video file) plus a campaign file
  in, finished clips and `POSTING.md` out.
- **`clipkit.py`**: the renderer (layouts, captions, headline, CTA, loudness, GPU encode). Also
  usable on its own with a plan file.
- **`live_clip.py`**: records a live stream and makes a batch of clips every hour.

## Campaign files (`campaigns/*.json`)

One file per campaign or streamer. See `campaigns/example_streamer_gta6.json` and
`campaigns/example_sponsor.json`.

| Key | Meaning |
|---|---|
| `name` | Shown in `POSTING.md` |
| `brief` | The campaign's rules in plain text. Added to Claude's prompts: windows that break them score 0-10, and titles, hooks and descriptions follow its caption rules |
| `must_say` | Words that must be **spoken inside** every clip (checked on the word-level transcript; a clip is stretched to the nearest mention or dropped) |
| `clips`, `min_seconds`, `max_seconds` | How many clips per window and their length |
| `captions`, `auto_hook` | Word-by-word captions on/off; OpenShorts' auto hook line (keep it off) |
| `cta_text` | Call to action drawn in the last 3.5 s of every clip ("Watch X live on Twitch") |
| `watermark`, `watermark_width`, `watermark_y` | A required sponsor/campaign logo burned in for the whole clip |
| `fix_words` | `{"clicks": "Clix"}`: names Whisper keeps mishearing, fixed in every caption |
| `caption_required` | Text every title/caption must contain; `{"text": ..., "add": ...}` adds it when missing |
| `headline` | `false` turns the headline box off for this campaign |
| `audience_rule`, `posting_checklist`, `posting_notes` | Copied into `POSTING.md` |

## Long streams (`campaign_clip.py --url`)

For streams too long to download in full:

```bash
python3 kit/campaign_clip.py campaigns/<campaign>.json \
  --url "https://www.youtube.com/watch?v=<id>" --layout layouts/<streamer>.json \
  [--from 1:00:00 --to 3:00:00] [--cookies secrets/youtube_cookies.txt]
```

1. Downloads only the audio of the window (about 60 MB per hour) into `footage/<campaign>_<id>/`
   (or `--work`).
2. Gives OpenShorts a black 640x360 stand-in video with that audio: Whisper (GPU) and Claude
   pick the moments and make the captions exactly as for a normal job.
3. Downloads only the picked parts in 1080p (each clip plus a few seconds either side) and
   finds where each starts by matching its audio against the full audio (sub-millisecond).
4. Renders the clips with clipkit (layout, captions, headline, CTA, loudness, GPU encode),
   then writes `POSTING.md` plus `_layout_check_grid.jpg` / `_layout_check.jpg` to confirm
   the facecam box.

Sources:

- **YouTube VODs**, also age-restricted ones with `--cookies` (a cookies.txt exported from a
  browser signed in to an 18+ account; keep it private, use a spare account).
- **Still-live YouTube streams**: read with `--live-from-start`; only the numbered 5-second
  pieces of the window are fetched, up to 2 minutes behind live.
- **Twitch VODs**, also still-live ones: the HLS playlist's 10-second pieces covering the window
  are fetched 8 at a time and trimmed (yt-dlp's seek hangs on 20-hour VODs).
- **A video file** you already cut to the allowed window: pass it instead of `--url`, with
  `--offset <h:mm:ss where it starts in the stream>` so stream times in `POSTING.md` are right.

Tip for finished YouTube VODs: if the audio download crawls, download the whole audio once
(`yt-dlp -f 140`), cut each window with `ffmpeg -ss S -t D -c copy` into `<work>/audio.m4a`, and
write `<work>/audio_window.json` as `{"url": "<the --url>", "start": S, "end": E}`. The kit
reuses it.

## Scan mode and hand-picked clips

The best way to clip a very long stream:

```bash
# 1. scan the stream in 1.5-2 hour windows: lists Claude's picks with stream times, renders nothing
python3 kit/campaign_clip.py campaigns/<c>.json --url "<link>" --from 0:00:00 --to 1:32:00 \
  --layout layouts/<s>.json --work footage/<s>/a --scan          # -> footage/<s>/a/scan.json

# 2. read the picks (and the transcript in openshorts/output/<job>/), choose the best moment

# 3. render exactly that range, with the same --from/--to and --work as the scan
python3 kit/campaign_clip.py campaigns/<c>.json --url "<link>" --from 0:00:00 --to 1:32:00 \
  --work footage/<s>/a --job <job id> --pick 1:12:03.5-1:12:24 \
  --layout layouts/<s>.json --out clips/<folder>
```

- Join ranges with `+` for a setup + payoff clip: `--pick "A-B+C-D"` (parts are rendered and
  joined; the call to action only on the last part).
- Titles, descriptions, Instagram captions and the headline come from
  `posting_overrides.json` in the `--out` folder:
  `{"1": {"title": "...", "description": "...", "instagram": "...", "headline": "BRO ..."}}`.
- `--fix-word OLD=NEW` (or `--fix-word 10:40:14.06=new` for one word at a stream time) fixes
  misheard caption words for one run.
- Whisper also transcribes game characters' dialogue. Check the game's subtitles in the frames
  before writing a title about what the streamer said.
- For Twitch, the channel's most-viewed clips (`twitch.tv/<name>/clips?range=24hr`) point at the
  best moments: a clip's creation time minus the VOD's start time is its spot in the VOD.

`--job <id>` without `--pick` re-renders an earlier job's own picks (after a layout fix,
say) without asking Claude again.

## Live mode (`live_clip.py`)

```bash
python3 kit/live_clip.py campaigns/<campaign>.json \
  --channel https://www.youtube.com/@SomeStreamer --wait \
  --layout layouts/<streamer>.json [--cookies secrets/youtube_cookies.txt]
```

- `--wait` checks the channel's live page every 3 minutes (`--poll`) until it goes live.
- The stream is recorded as it plays (`--height 1080`; 720 halves bandwidth and disk) into
  `footage/<campaign>_live_<date>/rec_NN.ts`, keeping YouTube's own timestamps.
- Every `--every 60` minutes of recording, that stretch goes through OpenShorts + Claude with
  the campaign brief, and clipkit renders the picks into
  `clips/<campaign>_live_<date>/batch_NN_HHMM-HHMM/` with its own `POSTING.md`.
- When the stream ends, on Ctrl+C or after `--max-hours`, the rest becomes a last batch.
- Needs the PC on and online for the whole stream: about 1.5 MB/s and 5 GB of disk per hour at
  1080p60.

## Layouts (`layouts/*.json`)

### Stack: facecam over gameplay

```
 y=0     ┌──────── facecam (face_box, scaled to fill) ───────┐
 y~620   ├──────── divider ──────── HEADLINE BOX ────────────┤
           captions just under the headline
         │         gameplay (9:16 slice of the frame,        │
         │         moved away from the facecam automatically) │
 y=1920  └───────────────────────────────────────────────────┘
```

1. **Measure the streamer's facecam once** (it stays in the same place on their stream):
   `python3 kit/clipkit.py frame <footage>.mp4 --at 120 --grid --out grid.jpg`. Read its
   width, height, left and top off the grid (1080p source pixels).
2. Save `{"layout": {"mode": "stack", "face_box": "w:h:x:y"}}` as `layouts/<streamer>.json`.
3. Check it: `--grid --layout layouts/<streamer>.json` draws the box in red; `--layout` alone
   renders one 9:16 preview frame. The same streamer can move the camera between streams, so
   check again on a new stream.

| Option | Meaning |
|---|---|
| `face_box` | Facecam in source pixels `w:h:x:y` (required) |
| `face_h` | Facecam panel height in output px (default from its shape, 320-1100); match the box's shape to avoid cropping it |
| `game_box` | Part of the frame used as gameplay `w:h:x:y` (default whole frame) |
| `game_x` / `game_zoom` | Horizontal position 0..1 / zoom of the gameplay slice (default centred, moved off the facecam) |
| `face_position` | `top` (default) or `bottom` |
| `divider` | Line between panels in px (default 6, 0 = none) |
| `caption_y` | Where caption lines end, 0..1 of the height |

### Blur

The whole 16:9 frame in the middle over a blurred copy (`"mode": "blur"`, optional `zoom`).
For streams without a facecam.

### Shared options

| Option | Meaning |
|---|---|
| `cta_box`, `cta_y`, `cta_size`, `cta_wrap`, `cta_pad` | Call-to-action box colour (ffmpeg colour, e.g. `0x9146FF@0.92`), position 0..1, size |
| `logo_y`, `logo_width` | Sponsor/campaign overlay position and size (px on 1080x1920) |
| `headline_size` (70), `headline_wrap` (19 chars, max 3 lines), `headline_pad`, `headline_y`, `headline_until_cta` | Headline box |
| `loudness` | Target LUFS (default -14); `null` turns levelling off |

## Headline box

A big white box with bold black capitals (Montserrat ExtraBold), the style of the biggest clip
pages. Text comes from `"headline"` in `posting_overrides.json` (a short, accurate meme-style
line, no emoji), else from the clip title without hashtags. In stack layouts it sits on the
seam between facecam and gameplay, captions move just under it, and it gives way to the call
to action in the last seconds. Line breaks are balanced and "GTA 6" never splits.

## Loudness

Each clip's loudness is measured and a fixed gain brings it to -14 LUFS (what Shorts and
Reels play at; they never turn a quiet clip up), with a peak limiter.

## clipkit on its own

```bash
python3 kit/clipkit.py probe  SRC.mp4                        # duration, size, fps, audio
python3 kit/clipkit.py scenes SRC.mp4 --out work/scenes      # scene cuts + contact sheets
python3 kit/clipkit.py render plan.json [--only NAME]        # render clips listed in a plan
python3 kit/clipkit.py still  plan.json --only NAME --at 2   # preview frame
python3 kit/clipkit.py frame  SRC.mp4 --at 120 --grid --out grid.jpg
python3 kit/clipkit.py restack openshorts/output/<job> --source SRC.mp4 \
  --layout layouts/<streamer>.json --out clips/<folder>     # re-render an OpenShorts job
```

A plan file lists clips (`{"name": "01_fight", "start": "1:02.5", "end": "1:21", "text": "..."}`)
with `source`, `out_dir`, `logo`, `layout` and optional `crf`, `fps`, `audio_fade`. Renders
encode on the GPU (`h264_nvenc`); `"encoder": "x264"` forces the CPU.

## POSTING.md

Every batch gets a `POSTING.md`: per clip, the YouTube title, YouTube description and
Instagram caption (with the campaign's required text and hashtags), stream times, why it was
picked, and the campaign's checklist. `python3 kit/repost_md.py campaigns/<c>.json
clips/<folder> --job <id>` rewrites it after you edit `posting_overrides.json`, without
re-rendering.
