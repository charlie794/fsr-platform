# processing/criteria_check.py
"""
Single source of truth for pass/fail criteria evaluation.

This module consolidates what used to be spread across three places:
  - criteria_loader.py  (still owns CSV parsing + spline smoothing — reused here)
  - criteria_eval.py    (orphaned; its res_at_forces sampler is re-homed here)
  - test_runner.py      (its private _load_criteria_csv/_interp/_check_criteria
                         duplicate is removed in favour of this module)

The envelope is built from the SAME parser + smoothing the plot overlay uses
(criteria_loader.generate_smoothed_line), so the pass/fail boundary is exactly
the max/min curve the operator sees on screen — "red line between the lines".

Two public pieces:
  - CriteriaEnvelope      : force -> (max, min) resistance bounds, with per-sample
                            and vectorised checks.
  - LiveCriteriaChecker   : streaming per-sample check with consecutive-violation
                            debounce, used during the probe descent so a breach
                            stops the probe the same way max-force does.

Plus small helpers used by the summary sheet:
  - resistance_at_forces()
  - criteria_forces_from_file()
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

import numpy as np

try:
    from Sensor_Testor.processing.criteria_loader import (
        parse_pass_fail_criteria_form,
        generate_smoothed_line,
    )
except Exception:  # pragma: no cover - flat layout fallback
    try:
        from processing.criteria_loader import (  # type: ignore
            parse_pass_fail_criteria_form,
            generate_smoothed_line,
        )
    except Exception:  # pragma: no cover
        parse_pass_fail_criteria_form = None  # type: ignore
        generate_smoothed_line = None         # type: ignore


# ============================================================
# Envelope
# ============================================================

class CriteriaEnvelope:
    """Force -> resistance max/min envelope loaded from a criteria CSV.

    Uses the same smoothing as the plot overlay so evaluation matches the
    drawn curves exactly. Checks only apply inside the force range where the
    envelope is defined; outside that range a sample is never a violation.
    """

    def __init__(self, xs_max, y_max, xs_min, y_min):
        self._xmax = list(xs_max) if xs_max else []
        self._ymax = list(y_max) if y_max else []
        self._xmin = list(xs_min) if xs_min else []
        self._ymin = list(y_min) if y_min else []

        xs_all: List[float] = []
        if self._xmax:
            xs_all += [self._xmax[0], self._xmax[-1]]
        if self._xmin:
            xs_all += [self._xmin[0], self._xmin[-1]]
        self.f_lo = min(xs_all) if xs_all else None
        self.f_hi = max(xs_all) if xs_all else None

    @property
    def valid(self) -> bool:
        return bool((self._xmax and self._ymax) or (self._xmin and self._ymin))

    # --------------------------------------------------------
    @classmethod
    def from_file(cls, path: str) -> Optional["CriteriaEnvelope"]:
        if parse_pass_fail_criteria_form is None:
            return None
        try:
            max_pts, min_pts = parse_pass_fail_criteria_form(path)
        except Exception:
            return None

        def _prep(pts):
            if not pts:
                return [], []
            xs, ys = zip(*sorted(pts, key=lambda p: p[0]))
            if len(xs) >= 2 and generate_smoothed_line is not None:
                return generate_smoothed_line(xs, ys, num_points=200)
            return list(xs), list(ys)

        xmax, ymax = _prep(max_pts)
        xmin, ymin = _prep(min_pts)
        env = cls(xmax, ymax, xmin, ymin)
        return env if env.valid else None

    # --------------------------------------------------------
    def bounds_array(self, F) -> Tuple[np.ndarray, np.ndarray]:
        """Vectorised interpolated (max, min) bounds for a force array.

        Returns two arrays the same length as F; entries outside the defined
        force range (or where a bound is undefined) are NaN.
        """
        F = np.asarray(F, dtype=float)
        ymax = np.full(F.shape, np.nan, dtype=float)
        ymin = np.full(F.shape, np.nan, dtype=float)

        inrange = np.isfinite(F)
        if self.f_lo is not None:
            inrange &= (F >= self.f_lo) & (F <= self.f_hi)

        if self._xmax and inrange.any():
            ymax[inrange] = np.interp(F[inrange], self._xmax, self._ymax)
        if self._xmin and inrange.any():
            ymin[inrange] = np.interp(F[inrange], self._xmin, self._ymin)
        return ymax, ymin

    def bound_at(self, force: float, which: str) -> float:
        """Single interpolated bound ('max' or 'min') at one force, or NaN."""
        try:
            f = float(force)
        except Exception:
            return float("nan")
        if self.f_lo is None or not math.isfinite(f) or f < self.f_lo or f > self.f_hi:
            return float("nan")
        if which == "max" and self._xmax:
            return float(np.interp(f, self._xmax, self._ymax))
        if which == "min" and self._xmin:
            return float(np.interp(f, self._xmin, self._ymin))
        return float("nan")


# ============================================================
# Live streaming checker (debounced)
# ============================================================

class LiveCriteriaChecker:
    """Streaming per-sample envelope check with consecutive-violation debounce.

    Feed processed (force_kg, resistance_ohm) chunks during the descent. Once
    `debounce` consecutive samples fall outside the envelope, a violation is
    latched: the first offending sample of that run is captured (force,
    resistance, which bound, bound value) along with a human-readable message.

    Debounce guards against a single filtered-noise sample halting a good test.
    """

    def __init__(self, envelope: Optional[CriteriaEnvelope], debounce: int = 4):
        self.env = envelope
        self.debounce = max(1, int(debounce))
        self._run = 0
        self._first: Optional[Tuple[float, float, str, float]] = None

        self.violated = False
        self.force = float("nan")
        self.resistance = float("nan")
        self.kind = ""            # 'max' or 'min'
        self.bound = float("nan")
        self.message = ""

    def feed(self, forces, resistances) -> bool:
        """Process one chunk. Returns True if a violation is latched."""
        if self.violated or self.env is None:
            return self.violated

        F = np.asarray(forces, dtype=float)
        R = np.asarray(resistances, dtype=float)
        n = int(min(F.size, R.size))
        if n == 0:
            return False
        F = F[:n]
        R = R[:n]

        ymax, ymin = self.env.bounds_array(F)
        over = np.isfinite(ymax) & (R > ymax)
        under = np.isfinite(ymin) & (R < ymin)
        bad = over | under

        for i in range(n):
            if bad[i]:
                if self._run == 0:
                    if over[i]:
                        self._first = (float(F[i]), float(R[i]), "max", float(ymax[i]))
                    else:
                        self._first = (float(F[i]), float(R[i]), "min", float(ymin[i]))
                self._run += 1
                if self._run >= self.debounce and self._first is not None:
                    f, r, k, b = self._first
                    self.violated = True
                    self.force, self.resistance, self.kind, self.bound = f, r, k, b
                    if k == "max":
                        self.message = (
                            f"Over MAX resistance at {f:.3g} kg "
                            f"(read {r:.0f} Ω, max is {b:.0f} Ω)"
                        )
                    else:
                        self.message = (
                            f"Under MIN resistance at {f:.3g} kg "
                            f"(read {r:.0f} Ω, min is {b:.0f} Ω)"
                        )
                    return True
            else:
                self._run = 0
        return False


# ============================================================
# Summary-sheet helpers
# ============================================================

def resistance_at_forces(F_arr, R_arr, forces, window: float = 0.02) -> Dict[float, Optional[float]]:
    """Measured resistance at each target force (median within a small window).

    Falls back to the nearest sample if the window is empty; returns None for a
    force the test never reached (nearest sample further than 0.05 kg away).
    """
    out: Dict[float, Optional[float]] = {}
    F = np.asarray(F_arr, dtype=float)
    R = np.asarray(R_arr, dtype=float)
    good = np.isfinite(F) & np.isfinite(R)
    F, R = F[good], R[good]
    for tf in forces:
        tf = float(tf)
        if F.size == 0:
            out[tf] = None
            continue
        m = np.abs(F - tf) <= window
        if int(m.sum()) >= 1:
            out[tf] = float(np.median(R[m]))
        else:
            j = int(np.argmin(np.abs(F - tf)))
            out[tf] = float(R[j]) if abs(float(F[j]) - tf) <= 0.05 else None
    return out


def criteria_forces_from_file(path: str) -> List[float]:
    """Sorted distinct force values that appear in the criteria file."""
    if parse_pass_fail_criteria_form is None:
        return []
    try:
        max_pts, min_pts = parse_pass_fail_criteria_form(path)
    except Exception:
        return []
    fs = sorted({round(float(p[0]), 4) for p in (list(max_pts) + list(min_pts))})
    return fs
