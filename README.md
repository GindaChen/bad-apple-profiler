# Bad Apple!! in a GPU profiler

Bad Apple!! played inside a profiler timeline. Each pixel row is a thread track, time is
the x axis, and every run of black pixels is one slice. Playback is 30 fps in
[Perfetto](https://ui.perfetto.dev), and the same picture can also be opened in NVIDIA
Nsight Systems.

No GPU and no profiling run are needed. The traces are written directly, with exact
timestamps.

```
row 00   ████████████████████            ████████   ← one track = one pixel row
row 01   ██████████████████                ██████
 ...     |<------- frame 0 ------->|<------- frame 1 ------->|   ← time
```

## AWS Neuron Explorer

The [Neuron Explorer demo](neuron/README.md) imports a synthetic trace and
plays it with the original soundtrack at 512×384 and 30 fps. It runs in
Docker on a Mac without Trainium hardware.

## Quick start

You need Python 3.9+ (standard library only), `ffmpeg`, and Google Chrome. This repo
contains no video or audio, so bring your own copy of the video.

```bash
python3 encode.py --video bad_apple.mp4 -o out/badapple      # ~4 s; writes out/badapple.json (+ Nsight files)
python3 play.py out/badapple.json                           # opens Perfetto and plays at 30 fps
```

To try it without a video, `python3 encode.py --test -o out/test` makes a bouncing ball.

## Making the video with music

```bash
python3 play.py out/badapple.json --wait    # loads everything, then holds on frame 0
# start a screen recording of the window, click into it, press Space
python3 align.py recording.mov --audio bad_apple_audio.m4a --source bad_apple.mp4 -o final.mp4
```

`align.py` finds where playback starts in your recording by matching per-frame brightness
against the source video. It searches coarse first, then refines to 1/60 s. It then trims
the recording and muxes the audio in at that offset. On a 3:42 QuickTime recording, the
drift stayed at 17 ms or less through the end of the song.

## How it works

### Encoding (`encode.py`)

- ffmpeg downsamples the video to 64×48 at 30 fps and thresholds it to black and white.
- Frame *f* takes the time window `[f, f+1) × 33.3 ms`. Each row's black runs become
  slices on that row's thread.
- Outputs:
  - `out.json`: a Chrome/Perfetto JSON trace, plus playback metadata.
  - `out.nvtxt`: NVTX push/pop ranges as text.
  - `out.nsys-rep`: built with Nsight Systems' bundled `ImportNvtxt`, if it's installed.
- `--verify` decodes the NVTXT back and checks every frame bit for bit.

The full song at 64×48 comes to 6,572 frames, about 505k slices, and a 40 MB JSON file.

### Playing it in Perfetto (`play.py`)

Perfetto isn't a video player, so `play.py` drives it over the Chrome DevTools protocol.
It launches its own Chrome with a temporary profile, sends the trace in, and calls the
UI's internals:

| Problem | What `play.py` does |
|---|---|
| ui.perfetto.dev can't fetch traces from `localhost` | Sends the bytes over DevTools and calls `app.openTraceFromBuffer` |
| No playback API | Calls `timeline.setVisibleWindow` once per frame from `requestAnimationFrame` |
| Tracks are at least ~24 px tall | Zooms out with DevTools device-metrics emulation. CSS `zoom` breaks Perfetto's canvas layout |
| Slice color is a hash of the name, and `cname` is ignored | Slices are named `" "`, so no label is drawn. A CSS grayscale and threshold filter turns them pure black |
| Half the frames showed **Loading…** boxes | Each track caches only ±1 screen-width of data and refetches outside it. The cache is widened to the whole trace, so every row loads once |

Result: 30 fps on screen, in sync with the wall clock, and no "Loading" draws. This
relies on Perfetto UI internals and was tested on v59.1, so a future release may break it.

Other `play.py` options are `--loop`, `--speed`, `--height`, `--color` (keep Perfetto's
colors), and `--record out.mp4`. `--record` renders frame by frame through DevTools
screenshots, with no screen recorder.

### Nsight Systems

`encode.py` also writes an `.nsys-rep` you can open in the Nsight Systems GUI. Nsight has
no playback or scripting API, so there you zoom to one frame and scroll. Notes from
building it:

- Use `RangePush`/`RangePop`. `RangeStartEnd` ranges ignore the thread and pile up in one
  "Annotations" lane.
- An empty `""` message makes `ImportNvtxt` abort.

`ImportNvtxt` is looked up in `$IMPORT_NVTXT`, then `PATH`, then the standard macOS and
Linux install locations. If it isn't found, the `.nsys-rep` step is skipped.

## Files

| File | |
|---|---|
| `encode.py` | Video → Perfetto JSON, NVTXT, `.nsys-rep` |
| `play.py` | Plays, waits for Space, or records a trace in Perfetto |
| `align.py` | Lines music up with a screen recording |

## Credits

Bad Apple!! is from the Touhou Project by ZUN; the original arrangement is by Alstroemeria
Records, and the shadow-art PV is by Anira. None of that material is included here. Perfetto is
by Google, and Nsight Systems is by NVIDIA.

Code: MIT License.
