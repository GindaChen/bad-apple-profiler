from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen
import os
import re
import shutil
from threading import Thread

DEFAULT_PORT = 3011
BASE_PORT = int(os.environ.get('NEURON_DEMO_PORT', str(DEFAULT_PORT)))
if not 1024 <= BASE_PORT <= 65532:
    raise SystemExit('NEURON_DEMO_PORT must be between 1024 and 65532')
BASE_URL = f'http://localhost:{BASE_PORT}'
PLAYER_URL = f'http://localhost:{BASE_PORT + 2}'
INSPECT_URL = f'http://localhost:{BASE_PORT + 3}'
PROFILE = 'bad-apple-512x384'
PROFILE_PATH = f'/profile/{PROFILE}'
ROUTE = b'location.pathname==="/profile/bad-apple-512x384"'
TIMELINE_ASSET = '/assets/neuron-explorer-timeline-viewer.CiM3WzfJ.js'

class Handler(BaseHTTPRequestHandler):
    def send_bytes(self, status, content, mime):
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def send_file(self, path, mime, compressed=False, ranges=False):
        if not path.exists():
            self.send_error(404)
            return
        size = path.stat().st_size
        match = re.fullmatch(r'bytes=(\d+)-(\d*)', self.headers.get('Range', '')) if ranges else None
        start, end = (int(match[1]), min(size - 1, int(match[2] or size - 1))) if match else (0, size - 1)
        if start > end:
            self.send_error(416)
            return
        self.send_response(206 if match else 200)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(end - start + 1))
        if compressed:
            self.send_header('Content-Encoding', 'gzip')
        if ranges:
            self.send_header('Accept-Ranges', 'bytes')
        if match:
            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        self.end_headers()
        with path.open('rb') as source:
            if not match:
                shutil.copyfileobj(source, self.wfile)
            else:
                source.seek(start)
                remaining = end - start + 1
                while remaining:
                    data = source.read(min(remaining, 128 * 1024))
                    self.wfile.write(data)
                    remaining -= len(data)

    def do_GET(self):
        if self.server.playback and self.path == '/player.js':
            self.send_file(Path(__file__).with_name('player.js'), 'text/javascript')
            return
        if self.server.playback and self.path == '/timeline.bin':
            self.send_file(self.server.data / 'timeline.bin.gz', 'application/octet-stream', compressed=True)
            return
        if self.server.playback and self.path == '/soundtrack.m4a':
            self.send_file(self.server.data / 'soundtrack.m4a', 'audio/mp4', ranges=True)
            return
        try:
            response = urlopen(BASE_URL + self.path, timeout=15)
        except HTTPError as err:
            response = err
        with response:
            content = response.read()
            mime = response.headers.get('Content-Type', 'application/octet-stream')
            if response.status == 200 and mime.startswith('text/html'):
                content = content.replace(b'localhost:3002', f'localhost:{BASE_PORT + 1}'.encode())
            if self.server.playback and response.status == 200 and mime.startswith('text/html') and self.path.split('?')[0] == PROFILE_PATH:
                content = content.replace(b'</head>', b'<script>window.NEURON_EXPLORER_FEATURE_FLAGS="TimelineViewerDebugHook";window.DISABLED_NEURON_EXPLORER_FEATURE_FLAGS="ResolutionAwareQuantization";</script></head>')
                player = f'<script src="/player.js" data-inspect-url="{INSPECT_URL}" defer></script>'
                content = content.replace(b'</body>', player.encode() + b'</body>')
            if self.server.playback and self.path == TIMELINE_ASSET:
                changes = [
                    (b'return Math.max(s,r)}(function(e)', b'return ' + ROUTE + b'?r:Math.max(s,r)}(function(e)'),
                    (b'this.drawTracksFromGroups(this.eventGroupings),this.refreshTracks(Fc.Visible,{refreshHbmUsage:!1,',
                     b'this.drawTracksFromGroups(this.eventGroupings),(' + ROUTE + b')||this.refreshTracks(Fc.Visible,{refreshHbmUsage:!1,'),
                    (b'let o=Math.max(2,Math.round((i-r)/(t.visibleEndTime-t.visibleStartTime)*t.trackWidth)),s=(r-t.visibleStartTime)/(t.visibleEndTime-t.visibleStartTime)*t.trackWidth+(o>2?.5:0)',
                     b'let o=(' + ROUTE + b')?(i-r)/(t.visibleEndTime-t.visibleStartTime)*t.trackWidth:Math.max(2,Math.round((i-r)/(t.visibleEndTime-t.visibleStartTime)*t.trackWidth)),s=(r-t.visibleStartTime)/(t.visibleEndTime-t.visibleStartTime)*t.trackWidth+((' + ROUTE + b')?0:o>2?.5:0)'),
                ]
                for original, patched in changes:
                    if content.count(original) != 1:
                        self.send_error(502, 'Explorer timeline version differs from the verified build')
                        return
                    content = content.replace(original, patched)
            self.send_bytes(response.status, content, mime)

def serve(data):
    with (ThreadingHTTPServer(('127.0.0.1', BASE_PORT + 2), Handler) as player,
          ThreadingHTTPServer(('127.0.0.1', BASE_PORT + 3), Handler) as inspect):
        player.data, player.playback = data, True
        inspect.playback = False
        thread = Thread(target=inspect.serve_forever, daemon=True)
        thread.start()
        print(f'Player: {PLAYER_URL}{PROFILE_PATH}', flush=True)
        print(f'Inspect: {INSPECT_URL}{PROFILE_PATH}', flush=True)
        try:
            player.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            inspect.shutdown()
