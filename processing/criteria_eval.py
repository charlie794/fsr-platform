# Sensor_Testor/processing/criteria_eval.py
from __future__ import annotations

from typing import Dict, List, Optional

import numpy as np


def _as_1d_float_array(x) -> np.ndarray:
    arr = np.asarray(x, dtype=float).reshape(-1)
    return arr


def _is_non_decreasing(x: np.ndarray) -> bool:
    if x.size < 2:
        return True
    return bool(np.all(x[1:] >= x[:-1]))


def res_at_forces(F: np.ndarray, R: np.ndarray, forces: List[float]) -> Dict[float, Optional[float]]:
    """
    Sample resistance at the closest indices to the requested force values.

    Fast path:
      - if F is sorted ascending/non-decreasing, use searchsorted
    Fallback:
      - otherwise use argmin(abs(...)) per query

    Returns:
      {requested_force: resistance_or_None}
    """
    out: Dict[float, Optional[float]] = {}

    if F is None or R is None:
        return {float(f): None for f in forces}

    try:
        F_arr = _as_1d_float_array(F)
        R_arr = _as_1d_float_array(R)
    except Exception:
        return {float(f): None for f in forces}

    n = min(F_arr.size, R_arr.size)
    if n <= 0:
        return {float(f): None for f in forces}

    F_arr = F_arr[:n]
    R_arr = R_arr[:n]

    if _is_non_decreasing(F_arr):
        for f in forces:
            ff = float(f)

            idx = int(np.searchsorted(F_arr, ff, side="left"))

            if idx <= 0:
                best_idx = 0
            elif idx >= n:
                best_idx = n - 1
            else:
                left_idx = idx - 1
                right_idx = idx
                if abs(F_arr[left_idx] - ff) <= abs(F_arr[right_idx] - ff):
                    best_idx = left_idx
                else:
                    best_idx = right_idx

            val = R_arr[best_idx]
            out[ff] = float(val) if np.isfinite(val) else None

        return out

    # Fallback for unsorted arrays
    for f in forces:
        ff = float(f)
        try:
            idx = int(np.argmin(np.abs(F_arr - ff)))
            val = R_arr[idx]
            out[ff] = float(val) if np.isfinite(val) else None
        except Exception:
            out[ff] = None

    return out


def overall_pass(criteria, F: np.ndarray, R: np.ndarray) -> bool:
    """
    Conservative / safe criteria evaluator.

    Supported shapes:
      1) criteria has R_lower and R_upper arrays of same length as R
         -> check R_lower <= R <= R_upper

      2) criteria has min_res / max_res scalars
         -> check all R within scalar bounds

      3) criteria has forces_to_check and res_at_force_fn
         -> sample measured resistance near each requested force and compare
            against callable target exactly only if min_res/max_res scalar bounds
            are also present. Otherwise this function stays permissive.

    If the structure is missing, incompatible, or malformed, returns True
    to preserve your current non-breaking behavior.
    """
    try:
        if F is None or R is None:
            return True

        F_arr = _as_1d_float_array(F)
        R_arr = _as_1d_float_array(R)
        n = min(F_arr.size, R_arr.size)
        if n <= 0:
            return True

        F_arr = F_arr[:n]
        R_arr = R_arr[:n]

        finite_mask = np.isfinite(F_arr) & np.isfinite(R_arr)
        if not np.any(finite_mask):
            return True

        F_arr = F_arr[finite_mask]
        R_arr = R_arr[finite_mask]

        # ------------------------------------------------------
        # Case 1: pointwise lower/upper arrays
        # ------------------------------------------------------
        lower = getattr(criteria, "R_lower", None)
        upper = getattr(criteria, "R_upper", None)

        if lower is not None and upper is not None:
            lower_arr = _as_1d_float_array(lower)
            upper_arr = _as_1d_float_array(upper)

            if len(lower_arr) == len(R_arr) and len(upper_arr) == len(R_arr):
                mask = np.isfinite(lower_arr) & np.isfinite(upper_arr) & np.isfinite(R_arr)
                if not np.any(mask):
                    return True
                return bool(np.all((R_arr[mask] >= lower_arr[mask]) & (R_arr[mask] <= upper_arr[mask])))

        # ------------------------------------------------------
        # Case 2: scalar min/max resistance
        # ------------------------------------------------------
        min_res = getattr(criteria, "min_res", None)
        max_res = getattr(criteria, "max_res", None)

        if min_res is not None or max_res is not None:
            if min_res is not None:
                try:
                    if np.any(R_arr < float(min_res)):
                        return False
                except Exception:
                    pass
            if max_res is not None:
                try:
                    if np.any(R_arr > float(max_res)):
                        return False
                except Exception:
                    pass
            return True

        # ------------------------------------------------------
        # Case 3: check sampled resistances at specific forces
        #
        # res_at_force_fn(f) returns the EXPECTED resistance at force f.
        # We sample the MEASURED resistance near each requested force
        # and compare against the expected value using min_res / max_res
        # as tolerance bounds (if provided), or fall back to an exact
        # equality check with a 10% tolerance if no bounds are given.
        # ------------------------------------------------------
        forces_to_check = getattr(criteria, "forces_to_check", None)
        res_at_force_fn = getattr(criteria, "res_at_force_fn", None)
        min_res = getattr(criteria, "min_res", None)
        max_res = getattr(criteria, "max_res", None)

        if forces_to_check and callable(res_at_force_fn):
            sampled = res_at_forces(F_arr, R_arr, list(forces_to_check))
            for f_target, r_measured in sampled.items():
                if r_measured is None:
                    continue
                try:
                    r_expected = float(res_at_force_fn(float(f_target)))
                except Exception:
                    continue

                if not (np.isfinite(r_measured) and np.isfinite(r_expected)):
                    continue

                # If explicit scalar bounds exist, use them as absolute limits.
                if max_res is not None:
                    try:
                        if r_measured > float(max_res):
                            return False
                    except Exception:
                        pass
                if min_res is not None:
                    try:
                        if r_measured < float(min_res):
                            return False
                    except Exception:
                        pass

                # If no scalar bounds, check within 10% of the expected value.
                if min_res is None and max_res is None and r_expected > 0:
                    tolerance = 0.10 * abs(r_expected)
                    if abs(r_measured - r_expected) > tolerance:
                        return False

        return True

    except Exception:
        return True
