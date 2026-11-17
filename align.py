#!/usr/bin/env python3
"""Add the music to a screen recording of play.py, lined up with the picture.

Finds where playback starts in the recording by matching brightness over time: each
recording frame's mean brightness falls as more of the picture is black, and the source
video's per-frame dark fraction gives the expected curve. The best-matching time shift
(to 1/60 s) is where the audio goes. Standard library only; needs ffmpeg.

    python3 align.py recording.mov -o out/badapple_final.mp4
"""
import argparse
import math
import subprocess
import sys

RATE = 60          # analysis rate (frames per second)
W, H = 64, 36      # analysis size for the recording


def gray_frames(path, w, h, rate, extra_vf=""):
    vf = f"fps={rate},scale={w}:{h}:flags=area,format=gray" + extra_vf
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-vf", vf, "-f", "rawvideo", "-"],
                         capture_output=True, check=True).stdout
    n = w * h
    return [raw[i:i + n] for i in range(0, len(raw) - n + 1, n)]


def zdiff(xs):
    """First differences, z-normalised (removes static screen content and scale)."""
    d = [b - a for a, b in zip(xs, xs[1:])]
    mu = sum(d) / len(d)
    sd = math.sqrt(sum((x - mu) ** 2 for x in d) / len(d)) or 1.0
    return [(x - mu) / sd for x in d]


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("recording")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--source", default="video/bad_apple.mp4", help="original video (for timing)")
    ap.add_argument("--audio", default="video/bad_apple_audio.m4a")
    ap.add_argument("--no-trim", action="store_true", help="keep the whole recording")
    ap.add_argument("--pad", type=float, default=0.5, help="seconds kept before/after playback")
    a = ap.parse_args()

    print("analysing source…", flush=True)
    src = [sum(1 for p in f if p < 128) / len(f) for f in gray_frames(a.source, 64, 48, RATE)]
    print("analysing recording…", flush=True)
    rec = [-sum(f) / len(f) for f in gray_frames(a.recording, W, H, RATE)]  # darker = higher
    if len(rec) < RATE * 5:
        sys.exit("recording too short")

    s, r = zdiff(src), zdiff(rec)

    def score(s, r, off):
        i0, i1 = max(0, -off), min(len(s), len(r) - off)
        if i1 - i0 < min(len(s), len(r)) // 2:
            return -2.0
        return sum(s[i] * r[i + off] for i in range(i0, i1)) / (i1 - i0)

    # Coarse search at RATE/K, then refine at full rate around the best coarse shift.
    K = 6
    pool = lambda xs: [sum(xs[i:i + K]) for i in range(0, len(xs) - K + 1, K)]
    sc, rc = pool(s), pool(r)
    coarse = max(range(-len(sc), len(rc)), key=lambda o: score(sc, rc, o))
    off = max(range(coarse * K - 2 * K, coarse * K + 2 * K + 1), key=lambda o: score(s, r, o))
    corr = score(s, r, off)
    start = off / RATE  # recording time where source frame 0 is shown
    dur = len(src) / RATE
    print(f"playback starts at {start:.3f}s in the recording (match score {corr:.2f})")
    if corr < 0.3:
        print("warning: weak match; check that the recording shows the Perfetto window", file=sys.stderr)

    cmd = ["ffmpeg", "-v", "error", "-y"]
    if a.no_trim:
        cmd += ["-i", a.recording, "-itsoffset", f"{start:.3f}", "-i", a.audio,
                "-map", "0:v", "-map", "1:a", "-c:v", "copy"]
    else:
        t0 = max(0.0, start - a.pad)
        cmd += ["-ss", f"{t0:.3f}", "-t", f"{dur + 2 * a.pad:.3f}", "-i", a.recording,
                "-itsoffset", f"{start - t0:.3f}", "-i", a.audio,
                "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-crf", "18", "-preset", "medium",
                "-pix_fmt", "yuv420p"]
    cmd += ["-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", a.out]
    subprocess.run(cmd, check=True)
    print(f"wrote {a.out}")


if __name__ == "__main__":
    main()
