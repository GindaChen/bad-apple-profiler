#!/usr/bin/env python3
"""Turn a black-and-white video into profiler traces (Perfetto JSON + Nsight NVTXT).

One thread row = one pixel row, time = the x axis, each run of pixels = one slice/range.
Writes <out>.json (Perfetto), <out>.nvtxt and, if Nsight Systems' ImportNvtxt is found,
<out>.nsys-rep. Standard library only; --video needs ffmpeg. See README.md.
"""
import argparse
import glob
import json
import math
import os
import re
import shutil
import subprocess
import sys


def find_import_nvtxt():
    """Nsight Systems' ImportNvtxt: $IMPORT_NVTXT, PATH, or a standard install location."""
    cands = [os.environ.get("IMPORT_NVTXT"), shutil.which("ImportNvtxt"),
             "/Applications/NVIDIA Nsight Systems.app/Contents/MacOS/ImportNvtxt"]
    cands += sorted(glob.glob("/opt/nvidia/nsight-systems/*/host-linux-x64/ImportNvtxt"), reverse=True)
    cands += sorted(glob.glob("/usr/local/cuda*/nsight-systems*/host-linux-x64/ImportNvtxt"), reverse=True)
    return next((c for c in cands if c and os.path.isfile(c)), None)


PID = 1
FRAME_TID = 10      # marker row, sorts above pixel rows
ROW_TID0 = 100      # pixel row r -> thread ROW_TID0 + r
LIT_COLOR = "0xFFFFFFFF"
T0_NS = 1_000_000   # start the timeline at 1 ms


# ---------- frame sources: each yields list[list[bool]] (rows x cols) ----------

def test_frames(cols, rows, n=90):
    """A bouncing ball: a built-in source that needs no video file."""
    r = rows * 0.22
    for f in range(n):
        t = f / n
        cx = r + (cols - 2 * r) * (0.5 - 0.5 * math.cos(2 * math.pi * t))
        cy = r + (rows - 2 * r) * abs(math.sin(3 * math.pi * t))
        yield [[(x + .5 - cx) ** 2 + (y + .5 - cy) ** 2 <= r * r for x in range(cols)]
               for y in range(rows)]


def video_frames(path, cols, rows, fps, threshold):
    cmd = ["ffmpeg", "-v", "error", "-i", path,
           "-vf", f"fps={fps},scale={cols}:{rows}:flags=area,format=gray",
           "-f", "rawvideo", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    size = cols * rows
    try:
        while True:
            buf = proc.stdout.read(size)
            if len(buf) < size:
                break
            yield [[buf[y * cols + x] >= threshold for x in range(cols)] for y in range(rows)]
    finally:
        proc.stdout.close()
        proc.terminate()
        proc.wait()


# ---------- encode / decode ----------

def col_ns(f, c, frame_ns, cols):
    return T0_NS + f * frame_ns + c * frame_ns // cols


def runs(row):
    """Yield (start, end) column spans of True values."""
    c, n = 0, len(row)
    while c < n:
        if row[c]:
            s = c
            while c < n and row[c]:
                c += 1
            yield s, c
        else:
            c += 1


def write_nvtxt(frames, path, cols, rows, fps, title):
    frame_ns = round(1e9 / fps)
    kept, events = [], 0
    with open(path, "w") as out:
        w = out.write
        w(f'SetFileDisplayName, "{title}"\n')
        w(f'NameProcess, {PID}, "{title}"\n')
        w(f"TimeBase = Manual\nProcessId = {PID}\nCategoryId = 0\nPayload = 0\n")
        w(f'NameOsThread, {PID}, {FRAME_TID}, "frames"\n')
        for r in range(rows):
            w(f'NameOsThread, {PID}, {ROW_TID0 + r}, "row {r:02d}"\n')
        w("@Marker, Time, ThreadId, Color, Message\n")
        # Push/pop ranges are tied to a thread row; RangeStartEnd ranges are not
        # (Nsight stacks those in one shared "Annotations" lane).
        w("@RangePush, Time, ThreadId, Color, Message\n")
        w("@RangePop, Time, ThreadId\n")
        for f, frame in enumerate(frames):
            kept.append(frame)
            w(f'Marker, {col_ns(f, 0, frame_ns, cols)}, {FRAME_TID}, 0xFF76B900, "frame {f}"\n')
            for r, row in enumerate(frame):
                for s, e in runs(row):
                    tid = ROW_TID0 + r
                    # empty "" message crashes ImportNvtxt
                    w(f'RangePush, {col_ns(f, s, frame_ns, cols)}, {tid}, {LIT_COLOR}, "px"\n')
                    w(f"RangePop, {col_ns(f, e, frame_ns, cols)}, {tid}\n")
                    events += 1
    return kept, events


def write_perfetto(frames, path, cols, rows, fps, title):
    """Same pixel layout as the NVTXT file, as Chrome/Perfetto JSON (ts/dur in microseconds).

    Slices are named " " so Perfetto draws no label. There is no frames track here:
    play.py takes the frame timing from the metadata instead.
    """
    frame_ns = round(1e9 / fps)
    us = lambda ns: round(ns / 1000, 3)
    ev = [{"ph": "M", "name": "process_name", "pid": PID, "args": {"name": title}}]
    for r in range(rows):
        tid = ROW_TID0 + r
        ev.append({"ph": "M", "name": "thread_name", "pid": PID, "tid": tid, "args": {"name": f"row {r:02d}"}})
        ev.append({"ph": "M", "name": "thread_sort_index", "pid": PID, "tid": tid, "args": {"sort_index": r}})
    for f, frame in enumerate(frames):
        for r, row in enumerate(frame):
            for s, e in runs(row):
                a, b = col_ns(f, s, frame_ns, cols), col_ns(f, e, frame_ns, cols)
                ev.append({"ph": "X", "name": " ", "pid": PID, "tid": ROW_TID0 + r,
                           "ts": us(a), "dur": us(b - a)})
    with open(path, "w") as fh:
        meta = {"fps": fps, "frames": len(frames), "t0_ns": T0_NS, "frame_ns": frame_ns, "rows": rows, "cols": cols}
        json.dump({"traceEvents": ev, "displayTimeUnit": "ms", "metadata": {"badapple": meta}},
                  fh, separators=(",", ":"))


PUSH = re.compile(r"^RangePush, (\d+), (\d+),")
POP = re.compile(r"^RangePop, (\d+), (\d+)")


def decode_nvtxt(path, cols, rows, fps, nframes):
    frame_ns = round(1e9 / fps)
    frames = [[[False] * cols for _ in range(rows)] for _ in range(nframes)]
    edges = [col_ns(0, c, frame_ns, cols) - T0_NS for c in range(cols + 1)]
    lookup = {v: c for c, v in enumerate(edges)}
    open_at = {}
    with open(path) as fh:
        for line in fh:
            m = PUSH.match(line)
            if m:
                open_at[int(m[2])] = int(m[1])
                continue
            m = POP.match(line)
            if not m:
                continue
            e, tid = int(m[1]), int(m[2])
            s = open_at.pop(tid)
            f = (s - T0_NS) // frame_ns
            off = f * frame_ns + T0_NS
            c0, c1 = lookup[s - off], lookup[e - off] if e - off in lookup else cols
            for c in range(c0, c1):
                frames[f][tid - ROW_TID0][c] = True
    return frames


def write_pgm(frame, path, scale=8):
    rows, cols = len(frame), len(frame[0])
    with open(path, "wb") as fh:
        fh.write(f"P5 {cols * scale} {rows * scale} 255\n".encode())
        for row in frame:
            line = bytes(255 if v else 0 for v in row for _ in range(scale))
            fh.write(line * scale)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--video", help="input video (needs ffmpeg)")
    src.add_argument("--test", action="store_true", help="built-in bouncing-ball animation")
    ap.add_argument("-o", "--out", required=True, help="output path prefix")
    ap.add_argument("--cols", type=int, default=64)
    ap.add_argument("--rows", type=int, default=48)
    ap.add_argument("--fps", type=float, default=30)
    ap.add_argument("--threshold", type=int, default=128)
    ap.add_argument("--invert", action="store_true")
    ap.add_argument("--max-frames", type=int)
    ap.add_argument("--verify", action="store_true")
    a = ap.parse_args()

    if a.test:
        frames = test_frames(a.cols, a.rows)
        title = "badapple-test"
    else:
        frames = video_frames(a.video, a.cols, a.rows, a.fps, a.threshold)
        title = os.path.splitext(os.path.basename(a.video))[0]
    if a.invert:
        frames = ([[not v for v in row] for row in fr] for fr in frames)
    if a.max_frames:
        frames = (fr for i, fr in enumerate(frames) if i < a.max_frames)

    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    nvtxt, rep = a.out + ".nvtxt", a.out + ".nsys-rep"
    kept, events = write_nvtxt(frames, nvtxt, a.cols, a.rows, a.fps, title)
    print(f"wrote {nvtxt}: {len(kept)} frames, {events} ranges")
    # Perfetto: dark pixels become slices, which play.py draws black on the white
    # timeline (silhouette on white). Nsight keeps lit pixels: white on its dark theme.
    dark = [[[not v for v in row] for row in fr] for fr in kept]
    write_perfetto(dark, a.out + ".json", a.cols, a.rows, a.fps, title)
    print(f"wrote {a.out}.json (open in ui.perfetto.dev)")

    if a.verify:
        got = decode_nvtxt(nvtxt, a.cols, a.rows, a.fps, len(kept))
        bad = sum(g != k for g, k in zip(got, kept))
        write_pgm(got[0], a.out + ".frame0.pgm")
        print(f"verify: {len(kept) - bad}/{len(kept)} frames round-trip exactly")
        if bad:
            sys.exit(1)

    tool = find_import_nvtxt()
    if not tool:
        print("skipped .nsys-rep: ImportNvtxt not found (install Nsight Systems or set $IMPORT_NVTXT)")
        return
    if os.path.exists(rep):
        os.remove(rep)
    res = subprocess.run([tool, "--cmd", "create", "-n", nvtxt, "-o", rep],
                         capture_output=True, text=True)
    print(res.stdout.strip().splitlines()[-1] if res.stdout.strip() else res.stderr.strip())
    if res.returncode or not os.path.exists(rep):
        sys.exit(f"ImportNvtxt failed ({res.returncode}): {res.stderr.strip()}")
    print(f"wrote {rep}")


if __name__ == "__main__":
    main()
