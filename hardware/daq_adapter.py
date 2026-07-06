from __future__ import annotations

import time
from typing import Iterable, List, Optional, Tuple

import numpy as np

# MCC DAQ HAT — real hardware only, no simulation fallback.
# Uses hat_list() to find the MCC-128 (select_hat_device does not exist
# in all versions of daqhats).
from daqhats import (
    mcc128,
    HatIDs,
    OptionFlags,
    AnalogInputMode,
    AnalogInputRange,
    hat_list,
)


def _find_mcc128_address() -> int:
    """Find the first MCC-128 HAT address. Raises if none found."""
    hats = hat_list(filter_by_id=HatIDs.MCC_128)
    if not hats:
        raise RuntimeError(
            "No MCC-128 HAT found. Check that the board is seated correctly "
            "and the daqhats library is installed."
        )
    return hats[0].address


class DaqAdapter:
    """
    DAQ adapter for the MCC-128 HAT. Real hardware only.

    Public API:
      - open()
      - close()
      - capture_window(seconds) -> (t, v_force, v_res)
      - start_continuous_scan()
      - read_continuous_scan(samples_per_channel=-1, timeout_s=0.0) -> (v_force_list, v_res_list)
      - stop_continuous_scan()
    """

    def __init__(self, channels: Iterable[int] = (0, 1), rate_hz: float = 1000.0):
        self.channels: List[int] = [int(ch) for ch in channels]
        if not self.channels:
            self.channels = [0, 1]

        self.rate: float = float(rate_hz)
        self.hat: Optional[mcc128] = None
        self._is_open = False

        self._scan_running = False
        self._scan_channel_count = max(1, len(self.channels))
        self._scan_chan_mask = 0
        for ch in self.channels:
            self._scan_chan_mask |= (1 << int(ch))

    @property
    def is_open(self) -> bool:
        return self._is_open

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def open(self) -> None:
        """Open the MCC-128. Raises if hardware not found."""
        if self._is_open:
            return
        addr = _find_mcc128_address()
        self.hat = mcc128(addr)
        # Set differential input mode — must be done before any a_in_read
        # or a_in_scan_start call.  All channels share one mode per device.
        self.hat.a_in_mode_write(AnalogInputMode.DIFF)
        # Set input range to ±10 V — MUST be done or device defaults to ±1 V,
        # which causes signals above 1 V to clip and scan to return zeros.
        self.hat.a_in_range_write(AnalogInputRange.BIP_10V)
        self._is_open = True
        print(f"[DAQ] MCC-128 opened at address {addr} — mode: DIFFERENTIAL  range: BIP_10V")

    def close(self) -> None:
        """Close the MCC-128."""
        if not self._is_open:
            return
        try:
            self.stop_continuous_scan()
        except Exception:
            pass
        self.hat = None
        self._is_open = False
        print("[DAQ] MCC-128 closed")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _force_res_channel_indices(self) -> Tuple[int, int]:
        ch_force = self.channels[0] if len(self.channels) > 0 else 0
        ch_res   = self.channels[1] if len(self.channels) > 1 else ch_force
        return ch_force, ch_res

    def _deinterleave_scan_data(
        self, data: np.ndarray
    ) -> Tuple[List[float], List[float]]:
        if data is None or data.size == 0:
            return [], []
        nch = max(1, int(self._scan_channel_count))
        if nch == 1:
            out = np.asarray(data, dtype=float).tolist()
            return out, out
        ch0 = np.asarray(data[0::nch], dtype=float)
        ch1 = np.asarray(data[1::nch], dtype=float)
        return ch0.tolist(), ch1.tolist()

    # ------------------------------------------------------------------
    # Polled capture
    # ------------------------------------------------------------------

    def capture_window(self, seconds: float) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """
        Polled capture from the MCC-128.
        Returns (t, v_force, v_res) as numpy arrays.
        Raises if the device is not open.
        """
        if not self._is_open or self.hat is None:
            raise RuntimeError("DAQ not open — call open() first")

        seconds   = max(1e-4, float(seconds))
        n_samples = max(1, int(round(self.rate * seconds)))

        ch_force, ch_res = self._force_res_channel_indices()

        v_force = np.empty(n_samples, dtype=float)
        v_res   = np.empty(n_samples, dtype=float)

        dt_target = 1.0 / self.rate if self.rate > 0 else 0.0
        t0 = time.monotonic()

        for i in range(n_samples):
            v_force[i] = float(self.hat.a_in_read(ch_force))
            v_res[i]   = float(self.hat.a_in_read(ch_res))

            if dt_target > 0:
                target    = t0 + (i + 1) * dt_target
                remaining = target - time.monotonic()
                while remaining > 0.0005:
                    time.sleep(remaining * 0.5)
                    remaining = target - time.monotonic()

        t1   = time.monotonic()
        span = max(1e-9, t1 - t0)
        t    = np.linspace(0.0, span, n_samples, endpoint=False, dtype=float)
        return t, v_force, v_res

    # ------------------------------------------------------------------
    # Continuous scan
    # ------------------------------------------------------------------

    def start_continuous_scan(self) -> None:
        """Start hardware-paced continuous scan. Raises if device not open."""
        if self._scan_running:
            return
        if not self._is_open or self.hat is None:
            raise RuntimeError("DAQ not open — call open() first")

        self._scan_channel_count = max(1, len(self.channels))
        self.hat.a_in_scan_start(
            self._scan_chan_mask,
            0,
            float(self.rate),
            OptionFlags.CONTINUOUS,
        )
        self._scan_running = True

    def read_continuous_scan(
        self,
        samples_per_channel: int = -1,
        timeout_s: float = 0.0,
    ) -> Tuple[List[float], List[float]]:
        """Drain samples from the scan buffer. Returns (v_force_list, v_res_list)."""
        if not self._scan_running:
            return [], []
        if self.hat is None:
            raise RuntimeError("DAQ not open")
        try:
            out  = self.hat.a_in_scan_read_numpy(
                int(samples_per_channel), float(timeout_s)
            )
            data = out.data
        except Exception:
            return [], []
        return self._deinterleave_scan_data(data)

    def stop_continuous_scan(self) -> None:
        """Stop scan and clean up the driver buffer."""
        if not self._scan_running:
            return
        if self.hat is not None:
            try:
                self.hat.a_in_scan_stop()
            except Exception:
                pass
            try:
                self.hat.a_in_scan_cleanup()
            except Exception:
                pass
        self._scan_running = False
