#!/usr/bin/env python3
"""Rewrite POSTING.md for an already-rendered clips folder (no re-render).

    python3 kit/repost_md.py campaigns/<campaign>.json clips/<campaign>/<chunk> \
        --job <openshorts job id> [--offset 7:40:00]

Picks up posting_overrides.json in the folder (reviewed titles, holds, notes).
"""
import argparse
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import campaign_clip as cc  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("campaign")
    ap.add_argument("out_dir")
    ap.add_argument("--job", required=True)
    ap.add_argument("--offset", default="0", help="where this chunk starts in the stream")
    args = ap.parse_args()
    campaign = json.load(open(args.campaign))
    out_dir = os.path.abspath(args.out_dir)
    _, meta = cc.load_meta(args.job)
    names = cc.clip_names(meta.get("shorts") or [])
    rendered = {i: os.path.join(out_dir, n + ".mp4") for i, n in names.items()
                if os.path.exists(os.path.join(out_dir, n + ".mp4"))}
    notes = []
    if glob.glob(os.path.join(out_dir, "_layout_check*.jpg")):
        notes = ["Layout check: `_layout_check_grid.jpg` (red box = facecam) and "
                 "`_layout_check.jpg` (how the clips are framed).", ""]
    cc.collect(args.job, campaign, out_dir, cc.parse_time(args.offset), "", None, rendered, notes)


if __name__ == "__main__":
    main()
