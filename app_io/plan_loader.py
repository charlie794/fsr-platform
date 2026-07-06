# app_io/plan_loader.py
from __future__ import annotations
from typing import List, Tuple, Dict, Any
import os

try:
    from Sensor_Testor.domain.models import TestStep
except ModuleNotFoundError:
    from domain.models import TestStep  # type: ignore


_SETTINGS: Dict[str, Any] = {
    "x pitch(mm)":                          float,
    "y pitch(mm)":                          float,
    "Number of Sensors in x":               int,
    "Number of Sensors in y":               int,
    "actuator speed(mm/s)":                 float,
    "speed between spaces(mm/s)":           float,
    "max force(kg)":                        float,
    "start force(kg)":                      float,
    "start position x":                     float,
    "start position y":                     float,
    "Safe Height (mm)":                     float,
    "Test Height (mm)":                     float,
    "Test Type":                            str,
    "Starting Position":                    "empty_ok",
    "Check Short Circuit":                  bool,
    "Check Open Circuit":                   bool,
    "Check if Preloaded":                   bool,
    "Preload Resistance Threshold (Ω)":     float,
    "Short-Circuit Threshold (Ω)":          float,
    "Raw Data":                             bool,
    "Filtered Data":                        bool,
    "Data on Same Sheet":                   bool,
    "Save Failed Data":                     bool,
    "Pass Fail Criteria":                   bool,
    "Force x Resistance":                   bool,
    "Force x Sample Number":                bool,
    "Resistance x Sample":                  bool,
}

_TEST_HEADER = (
    "Test,X Position,Y Position,Force,Speed of Test,Speed between Test,"
    "Safe Height (mm),Test Height (mm),Golden Curve,P/F Criteria"
)




def _coerce(key: str, val: str) -> Any:
    typ = _SETTINGS[key]
    if typ == "empty_ok":
        return None if val == "" else val
    if typ is bool:
        return val.lower() == "true"
    if typ is int:
        try:
            return int(val)
        except Exception:
            raise ValueError(f'Setting "{key}" must be an integer.')
    if typ is float:
        try:
            return float(val)
        except Exception:
            raise ValueError(f'Setting "{key}" must be a number.')
    return val


def load_grid_plan_csv(path: str) -> Tuple[List[TestStep], Dict[str, Any]]:

    with open(path, "r", encoding="utf-8") as f:
        lines = [ln.rstrip("\r\n") for ln in f]

    while lines and not lines[-1]:
        lines.pop()

    if not lines:
        raise ValueError("File is empty.")
    if lines[0] != "Settings":
        raise ValueError('First line must be "Settings".')

    # Settings block — runs until blank line
    idx = 1
    settings_lines = []
    while idx < len(lines) and lines[idx]:
        settings_lines.append(lines[idx])
        idx += 1

    if not settings_lines:
        raise ValueError("Settings block is empty.")
    if idx >= len(lines) or lines[idx] != "":
        raise ValueError("Expected blank line after settings.")
    idx += 1

    if idx >= len(lines) or lines[idx] != _TEST_HEADER:
        raise ValueError("Test header mismatch.")
    idx += 1

    # Parse settings
    settings: Dict[str, Any] = {}
    seen: set = set()
    for line in settings_lines:
        key_str, val_str = line.split(",", 1)
        key_str = key_str.strip()
        val_str = val_str.strip()
        if key_str not in _SETTINGS:
            raise ValueError(f'Unexpected setting "{key_str}".')
        if key_str in seen:
            raise ValueError(f'Duplicate setting "{key_str}".')
        settings[key_str] = _coerce(key_str, val_str)
        seen.add(key_str)

    missing = [k for k in _SETTINGS if k not in seen]
    if missing:
        raise ValueError(f"Missing settings: {missing}")
    if settings["Test Type"] != "Grid Test":
        raise ValueError('Test Type must be "Grid Test".')

    # Parse test rows
    steps: List[TestStep] = []
    for ln in lines[idx:]:
        if not ln:
            raise ValueError("Blank line inside table.")
        parts = ln.split(",")
        if len(parts) != 10:
            raise ValueError("Each test row must have 10 columns.")
        test_id, x, y, force, v_test, v_travel, safe_z, test_z, golden, criteria = parts
        try:
            steps.append(TestStep(
                test_id      = test_id.strip(),
                x            = float(x),
                y            = float(y),
                force_target = float(force),
                v_test       = float(v_test),
                v_travel     = float(v_travel),
                safe_z       = float(safe_z),
                test_z       = float(test_z),
                golden_curve = golden.strip() or None,
                criteria_file= criteria.strip() or None,
            ))
        except Exception:
            raise ValueError(f'Invalid value in test row "{test_id.strip()}".')

    return (steps, settings)
