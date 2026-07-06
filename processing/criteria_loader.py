from __future__ import annotations

import csv
import os
import re
from typing import Dict, List, Literal, Tuple

import numpy as np
from scipy.interpolate import UnivariateSpline


# ============================================================
# Small cache: keyed by absolute path + mtime_ns
# ============================================================

_PARSE_CACHE: Dict[tuple[str, int], Tuple[List[Tuple[float, float]], List[Tuple[float, float]]]] = {}


# ============================================================
# Utilities
# ============================================================

def _norm(s: str) -> str:
    return re.sub(r"[\s_]+", "", (s or "").strip().lower())


def _parse_num(s):
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None

    s_clean = s.replace(" ", "")

    # single comma = decimal separator; otherwise commas are thousands separators
    if "." not in s_clean and s_clean.count(",") == 1:
        s_clean = s_clean.replace(",", ".")
    else:
        s_clean = s_clean.replace(",", "")

    try:
        return float(s_clean)
    except ValueError:
        return None


def _classify_type(t):
    s = (t or "").lower()
    if "resist" in s:
        return "resistance"
    if "force" in s:
        return "force"
    return None


def _find_column(headers, *cands):
    header_map = {_norm(h): h for h in (headers or []) if h is not None}
    flat = [c for group in cands for c in (group if isinstance(group, (list, tuple)) else [group])]

    for cand in flat:
        key = _norm(cand)
        if key in header_map:
            return header_map[key]

    for hnorm, orig in header_map.items():
        if any(_norm(c) in hnorm for c in flat):
            return orig

    return None


def _get_cache_key(path: str) -> tuple[str, int] | None:
    try:
        abs_path = os.path.abspath(path)
        stat = os.stat(abs_path)
        return abs_path, int(stat.st_mtime_ns)
    except Exception:
        return None


# ============================================================
# Public API (CSV only)
# ============================================================

def parse_pass_fail_criteria_form(criteria_file_path: str) -> Tuple[List[Tuple[float, float]], List[Tuple[float, float]]]:
    """
    Parse a plain CSV file and return:

        (max_points, min_points)

    where each entry is:
        (force, resistance)

    Accepts:
      - comma / semicolon / tab delimiters
      - decimal commas
      - either "force at resistance" style or "resistance at force" style
    """
    cache_key = _get_cache_key(criteria_file_path)
    if cache_key is not None and cache_key in _PARSE_CACHE:
        cached_max, cached_min = _PARSE_CACHE[cache_key]
        return list(cached_max), list(cached_min)

    with open(criteria_file_path, "r", encoding="utf-8", errors="replace", newline="") as f:
        sample = f.read(4096)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except Exception:
            dialect = csv.excel

        rdr = csv.DictReader(f, dialect=dialect)
        headers = list(rdr.fieldnames or [])
        rows = list(rdr)

    h_type = _find_column(headers, ["type", "rule", "criterion", "criteria", "limit type"])
    h_val = _find_column(headers, ["value", "target", "setpoint", "at", "x", "axis value"])
    h_max = _find_column(headers, ["max", "upper", "upper limit", "max value", "high"])
    h_min = _find_column(headers, ["min", "lower", "lower limit", "min value", "low"])
    h_force = _find_column(headers, ["force", "force (n)", "force (x)", "x(force)"])
    h_res = _find_column(headers, ["resistance", "resistance (Ω)", "resistance (ohms)", "y(resistance)", "ohms", "Ω"])

    max_points: List[Tuple[float, float]] = []
    min_points: List[Tuple[float, float]] = []

    for row in rows:
        t_raw = (row.get(h_type, "") if h_type else "").strip()
        v_raw = (row.get(h_val, "") if h_val else "").strip()
        max_raw = (row.get(h_max, "") if h_max else "").strip()
        min_raw = (row.get(h_min, "") if h_min else "").strip()

        tclass = _classify_type(t_raw)

        if not v_raw and (h_force or h_res):
            if tclass == "force" and h_force:
                v_raw = (row.get(h_force, "") or "").strip()
            elif tclass == "resistance" and h_res:
                v_raw = (row.get(h_res, "") or "").strip()

        v_num = _parse_num(v_raw)
        max_num = _parse_num(max_raw)
        min_num = _parse_num(min_raw)

        if tclass == "force" and v_num is not None:
            if max_num is not None:
                max_points.append((v_num, max_num))
            if min_num is not None:
                min_points.append((v_num, min_num))

        elif tclass == "resistance" and v_num is not None:
            # "at specific resistance" rows define a HORIZONTAL criterion:
            # the value column is a resistance (Y axis) and max/min are force
            # bounds (X axis).  These must NOT be added to the force-vs-resistance
            # envelope because the force values (e.g. 0.0001 kg) end up as rogue
            # near-zero X coordinates that create a spike on the left edge of the
            # plotted curve.  Skip for plotting; pass/fail checking uses them
            # separately via the raw parsed points.
            pass

        elif h_force and h_res:
            fval = _parse_num(row.get(h_force, ""))
            rval = _parse_num(row.get(h_res, ""))

            if fval is not None and (max_num is not None or min_num is not None):
                if max_num is not None:
                    max_points.append((fval, max_num))
                if min_num is not None:
                    min_points.append((fval, min_num))

            elif rval is not None and (max_num is not None or min_num is not None):
                if max_num is not None:
                    max_points.append((max_num, rval))
                if min_num is not None:
                    min_points.append((min_num, rval))

    # Sort and de-dupe
    max_points = sorted(set(max_points), key=lambda p: (p[0], p[1]))
    min_points = sorted(set(min_points), key=lambda p: (p[0], p[1]))

    if cache_key is not None:
        _PARSE_CACHE[cache_key] = (list(max_points), list(min_points))

    return max_points, min_points


def generate_smoothed_line(x_points, y_points, num_points: int = 200):
    """
    Smoothing spline on CSV-derived points.
    Returns (x_smooth, y_smooth) as lists.
    """
    x_points = list(x_points)
    y_points = list(y_points)

    if len(x_points) < 2:
        return x_points, y_points

    pairs = sorted(zip(x_points, y_points), key=lambda p: p[0])
    x = np.array([p[0] for p in pairs], dtype=float)
    y = np.array([p[1] for p in pairs], dtype=float)

    try:
        x_new = np.linspace(x[0], x[-1], num_points)

        if np.all(y > 0):
            # Log-space spline: fitting log(y) vs x preserves the natural
            # 1/x decay shape and avoids the oscillation/spike artefacts
            # caused by the old 1/y approach.  Larger s (0.5 per point
            # instead of 0.001) prevents over-fitting between sparse points.
            log_y = np.log(y)
            s = max(len(x) * 0.5, 1.0)
            spline = UnivariateSpline(x, log_y, s=s)
            y_new = np.exp(spline(x_new))
            y_new = np.clip(y_new, y.min() * 0.1, y.max() * 10.0)
        else:
            spline = UnivariateSpline(x, y, s=max(len(x) * 0.5, 1.0))
            y_new = spline(x_new)

        # Preserve exact endpoints
        x_new[0], y_new[0] = x[0], y[0]
        x_new[-1], y_new[-1] = x[-1], y[-1]
        return x_new.tolist(), y_new.tolist()

    except Exception:
        x_new = np.linspace(x[0], x[-1], num_points)
        y_new = np.interp(x_new, x, y)
        x_new[0], y_new[0] = x[0], y[0]
        x_new[-1], y_new[-1] = x[-1], y[-1]
        return x_new.tolist(), y_new.tolist()


# ============================================================
# Compatibility shim for Engineering Mode
# ============================================================

def load_criteria_curve(
    csv_path: str,
    which: Literal["max", "min", "both"] = "both",
    smooth: bool = True,
    points: int = 200,
) -> Tuple[List[float], List[float]] | Dict[str, Tuple[List[float], List[float]]]:
    """
    Back-compat helper expected by ui/engineering_mode.py.

    Args:
        csv_path: path to pass_fail_criteria.csv
        which:    "max", "min", or "both"
        smooth:   apply spline smoothing
        points:   number of points for the smoothed curve

    Returns:
        - if which in {"max","min"}: (x_list, y_list)
        - if which == "both": {"max": (x_list, y_list), "min": (x_list, y_list)}
    """
    max_pts, min_pts = parse_pass_fail_criteria_form(csv_path)

    def _prep(pairs: List[Tuple[float, float]]):
        if not pairs:
            return [], []
        xs, ys = zip(*sorted(pairs, key=lambda p: p[0]))
        if smooth and len(xs) >= 2:
            return generate_smoothed_line(xs, ys, num_points=points)
        return list(xs), list(ys)

    max_xy = _prep(max_pts)
    min_xy = _prep(min_pts)

    if which == "max":
        return max_xy
    if which == "min":
        return min_xy
    return {"max": max_xy, "min": min_xy}
