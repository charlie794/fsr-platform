# domain/models.py
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Callable, Any
import time
import os
import re
import json
import tempfile


@dataclass(frozen=True)
class RunConfig:
    x_pitch: float
    y_pitch: float
    n_x: int
    n_y: int
    v_test: float
    v_travel: float
    start_x: float
    start_y: float
    safe_z: float
    test_z: float
    start_force: float
    max_force: float
    flags: Dict[str, bool]
    preload_res_threshold_ohm: Optional[float] = None
    short_circuit_threshold_ohm: Optional[float] = None


@dataclass(frozen=True)
class TestStep:
    test_id: str
    x: float
    y: float
    v_test: float
    v_travel: float
    safe_z: float
    test_z: float
    force_target: Optional[float]
    golden_curve: Optional[str]
    criteria_file: Optional[str]


@dataclass(frozen=True)
class Criteria:
    forces_to_check: List[float]
    res_at_force_fn: Optional[Callable[[float], float]] = None
    max_force: Optional[float] = None
    min_force: Optional[float] = None
    max_res: Optional[float] = None
    min_res: Optional[float] = None


@dataclass
class SoftTouchResult:
    x: Optional[float]
    y: Optional[float]
    z: Optional[float]
    voltage: Optional[float]
    threshold: float
    timestamp: float = field(default_factory=time.time)


@dataclass
class RuntimeState:
    last_soft_touch: Optional[SoftTouchResult] = None
    last_step_z_stop: Optional[float] = None


runtime_state = RuntimeState()


# ---------------------------------------------------------------------------
# Calibration strings
# Hard-coded defaults here.  On startup _load_persisted_calibrations() reads
# the JSON file and overwrites these if a valid entry is found.
# On save, only the JSON file is written — we no longer rewrite this source
# file.  JSON is the single source of persistence truth.
# ---------------------------------------------------------------------------

latest_resistance_calibration: Optional[str] = (
    'model=power_rational; Vmax=5.163760878749173; k=979.279325759851; n=0.9977140320328028'
)
latest_force_calibration: Optional[str] = 'y = (x - 0.20059) * 645.36468'

_PERSIST_DIR  = "/home/charlie/Documents/Calibrations"
_PERSIST_FILE = os.path.join(_PERSIST_DIR, "latest_calibrations.json")


def _atomic_write_json(path: str, obj: dict) -> None:
    """Write JSON atomically (temp-file + rename)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp_", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except Exception:
            pass
        raise


def _load_persisted_calibrations() -> None:
    """Load calibration strings from JSON on startup."""
    global latest_resistance_calibration, latest_force_calibration
    try:
        if not os.path.exists(_PERSIST_FILE):
            return
        with open(_PERSIST_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        r    = data.get("latest_resistance_calibration")
        fcal = data.get("latest_force_calibration")
        if isinstance(r, str) and r.strip():
            latest_resistance_calibration = r
        if isinstance(fcal, str) and fcal.strip():
            latest_force_calibration = fcal
    except Exception:
        pass  # never break startup


def _persist_to_json() -> None:
    """Write both calibration strings to JSON."""
    try:
        _atomic_write_json(
            _PERSIST_FILE,
            {
                "latest_resistance_calibration": latest_resistance_calibration,
                "latest_force_calibration":      latest_force_calibration,
            },
        )
    except Exception:
        pass


def _normalize_force_calibration(eq: str) -> str:
    """Normalize common force calibration forms to: 'y = m * (x - c)'."""
    if not isinstance(eq, str):
        return str(eq)
    s = eq.strip()
    if not s:
        return s

    s2 = s.lower().replace("force", "y").replace("v", "x")

    m1 = re.search(r"y\s*=\s*\(\s*x\s*([+-])\s*([0-9]*\.?[0-9]+)\s*\)\s*\*\s*([0-9]*\.?[0-9]+)", s2)
    if m1:
        sign, c_str, m_str = m1.group(1), m1.group(2), m1.group(3)
        c = float(c_str)
        if sign == '+':
            c = -c
        return f"y = {float(m_str):.8g} * (x - {c:.8g})"

    m2 = re.search(r"y\s*=\s*([0-9]*\.?[0-9]+)\s*\*\s*\(\s*x\s*([+-])\s*([0-9]*\.?[0-9]+)\s*\)", s2)
    if m2:
        m  = float(m2.group(1))
        c  = float(m2.group(3))
        if m2.group(2) == '+':
            c = -c
        return f"y = {m:.8g} * (x - {c:.8g})"

    m3 = re.search(r"y\s*=\s*([0-9]*\.?[0-9]+)\s*\*?\s*x\s*([+-])\s*([0-9]*\.?[0-9]+)", s2)
    if m3:
        m  = float(m3.group(1))
        b  = float(m3.group(3))
        if m3.group(2) == '-':
            b = -b
        c  = -b / m if abs(m) > 1e-12 else 0.0
        return f"y = {m:.8g} * (x - {c:.8g})"

    return s


def set_latest_resistance_calibration(eq: str) -> None:
    global latest_resistance_calibration
    if not isinstance(eq, str) or not eq.strip():
        return
    latest_resistance_calibration = eq
    _persist_to_json()


def set_latest_force_calibration(eq: str) -> None:
    global latest_force_calibration
    if not isinstance(eq, str) or not eq.strip():
        return
    latest_force_calibration = _normalize_force_calibration(eq)
    _persist_to_json()


# Load from JSON on import (so calibrations survive reboots)
_load_persisted_calibrations()


# ---------------------------------------------------------------------------
# Global store
# ---------------------------------------------------------------------------

@dataclass
class GlobalStore:
    plan_path: Optional[str] = None
    steps: List[TestStep] = field(default_factory=list)
    settings: Dict[str, Any] = field(default_factory=dict)
    run_config: Optional[RunConfig] = None
    output_folder: Optional[str] = None
    output_base: Optional[str] = None
    job_details: Dict[str, Any] = field(default_factory=dict)

    def set_plan(self, path: str, steps: List[TestStep], settings: Dict[str, Any]):
        self.plan_path = path
        self.steps     = steps
        self.settings  = settings

    def set_run_config(self, cfg: RunConfig):
        self.run_config = cfg

    def set_output_context(self, folder: str, base: Optional[str], job_details: Dict[str, Any]):
        self.output_folder = folder
        self.output_base   = base
        self.job_details   = job_details or {}


store = GlobalStore()

# ---------------------------------------------------------------------------
# Axis geometry
# ---------------------------------------------------------------------------

x_length: float = 124.0
y_length: float = 140.0
z_length: float = 61.0

x_0: float = 0.0
y_0: float = 0.0
z_0: float = 0.0
