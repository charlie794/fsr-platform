# runner/test_runner.py
from __future__ import annotations

import math
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np
from PyQt5.QtCore import QObject, pyqtSignal

# ---------------------------------------------------------------------------
# Imports — two-level fallback (package vs flat)
# ---------------------------------------------------------------------------
from Sensor_Testor.domain import models
from Sensor_Testor.domain.models import RunConfig, TestStep, runtime_state, store
from Sensor_Testor.runner.path_utils import resolve_criteria_path

from Sensor_Testor.processing.stream_filter import StreamFilter
from Sensor_Testor.processing.calibration import (
    ForceModel,
    PowerRationalModel,
    get_force_model,
    get_resistance_model,
)


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
      - Raw voltage filtered with a streaming median+MA cascade.
      - Resistance via PowerRationalModel.r_from_v_array.
      - Pre-allocated doubling numpy buffer; GUI 30 Hz timer calls ring_snapshot().
      - No per-sample signals.

    The top graph shows filtered+calibrated force/resistance; the bottom
    graph shows the raw CH0/CH2 voltages. Live plotting pulls incremental
    tails via snapshot_tail() from the GUI timer.
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
    def _force_model(self) -> ForceModel:
        """Current force calibration, always kg per volt."""
        return get_force_model()

    def _res_model(self) -> Optional[PowerRationalModel]:
        return get_resistance_model()

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
        force_mod = self._force_model()
        res_mod = self._res_model()

        self._log(f"[Live] force: kg = {force_mod.m:.6g} * (V - {force_mod.c:.6g})")
        if res_mod:
            self._log(f"[Live] resistance: power_rational  Vmax={res_mod.Vmax:.4g}  k={res_mod.k:.4g}  n={res_mod.n:.4g}")
        else:
            self._log("[Live] *** WARNING: no valid power_rational resistance "
                      "calibration found. Resistance columns will contain RAW "
                      "CH2 VOLTS, not ohms. Re-run Resistance Calibration. ***")

        # Streaming spike+noise filters — one per channel, applied to the raw
        # voltage BEFORE the calibration equations (that's where the noise
        # lives). Reset each step so there's no carryover between sensors.
        force_filt = StreamFilter(med_w=5, ma_w=15)
        res_filt   = StreamFilter(med_w=5, ma_w=15)

        def _live_loop() -> None:
            """
            Identical structure to the oscilloscope worker:
              1. Check samples_available.
              2. Read with a_in_scan_read_numpy (releases GIL).
              3. Deinterleave CH0 / CH2.
              4. Filter raw voltage (median+MA), then calibrate.
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

                # ── Filter raw voltage, THEN apply calibration equations ──
                # Top graph = filtered+calibrated (_pb_f/_pb_r).
                # Bottom graph = raw voltage (_pb_rawf/_pb_rawr), set below.
                fa_f = force_filt.process(fa)   # filtered CH0 voltage
                ra_f = res_filt.process(ra)     # filtered CH2 voltage

                fa_kg = force_mod.force_kg(fa_f)   # volts → kg (units handled in model)

                if res_mod is not None:
                    ra_ohm = res_mod.r_from_v_array(ra_f)
                else:
                    # No calibration -> pass raw volts through. Loudly warned at
                    # step start; NaN would break plotting, so volts it is.
                    ra_ohm = ra_f.copy()

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
                        # Bottom graph uses the untouched raw voltages (CH0/CH2).
                        # Both graphs gate on start-force so they stay aligned.
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
        """Return the FULL (force_kg, resistance_ohm, raw_ch0, raw_ch2) arrays.

        Kept for back-compat. New plotting uses snapshot_tail() which copies
        only the newest samples each frame instead of the whole buffer.
        Top graph = filtered+calibrated force/resistance. Bottom = raw voltage.
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
        return f, r, rf, rr

    def snapshot_tail(self, have: int) -> tuple:
        """Incremental snapshot for live plotting.

        Given how many samples the caller already has, return only the NEW
        samples since then, plus the current total count:

            (total, force_kg, resistance_ohm, raw_ch0, raw_ch2)

        Only the new tail is copied (small, roughly constant per frame) and
        the lock is held just long enough for that copy, so the DAQ thread is
        never blocked by a growing full-buffer copy.

        If total < have the buffer was reset (new step began) — the caller
        should discard its accumulators and start fresh from the returned data.
        """
        with self._pb_lock:
            total = self._pb_n
            if total == 0:
                empty = np.empty(0, dtype=np.float32)
                return 0, empty, empty, empty, empty
            start = 0 if (have > total or have < 0) else have
            f  = self._pb_f   [start:total].copy()
            r  = self._pb_r   [start:total].copy()
            rf = self._pb_rawf[start:total].copy()
            rr = self._pb_rawr[start:total].copy()
        return total, f, r, rf, rr

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
            force_mod = self._force_model()
            if force_target_kg is not None and abs(force_mod.m) > 1e-9:
                # Invert: force_kg = m*(V-c)  ->  V = force_kg/m + c
                raw_at_target = force_target_kg / force_mod.m + force_mod.c
                threshold_v   = abs(raw_at_target - force_mod.c)
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
            # Filtered = _pb_f/_pb_r (filtered+calibrated force kg + resistance ohms).
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
