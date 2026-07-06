# processing/calibration.py
import numpy as np
from typing import Tuple

def apply_calibration(voltage_force: np.ndarray, voltage_res: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    # Replace these with your real calibration equations
    force = (voltage_force - 0.2) * 50.0
    resistance = 1.0 / np.clip(voltage_res + 0.01, 1e-6, None) * 1000.0
    return force, resistance
