"""Encode video as framework spans and pack the events returned by Explorer."""

import gzip
import struct
import subprocess
from pathlib import Path

import duckdb
import numpy as np

WIDTH, HEIGHT, FPS = 512, 384, 30
FIRST_THREAD = 101
US = 1_000_000
NS = 1_000_000_000
ANCHOR_US = 1


def varint(value):
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    return bytes(result) + bytes([value])


def integer(field, value):
    return varint(field * 8) + varint(value)


def message(field, value):
    if isinstance(value, str):
        value = value.encode()
    return varint(field * 8 + 2) + varint(len(value)) + value


def event(thread, start, duration):
    return (f'{{"ph":"X","name":" ","cat":"cpu_op","pid":100,"tid":{thread},'
            f'"ts":{start},"dur":{duration},"args":{{}}}}')


def frames_from_video(video, seconds=None):
    command = ['ffmpeg', '-nostdin', '-v', 'error', '-i', str(video), '-vf',
               f'fps={FPS},scale={WIDTH}:{HEIGHT}:flags=area,format=gray']
    if seconds is not None:
        command += ['-frames:v', str(seconds * FPS)]
    command += ['-f', 'rawvideo', 'pipe:1']
    with subprocess.Popen(command, stdout=subprocess.PIPE) as process:
        try:
            while data := process.stdout.read(WIDTH * HEIGHT):
                if len(data) != WIDTH * HEIGHT:
                    raise ValueError('FFmpeg returned an incomplete frame')
                yield np.frombuffer(data, dtype=np.uint8).reshape(HEIGHT, WIDTH) >= 128
            if process.wait() != 0:
                raise ValueError('FFmpeg could not decode the source video')
        finally:
            if process.poll() is None:
                process.kill()


def encode(video, session, seconds):
    session.mkdir(parents=True, exist_ok=True)
    base = US
    frames = 0
    spans = 0
    with (session / 'trace.json').open('w') as trace:
        trace.write('{"traceEvents":[')
        # Anchors make every row discoverable and set the time origin, even for a black opening.
        trace.write(','.join(event(thread, base, ANCHOR_US)
                             for thread in range(FIRST_THREAD, FIRST_THREAD + HEIGHT)))
        for frames, mask in enumerate(frames_from_video(video, seconds), 1):
            rows, edges = np.nonzero(np.diff(mask, axis=1, prepend=False, append=False))
            times = base + ((frames - 1) * WIDTH + edges) * US // (FPS * WIDTH)
            starts, durations = times[::2], times[1::2] - times[::2]
            # Chrome complete events are ordered by start time within each frame.
            order = np.argsort(starts, kind='stable')
            entries = zip((rows[::2][order] + FIRST_THREAD).tolist(),
                          starts[order].tolist(), durations[order].tolist())
            if len(order):
                trace.write(',' + ','.join(event(*entry) for entry in entries))
            spans += len(order)
            if frames % 1500 == 0:
                print(f'Encoded {frames:,} frames', flush=True)
        if not frames:
            raise ValueError('Video contains no frames')
        total_us = frames * US // FPS
        trace.write(',' + event(FIRST_THREAD, base + total_us - 1, ANCHOR_US) + ']}')
    # Minimal Neuron session metadata. No runtime or device events are fabricated.
    metadata = (
        integer(1, 3) + integer(2, 100) + message(4, 'i-synthetic') + integer(5, 1)
        + message(6, message(1, 'ntrace.pb') + integer(2, 1))
        + message(7, integer(1, HEIGHT) + message(2, 'Synthetic CPU')) + integer(9, 1)
        + integer(11, base * 1000) + integer(12, (base + total_us) * 1000)
        + message(15, 'synthetic')
    )
    (session / 'trace_info.pb').write_bytes(metadata)
    (session / 'ntrace.pb').write_bytes(b'')
    return frames, spans


def pack(parquet: Path, output: Path, frames: int) -> int:
    """Build the playback index from Explorer's processed trace, never from video pixels."""
    columns = duckdb.execute(
        """SELECT start_ts, end_ts, thread_id
           FROM read_parquet(?)
           WHERE trace_event_source = 'framework' AND duration_ns > ?""",
        [str(parquet), ANCHOR_US * 1000],
    ).fetchnumpy()
    starts, ends, threads = (columns[name] for name in ('start_ts', 'end_ts', 'thread_id'))

    def units(timestamps):
        # Explorer stores nanoseconds. Input timestamps were floored to microseconds.
        positions = (timestamps * (FPS * WIDTH) + NS // 2) // NS
        if not np.array_equal((positions * US // (FPS * WIDTH)) * 1000, timestamps):
            raise ValueError('Explorer returned an unexpected trace timestamp')
        return positions

    start, end = units(starts), units(ends)
    frame, left = divmod(start, WIDTH)
    right = end - frame * WIDTH
    row = threads - FIRST_THREAD
    if (np.any((row < 0) | (row >= HEIGHT)) or np.any((frame < 0) | (frame >= frames))
            or np.any(right <= left) or np.any(right > WIDTH)):
        raise ValueError('Imported span extends outside the expected frame or track')
    slot = frame * HEIGHT + row
    order = np.lexsort((left, slot))
    offsets = np.empty(frames * HEIGHT + 1, dtype='<u4')
    offsets[0] = 0
    np.cumsum(np.bincount(slot, minlength=frames * HEIGHT), out=offsets[1:])
    pairs = np.column_stack((left[order], right[order])).astype('<u2')
    with gzip.open(output, 'wb', compresslevel=3) as dest:
        dest.write(struct.pack('<III', frames, WIDTH, HEIGHT))
        dest.write(offsets.tobytes())
        dest.write(pairs.tobytes())
    return len(pairs)
