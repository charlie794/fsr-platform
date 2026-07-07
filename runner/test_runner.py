# runner/test_runner.py
from __future__ import annotations

import math
import os
import re
import threading
import time
from dataclasses import dataclass
from collections import deque
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np
from PyQt5.QtCore import QObject, pyqtSignal

# ---------------------------------------------------------------------------
# Imports — two-level fallback (package vs flat)
# ---------------------------------------------------------------------------
try:
    from Sensor_Testor.domain import models
    from Sensor_Testor.domain.models import RunConfig, TestStep, runtime_state, store
except Exception:
    import domain.models as models          # type: ignore
    from domain.models import RunConfig, TestStep, runtime_state, store  # type: ignore

try:
    from Sensor_Testor.runner.path_utils import resolve_criteria_path
except Exception:
    try:
        from runner.path_utils import resolve_criteria_path  # type: ignore
    except Exception:
        resolve_criteria_path = None  # type: ignore

try:
    from Sensor_Testor.processing.filters import ButterworthLP
except Exception:
    try:
        from processing.filters import ButterworthLP  # type: ignore
    except Exception:
        from filters import ButterworthLP  # type: ignore


# ---------------------------------------------------------------------------
# Resistance model — power_rational only (RationalVRModel / txt-file fallback
# removed: power_rational from models.py is the current standard everywhere)
# ---------------------------------------------------------------------------

class PowerRationalModel:
    """R(V) = (k * V / (Vmax - V)) ^ (1/n)"""

    def __init__(self, Vmax: float, k: float, n: float):
        self.Vmax = float(Vmax)
        self.k    = float(k)
        self.n    = float(n)

    def r_from_v_array(self, v_arr: np.ndarray) -> np.ndarray:
        v     = np.asarray(v_arr, dtype=float)
        denom = self.Vmax - v
        with np.errstate(divide="ignore", invalid="ignore"):
            r = np.where(
                (denom > 0) & (v > 0),
                (self.k * v / denom) ** (1.0 / self.n),
                np.nan,
            )
        return r


def _parse_power_rational(cal_str: str) -> Optional[PowerRationalModel]:
    """Parse 'model=power_rational; Vmax=...; k=...; n=...' string."""
    try:
        Vmax = float(re.search(r"Vmax=([^\s;]+)", cal_str).group(1))
        k    = float(re.search(r"k=([^\s;]+)",    cal_str).group(1))
        n    = float(re.search(r"n=([^\s;]+)",    cal_str).group(1))
        return PowerRationalModel(Vmax=Vmax, k=k, n=n)
    except Exception:
        return None


def _get_resistance_model() -> Optional[PowerRationalModel]:
    """Read models.py and return a PowerRationalModel, or None."""
    try:
        cal_str = getattr(models, "latest_resistance_calibration", "") or ""
        if "power_rational" in cal_str:
            return _parse_power_rational(cal_str)
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Force calibration parsing  y = m * (x - c)
# ---------------------------------------------------------------------------

def _parse_force_cal(eq: str) -> Tuple[float, float]:
    """Return (m, c) for  force_kg = m * (CH0_V - c)."""
    if not eq:
        return 1.0, 0.0
    s = eq.strip().lower().replace(" ", "")
    if s.startswith("y="):
        s = s[2:]

    # m*(x-c) or m*(x+c)
    m = re.fullmatch(r"([+\-]?\d*\.?\d+(?:e[+\-]?\d+)?)\*\(x([+\-]\d*\.?\d+(?:e[+\-]?\d+)?)\)", s)
    if m:
        return float(m.group(1)), -float(m.group(2))

    # (x-c)*m or (x+c)*m
    m = re.fullmatch(r"\(x([+\-]\d*\.?\d+(?:e[+\-]?\d+)?)\)\*([+\-]?\d*\.?\d+(?:e[+\-]?\d+)?)", s)
    if m:
        return float(m.group(2)), -float(m.group(1))

    # m*x+b
    m = re.fullmatch(r"([+\-]?\d*\.?\d+(?:e[+\-]?\d+)?)\*x([+\-]\d*\.?\d+(?:e[+\-]?\d+)?)", s)
    if m:
        mm, b = float(m.group(1)), float(m.group(2))
        return mm, (-b / mm if abs(mm) > 1e-12 else 0.0)

    return 1.0, 0.0


# ---------------------------------------------------------------------------
# Smooth curve interpolation for Force × Resistance plot
#
# The physical relationship between force and resistance is approximately
# R ∝ 1/F (a hyperbolic / 1/x curve).  pyqtgraph's default setData draws
# straight lines between sample points, which produces a staircase on a
# log-resistance axis and misrepresents the shape between samples.
#
# Fix: interpolate in log(R) space using a monotone cubic (Pchip) spline,
# then densify to ~500 pts so the rendered curve follows the 1/x shape.
# Only called for the Force × Resistance plot — the sample-number plots
# use raw index spacing and don't need this treatment.
# ---------------------------------------------------------------------------

def _smooth_force_resistance(
    force_kg: np.ndarray,
    res_ohm: np.ndarray,
    n_out: int = 500,
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Return a densified (f_smooth, r_smooth) pair that follows the natural
    1/x curve between measured points.

    Steps:
      1. Drop NaN / non-positive resistance values.
      2. Sort by force (x axis).
      3. Deduplicate force values (pchip requires strictly monotone x).
      4. Fit a Pchip spline in log10(R) space.
      5. Evaluate on a fine grid and exponentiate back to linear R.

    Falls back to the raw arrays if anything goes wrong (< 2 valid points,
    scipy unavailable, etc.).
    """
    try:
        from scipy.interpolate import PchipInterpolator
    except ImportError:
        return force_kg, res_ohm

    try:
        f = np.asarray(force_kg, dtype=float)
        r = np.asarray(res_ohm,  dtype=float)

        # Keep only finite, positive-resistance points
        valid = np.isfinite(f) & np.isfinite(r) & (r > 0)
        f, r  = f[valid], r[valid]
        if len(f) < 2:
            return f, r

        # Sort by force
        order = np.argsort(f)
        f, r  = f[order], r[order]

        # Deduplicate (Pchip needs strictly increasing x)
        _, idx = np.unique(f, return_index=True)
        f, r   = f[idx], r[idx]
        if len(f) < 2:
            return f, r

        log_r = np.log10(r)
        spline = PchipInterpolator(f, log_r)

        f_fine   = np.linspace(f[0], f[-1], n_out)
        r_fine   = 10.0 ** spline(f_fine)

        return f_fine.astype(np.float32), r_fine.astype(np.float32)

    except Exception:
        return force_kg, res_ohm


# ---------------------------------------------------------------------------
# Step result
# ---------------------------------------------------------------------------

@dataclass
class StepRunResult:
    passed: bool
    threshold_reached: bool
    criteria_failed: bool
    criteria_message: str = ""
    failure_reason: str = ""
    raw_force_v: Optional[List[float]] = None
    raw_res_v: Optional[List[float]] = None
    processed_force: Optional[List[float]] = None
    processed_resistance: Optional[List[float]] = None


# ---------------------------------------------------------------------------
# TestRunnerWorker
# ---------------------------------------------------------------------------

class TestRunnerWorker(QObject):
    """
    Executes one test step at a time.

    DAQ loop mirrors the oscilloscope exactly:
      - Continuous scan, check samples_available, read numpy, process in bulk.
      - Force filtered with a stateful Butterworth LP (10 Hz cutoff).
      - Resistance via PowerRationalModel.r_from_v_array.
      - Pre-allocated doubling numpy buffer; GUI 30 Hz timer calls ring_snapshot().
      - No per-sample signals.

    The Force × Resistance plot data is passed through _smooth_force_resistance()
    before setData so lines follow the natural 1/x hyperbolic shape.
    """

    progress   = pyqtSignal(int, str)
    result     = pyqtSignal(int, bool)
    finished   = pyqtSignal()
    error      = pyqtSignal(str)
    sample_ready = pyqtSignal(float, float, int)   # back-compat, not used for plotting
    step_started = pyqtSignal(int)
    log_message  = pyqtSignal(str)

    def __init__(
        self,
        cfg: RunConfig,
        steps: Iterable[TestStep],
        criteria: Dict[str, Any],
        duet: Any,
        smac: Any,
        daq: Any,
        writers: Any,
        terminal_logger: Optional[Any] = None,
    ):
        super().__init__()
        self.cfg      = cfg
        self.steps    = list(steps or [])
        self.criteria = criteria or {}
        self.duet     = duet
        self.smac     = smac
        self.daq      = daq
        self.writers  = writers
        self.terminal_logger = terminal_logger

        self.force_ch = 0
        self.res_ch   = 2

        # DAQ live loop state
        self._live_stop:             Optional[threading.Event] = None
        self._live_thread:           Optional[threading.Thread] = None
        self._live_trigger_enabled:  bool  = False
        self._live_triggered:        bool  = False
        self._live_threshold_v:      float = 0.1
        self._live_force_offset:     float = 0.0

        # Start-force gate — plotting/buffering only begins once this is
        # crossed. Separate from _live_threshold_v (the max-force stop trigger).
        self._live_start_force_kg:    float = 0.0
        self._live_started:           bool  = False

        # Processed data lists — filled during scan, passed to writers
        self._live_force_processed: List[float] = []
        self._live_res_processed:   List[float] = []
        self._collect_processed:    bool = False

        self._stop_requested: bool = False

        # Pre-allocated numpy ring buffers (doubling strategy, same as oscilloscope)
        _INIT = 4096
        self._pb_f    = np.empty(_INIT, dtype=np.float32)   # processed force (kg)
        self._pb_r    = np.empty(_INIT, dtype=np.float32)   # processed resistance (Ω)
        self._pb_rawf = np.empty(_INIT, dtype=np.float32)   # raw CH0 voltage
        self._pb_rawr = np.empty(_INIT, dtype=np.float32)   # raw CH2 voltage
        self._pb_n    = 0
        self._pb_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------
    def _log(self, msg: str) -> None:
        try:
            print(msg, flush=True)
        except Exception:
            pass
        try:
            self.log_message.emit(str(msg))
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Calibration helpers (read models.py directly — no caching layer)
    # ------------------------------------------------------------------
    def _force_m_c(self) -> Tuple[float, float]:
        eq = getattr(models, "latest_force_calibration", "") or ""
        return _parse_force_cal(str(eq))

    def _res_model(self) -> Optional[PowerRationalModel]:
        return _get_resistance_model()

    # ------------------------------------------------------------------
    # Live plot pre-arm  (mirrors oscilloscope _start_worker exactly)
    # ------------------------------------------------------------------
    def prearm_live_plot(self, step_idx: int) -> None:
        """Start DAQ continuous scan and launch live loop."""
        if self.daq is None:
            return

        try:
            self.step_started.emit(step_idx)
        except Exception:
            pass

        # Stop any previous scan cleanly
        if self._live_thread is not None and self._live_thread.is_alive():
            self.stop_live_plot()

        self._live_trigger_enabled = False
        self._live_triggered       = False
        self._live_started         = False
        self._live_stop            = threading.Event()

        try:
            self.daq.start_continuous_scan()
            self._log("[Live] Continuous scan started.")
            # Diagnostic — read scan status immediately after start
            hat = getattr(self.daq, "hat", None)
            if hat is not None:
                try:
                    import time as _t; _t.sleep(0.1)
                    st = hat.a_in_scan_status()
                    self._log(f"[Live] scan status 100ms after start: "
                              f"running={st.running}  "
                              f"samples_available={st.samples_available}  "
                              f"buffer_size={getattr(st,'buffer_size_samples',None)}")
                except Exception as e:
                    self._log(f"[Live] scan status check failed: {e}")
        except Exception as e:
            self._log(f"[Live] start_continuous_scan failed: {e}")
            self._live_stop.set()
            return

        # Snapshot calibration at start of each step
        force_m, force_c = self._force_m_c()
        res_mod = self._res_model()

        self._log(f"[Live] force: m={force_m:.6g}  c={force_c:.6g}")
        if res_mod:
            self._log(f"[Live] resistance: power_rational  Vmax={res_mod.Vmax:.4g}  k={res_mod.k:.4g}  n={res_mod.n:.4g}")
        else:
            self._log("[Live] resistance: no model found — plotting raw CH2 voltage")

        # Butterworth LP for force — 10 Hz cutoff, 1 kHz sample rate
        # Reset each step so there's no carryover between sensors.
        force_filter = ButterworthLP(cutoff_hz=10.0, sample_rate_hz=1000.0)

        # Stateful moving average — deques persist across DAQ chunk boundaries
        # so there's no reset artefact at chunk edges.
        _MA_WINDOW = 20
        _ma_f = deque(maxlen=_MA_WINDOW)
        _ma_r = deque(maxlen=_MA_WINDOW)

        def _apply_ma(arr: np.ndarray, buf: deque) -> np.ndarray:
            out = np.empty_like(arr)
            for i, v in enumerate(arr):
                buf.append(float(v))
                out[i] = sum(buf) / len(buf)
            return out

        def _live_loop() -> None:
            """
            Identical structure to the oscilloscope worker:
              1. Check samples_available.
              2. Read with a_in_scan_read_numpy (releases GIL).
              3. Deinterleave CH0 / CH2.
              4. Apply Butterworth LP to force.
              5. Apply resistance model.
              6. Push into ring buffer.
              7. Check threshold on RAW CH0 (offset-corrected).
            """
            total_raw  = 0
            CHUNK      = 50       # minimum read (~50 ms @ 1 kHz)
            logged_first = False

            while self._live_stop is not None and not self._live_stop.is_set():
                # ── Read DAQ ─────────────────────────────────────────────
                try:
                    hat = getattr(self.daq, "hat", None)
                    if hat is not None:
                        try:
                            avail  = hat.a_in_scan_status().samples_available
                            read_n = max(CHUNK, min(avail, CHUNK * 8))
                        except Exception:
                            read_n = CHUNK
                        res    = hat.a_in_scan_read_numpy(read_n, 0.1)
                        data   = res.data   # interleaved: [f0,r0, f1,r1, ...]
                        n_tot  = data.size // 2
                        if n_tot == 0:
                            time.sleep(0.002)
                            continue
                        fa = np.ascontiguousarray(data[0::2][:n_tot], dtype=np.float64)
                        ra = np.ascontiguousarray(data[1::2][:n_tot], dtype=np.float64)
                    else:
                        # Fallback to DaqAdapter.read_continuous_scan
                        f_list, r_list = self.daq.read_continuous_scan(
                            samples_per_channel=CHUNK, timeout_s=0.05
                        )
                        n_tot = min(len(f_list), len(r_list))
                        if n_tot == 0:
                            time.sleep(0.002)
                            continue
                        fa = np.array(f_list[:n_tot], dtype=np.float64)
                        ra = np.array(r_list[:n_tot], dtype=np.float64)
                except Exception as e:
                    self._log(f"[Live] DAQ read error: {e}")
                    time.sleep(0.01)
                    continue

                total_raw += n_tot

                # ── Apply equations (same as oscilloscope) ────────────────
                # Force: Butterworth LP then calibration then moving average
                fa_filt   = force_filter.process(fa)
                fa_kg     = force_m * (fa_filt - force_c) / 1000.0  # grams → kg
                fa_kg     = _apply_ma(fa_kg, _ma_f)

                # Resistance: power_rational model then moving average
                if res_mod is not None:
                    ra_ohm = res_mod.r_from_v_array(ra)
                else:
                    ra_ohm = ra.copy()
                ra_ohm = _apply_ma(ra_ohm, _ma_r)

                # Keep an untouched copy of the raw force chunk for the
                # max-force check below — the start-gate trims fa/ra/fa_kg/
                # ra_ohm in place, but max-force must see every sample.
                fa_raw_chunk = fa.copy()

                # ── Start-force gate ───────────────────────────────────────
                # Compare calibrated kg directly against start_force_kg —
                # same unit the user sets in the plan. Nothing pushed to the
                # ring buffer or accumulated until this is crossed, so nothing
                # appears on either graph until start force is reached.
                if not self._live_started:
                    above = np.flatnonzero(fa_kg >= float(self._live_start_force_kg))
                    if above.size > 0:
                        hit_idx = int(above[0])
                        self._live_started = True
                        self._log(
                            f"[Live] START force reached  "
                            f"force={float(fa_kg[hit_idx]):.4f}kg  "
                            f">= {self._live_start_force_kg:.4f}kg — plotting begins"
                        )
                        fa, ra, fa_kg, ra_ohm = fa[hit_idx:], ra[hit_idx:], fa_kg[hit_idx:], ra_ohm[hit_idx:]
                    else:
                        fa = fa[:0]; ra = ra[:0]; fa_kg = fa_kg[:0]; ra_ohm = ra_ohm[:0]

                # ── Push into ring buffer (only post-start-threshold data) ──
                n = fa.size
                if n > 0:
                    with self._pb_lock:
                        end = self._pb_n + n
                        if end > len(self._pb_f):
                            new_cap = max(end, len(self._pb_f) * 2)
                            self._pb_f    = np.resize(self._pb_f,    new_cap)
                            self._pb_r    = np.resize(self._pb_r,    new_cap)
                            self._pb_rawf = np.resize(self._pb_rawf, new_cap)
                            self._pb_rawr = np.resize(self._pb_rawr, new_cap)
                        self._pb_f   [self._pb_n:end] = fa_kg.astype(np.float32)
                        self._pb_r   [self._pb_n:end] = ra_ohm.astype(np.float32)
                        # Bottom graph also uses calibrated values — raw volts
                        # are not stored. Both graphs gate on start-force.
                        self._pb_rawf[self._pb_n:end] = fa.astype(np.float32)
                        self._pb_rawr[self._pb_n:end] = ra.astype(np.float32)
                        self._pb_n = end

                    # ── Accumulate for writers / criteria ─────────────────
                    if self._collect_processed:
                        self._live_force_processed.extend(fa_kg.tolist())
                        self._live_res_processed.extend(ra_ohm.tolist())

                # ── Max-force threshold check — runs on every raw sample
                # read this iteration, independent of the start gate, so the
                # probe stop never depends on whether plotting has begun. ───
                if self._live_trigger_enabled:
                    corr = np.abs(fa_raw_chunk - float(self._live_force_offset))
                    if np.any(corr >= float(self._live_threshold_v)):
                        self._live_triggered = True
                        self._log(
                            f"[Live] MAX-FORCE threshold HIT  "
                            f"|CH0-offset|={float(corr.max()):.5f}V  "
                            f">= {self._live_threshold_v:.5f}V"
                        )

                # ── Log first chunk ───────────────────────────────────────
                if not logged_first and n > 0:
                    logged_first = True
                    self._log(
                        f"[Live] first chunk n={n}  "
                        f"CH0={float(fa[0]):.5f}V  CH2={float(ra[0]):.5f}V  "
                        f"→ force={float(fa_kg[0]):.4f}kg  "
                        f"res={float(ra_ohm[0]):.4g}Ω"
                    )

            self._log(f"[Live] loop exited. total_raw={total_raw}")

        self._live_thread = threading.Thread(target=_live_loop, daemon=True)
        self._live_thread.start()

    def enable_live_trigger(self, threshold_v: float, force_offset: float = 0.0,
                             start_threshold_kg: float = 0.0) -> None:
        self._live_threshold_v        = float(threshold_v)
        self._live_force_offset       = float(force_offset)
        self._live_start_force_kg  = float(start_threshold_kg)
        self._live_triggered       = False
        self._live_started         = (start_threshold_kg <= 0.0)
        self._live_trigger_enabled = True

    def ring_snapshot(self) -> tuple:
        """Return (force_kg, resistance_ohm, raw_ch0, raw_ch2) arrays.
        Called by the GUI 30 Hz timer.  Thread-safe via _pb_lock.
        force_kg and resistance_ohm are passed through _smooth_force_resistance
        so the Force × Resistance plot follows the natural 1/x curve.
        """
        with self._pb_lock:
            n = self._pb_n
            if n == 0:
                empty = np.empty(0, dtype=np.float32)
                return empty, empty, empty, empty
            f   = self._pb_f   [:n].copy()
            r   = self._pb_r   [:n].copy()
            rf  = self._pb_rawf[:n].copy()
            rr  = self._pb_rawr[:n].copy()

        # Smooth F×R for plotting (1/x shape via log-space Pchip spline)
        f_smooth, r_smooth = _smooth_force_resistance(f, r)
        return f_smooth, r_smooth, rf, rr

    def stop_live_plot(self) -> None:
        if self._live_stop is not None:
            self._live_stop.set()
        # Stop DAQ first so the read call inside the loop returns immediately
        try:
            if self.daq is not None and hasattr(self.daq, "stop_continuous_scan"):
                self.daq.stop_continuous_scan()
        except Exception:
            pass
        try:
            if self._live_thread is not None:
                self._live_thread.join(timeout=1.0)
        except Exception:
            pass
        self._live_thread           = None
        self._live_stop             = None
        self._live_trigger_enabled  = False
        self._live_triggered        = False

    # ------------------------------------------------------------------
    # Criteria helpers (unchanged logic, kept compact)
    # ------------------------------------------------------------------
    def _resolve_criteria_path(self, filename: Optional[str]) -> Optional[str]:
        if resolve_criteria_path is not None:
            return resolve_criteria_path(filename)
        return os.path.abspath(str(filename)) if filename else None

    def _load_criteria_csv(self, path: str):
        import csv
        xs, maxs, mins = [], [], []
        with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                return [], None, None
            def norm(h): return (h or "").strip().lower().replace(" ", "")
            fields = {norm(h): h for h in reader.fieldnames if h}
            def pick(*cands):
                for c in cands:
                    if c in fields: return fields[c]
                return None
            col_x   = pick("value", "x", "force", "forces", "f")
            col_max = pick("max", "ymax", "maxresistance", "rmax", "upper")
            col_min = pick("min", "ymin", "minresistance", "rmin", "lower")
            if col_x is None:
                return [], None, None
            for row in reader:
                xv = (row.get(col_x) or "").strip()
                if not xv:
                    continue
                try:
                    xs.append(float(xv))
                except Exception:
                    continue
                if col_max:
                    try: maxs.append(float((row.get(col_max) or "").strip()))
                    except: maxs.append(float("nan"))
                if col_min:
                    try: mins.append(float((row.get(col_min) or "").strip()))
                    except: mins.append(float("nan"))
        if len(xs) >= 2:
            pairs = sorted(zip(xs,
                               maxs if maxs else [float("nan")] * len(xs),
                               mins if mins else [float("nan")] * len(xs)),
                           key=lambda t: t[0])
            xs   = [p[0] for p in pairs]
            maxs = [p[1] for p in pairs] if maxs else []
            mins = [p[2] for p in pairs] if mins else []
        return xs, (maxs if maxs and len(maxs) == len(xs) else None), \
                   (mins if mins and len(mins) == len(xs) else None)

    def _interp(self, xs, ys, xq):
        if not xs: return float("nan")
        if xq <= xs[0]:  return ys[0]
        if xq >= xs[-1]: return ys[-1]
        lo, hi = 0, len(xs) - 1
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if xs[mid] <= xq: lo = mid
            else:              hi = mid
        x0, x1, y0, y1 = xs[lo], xs[hi], ys[lo], ys[hi]
        if not (math.isfinite(y0) and math.isfinite(y1)) or abs(x1 - x0) < 1e-12:
            return float("nan")
        return y0 + (y1 - y0) * (xq - x0) / (x1 - x0)

    def _check_criteria(self, xs, y_max, y_min):
        if not self._live_force_processed or not self._live_res_processed:
            return False, "no live data"
        if not xs or (y_max is None and y_min is None):
            return False, "no criteria"
        x_lo, x_hi = xs[0], xs[-1]
        for f, r in zip(self._live_force_processed, self._live_res_processed):
            try:
                ff, rr = float(f), float(r)
            except Exception:
                continue
            if not (math.isfinite(ff) and math.isfinite(rr)):
                continue
            if ff < x_lo or ff > x_hi:
                continue
            if y_max is not None:
                yb = self._interp(xs, y_max, ff)
                if math.isfinite(yb) and rr > yb:
                    return True, f"Above MAX at {ff:.4g} kg (R={rr:.4g} > {yb:.4g})"
            if y_min is not None:
                yb = self._interp(xs, y_min, ff)
                if math.isfinite(yb) and rr < yb:
                    return True, f"Below MIN at {ff:.4g} kg (R={rr:.4g} < {yb:.4g})"
        return False, "within envelope"

    # ------------------------------------------------------------------
    # Core: execute one step
    # ------------------------------------------------------------------
    def run_step(self, step: TestStep) -> StepRunResult:
        if not hasattr(self, "_step_counter"):
            self._step_counter = 0
        self._step_counter += 1
        idx = self._step_counter

        _tid = getattr(step, "test_id", f"step{idx}")
        self._log(f"\n[Step] ╔══ STEP {idx} START  id={_tid}"
                  f"  X={getattr(step,'x','?')}  Y={getattr(step,'y','?')}"
                  f"  force_target={getattr(step,'force_target','?')}kg"
                  f"  v_test={getattr(step,'v_test','?')}mm/s")

        reached          = False
        criteria_failed  = False
        criteria_msg     = ""
        raw_force_v: List[float] = []
        raw_res_v:   List[float] = []

        try:
            # 1. Reset per-step state
            self.stop_live_plot()
            self._live_force_processed = []
            self._live_res_processed   = []
            self._collect_processed    = True
            with self._pb_lock:
                self._pb_n = 0

            # 2. Pre-arm DAQ scan
            self.prearm_live_plot(idx)

            # Read motion parameters live from store.settings so CSV changes
            # take effect immediately without restarting the test.
            S = getattr(store, "settings", {}) or {}
            def _s(key, default):
                try: return float(S.get(key) or default)
                except Exception: return float(default)

            step_safe_z     = _s("Safe Height (mm)", 10.0)
            test_height_mm  = _s("Test Height (mm)", 0.0)
            v_travel_mm_min = _s("speed between spaces(mm/s)", 3000.0)
            feed_mm_min     = _s("actuator speed(mm/s)", 60.0)
            if feed_mm_min <= 0:  feed_mm_min = 60.0
            if v_travel_mm_min <= 0: v_travel_mm_min = 3000.0
            v_test_mm_s = feed_mm_min / 60.0  # for timeout only

            # Pre-press approach height = soft touch contact Z + Test Height.
            # Safe Height is only used for XY-travel clearance (handled in
            # grid_runner before this step starts) — it is NOT the pre-press
            # height. If no soft touch result exists yet, fall back to
            # Safe Height so the step doesn't crash, but log a clear warning.
            st_result = getattr(runtime_state, "last_soft_touch", None)
            soft_touch_z = getattr(st_result, "z", None) if st_result else None

            if soft_touch_z is not None:
                approach_z = float(soft_touch_z) + test_height_mm
                self._log(f"[Step] approach Z = soft_touch_z({soft_touch_z:.4f}) "
                          f"+ test_height({test_height_mm:.4f}) = {approach_z:.4f} mm")
            else:
                approach_z = step_safe_z
                self._log(f"[Step] WARNING: no soft touch result available — "
                          f"using Safe Height ({step_safe_z:.4f} mm) as approach Z. "
                          f"Run Soft Touch first for correct pre-press positioning.")

            try:
                self.duet.send_gcode("G90")
                self.duet.send_gcode(f"G1 Z{approach_z:.3f} F{v_travel_mm_min:.0f}")
                self.duet.send_gcode("M400")
                self._log(f"[Step] approach Z={approach_z:.3f} mm  F{v_travel_mm_min:.0f}")
            except Exception as e:
                self._log(f"[Step] Approach-Z move error: {e}")

            travel_mm    = max(15.0, approach_z + 5.0)
            max_time     = (travel_mm / v_test_mm_s) + 10.0

            # Force threshold: 'max force(kg)' from the Settings block is the
            # primary source. Per-row 'Force' column (step.force_target) is
            # used only if the row explicitly overrides it.
            max_force_kg = _s("max force(kg)", 0.0)
            force_target_kg = max_force_kg if max_force_kg > 0 else None
            try:
                row_force = float(step.force_target)
                if row_force > 0:
                    force_target_kg = row_force
            except Exception:
                pass

            threshold_v = 0.1      # default fallback (V delta)
            force_m, force_c = self._force_m_c()
            if force_target_kg is not None and abs(force_m) > 1e-9:
                # Invert: force_kg = m*(V-c)/1000 → V = force_kg*1000/m + c
                raw_at_target = (force_target_kg * 1000.0) / force_m + force_c
                threshold_v   = abs(raw_at_target - force_c)
                self._log(f"[Step] max_force={force_target_kg:.4f}kg  "
                          f"→ threshold_v={threshold_v:.5f}V")
            _FLOOR = 0.05
            if threshold_v < _FLOOR:
                self._log(f"[Step] threshold_v={threshold_v:.6f}V < floor={_FLOOR}V — clamped")
                threshold_v = _FLOOR

            # Start-force threshold: 'start force(kg)' from the Settings block.
            # Passed directly in kg — the gate in the live loop compares against
            # calibrated fa_kg so no voltage conversion needed here.
            # 0 or unset = start immediately.
            start_force_kg = _s("start force(kg)", 0.0)
            if start_force_kg >= (force_target_kg or 0.0) and start_force_kg > 0:
                self._log(f"[Step] WARNING: start_force ({start_force_kg:.4f}kg) >= "
                          f"max_force ({force_target_kg:.4f}kg) — using 0 (start immediately).")
                start_force_kg = 0.0
            else:
                self._log(f"[Step] start_force={start_force_kg:.4f}kg")

            # 5. Baseline: drain ~0.5 s of live scan for force offset
            force_offset = 0.0
            try:
                baseline = []
                t_bl = time.time()
                while time.time() - t_bl < 0.5:
                    hat = getattr(self.daq, "hat", None)
                    if hat is not None:
                        try:
                            avail = hat.a_in_scan_status().samples_available
                            if avail >= 20:
                                res  = hat.a_in_scan_read_numpy(20, 0.05)
                                data = res.data
                                n    = data.size // 2
                                baseline.extend(data[0::2][:n].tolist())
                        except Exception:
                            pass
                    time.sleep(0.02)
                if baseline:
                    force_offset = float(np.mean(baseline))
                self._log(f"[Step] baseline: {len(baseline)} samples  offset={force_offset:.5f}V")
            except Exception as e:
                self._log(f"[Step] baseline error (offset=0): {e}")

            # 6. Arm probe and send G38.2
            try:
                self.duet.probe_arm()
                self._log("[Step] GPIO 17 HIGH — armed")
            except Exception as e:
                self._log(f"[Step] probe_arm error: {e}")

            try:
                self.duet.send_gcode(f'M558 P5 C"!io0.in" F{feed_mm_min:.0f}')
            except Exception as e:
                self._log(f"[Step] M558 error: {e}")

            self.enable_live_trigger(threshold_v, force_offset=force_offset,
                                     start_threshold_kg=start_force_kg)

            try:
                self.duet.send_gcode("G90")
                self.duet.send_gcode_nowait("G38.2 P0 Z0")
                self._log("[Step] G38.2 P0 Z0 sent")
            except Exception as e:
                self._log(f"[Step] G38.2 error: {e}")

            # 7. Poll for threshold or timeout
            t_start = time.time()
            while True:
                if time.time() - t_start >= max_time:
                    self._log("[Step] timeout")
                    break
                if self._live_triggered:
                    reached = True
                    self._log("[Step] threshold reached")
                    break
                if self._stop_requested:
                    self._log("[Step] stop requested")
                    break
                time.sleep(0.005)

            # 8. Release probe
            try:
                self.duet.probe_release()
                self._log("[Step] GPIO 17 LOW — released")
            except Exception as e:
                self._log(f"[Step] probe_release error: {e}")

            # 9. Read Z stop
            try:
                self.duet.send_gcode("G90")
                pos, _ = self.duet.get_position(ok_timeout_s=2.0)
                z_stop = pos.get("Z")
                if z_stop is not None:
                    self._log(f"[Step] Z stop = {z_stop:.4f} mm")
                    if runtime_state is not None:
                        runtime_state.last_step_z_stop = float(z_stop)
            except Exception as e:
                self._log(f"[Step] M114 error: {e}")

            # 10. Retract — return to safe Z (always up, never down)
            try:
                self.duet.send_gcode("G90")
                self.duet.send_gcode(f"G1 Z{step_safe_z:.3f} F{v_travel_mm_min:.0f}")
                self.duet.send_gcode("M400")
                self._log(f"[Step] retracted to safe Z={step_safe_z:.3f} mm")
            except Exception as e:
                self._log(f"[Step] retract error: {e}")

            self.stop_live_plot()
            self._collect_processed = False

            # 11. Criteria check
            criteria_failed = False
            criteria_msg    = ""
            # Criteria check is disabled while threshold triggering is being validated.
            # Re-enable by uncommenting the block below and removing the two lines above.
            # try:
            #     S = store.settings or {}
            #     if str(S.get("Pass Fail Criteria", False)).lower() in ("true","1","yes"):
            #         crit_name = getattr(step, "criteria_file", None)
            #         if crit_name:
            #             crit_path = self._resolve_criteria_path(str(crit_name))
            #             if crit_path and os.path.isfile(crit_path):
            #                 xs, y_max, y_min = self._load_criteria_csv(crit_path)
            #                 criteria_failed, criteria_msg = self._check_criteria(xs, y_max, y_min)
            # except Exception as e:
            #     self._log(f"[Criteria] error: {e}")

            # 12. Log data summary
            pf = np.array([v for v in self._live_force_processed if v == v], dtype=float)
            pr = np.array([v for v in self._live_res_processed   if v == v], dtype=float)
            self._log(f"[Step] data: {len(pf)} force + {len(pr)} res samples")
            if len(pf):
                self._log(f"[Step]   force kg: min={pf.min():.4f} mean={pf.mean():.4f} max={pf.max():.4f}")
            if len(pr):
                self._log(f"[Step]   res Ω:    min={pr.min():.4g} mean={pr.mean():.4g} max={pr.max():.4g}")

            passed = reached and not criteria_failed
            self._log(f"[Step] ╚══ RESULT: {'PASS' if passed else 'FAIL'}")

            # 13. Write results — pass ring buffer numpy arrays directly.
            # Raw = _pb_rawf/_pb_rawr (actual CH0/CH2 voltages).
            # Filtered = _pb_f/_pb_r (Butterworth force kg + power_rational ohms).
            # Both come from the same ring buffer positions so sample pairing
            # is guaranteed — sample i in raw corresponds to sample i in filtered.
            try:
                if self.writers is not None and hasattr(self.writers, "write_step_result"):
                    with self._pb_lock:
                        n = self._pb_n
                        raw_f   = self._pb_rawf[:n].copy()
                        raw_r   = self._pb_rawr[:n].copy()
                        filt_f  = self._pb_f   [:n].copy()
                        filt_r  = self._pb_r   [:n].copy()
                    self.writers.write_step_result(
                        step, raw_f, raw_r, filt_f, filt_r
                    )
            except Exception as e:
                import traceback
                self._log(f"[Writers] save error: {e}\n{traceback.format_exc()}")

            return StepRunResult(
                passed=passed,
                threshold_reached=reached,
                criteria_failed=criteria_failed,
                criteria_message=criteria_msg,
                failure_reason=(
                    ("threshold not reached" if not reached else "") +
                    (("; " + criteria_msg) if criteria_failed and criteria_msg else
                     ("; criteria failed" if criteria_failed else ""))
                ).strip("; "),
                raw_force_v=list(raw_force_v),
                raw_res_v=list(raw_res_v),
                processed_force=list(self._live_force_processed),
                processed_resistance=list(self._live_res_processed),
            )

        except Exception as e:
            self._log(f"[Step] fatal error: {e}")
            try:
                self.error.emit(str(e))
            except Exception:
                pass
            self.stop_live_plot()
            self._collect_processed = False
            return StepRunResult(
                passed=False, threshold_reached=False, criteria_failed=False,
                failure_reason=str(e),
                raw_force_v=list(raw_force_v), raw_res_v=list(raw_res_v),
                processed_force=list(self._live_force_processed),
                processed_resistance=list(self._live_res_processed),
            )

    # ------------------------------------------------------------------
    def run(self) -> bool:
        ok_all  = True
        n_steps = len(self.steps)
        self._log(f"[Run] ████ TEST RUN START — {n_steps} step(s) ████")
        for i, st in enumerate(self.steps, start=1):
            self._log(f"[Run] dispatching step {i}/{n_steps}")
            try:
                sr = self.run_step(st)
            except Exception as e:
                self._log(f"[Run] run_step({i}) raised: {e}")
                sr = StepRunResult(passed=False, threshold_reached=False, criteria_failed=False)
            passed = sr.passed if isinstance(sr, StepRunResult) else bool(sr)
            try:
                self.result.emit(i, passed)
            except Exception:
                pass
            ok_all = ok_all and passed
            self._log(f"[Run] step {i}/{n_steps} done — passed={passed}")
        self._log(f"[Run] ████ COMPLETE — {'PASS' if ok_all else 'FAIL'} ████")
        try:
            self.finished.emit()
        except Exception:
            pass
        return ok_all
