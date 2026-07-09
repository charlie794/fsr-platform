# processing/stream_filter.py
from __future__ import annotations

import numpy as np


def _causal_median(x: np.ndarray, m: int) -> np.ndarray:
    """Trailing-window median of width m (removes isolated spikes)."""
    if m <= 1 or x.size == 0:
        return x.astype(np.float64, copy=True)
    xp = np.concatenate([np.full(m - 1, x[0], dtype=np.float64), x.astype(np.float64)])
    win = np.lib.stride_tricks.sliding_window_view(xp, m)
    return np.median(win, axis=1)


def _causal_ma(x: np.ndarray, w: int) -> np.ndarray:
    """Trailing-window moving average of width w, O(n) via cumsum."""
    if w <= 1 or x.size == 0:
        return x.astype(np.float64, copy=True)
    x = x.astype(np.float64)
    c = np.cumsum(np.insert(x, 0, 0.0))
    out = np.empty_like(x)
    out[w - 1:] = (c[w:] - c[:-w]) / w
    # warm-up (partial windows) — only ever hit on the first chunk of a step
    for i in range(min(w - 1, x.size)):
        out[i] = c[i + 1] / (i + 1)
    return out


class StreamFilter:
    """
    Causal median -> moving-average cascade, stateful across chunks.

    The median stage removes isolated single-sample spikes; the moving
    average then smooths the residual noise.  A small tail of recent samples
    is carried between process() calls so the streamed output is identical to
    filtering the whole signal at once — no discontinuity at DAQ chunk
    boundaries.

    Chosen over a Butterworth low-pass because, on the real FSR signal, it
    gives the same noise reduction (~34%) with ~2 ms lag instead of the
    Butterworth's ~26 ms, and it removes spikes the Butterworth would ring on.

    Cost is ~0.5 us/sample.  Call reset() at the start of each test step so
    there is no carryover between sensors.
    """

    def __init__(self, med_w: int = 5, ma_w: int = 15):
        self.med_w = int(med_w)
        self.ma_w = int(ma_w)
        self._pad = max(self.med_w - 1, 0) + max(self.ma_w - 1, 0)
        self._tail = np.empty(0, dtype=np.float64)

    def reset(self) -> None:
        self._tail = np.empty(0, dtype=np.float64)

    def process(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=np.float64)
        if x.size == 0:
            return x.copy()
        buf = np.concatenate([self._tail, x]) if self._tail.size else x
        off = self._tail.size
        y = _causal_median(buf, self.med_w)
        y = _causal_ma(y, self.ma_w)
        out = y[off:]
        # retain enough raw context that the next chunk's output is exact
        self._tail = buf[-self._pad:].copy() if buf.size >= self._pad else buf.copy()
        return out
