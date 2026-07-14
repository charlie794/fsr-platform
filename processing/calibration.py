# processing/calibration.py
"""
Single source of truth for sensor calibration.

Everything that turns raw DAQ voltage into physical units imports from this
module: the live test runner, the DAQ oscilloscope, and the equations
debugger.  Previously each carried its own copy of the parsing and
evaluation code, and the copies drifted apart:

  * test_runner divided force by 1000 (grams -> kg) while DAQ_oscilloscope
    divided by 9.81 (newtons -> kg), a ~102x disagreement, even though the
    oscilloscope's comment claimed the two were identical.
  * test_runner only recognised 'model=power_rational' while the resistance
    calibration UI had started writing 'model=rational', so the runner
    silently fell back to storing raw voltage in the resistance column.

Keeping one implementation here means a fix happens once.

Units
-----
Force calibration strings are  'y = m * (x - c)'  where x is CH0 volts.
Newer strings carry a '; units=kg' suffix and yield kilograms directly.
Legacy strings with no suffix came from the old grams-based calibration UI
and are converted (divided by 1000) on load, so calibrations saved before
the switch keep reading correctly.

Resistance calibration is always the power-rational model:
    R(V) = (k * V / (Vmax - V)) ** (1 / n)
"""

from __future__ import annotations

import re
from typing import Optional, Tuple

import numpy as np

GRAMS_PER_KG = 1000.0


# ---------------------------------------------------------------------------
# Force
# ---------------------------------------------------------------------------

class ForceModel:
    """force_kg = m * (V - c), with m already normalised to kg per volt."""

    __slots__ = ("m", "c")

    def __init__(self, m: float, c: float):
        self.m = float(m)
        self.c = float(c)

    def force_kg(self, v_arr) -> np.ndarray:
        v = np.asarray(v_arr, dtype=float)
        return self.m * (v - self.c)

    def __repr__(self) -> str:
        return f"ForceModel(kg = {self.m:.6g} * (V - {self.c:.6g}))"


def format_force_calibration(offset_v: float, gain_kg_per_v: float) -> str:
    """Build the canonical force calibration string (kg per volt)."""
    return f"y = {float(gain_kg_per_v):.8g} * (x - {float(offset_v):.8g}); units=kg"


def parse_force_calibration(eq: Optional[str]) -> ForceModel:
    """Parse a force calibration string into a ForceModel in kg/volt.

    Accepts both  'y = m * (x - c)'  and  'y = (x - c) * m'.
    If the string carries no 'units=' marker it is assumed to be the legacy
    grams-per-volt form and is converted to kg.
    """
    if not eq or not isinstance(eq, str):
        return ForceModel(1.0, 0.0)

    s = eq.strip()
    units = "g"
    um = re.search(r"units\s*=\s*([a-zA-Z]+)", s)
    if um:
        units = um.group(1).strip().lower()

    body = s.split(";")[0].lower().replace(" ", "")

    m_val: Optional[float] = None
    c_val: Optional[float] = None

    # y=m*(x-c)  /  y=m*(x+c)
    mm = re.search(r"y=(-?[0-9.eE+-]+)\*\(x([+-])([0-9.eE+-]+)\)", body)
    if mm:
        m_val = float(mm.group(1))
        c_val = float(mm.group(3))
        if mm.group(2) == "+":
            c_val = -c_val

    # y=(x-c)*m  /  y=(x+c)*m
    if m_val is None:
        mm = re.search(r"y=\(x([+-])([0-9.eE+-]+)\)\*(-?[0-9.eE+-]+)", body)
        if mm:
            c_val = float(mm.group(2))
            if mm.group(1) == "+":
                c_val = -c_val
            m_val = float(mm.group(3))

    if m_val is None or c_val is None:
        return ForceModel(1.0, 0.0)

    if units in ("g", "gram", "grams"):
        m_val = m_val / GRAMS_PER_KG

    return ForceModel(m_val, c_val)


# ---------------------------------------------------------------------------
# Resistance -- power-rational model
# ---------------------------------------------------------------------------

class PowerRationalModel:
    """R(V) = (k * V / (Vmax - V)) ** (1 / n)"""

    __slots__ = ("Vmax", "k", "n")

    def __init__(self, Vmax: float, k: float, n: float):
        self.Vmax = float(Vmax)
        self.k = float(k)
        self.n = float(n)

    def r_from_v_array(self, v_arr) -> np.ndarray:
        v = np.asarray(v_arr, dtype=float)
        denom = self.Vmax - v
        with np.errstate(divide="ignore", invalid="ignore"):
            return np.where(
                (denom > 0) & (v > 0),
                (self.k * v / denom) ** (1.0 / self.n),
                np.nan,
            )

    def v_from_r(self, r):
        """Inverse: V = Vmax * R^n / (k + R^n).  Used to draw the fitted curve."""
        r = np.asarray(r, dtype=float)
        rn = np.power(r, self.n)
        return self.Vmax * rn / (self.k + rn)

    def __repr__(self) -> str:
        return (f"PowerRationalModel(Vmax={self.Vmax:.6g}, "
                f"k={self.k:.6g}, n={self.n:.6g})")


def format_power_rational(Vmax: float, k: float, n: float) -> str:
    """Build the canonical resistance calibration string."""
    return (f"model=power_rational; Vmax={float(Vmax):.16g}; "
            f"k={float(k):.16g}; n={float(n):.16g}")


def parse_resistance_calibration(cal_str: Optional[str]) -> Optional[PowerRationalModel]:
    """Parse 'model=power_rational; Vmax=..; k=..; n=..' -> PowerRationalModel.

    Returns None if the string is missing, malformed, or is the obsolete
    'model=rational' polynomial form that this platform no longer uses.
    """
    if not cal_str or not isinstance(cal_str, str):
        return None
    if "power_rational" not in cal_str:
        return None
    try:
        Vmax = float(re.search(r"Vmax=([^\s;]+)", cal_str).group(1))
        k = float(re.search(r"\bk=([^\s;]+)", cal_str).group(1))
        n = float(re.search(r"\bn=([^\s;]+)", cal_str).group(1))
    except Exception:
        return None
    if not np.isfinite([Vmax, k, n]).all() or n == 0.0:
        return None
    return PowerRationalModel(Vmax, k, n)


def _lnr_sensitivity(V: np.ndarray, Vmax: float, n: float) -> np.ndarray:
    """d(ln R)/dV for the power-rational model.

    ln R = (1/n) * [ln k + ln V - ln(Vmax - V)]
      =>  d(ln R)/dV = (1/n) * [1/V + 1/(Vmax - V)]

    This blows up as V approaches Vmax, which is exactly why resistance
    readings become unreliable at the unloaded (high-R) end of the sensor.
    """
    denom = np.clip(Vmax - V, 1e-9, None)
    v = np.clip(V, 1e-9, None)
    return (1.0 / n) * (1.0 / v + 1.0 / denom)


def _fit_weights(V: np.ndarray, Vmax: float, n: float) -> np.ndarray:
    """Inverse-variance weights for a log-R least-squares fit.

    Assuming roughly constant voltage noise, the variance of each ln(R)
    residual is proportional to (d ln R / dV)^2, so the statistically optimal
    weight is the reciprocal of that.  Points recorded near Vmax -- where a
    millivolt of noise moves R by tens of percent -- are therefore downweighted
    automatically, in proportion to how noisy they actually are.  Nothing is
    hand-tuned and no point is discarded.
    """
    s = _lnr_sensitivity(V, Vmax, n)
    w = 1.0 / np.clip(s * s, 1e-30, None)
    m = float(np.mean(w))
    return w / m if m > 0 else np.ones_like(w)


def _linear_stage(lnR: np.ndarray, V: np.ndarray, vmax: float,
                  w: Optional[np.ndarray] = None):
    """Closed-form (k, n) for a fixed Vmax, optionally weighted."""
    denom = vmax - V
    if np.any(denom <= 1e-9):
        return None
    y = np.log(V / denom)
    A = np.vstack([lnR, np.ones_like(lnR)]).T
    if w is not None:
        sw = np.sqrt(w)
        A = A * sw[:, None]
        y = y * sw
    try:
        sol, *_ = np.linalg.lstsq(A, y, rcond=None)
    except Exception:
        return None
    n_, neg_ln_k = sol
    if not np.isfinite(n_) or n_ <= 0:
        return None
    return float(np.exp(-neg_ln_k)), float(n_)


def fit_power_rational(R, V, weighted: bool = True
                       ) -> Optional[Tuple[float, float, float]]:
    """Fit R = (k*V/(Vmax-V))**(1/n) to calibration points.

    Returns (Vmax, k, n) or None.

    For a fixed Vmax the model linearises, because
        n*ln(R) = ln(k) + ln(V / (Vmax - V))
    so a least-squares line of ln(V/(Vmax-V)) against ln(R) yields n (the
    slope) and k (from the intercept).  Vmax is found by a coarse scan over
    (max(V), 3*max(V)], then a nonlinear polish refines all three together.

    Residuals are taken in log-R space because R spans decades; an
    absolute-error fit would be dominated by the largest resistances.

    With weighted=True (the default) each residual is scaled by an
    inverse-variance weight derived from d(ln R)/dV -- see _fit_weights.
    Because those weights depend on the parameters being fitted, they are
    held fixed inside each solve and refreshed between solves (iteratively
    reweighted least squares); recomputing them inside the residual would let
    the optimiser lower its own cost by inflating Vmax.

    On noise-free data the weighted and unweighted fits agree exactly.  On
    noisy data, weighting cuts the error roughly sevenfold across the loaded
    (low-resistance) range the sensor actually operates in, at the cost of a
    few percent at the unloaded near-open-circuit end.
    """
    R = np.asarray(R, dtype=float)
    V = np.asarray(V, dtype=float)
    mask = np.isfinite(R) & np.isfinite(V) & (R > 0) & (V > 0)
    R, V = R[mask], V[mask]
    if R.size < 3:
        return None

    lnR = np.log(R)
    v_top = float(V.max())

    def _sse(vmax: float) -> float:
        p = _linear_stage(lnR, V, vmax)
        if p is None:
            return np.inf
        k_, n_ = p
        with np.errstate(all="ignore"):
            pred = (k_ * V / (vmax - V)) ** (1.0 / n_)
        if not np.all(np.isfinite(pred)) or np.any(pred <= 0):
            return np.inf
        r = np.log(pred) - lnR
        return float(r @ r)

    grid = np.linspace(v_top * (1.0 + 1e-9), v_top * 3.0, 600)
    scores = [_sse(v) for v in grid]
    best_i = int(np.argmin(scores))
    if not np.isfinite(scores[best_i]):
        return None

    v0 = float(grid[best_i])
    seed = _linear_stage(lnR, V, v0)
    if seed is None:
        return None
    k0, n0 = seed

    params = [v0, np.log(k0), n0]

    try:
        from scipy.optimize import least_squares
    except Exception:
        return float(params[0]), float(np.exp(params[1])), float(params[2])

    n_outer = 3 if weighted else 1
    for _ in range(n_outer):
        w = _fit_weights(V, params[0], params[2]) if weighted else None

        def _resid(q, _w=w):
            vmax, ln_k, n_ = q
            denom = vmax - V
            if np.any(denom <= 1e-12) or n_ <= 0:
                return np.full(R.size, 1e3)
            with np.errstate(all="ignore"):
                pred = (np.exp(ln_k) * V / denom) ** (1.0 / n_)
            pred = np.clip(pred, 1e-12, None)
            r = np.log(pred) - lnR
            return r * np.sqrt(_w) if _w is not None else r

        try:
            res = least_squares(
                _resid,
                params,
                bounds=([v_top * (1.0 + 1e-12), -50.0, 1e-3],
                        [v_top * 5.0, 50.0, 10.0]),
                method="trf",
                max_nfev=5000,
            )
            params = list(res.x)
        except Exception:
            break

    return float(params[0]), float(np.exp(params[1])), float(params[2])


# ---------------------------------------------------------------------------
# Convenience accessors -- read the current calibration from domain.models
# ---------------------------------------------------------------------------

def get_force_model() -> ForceModel:
    """ForceModel built from models.latest_force_calibration (kg per volt)."""
    try:
        from Sensor_Testor.domain import models
        return parse_force_calibration(getattr(models, "latest_force_calibration", ""))
    except Exception:
        return ForceModel(1.0, 0.0)


def get_resistance_model() -> Optional[PowerRationalModel]:
    """PowerRationalModel from models.latest_resistance_calibration, or None."""
    try:
        from Sensor_Testor.domain import models
        return parse_resistance_calibration(
            getattr(models, "latest_resistance_calibration", "")
        )
    except Exception:
        return None
