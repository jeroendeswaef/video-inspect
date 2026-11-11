"""Align a clean audio track (e.g. the studio recording of a song) with the audio of a video.

The sound in the video is the same song, but recorded off a speaker with a phone: it starts at a
different point in the song and, because of tiny dropouts, the offset *changes* a little during
the recording (steady stretches separated by small jumps). So the result is not a single offset
but a list of "anchors" [(video_time_us, offset_us), ...]: from video_time on, the clean track
position is  video_position + offset  (until the next anchor).

Needs ffmpeg (to decode) plus numpy and scipy; the result is cached by the application, so this only
runs once per video/track pair.
"""
import subprocess

import numpy as np
import scipy.signal as ss

SR = 8000    # analysis sample rate
HOP = 160    # 20 ms feature frames
FRAME_S = HOP / SR


def decode(path):
    """Mono float32 samples at SR Hz."""
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-nostdin", "-i", path, "-vn", "-ac", "1", "-ar", str(SR),
         "-f", "f32le", "-"],
        capture_output=True, check=True).stdout
    return np.frombuffer(out, dtype=np.float32)


def features(x):
    """Log band energies (24 bands, 100-3800 Hz), normalised per band: shape (bands, frames)."""
    f, _t, z = ss.stft(x, SR, nperseg=512, noverlap=512 - HOP)
    mag = np.abs(z)
    edges = np.geomspace(100, 3800, 25)
    bands = [mag[(f >= a) & (f < b)].mean(axis=0) for a, b in zip(edges[:-1], edges[1:])]
    feat = np.log(np.array(bands) + 1e-4)
    feat -= feat.mean(axis=1, keepdims=True)
    feat /= feat.std(axis=1, keepdims=True) + 1e-9
    return feat


def xcorr(a, b):
    """Cross-correlation summed over bands. Returns (lags, values); a lag L means that b's
    content appears L frames later in a."""
    n = a.shape[1] + b.shape[1]
    size = 1 << (n - 1).bit_length()
    fa, fb = np.fft.rfft(a, size, axis=1), np.fft.rfft(b, size, axis=1)
    c = np.fft.irfft(fa * np.conj(fb), size, axis=1).sum(axis=0)
    lags = np.concatenate([np.arange(0, a.shape[1]), np.arange(-b.shape[1] + 1, 0)])
    c = np.concatenate([c[:a.shape[1]], c[size - b.shape[1] + 1:]])
    return lags, c


def _local_offset(song_f, vid_f, coarse, start_s, dur_s, search_s=3.0):
    """Offset (song position - video position, seconds) of a video window, and its quality."""
    a = int(start_s / FRAME_S)
    w = int(dur_s / FRAME_S)
    seg = vid_f[:, a:a + w]
    if seg.shape[1] < w // 2:
        return None, 0.0
    lags, c = xcorr(song_f, seg)
    expect = int((start_s + coarse) / FRAME_S)
    mask = np.abs(lags - expect) < int(search_s / FRAME_S)
    cm = np.where(mask, c, -1e9)
    j = int(np.argmax(cm))
    y0, y1, y2 = cm[j - 1], cm[j], cm[j + 1]
    denom = y0 - 2 * y1 + y2
    d = 0.5 * (y0 - y2) / denom if denom != 0 else 0.0     # sub-frame peak position
    quality = cm[j] / np.sort(c[mask])[-8]
    return (lags[j] + d) * FRAME_S - start_s, float(quality)


def _refine_step(song_f, vid_f, t_s, off_a, off_b, span_s=3.5):
    """Exact time of a jump from offset off_a to off_b near t_s: the split point where the video
    frames match the song best with off_a before it and off_b after it."""
    sa, sb = int(round(off_a / FRAME_S)), int(round(off_b / FRAME_S))
    lo = max(int((t_s - span_s) / FRAME_S), max(0, -sa, -sb))
    hi = min(int((t_s + span_s) / FRAME_S), vid_f.shape[1] - 1,
             song_f.shape[1] - 1 - max(sa, sb))
    if hi <= lo:
        return t_s
    f = np.arange(lo, hi)
    score_a = (vid_f[:, f] * song_f[:, f + sa]).sum(axis=0)
    score_b = (vid_f[:, f] * song_f[:, f + sb]).sum(axis=0)
    # split at index i: frames [lo, lo+i) use a, the rest use b
    total = np.concatenate([[0], np.cumsum(score_a)]) + (score_b.sum() - np.concatenate([[0], np.cumsum(score_b)]))
    return (lo + int(np.argmax(total))) * FRAME_S


def compute_anchors(video_path, audio_path, tolerance_s=0.03, progress=None):
    """Return anchors [(video_time_us, offset_us), ...] sorted by time (first one at 0)."""
    say = progress or (lambda msg: None)
    say("decoding audio…")
    song, vid = decode(audio_path), decode(video_path)
    say("analysing…")
    song_f, vid_f = features(song), features(vid)

    lags, c = xcorr(vid_f, song_f)           # coarse: where does the song start in the video
    coarse = -lags[int(np.argmax(c))] * FRAME_S

    dur, hop = 4.0, 2.0
    total_s = vid_f.shape[1] * FRAME_S
    times, offs, quals = [], [], []
    start = 0.0
    while start + dur <= total_s:
        o, q = _local_offset(song_f, vid_f, coarse, start, dur)
        if o is not None:
            times.append(start + dur / 2)
            offs.append(o)
            quals.append(q)
        start += hop
    times, offs, quals = np.array(times), np.array(offs), np.array(quals)
    # a window is reliable if its match is distinct and not far from the overall match
    good = (quals >= 1.08) & (np.abs(offs - np.median(offs)) < 0.75)
    if good.sum() < 3:
        raise RuntimeError("the audio does not seem to match the video (no reliable alignment found)")
    offs = np.interp(times, times[good], offs[good])      # fill unreliable windows
    offs = ss.medfilt(np.pad(offs, 1, mode="edge"), 3)[1:-1]

    # split into steady stretches: a new one starts when two windows in a row differ by more than
    # the tolerance from the current stretch
    stretches = [[0, []]]                       # [start index, offsets]
    pending = 0
    for i, o in enumerate(offs):
        cur = np.median(stretches[-1][1]) if stretches[-1][1] else o
        if abs(o - cur) > tolerance_s:
            pending += 1
            if pending >= 2:
                stretches.append([i - pending + 1, [offs[k] for k in range(i - pending + 1, i + 1)]])
                pending = 0
        else:
            pending = 0
            stretches[-1][1].append(o)
    values = [float(np.median(s[1])) for s in stretches]

    anchors = [(0.0, values[0])]
    for n in range(1, len(stretches)):
        approx = times[stretches[n][0]] - hop / 2
        t = _refine_step(song_f, vid_f, approx, values[n - 1], values[n])
        anchors.append((t, values[n]))
    say("done")
    return [(int(round(t * 1e6)), int(round(o * 1e6))) for t, o in anchors], \
        {"windows": len(times), "max_error_s": float(np.abs(
            offs - np.array([values[max(i for i, s in enumerate(stretches) if s[0] <= k)]
                             for k in range(len(offs))])).max())}


def offset_at(anchors, video_us):
    """Offset (us) valid at video time video_us."""
    off = anchors[0][1]
    for t, o in anchors:
        if t <= video_us:
            off = o
        else:
            break
    return off


if __name__ == "__main__":
    import sys
    import time
    t0 = time.time()
    anchors, info = compute_anchors(sys.argv[1], sys.argv[2], progress=print)
    for t, o in anchors:
        print(f"from video {t / 1e6:8.3f}s: clean track = video + {o / 1e6:.3f}s")
    print(info, f"{time.time() - t0:.1f}s")
