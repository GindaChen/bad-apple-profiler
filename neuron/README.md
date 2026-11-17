# Bad Apple!! in AWS Neuron Explorer

This demo imports one synthetic framework trace into AWS Neuron Explorer and
plays its System Timeline at 512×384 and 30 fps. It uses the Linux Explorer
binary in Docker, so the browser can run on a Mac without Trainium hardware or
an AWS account.

## Run it

Install Docker (with Linux AMD64 support), [uv](https://docs.astral.sh/uv/),
and FFmpeg. Allow about 1 GB of free space for the full trace and its Docker
copy, plus space for the Docker image. On a Mac with Homebrew, `brew install uv ffmpeg` installs the two
command-line tools.

From the repo root:

```bash
uv run --script neuron/demo.py install
uv run --script neuron/demo.py prepare video/bad_apple.mp4 --audio video/bad_apple_audio.m4a
uv run --script neuron/demo.py serve
```

Use a local copy of the [shadow animation with the original soundtrack](https://www.youtube.com/watch?v=i41KoE0iMYU).
If your video already contains that audio, omit `--audio`. If you supply
separate files, use the picture and soundtrack from the same cut, both
starting at the same point. Add `--seconds 10` to `prepare` for a quick test.

Open [the player](http://localhost:3013/profile/bad-apple-512x384) and click
**Play with music**. Expand the **System Timeline** widget if your window
shows only the top rows. The source copy used for the recording has 6,572
frames at 30 fps (3:39). The soundtrack is the playback clock. A slow
browser can miss redraws while the next frame still follows the music.

The installer fetches the pinned Linux AMD64 packages from AWS and checks
their SHA-256 digests. The Explorer backend uses ports 3011 and 3012.
The player uses 3013, and the inspection route uses 3014. All four listen
on localhost. If those ports are busy, set `NEURON_DEMO_PORT` to the first
of four free ports for all three commands. Use the URLs printed by `serve`.
Stop the proxy with Ctrl+C and stop Explorer with
`docker stop bad-apple-neuron-explorer` (append the base port when using a
nondefault port).
Generated data goes in the ignored `out/neuron/` directory. The repo does
not include the video, soundtrack, generated traces, or AWS binaries.

## Try it without Bad Apple media

After `install`, make a short test clip with its own audio and run the same
pipeline. To switch back, stop `serve` and run `prepare` again with Bad Apple.

```bash
mkdir -p out
ffmpeg -f lavfi -i 'testsrc2=size=480x360:rate=30:duration=2' \
  -f lavfi -i 'sine=frequency=440:duration=2' \
  -c:v libx264 -pix_fmt yuv420p -c:a aac -shortest out/neuron-test.mp4
uv run --script neuron/demo.py prepare out/neuron-test.mp4
uv run --script neuron/demo.py serve
```

The player should show a two-second timeline and play the test tone.
The inspection link opens the imported trace without the music player.

## Inspect the trace

1. Open [the same profile in stock Explorer](http://localhost:3014/profile/bad-apple-512x384).
   This port serves stock Explorer HTML and assets. It configures the local
   API port and does not inject the player or change the timeline renderer.
   The System Timeline shows framework tracks for thread IDs 101 through 484.
   Zoom into a short time range and click an event to inspect its timestamp
   and duration.
2. In `out/neuron/raw/i-synthetic_pid_100/trace.json`, each Chrome complete
   event is a white span. Thread ID selects the pixel row. Within the frame's
   33⅓ ms window, the timestamp and duration select the horizontal position
   and width. The session files identify the trace as synthetic.
3. Explorer ingests those files and writes `SystemProfileEvents.parquet`.
   The `prepare` command copies that table out of Explorer and builds
   `timeline.bin.gz` from its imported timestamps and thread IDs. It checks
   that the number of image spans matches the encoder's count.

The playback server serves the packed spans and an audio-only file. It does
not serve the input video. The browser player uses the audio clock to select
one frame's span events, sends those events to the official System Timeline's
track objects, and asks Explorer to draw its own canvas. In the browser
console on the player page, `document.querySelectorAll('video').length`
returns zero.

The player uses version-specific Explorer internals to set the visible time
window, fit the rows, and preload the spans. The stock trace remains
inspectable independently. Smooth motion comes from loading the trace
spans once and avoiding a server query on every frame.
