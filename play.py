#!/usr/bin/env python3
"""Auto-play a badapple trace in Perfetto (ui.perfetto.dev).

Launches a separate Chrome window (temporary profile), sends it the trace over the
DevTools protocol, then shows exactly one frame of the timeline per 1/fps seconds.
Standard library only.

    python3 play.py out/test.json [--loop] [--speed 1.0]
    python3 play.py out/test.json --record out/test.mp4   # frame-exact video
"""
import argparse
import base64
import json
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request

def find_chrome():
    """Chrome/Chromium: $CHROME, a standard macOS install, or PATH."""
    cands = [os.environ.get("CHROME"), "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
             "/Applications/Chromium.app/Contents/MacOS/Chromium"]
    cands += [shutil.which(n) for n in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")]
    return next((c for c in cands if c and os.path.isfile(c)), None)

PERFETTO = "https://ui.perfetto.dev/#!/"


# ---------- minimal websocket client (enough for the DevTools protocol) ----------

class WS:
    def __init__(self, url):
        host_port, path = url[len("ws://"):].split("/", 1)
        host, port = host_port.split(":")
        self.s = socket.create_connection((host, int(port)))
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall((f"GET /{path} HTTP/1.1\r\nHost: {host_port}\r\nUpgrade: websocket\r\n"
                        f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                        "Sec-WebSocket-Version: 13\r\n\r\n").encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            resp += self.s.recv(4096)
        if b" 101 " not in resp.split(b"\r\n", 1)[0]:
            raise RuntimeError(f"websocket upgrade failed: {resp[:200]!r}")
        self.buf = resp.split(b"\r\n\r\n", 1)[1]

    def send(self, text):
        data = text.encode()
        n = len(data)
        head = bytes([0x81])
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 1 << 16:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        self.s.sendall(head + mask + _xor(data, mask))

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.s.recv(1 << 16)
            if not chunk:
                raise ConnectionError("devtools socket closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv(self):
        msg = b""
        while True:
            b0, b1 = self._read(2)
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            msg += self._read(n)
            if b0 & 0x80:
                return msg.decode()


def _xor(data, mask):
    """Apply the 4-byte websocket mask (fast path via big ints)."""
    n = len(data)
    if n == 0:
        return b""
    m = (mask * (n // 4 + 1))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(m, "big")).to_bytes(n, "big")


class DevTools:
    def __init__(self, ws_url):
        self.ws, self.next_id = WS(ws_url), 0

    def call(self, method, params=None):
        self.next_id += 1
        self.ws.send(json.dumps({"id": self.next_id, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self.next_id:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    def eval(self, expr, await_promise=False):
        self.next_id += 1
        self.ws.send(json.dumps({"id": self.next_id, "method": "Runtime.evaluate",
                                 "params": {"expression": expr, "awaitPromise": await_promise,
                                            "returnByValue": True}}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self.next_id:
                res = msg.get("result", {})
                if "exceptionDetails" in res:
                    raise RuntimeError(res["exceptionDetails"].get("exception", {}).get("description"))
                return res.get("result", {}).get("value")


def wait_for(fn, what, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        try:
            v = fn()
            if v:
                return v
        except Exception:
            pass
        time.sleep(0.3)
    sys.exit(f"timed out waiting for {what}")


# ---------- page-side view setup and playback loop ----------

TITLEBAR_PX = 30     # window title bar above the page, in screen px
HEADER_CSS_PX = 250  # Perfetto search bar, overview, time axis and bottom bar, in CSS px
NAMES_CSS_PX = 265   # track-name column plus scrollbar, in CSS px
TRACK_CSS_PX = 24    # height of one thread track (Perfetto won't go lower)

# Perfetto tracks have a fixed height, so more rows means zooming the whole page out.
# CSS zoom confuses Perfetto's canvas layout, so instead the player uses DevTools
# device-metrics emulation: Perfetto lays out a larger viewport and Chrome scales the
# rendered page down to fit the window.
# The black-and-white look is a CSS filter on the timeline canvases: grayscale, darken
# so every slice color falls below 50% while light grid lines stay above, then a steep
# contrast step to threshold. Slices (dark pixels) end up black on the white page.
VIEW_JS = r"""
(() => {
  if (app.sidebar.visible) app.sidebar.toggleVisibility();
  // Hide (not accept) the cookie notice: it covers the bottom-left of the picture.
  const hideCookies = () => {
    for (const el of document.querySelectorAll('body *'))
      if (el.childElementCount < 6 && /^This site uses cookies/.test(el.textContent || ''))
        (el.closest('[class*="cookie"]') || el.parentElement || el).style.display = 'none';
  };
  hideCookies(); setTimeout(hideCookies, 2000); setTimeout(hideCookies, 6000);
  if (%(bw)s) {
    const st = document.createElement('style');
    st.textContent = `.pf-virtual-overlay-canvas__canvas-container canvas {
      filter: grayscale(1) brightness(0.58) contrast(60); }`;
    document.head.appendChild(st);
  }
  return true;
})()
"""

# "Loading..." fix. Each Perfetto slice track caches data only for the visible window
# plus one window either side, and refetches (painting a gray "Loading..." box until
# the data lands) whenever the view leaves it. Stepping one frame at a time left the
# cache every other frame. Here the cache is widened to the whole trace, so each row
# is fetched once at the frame zoom level and never again. The "Loading..." text is
# also dropped for the one-time initial fetch.
CACHE_JS = r"""
(() => {
  const P = CanvasRenderingContext2D.prototype;
  if (!P.__badappleOrig) {
    P.__badappleOrig = P.fillText;
    P.fillText = function (txt, ...rest) {
      if (typeof txt === 'string' && txt.startsWith('Loading')) { window.__badappleLoadingDraws = (window.__badappleLoadingDraws || 0) + 1; return; }
      return P.__badappleOrig.call(this, txt, ...rest);
    };
  }
  const start = BigInt(app.trace.traceInfo.start), end = BigInt(app.trace.traceInfo.end);
  const protos = new Set();
  for (const t of app.trace.tracks.getAllTracks())
    if (t.renderer && t.renderer.bufferedBounds) protos.add(Object.getPrototypeOf(t.renderer.bufferedBounds));
  for (const proto of protos) {
    if (proto.__badappleOrig) continue;
    proto.__badappleOrig = proto.update;
    proto.update = function (vis, res) {
      if (vis.start < this.loadedStart || vis.end > this.loadedEnd || res !== this.loadedResolution) {
        const r = BigInt(res);
        this.loadedStart = (start / r) * r;
        this.loadedEnd = ((end + r - 1n) / r) * r;
        this.loadedResolution = res;
      }
      return { start: this.loadedStart, end: this.loadedEnd, resolution: this.loadedResolution };
    };
  }
  return protos.size;
})()
"""

# Playback: show the frame that matches wall-clock time.
PLAYER_JS = r"""
(() => {
  const m = %(meta)s, speed = %(speed)s, loop = %(loop)s, waitKey = %(wait)s;
  const tl = app.trace.timeline;
  const Span = tl.visibleWindow.constructor, HpTime = tl.visibleWindow.start.constructor;
  const show = (f) => tl.setVisibleWindow(
      new Span(new HpTime(BigInt(m.t0_ns + f * m.frame_ns), 0), m.frame_ns));
  if (window.__badapple) cancelAnimationFrame(window.__badapple.raf);
  const st = window.__badapple = { t0: performance.now(), raf: 0, presented: 0, frame: -1 };
  const tick = (now) => {
    let f = Math.floor((now - st.t0) / 1000 * m.fps * speed);
    if (f >= m.frames) {
      if (!loop) { st.done = true; return; }
      st.t0 = now; f = 0;
    }
    if (f !== st.frame) { st.presented++; st.frame = f; show(f); }
    st.raf = requestAnimationFrame(tick);
  };
  const start = () => { st.t0 = performance.now(); st.started = Date.now(); st.raf = requestAnimationFrame(tick); };
  if (!waitKey) { start(); return true; }
  // Hold on frame 0 until Space is pressed in the window (handy for screen recording).
  show(0); st.waiting = true;
  const onKey = (e) => {
    if (e.code !== 'Space') return;
    e.preventDefault(); e.stopPropagation();
    window.removeEventListener('keydown', onKey, true);
    st.waiting = false; start();
  };
  window.addEventListener('keydown', onKey, true);
  return true;
})()
"""


# Recording: step through frames one at a time and wait until Perfetto has nothing
# left to redraw before each screenshot, so every video frame is exactly one complete
# timeline frame (no matter how long a redraw takes).
RECORD_SETUP_JS = r"""
(() => {
  const m = %(meta)s, tl = app.trace.timeline;
  const Span = tl.visibleWindow.constructor, HpTime = tl.visibleWindow.start.constructor;
  const nextFrame = () => new Promise((r) => requestAnimationFrame(() => r()));
  window.__badappleRender = async (f) => {
    tl.setVisibleWindow(new Span(new HpTime(BigInt(m.t0_ns + f * m.frame_ns), 0), m.frame_ns));
    const loads = () => window.__badappleLoadingDraws || 0;
    // Done when Perfetto has no redraw pending and the last two passes drew no
    // (hidden) "Loading" box, i.e. every row has its data on screen.
    for (let i = 0; i < 600; i++) {
      const before = loads();
      await nextFrame(); await nextFrame();
      if (!app.trace.raf.hasPendingRedraws && loads() === before) break;
    }
    return loads();
  };
  return true;
})()
"""


def record(dt, meta, out, max_frames, height, audio):
    """Render every frame and encode them with ffmpeg as an MP4."""
    n = min(meta["frames"], max_frames or meta["frames"])
    cw, ch, dpr = dt.eval("[innerWidth, innerHeight, devicePixelRatio]")
    clip = {"x": 0, "y": 0, "width": cw, "height": ch, "scale": height / ch / dpr}
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "image2pipe", "-framerate", str(meta["fps"]),
           "-c:v", "mjpeg", "-i", "-"]
    if audio:
        cmd += ["-i", audio, "-map", "0:v", "-map", "1:a", "-c:a", "aac", "-b:a", "192k", "-shortest"]
    cmd += ["-vf", "scale=-2:" + str(height) + ":flags=lanczos", "-c:v", "libx264",
            "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p", "-movflags", "+faststart", out]
    enc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    t0 = time.time()
    for f in range(n):
        dt.eval(f"window.__badappleRender({f})", await_promise=True)
        shot = dt.call("Page.captureScreenshot", {"format": "jpeg", "quality": 92, "clip": clip,
                                                  "captureBeyondViewport": False})
        enc.stdin.write(base64.b64decode(shot["data"]))
        if (f + 1) % 300 == 0 or f + 1 == n:
            el = time.time() - t0
            print(f"  {f + 1}/{n} frames, {el:.0f}s elapsed, ~{el / (f + 1) * (n - f - 1):.0f}s left",
                  flush=True)
    enc.stdin.close()
    if enc.wait():
        sys.exit("ffmpeg failed")
    loading = dt.eval("window.__badappleLoadingDraws || 0")
    print(f"wrote {out}: {n} frames at {meta['fps']} fps ({n / meta['fps']:.1f}s), "
          f"{loading} 'Loading' draws suppressed")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("trace", help="JSON trace written by encode.py")
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--port", type=int, default=9333)
    ap.add_argument("--height", type=int, default=920, help="Chrome window height in px")
    ap.add_argument("--zoom", type=float, help="page zoom (default: fit all rows in the window)")
    ap.add_argument("--color", action="store_true",
                    help="keep Perfetto's slice colors instead of the black-and-white filter")
    ap.add_argument("--wait", action="store_true", help="hold on frame 0 until Space is pressed")
    ap.add_argument("--record", metavar="MP4", help="render every frame to this MP4 instead of playing live")
    ap.add_argument("--max-frames", type=int, help="with --record: stop after N frames")
    ap.add_argument("--video-height", type=int, default=1080, help="with --record: output height in px")
    ap.add_argument("--audio", help="with --record: audio file to mux into the MP4")
    a = ap.parse_args()

    raw = open(a.trace, "rb").read()
    meta = json.loads(raw).get("metadata", {}).get("badapple")
    if not meta:
        sys.exit("trace has no badapple metadata; regenerate it with encode.py")

    # Zoom so every row fits the window height, then pick the width that keeps pixels square.
    rows, cols = meta["rows"], meta.get("cols", meta["rows"] * 4 // 3)
    need_h, need_w = HEADER_CSS_PX + TRACK_CSS_PX * rows, NAMES_CSS_PX + TRACK_CSS_PX * cols
    zoom = a.zoom or min(1.0, (a.height - TITLEBAR_PX) / need_h)
    size = f"{round(need_w * zoom)},{a.height}"

    # Treat SIGTERM like Ctrl-C so the Chrome window is always cleaned up.
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    chrome_bin = find_chrome()
    if not chrome_bin:
        sys.exit("Chrome not found; install Google Chrome or set $CHROME")
    profile = tempfile.mkdtemp(prefix="badapple-chrome-")
    chrome = subprocess.Popen([chrome_bin, f"--remote-debugging-port={a.port}", f"--user-data-dir={profile}",
                               "--no-first-run", "--no-default-browser-check",
                               # keep rendering at full speed even if the window is covered
                               "--disable-background-timer-throttling",
                               "--disable-renderer-backgrounding",
                               "--disable-backgrounding-occluded-windows",
                               f"--window-size={size}", f"--app={PERFETTO}"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        def page():
            with urllib.request.urlopen(f"http://127.0.0.1:{a.port}/json") as r:
                return next((t["webSocketDebuggerUrl"] for t in json.load(r)
                             if t.get("type") == "page" and "perfetto" in t.get("url", "")), None)
        dt = DevTools(wait_for(page, "Chrome DevTools"))
        wait_for(lambda: dt.eval("!!(window.app && app.openTraceFromBuffer)"), "Perfetto UI")
        # Fit the real page size exactly: emulate a viewport of page/zoom CSS px.
        iw, ih, dpr = dt.eval("[innerWidth, innerHeight, devicePixelRatio]")
        zoom = a.zoom or min(1.0, ih / need_h)
        dt.call("Emulation.setDeviceMetricsOverride", {
            "width": round(iw / zoom), "height": round(ih / zoom),
            "deviceScaleFactor": dpr, "mobile": False, "scale": zoom})
        dt.eval(VIEW_JS % {"bw": str(not a.color).lower()})

        # Send the trace in chunks, then open it from an in-page buffer.
        b64 = base64.b64encode(raw).decode()
        dt.eval("window.__trace = []")
        for i in range(0, len(b64), 4 << 20):
            dt.eval(f"window.__trace.push('{b64[i:i + (4 << 20)]}')")
        name = os.path.basename(a.trace)
        dt.eval(f"""(async () => {{
            const s = atob(window.__trace.join('')); delete window.__trace;
            const u = new Uint8Array(s.length);
            for (let i = 0; i < s.length; i++) u[i] = s.charCodeAt(i);
            await app.openTraceFromBuffer({{buffer: u.buffer, title: {json.dumps(name)}, fileName: {json.dumps(name)}}});
            return true; }})()""", await_promise=True)
        wait_for(lambda: dt.eval("!!(app.trace && app.trace.timeline)"), "trace to load", 120)
        time.sleep(1.0)  # let the tracks render once

        if not dt.eval(CACHE_JS):
            print("warning: Perfetto track cache not found; rows may flash 'Loading'")
        # Warm up: show frame 0 and wait until every row has loaded its data once.
        dt.eval(RECORD_SETUP_JS % {"meta": json.dumps(meta)})
        dt.eval("window.__badappleRender(0)", await_promise=True)
        if a.record:
            record(dt, meta, a.record, a.max_frames, a.video_height, a.audio)
            return
        dt.eval(PLAYER_JS % {"meta": json.dumps(meta), "speed": a.speed, "loop": str(a.loop).lower(),
                             "wait": str(a.wait).lower()})
        if a.wait:
            print("ready on frame 0. Click the Perfetto window and press Space to start.", flush=True)
            while dt.eval("window.__badapple.waiting"):
                time.sleep(0.05)
            print(f"started at {time.strftime('%H:%M:%S')}", flush=True)
        secs = meta["frames"] / meta["fps"] / a.speed
        print(f"playing {meta['frames']} frames at {meta['fps'] * a.speed:g} fps "
              f"({secs:.1f}s){' on loop' if a.loop else ''}. Ctrl-C to quit.")
        while a.loop or not dt.eval("!!(window.__badapple && window.__badapple.done)"):
            time.sleep(0.5)
        st = dt.eval("JSON.stringify(window.__badapple)")
        shown = json.loads(st)["presented"]
        print(f"done: {shown} of {meta['frames']} frames shown "
              f"({shown / secs:.1f} fps on screen). Close the Chrome window or press Ctrl-C.")
        chrome.wait()
    except KeyboardInterrupt:
        pass
    finally:
        chrome.terminate()
        shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    main()
