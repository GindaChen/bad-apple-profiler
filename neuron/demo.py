# /// script
# requires-python = ">=3.10"
# dependencies = ["duckdb==1.5.4", "numpy>=2,<3"]
# ///
"""Install the pinned Explorer, import a trace, and run the music player."""

import argparse
import json
import shutil
import subprocess
import time
from pathlib import Path
from urllib.request import urlopen

from server import BASE_PORT, BASE_URL, DEFAULT_PORT, INSPECT_URL, PLAYER_URL, PROFILE, serve
from trace import FPS, encode, pack

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT.parent / 'out' / 'neuron'
CONTAINER = 'bad-apple-neuron-explorer'
if BASE_PORT != DEFAULT_PORT:
    CONTAINER += f'-{BASE_PORT}'
IMAGE = 'bad-apple-neuron-explorer:2.33.0'


def run(*command, **args):
    return subprocess.run(command, check=True, **args)


def install():
    run('docker', 'build', '--platform', 'linux/amd64', '-t', IMAGE, str(ROOT))
    exists = subprocess.run(['docker', 'container', 'inspect', CONTAINER],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    if exists:
        run('docker', 'start', CONTAINER)
    else:
        run('docker', 'run', '-d', '--name', CONTAINER, '--platform', 'linux/amd64',
            '-p', f'127.0.0.1:{BASE_PORT}:13011', '-p', f'127.0.0.1:{BASE_PORT + 1}:13012',
            '-v', f'{CONTAINER}-profiles:/profiles', IMAGE)
    for _ in range(30):
        try:
            with urlopen(BASE_URL, timeout=1) as response:
                response.read(1)
            break
        except OSError:
            time.sleep(1)
    else:
        raise SystemExit(f'Explorer did not start. Check docker logs {CONTAINER}.')
    print(f'Explorer is installed in Docker container {CONTAINER}. Run prepare, then serve.')


def prepare(video, audio, seconds):
    if not video.is_file():
        raise SystemExit(f'Video not found: {video}')
    if audio and not audio.is_file():
        raise SystemExit(f'Audio not found: {audio}')
    try:
        run('docker', 'exec', CONTAINER, 'neuron-explorer', '--version', capture_output=True)
    except subprocess.CalledProcessError as error:
        raise SystemExit('Explorer is unavailable. Run install first.') from error
    incoming = OUTPUT / 'raw' / 'i-synthetic_pid_100'
    incoming.mkdir(parents=True, exist_ok=True)
    info = OUTPUT / 'manifest.json'
    info.unlink(missing_ok=True)
    audio_file = OUTPUT / 'soundtrack.m4a'
    soundtrack = audio or video
    codec = run('ffprobe', '-v', 'error', '-select_streams', 'a:0',
                '-show_entries', 'stream=codec_name', '-of', 'csv=p=0', str(soundtrack),
                capture_output=True, text=True).stdout.strip()
    if not codec:
        raise SystemExit('No audio stream found. Pass a separate soundtrack with --audio.')
    if codec == 'aac' and soundtrack.suffix.lower() == '.m4a':
        shutil.copyfile(soundtrack, audio_file)
    else:
        audio_codec = ['-c:a', 'copy'] if codec == 'aac' else ['-c:a', 'aac', '-b:a', '192k']
        run('ffmpeg', '-nostdin', '-v', 'error', '-y', '-i', str(soundtrack),
            '-map', '0:a:0', *audio_codec, '-movflags', '+faststart', str(audio_file))
    frames, encoded = encode(video, incoming, seconds)
    run('docker', 'exec', CONTAINER, 'mkdir', '-p', '/tmp/bad-apple-trace')
    run('docker', 'cp', str(OUTPUT / 'raw') + '/.', f'{CONTAINER}:/tmp/bad-apple-trace/')
    run('docker', 'exec', CONTAINER, 'neuron-explorer', 'view', '-d', '/tmp/bad-apple-trace',
        '--display-name', PROFILE, '--ignore-device-profile', '--data-path', '/profiles',
        '--port', '3011', '--ingest-only', '--force')
    parquet = OUTPUT / 'SystemProfileEvents.parquet'
    remote = f'/profiles/profiles/global/{PROFILE}@latest/SystemProfileEvents.parquet'
    run('docker', 'cp', f'{CONTAINER}:{remote}', str(parquet))
    count = pack(parquet, OUTPUT / 'timeline.bin.gz', frames)
    if count != encoded:
        raise SystemExit(f'Expected {encoded:,} spans, but Explorer returned {count:,}')
    info.write_text(json.dumps({'frames': frames, 'spans': count, 'fps': FPS}, indent=2) + '\n')
    print(f'Packed {count:,} spans directly from Explorer Parquet across {frames:,} frames.')
    print(f'Inspect the trace without the player: {INSPECT_URL}/profile/{PROFILE}')
    print(f'Run the music player with the serve command: {PLAYER_URL}/profile/{PROFILE}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('install', help='Install and start the pinned Explorer Docker image')
    build = commands.add_parser('prepare', help='Encode, import, and prepare playback from Explorer data')
    build.add_argument('video', type=Path)
    build.add_argument('--audio', type=Path, help='Separate soundtrack starting at the first video frame')
    build.add_argument('--seconds', type=int, help='Encode only the first N seconds')
    commands.add_parser('serve', help='Serve the audio-synchronized timeline on localhost')
    args = parser.parse_args()
    if args.command == 'install':
        install()
    elif args.command == 'prepare':
        if args.seconds is not None and args.seconds <= 0:
            parser.error('--seconds must be positive')
        prepare(args.video.resolve(), args.audio.resolve() if args.audio else None, args.seconds)
    elif args.command == 'serve':
        if not (OUTPUT / 'manifest.json').is_file():
            parser.error('Run prepare first')
        serve(OUTPUT)


if __name__ == '__main__':
    main()
