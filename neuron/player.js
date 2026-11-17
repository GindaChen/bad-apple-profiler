(() => {
  if (location.pathname !== '/profile/bad-apple-512x384') return;
  const fps = 30;
  const audio = new Audio('/soundtrack.m4a');
  audio.preload = 'auto';
  const panel = document.createElement('div');
  panel.style.cssText = 'position:fixed;right:12px;bottom:12px;z-index:9999;display:flex;flex-wrap:wrap;align-items:center;gap:10px;background:#161b22;color:#fff;border:1px solid #64748b;border-radius:8px;padding:8px 12px;font:13px system-ui;max-width:calc(100% - 24px)';
  const button = document.createElement('button');
  button.textContent = 'Play with music';
  button.disabled = true;
  const seek = document.createElement('input');
  seek.type = 'range';
  seek.ariaLabel = 'Playback position';
  seek.min = '0';
  seek.max = '0';
  seek.step = '0.001';
  seek.value = '0';
  seek.disabled = true;
  const status = document.createElement('span');
  status.textContent = 'Loading full trace…';
  const inspect = document.createElement('a');
  inspect.href = document.currentScript.dataset.inspectUrl + location.pathname;
  inspect.target = '_blank';
  inspect.rel = 'noopener';
  inspect.textContent = 'Inspect imported trace';
  inspect.style.color = '#bdd4ff';
  panel.append(button, seek, status, inspect);
  document.body.append(panel);

  let data;
  let image;
  let tracks;
  let current = -1;
  let seen;
  let rendered = 0;
  let lastRedraw = 0;
  let finished = false;
  let playbackStarted = false;
  let playbackError = null;

  fetch('/timeline.bin').then(response => {
    if (!response.ok) throw Error('Could not load timeline data');
    return response.arrayBuffer();
  }).then(buffer => {
    const [frameCount, width, height] = new Uint32Array(buffer, 0, 3);
    const offsets = new Uint32Array(buffer, 12, frameCount * height + 1);
    const spans = new Uint16Array(buffer, 12 + offsets.byteLength);
    if (width !== 512 || height !== 384 || frameCount < 1 ||
        spans.length !== offsets[offsets.length - 1] * 2) throw Error('Trace format mismatch');
    data = {frameCount, width, height, offsets, spans};
    seen = new Uint8Array(frameCount);
    seek.max = String(frameCount / fps);
    status.textContent = 'Opening 384 timeline tracks…';
  }).catch(error => { status.textContent = error.message; });

  button.onclick = async () => {
    if (!audio.paused) {
      audio.pause();
      return;
    }
    if (finished || audio.ended) {
      audio.currentTime = 0;
      seen.fill(0);
      rendered = 0;
      finished = false;
    }
    playbackError = null;
    try {
      await audio.play();
    } catch (error) {
      playbackError = `Audio could not start: ${error.message}`;
    }
  };
  seek.oninput = () => {
    finished = false;
    audio.currentTime = Number(seek.value);
  };
  audio.addEventListener('play', () => {
    playbackStarted = true;
    button.textContent = 'Pause';
  });
  audio.addEventListener('pause', () => {
    button.textContent = (finished || audio.ended) ? 'Replay with music' : 'Resume with music';
  });
  audio.addEventListener('error', () => { playbackError = 'Could not load the soundtrack'; });

  function prepare(viewer) {
    if (!data || !viewer?.image) return;
    const rows = Object.entries(viewer.image.tracks).filter(([name]) => name.includes('/framework/'));
    if (rows.length !== data.height) return;
    rows.sort(([a], [b]) => Number(a.split(':').at(-1)) - Number(b.split(':').at(-1)));
    tracks = rows.map(([, track], index) => {
      track.name = String(index + 1);
      return track;
    });
    viewer.image.trackOrder = rows.map(([key]) => key);
    image = viewer.image;
    const options = image.plotOption;
    options.displayNameOnTrack = false;
    options.sameRowDisplay = true;
    options.eventBoundaryHeight = 0;
    options.trackHeightPadding = 0;
    options.eventHeight = 1;
    options.eventGap = 0;
    viewer.maxEndTime = Math.floor(data.frameCount * 1e6 / fps) * 1000;
    options.endTime = viewer.maxEndTime;
    image.trackNameColor = 'transparent';
    image.trackBackgroundColor = '#000000';
    image.container.style.background = '#000000';
    image.container.style.maxWidth = '654px';
    image.container.style.marginInline = 'auto';
    image.gridColor = 'transparent';
    image.resetTransientTrackProps();
    image.resizeCanvas();
    button.disabled = false;
    seek.disabled = false;
  }

  function render(viewer, frame) {
    const {height, width, offsets, spans} = data;
    const frameUnit = frame * width;
    const frameStart = Math.floor(frame * 1e6 / fps) * 1000;
    const frameEnd = Math.floor((frame + 1) * 1e6 / fps) * 1000;
    const rowStart = frame * height;
    for (let row = 0; row < height; row += 1) {
      const track = tracks[row];
      const first = offsets[rowStart + row];
      const last = offsets[rowStart + row + 1];
      const events = new Array(last - first);
      for (let run = first; run < last; run += 1) {
        const left = spans[run * 2];
        const right = spans[run * 2 + 1];
        const startTime = Math.floor((frameUnit + left) * 1e6 / (fps * width)) * 1000;
        const endTime = Math.floor((frameUnit + right) * 1e6 / (fps * width)) * 1000;
        events[run - first] = {
          startTime, endTime, duration: endTime - startTime, id: run + 385,
          name: ' ', color: '#ffffff', rowNumber: 0, layer: track.layer, visible: true
        };
      }
      track.events = events;
      track.hasNewData = true;
    }
    image.plotOption.visibleStartTime = frameStart;
    image.plotOption.visibleEndTime = frameEnd;
    viewer.recalculateZoomFactor();
    image.redraw();
    current = frame;
  }

  function timeLabel(time) {
    const seconds = Math.floor(time);
    return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
  }

  function tick(now) {
    const viewer = window.__systemTimelineViewer;
    if (!image) prepare(viewer);
    if (image) {
      const duration = data.frameCount / fps;
      if (audio.currentTime >= duration && !audio.paused) {
        finished = true;
        audio.pause();
      }
      const audioTime = Math.min(audio.currentTime, duration);
      const frame = Math.min(data.frameCount - 1, Math.floor(audioTime * fps));
      if (frame !== current) {
        render(viewer, frame);
      } else if (audio.paused && now - lastRedraw > 200) {
        lastRedraw = now;
        image.redraw();
      }
      if (!audio.paused && !audio.seeking && !seen[frame]) {
        seen[frame] = 1;
        rendered += 1;
      }
      seek.value = String(audioTime);
      if (playbackError) {
        status.textContent = playbackError;
      } else if (finished || audio.ended) {
        status.textContent = `Finished · ${rendered.toLocaleString()}/${data.frameCount.toLocaleString()} frames drawn`;
      } else if (playbackStarted) {
        status.textContent = `${timeLabel(audioTime)} / ${timeLabel(duration)} · Frame ${frame + 1}/${data.frameCount} · ${rendered} drawn`;
      } else {
        status.textContent = `Ready · 512×384 · 30 fps · ${timeLabel(duration)}`;
      }
    }
    requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
})();
