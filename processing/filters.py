# processing/filters.py
from __future__ import annotations
import numpy as np


# ============================================================
# Butterworth low-pass filter — stateful, sample-by-sample
# Matches the oscilloscope's IIR pattern exactly.
# ============================================================

class ButterworthLP:
    """
    Second-order Butterworth low-pass filter, Direct Form II transposed.
    Designed once from (cutoff_hz, sample_rate_hz), then fed samples
    one chunk at a time with process().  State persists between chunks
    so the filter is continuous across DAQ read boundaries.

    Used for force (CH0) to smooth the signal without introducing the
    phase distortion of a moving average.
    """

    def __init__(self, cutoff_hz: float = 10.0, sample_rate_hz: float = 1000.0):
        from scipy.signal import butter
        b, a = butter(2, cutoff_hz / (0.5 * sample_rate_hz), btype="low", analog=False)
        self.b = b.astype(float)
        self.a = a.astype(float)
        # Direct Form II transposed state (length = filter_order)
        self._zi = np.zeros(max(len(b), len(a)) - 1, dtype=float)

    def reset(self) -> None:
        self._zi[:] = 0.0

    def process(self, x: np.ndarray) -> np.ndarray:
        """Filter array x, updating state in-place. Returns filtered array."""
        from scipy.signal import lfilter_zi, lfilter
        if x.size == 0:
            return x.copy()
        x = np.asarray(x, dtype=float)
        y, self._zi = lfilter(self.b, self.a, x, zi=self._zi)
        return y

    def init_from_value(self, x0: float) -> None:
        """Initialise filter state as if it had been running at constant x0.
        Eliminates startup transient when first contact is made."""
        from scipy.signal import lfilter_zi
        zi_unit = lfilter_zi(self.b, self.a)
        self._zi = zi_unit * float(x0)


# ============================================================
# EMA — kept for back-compat (used nowhere currently but harmless)
# ============================================================

def ema(x: np.ndarray, alpha: float = 0.2) -> np.ndarray:
    """Exponential moving average filter."""
    x = np.asarray(x, dtype=float)
    if x.size == 0:
        return x
    alpha = float(alpha)
    try:
        from scipy.signal import lfilter, lfiltic
        b = [alpha]
        a = [1.0, -(1.0 - alpha)]
        zi = lfiltic(b, a, y=[x[0]], x=[x[0]])
        y, _ = lfilter(b, a, x, zi=zi)
        return y.astype(float)
    except ImportError:
        y = np.empty_like(x)
        y[0] = x[0]
        om = 1.0 - alpha
        for i in range(1, len(x)):
            y[i] = alpha * x[i] + om * y[i - 1]
        return y
