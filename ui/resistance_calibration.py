from __future__ import annotations

import os
import glob
import re
from datetime import datetime
from typing import List, Optional, Tuple, Dict, Any

from Sensor_Testor.domain import models  # to update models.resistance_cal_file
import numpy as np

from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QTableWidget, QTableWidgetItem, QWidget, QMenu
)
from PyQt5.QtCore import Qt, QPoint

import pyqtgraph as pg  # plotting (same lib as Operator Mode)

# Try to import the DAQ adapter.
# This MUST NOT break the import of ResistanceCalibration,
# so we safely fall back to DaqAdapter = None if it fails.
try:
    from Sensor_Testor.hardware.daq_adapter import DaqAdapter
except Exception:
    try:
        from hardware.daq_adapter import DaqAdapter
    except Exception:
        DaqAdapter = None


class ResistanceCalibration(QDialog):
    """
    Guided resistance calibration using a rational fit with a plateau
    at the high-resistance end.

    - Uses fixed target resistances:
        1, 5, 10, 50, 100, 500,
        1000, 5000, 10000, 50000,
        100000, 500000, 1000000,
        5000000 (Ohms)

    - Scan:
        Reads channel 2 via DaqAdapter(channels=(1,2,3)), averages it,
        and stores (R, V).

    - Generate Calibration:
        Fits V(R) with

            t = R / scale
            P(t) = a0 + a1 t + ... + a_m t^m
            Q(t) = 1  + b1 t + ... + b_n t^n
            V ≈ P(t) / Q(t)

        Constraints:
          - a0 is free (curve does NOT have to go through 0,0),
            but nudged away from exactly 0.
          - No real roots of Q(t) in [min(R), max(R)] (no internal poles).
          - No steep decreases anywhere (small/local dips allowed).
          - Last ~30% of R range must form a “plateau”:
              * variation in V is small,
              * no noticeable downward slope.

        Plot (axes flipped for you):
          - X axis  = Voltage (V)  [“force”]
          - Y axis  = Resistance (Ω)
          - White dots = measured points
          - Yellow line = fitted rational curve

        Files:
          - Saved to /home/charlie/Documents/Calibrations/Resistance_calibration
          - Name: ResistanceCal_YYYYMMDD_HHMMSS.csv
          - Contains both:
              * model parameters (scale, degrees, coefficients)
              * data points as CSV rows (R_ohm,V_V)

        UI:
          - Table on the LEFT
          - Graph on the RIGHT
    """

    DEFAULT_NUM_DEG = 5  # numerator degree
    DEFAULT_DEN_DEG = 5  # denominator degree

    TARGET_RESISTANCES = [
        1,
        5,
        10,
        50,
        100,
        500,
        1_000,
        5_000,
        10_000,
        50_000,
        100_000,
        500_000,
        1_000_000,
        5_000_000,
    ]

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)

        self.setWindowTitle("Resistance Calibration")

        self.current_index = 0
        self.resistance_values: List[float] = []
        self.voltage_values: List[float] = []

        # Graph handles
        self.graph_widget = None
        self.cal_plot = None
        self.data_curve = None
        self.fit_curve = None

        self._build_ui()

    # -------------------------------------------------------------------------
    # UI setup
    # -------------------------------------------------------------------------

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # Instruction row
        instr_row = QHBoxLayout()
        instr_row.addWidget(QLabel("Instruction:", self))
        self.instruction_label = QLabel("", self)
        self.instruction_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        instr_row.addWidget(self.instruction_label)
        layout.addLayout(instr_row)

        # Status label
        self.status = QLabel("", self)
        layout.addWidget(self.status)

        # Navigation / Scan buttons
        btn_row = QHBoxLayout()
        self.prev_btn = QPushButton("Previous", self)
        self.prev_btn.clicked.connect(self.go_previous)
        self.scan_btn = QPushButton("Scan", self)
        self.scan_btn.clicked.connect(self.do_scan)
        self.next_btn = QPushButton("Next", self)
        self.next_btn.clicked.connect(self.go_next)
        btn_row.addWidget(self.prev_btn)
        btn_row.addWidget(self.scan_btn)
        btn_row.addWidget(self.next_btn)
        layout.addLayout(btn_row)

        # Table for points
        self.table = QTableWidget(self)
        self.table.setColumnCount(2)
        self.table.setHorizontalHeaderLabels(["Resistance (Ω)", "Voltage (V)"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)

        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.open_table_menu)

        # SIDE-BY-SIDE: table left, graph right
        side = QHBoxLayout()
        side.addWidget(self.table, stretch=1)

        self.graph_widget = pg.GraphicsLayoutWidget(show=True)
        self.graph_widget.setBackground('k')
        side.addWidget(self.graph_widget, stretch=1)

        layout.addLayout(side)

        # Plot inside graph widget (axes flipped: x=Voltage, y=Resistance)
        self.cal_plot = self.graph_widget.addPlot(row=0, col=0)
        p = self.cal_plot
        p.setLabel('bottom', 'Voltage', units='V')
        p.setLabel('left', 'Resistance', units='Ω')
        p.showGrid(x=True, y=True, alpha=0.3)
        p.setDownsampling(mode='peak')
        p.setClipToView(True)

        self.data_curve = p.plot([], [], pen=None, symbol='o', symbolSize=6, symbolBrush='w')
        self.fit_curve = p.plot([], [], pen=pg.mkPen('y', width=2))

        # Generate calibration button
        self.gen_btn = QPushButton("Generate Calibration", self)
        self.gen_btn.clicked.connect(self.generate_calibration)
        layout.addWidget(self.gen_btn)

        # Initialise UI
        self.update_instruction_label()
        self.update_nav_buttons()

        self.setMinimumWidth(800)
        self.setMinimumHeight(500)

    # -------------------------------------------------------------------------
    # Table context menu
    # -------------------------------------------------------------------------

    def open_table_menu(self, pos: QPoint):
        if self.table.rowCount() == 0:
            return

        row = self.table.currentRow()
        if row < 0:
            return

        menu = QMenu(self)
        delete_action = menu.addAction("Delete row")
        action = menu.exec_(self.table.viewport().mapToGlobal(pos))
        if action == delete_action:
            if 0 <= row < len(self.resistance_values):
                del self.resistance_values[row]
            if 0 <= row < len(self.voltage_values):
                del self.voltage_values[row]
            self.table.removeRow(row)

            self._update_plot(
                np.array(self.resistance_values, dtype=float),
                np.array(self.voltage_values, dtype=float),
                params=None,
            )

    # -------------------------------------------------------------------------
    # Navigation
    # -------------------------------------------------------------------------

    def update_instruction_label(self):
        if 0 <= self.current_index < len(self.TARGET_RESISTANCES):
            r = self.TARGET_RESISTANCES[self.current_index]
            self.instruction_label.setText(f"Change resistor to {r} Ω, then press Scan.")
        else:
            self.instruction_label.setText("No more targets.")

    def update_nav_buttons(self):
        self.prev_btn.setEnabled(self.current_index > 0)
        self.next_btn.setEnabled(self.current_index < len(self.TARGET_RESISTANCES) - 1)

    def go_previous(self):
        if self.current_index > 0:
            self.current_index -= 1
            self.update_instruction_label()
            self.update_nav_buttons()

    def go_next(self):
        if self.current_index < len(self.TARGET_RESISTANCES) - 1:
            self.current_index += 1
            self.update_instruction_label()
            self.update_nav_buttons()

    # -------------------------------------------------------------------------
    # DAQ handling
    # -------------------------------------------------------------------------

    def _read_mean_voltage_from_daq(self) -> Optional[float]:
        """
        Read mean voltage for the resistance channel using differential CH2
        (MCC-128 pins 4+5).  Opens DaqAdapter(channels=(0, 2)) so the DAQ
        is configured identically to the live test run — differential mode,
        CH0=force, CH2=resistance.  We only use the CH2 (resistance) output.
        """
        if DaqAdapter is None:
            print("[ResistanceCalibration] DaqAdapter not available.")
            return None

        daq = DaqAdapter(channels=(0, 2), rate_hz=1000.0)
        try:
            daq.open()
            # capture_window returns (t, v_force, v_res)
            # v_res is differential CH2 — exactly what the live loop uses
            out = daq.capture_window(1.0)

            if isinstance(out, (list, tuple)) and len(out) >= 3:
                v_res = out[2]  # index 2 = v_res (differential CH2)
            else:
                print("[ResistanceCalibration] Unexpected DAQ output:", out)
                return None

            v_res = np.asarray(v_res, dtype=float)
            if v_res.size == 0:
                return None

            mean_v = float(np.mean(v_res))
            print(f"[ResistanceCalibration] CH2 diff scan: {v_res.size} samples  mean={mean_v:.6f} V")
            return mean_v
        except Exception as e:
            print("[ResistanceCalibration] DAQ read error:", e)
            return None
        finally:
            try:
                daq.close()
            except Exception:
                pass

    def do_scan(self):
        if not (0 <= self.current_index < len(self.TARGET_RESISTANCES)):
            self.status.setText("No valid target selected.")
            return

        target_R = float(self.TARGET_RESISTANCES[self.current_index])
        mean_v = self._read_mean_voltage_from_daq()
        if mean_v is None or np.isnan(mean_v):
            self.status.setText("DAQ read failed.")
            return

        self.append_row(target_R, mean_v)
        self.status.setText(f"Scanned {target_R} Ω: mean voltage = {mean_v:.6f} V")

    def append_row(self, R: float, V: float):
        row_idx = self.table.rowCount()
        self.table.insertRow(row_idx)

        item_r = QTableWidgetItem(f"{R:.6g}")
        item_v = QTableWidgetItem(f"{V:.6g}")
        item_r.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        item_v.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)

        self.table.setItem(row_idx, 0, item_r)
        self.table.setItem(row_idx, 1, item_v)

        self.resistance_values.append(R)
        self.voltage_values.append(V)

        self._update_plot(
            np.array(self.resistance_values, dtype=float),
            np.array(self.voltage_values, dtype=float),
            params=None,
        )

    # -------------------------------------------------------------------------
    # Fitting helpers
    # -------------------------------------------------------------------------

    @staticmethod
    def _adjust_degrees(
        num_deg: int,
        den_deg: int,
        n_points: int,
        fix_a0: bool = False,
    ) -> Tuple[int, int]:
        """
        Make sure we don't have more unknowns than data points.
        """
        def unknowns(m: int, n: int) -> int:
            return (m + 1 - (1 if fix_a0 else 0)) + n

        while unknowns(num_deg, den_deg) > n_points and (num_deg > 1 or den_deg > 1):
            if num_deg >= den_deg and num_deg > 1:
                num_deg -= 1
            elif den_deg > 1:
                den_deg -= 1
            else:
                break

        return max(1, num_deg), max(1, den_deg)

    @staticmethod
    def _den_has_root_in_range(den: np.ndarray, scale: float,
                               r_min: float, r_max: float) -> bool:
        """
        True if denominator polynomial has a real root inside [r_min, r_max].
        """
        if den is None or len(den) < 2:
            return False

        coeffs = den[::-1]  # np.roots needs highest degree first
        roots = np.roots(coeffs)
        if roots.size == 0:
            return False

        for z in roots:
            if abs(z.imag) < 1e-8:
                t_root = float(z.real)
                R_root = t_root * scale
                if r_min <= R_root <= r_max:
                    return True
        return False

    @classmethod
    def _has_steep_decrease(cls, R: np.ndarray, params: Dict[str, Any]) -> bool:
        """
        Detect a *steep* drop anywhere in [min(R), max(R)].
        Small dips are allowed; big downward jumps are not.
        """
        if len(R) < 2:
            return False

        r_min = float(np.min(R))
        r_max = float(np.max(R))
        if r_max <= r_min:
            return False

        r_line = np.linspace(r_min, r_max, 400)
        v_line = cls.eval_rational(r_line, params)

        dv = np.diff(v_line)
        dr = np.diff(r_line)
        slopes = dv / dr

        # baseline average magnitude
        total_amp = float(v_line[-1] - v_line[0])
        avg_slope = total_amp / (r_max - r_min + 1e-12)
        avg_mag = max(abs(avg_slope), 1e-12)

        K = 3.0  # how many times steeper than "average" we allow
        min_slope = float(np.min(slopes))

        return min_slope < -K * avg_mag

    @classmethod
    def _violates_plateau(cls, R: np.ndarray, params: Dict[str, Any]) -> bool:
        """
        Check that the top-end region behaves like a plateau:
          - last ~30% of R range should have small variation in V
          - and no significant downward slope there.

        Returns True if the fit *violates* this behaviour.
        """
        if len(R) < 3:
            return False

        r_min = float(np.min(R))
        r_max = float(np.max(R))
        if r_max <= r_min:
            return False

        r_line = np.linspace(r_min, r_max, 400)
        v_line = cls.eval_rational(r_line, params)

        total_amp = float(np.max(v_line) - np.min(v_line))
        if total_amp <= 0:
            return False  # perfectly flat anyway

        # we look at the last 30% of the range
        frac_start = 0.7
        r_cut = r_min + frac_start * (r_max - r_min)
        mask = r_line >= r_cut
        if not np.any(mask):
            return False

        r_seg = r_line[mask]
        v_seg = v_line[mask]
        if len(r_seg) < 3:
            return False

        # 1) variation in plateau section should be relatively small
        seg_range = float(np.max(v_seg) - np.min(v_seg))
        plateau_tol = 0.08 * total_amp  # allow ~8% of total vertical span
        if seg_range > plateau_tol:
            return True  # too much wobble in plateau

        # 2) no noticeable downward slope in plateau section
        dv_seg = np.diff(v_seg)
        dr_seg = np.diff(r_seg)
        slopes_seg = dv_seg / dr_seg

        # allow tiny numerical noise; forbid larger negative slopes
        neg_tol = -0.02 * (total_amp / (r_max - r_min + 1e-12))
        if np.min(slopes_seg) < neg_tol:
            return True

        return False

    @classmethod
    def fit_rational(
        cls,
        R: np.ndarray,
        V: np.ndarray,
        num_deg: int | None = None,
        den_deg: int | None = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Fit V ≈ P(t)/Q(t) with constraints:
          - a0 free, but nudged away from exactly 0;
          - no denominator roots inside data range (no internal poles);
          - no steep decreases anywhere;
          - last ~30% of the range must be a plateau.
        """
        R = np.asarray(R, dtype=float)
        V = np.asarray(V, dtype=float)
        n_points = len(R)
        if n_points < 3:
            return None

        if num_deg is None:
            num_deg = cls.DEFAULT_NUM_DEG
        if den_deg is None:
            den_deg = cls.DEFAULT_DEN_DEG

        fix_a0 = False
        num_deg, den_deg = cls._adjust_degrees(num_deg, den_deg, n_points, fix_a0=fix_a0)

        r_min = float(R.min())
        r_max = float(R.max())

        scale = float(np.max(np.abs(R)))
        if scale == 0.0:
            scale = 1.0
        t_all = R / scale

        while True:
            # Linear system A * coeffs = V, coeffs = [a0..a_m, b1..b_n]
            rows = []
            rhs = []
            for ti, yi in zip(t_all, V):
                a_terms = [ti ** k for k in range(0, num_deg + 1)]
                b_terms = [-yi * (ti ** k) for k in range(1, den_deg + 1)]
                rows.append(a_terms + b_terms)
                rhs.append(yi)

            A = np.asarray(rows, dtype=float)
            b_vec = np.asarray(rhs, dtype=float)

            try:
                coeffs, *_ = np.linalg.lstsq(A, b_vec, rcond=None)
            except Exception as e:
                print("[ResistanceCalibration] fit_rational lstsq error:", e)
                return None

            a = coeffs[: num_deg + 1]
            b_rest = coeffs[num_deg + 1:]

            den = np.empty(den_deg + 1, dtype=float)
            den[0] = 1.0
            den[1:] = b_rest

            # Nudge a0 away from exactly zero so "a0 can't be 0"
            if abs(a[0]) < 1e-12:
                a[0] = 1e-12

            # 1) no internal poles
            if cls._den_has_root_in_range(den, scale, r_min, r_max):
                if den_deg > 1:
                    den_deg -= 1
                elif num_deg > 1:
                    num_deg -= 1
                else:
                    return {
                        "model": "rational",
                        "scale": scale,
                        "num_deg": num_deg,
                        "den_deg": den_deg,
                        "num": a,
                        "den": den,
                    }
                num_deg, den_deg = cls._adjust_degrees(num_deg, den_deg, n_points, fix_a0=fix_a0)
                continue

            params = {
                "model": "rational",
                "scale": scale,
                "num_deg": num_deg,
                "den_deg": den_deg,
                "num": a,
                "den": den,
            }

            # 2) forbid steep decreases anywhere
            if cls._has_steep_decrease(R, params):
                if num_deg > 1:
                    num_deg -= 1
                elif den_deg > 1:
                    den_deg -= 1
                else:
                    return params
                num_deg, den_deg = cls._adjust_degrees(num_deg, den_deg, n_points, fix_a0=fix_a0)
                continue

            # 3) enforce plateau at top end
            if cls._violates_plateau(R, params):
                if num_deg > 1:
                    num_deg -= 1
                elif den_deg > 1:
                    den_deg -= 1
                else:
                    return params
                num_deg, den_deg = cls._adjust_degrees(num_deg, den_deg, n_points, fix_a0=fix_a0)
                continue

            # Passed all checks: good fit
            return params

    @staticmethod
    def eval_rational(R: np.ndarray, params: Dict[str, Any]) -> np.ndarray:
        R = np.asarray(R, dtype=float)
        scale = float(params["scale"])
        t = R / scale

        a = np.asarray(params["num"], dtype=float)
        b = np.asarray(params["den"], dtype=float)

        num = np.zeros_like(t) + a[-1]
        for coef in reversed(a[:-1]):
            num = num * t + coef

        den = np.zeros_like(t) + b[-1]
        for coef in reversed(b[:-1]):
            den = den * t + coef

        den = np.where(den == 0.0, 1e-15, den)
        return num / den

    # -------------------------------------------------------------------------
    # Plot helper (axes flipped)
    # -------------------------------------------------------------------------

    def _update_plot(self, R: np.ndarray, V: np.ndarray, params: Optional[Dict[str, Any]] = None):
        """
        Plot with axes flipped:
          x = Voltage (V)
          y = Resistance (Ω)
        """
        if self.cal_plot is None or self.data_curve is None or self.fit_curve is None:
            return

        if R is None or V is None or len(R) == 0:
            self.data_curve.setData([], [])
            self.fit_curve.setData([], [])
            return

        R = np.asarray(R, dtype=float)
        V = np.asarray(V, dtype=float)

        # Scatter: x = V, y = R
        self.data_curve.setData(V, R)

        if params is not None:
            R_line = np.linspace(R.min(), R.max(), 400)
            V_line = self.eval_rational(R_line, params)
            # Fit curve: x = V_line, y = R_line
            self.fit_curve.setData(V_line, R_line)
        else:
            self.fit_curve.setData([], [])

    # -------------------------------------------------------------------------
    # Calibration main logic
    # -------------------------------------------------------------------------


    def generate_calibration(self):
        if len(self.resistance_values) < 3:
            self.status.setText("Need at least 3 samples for a rational fit.")
            return

        R = np.array(self.resistance_values, dtype=float)
        V = np.array(self.voltage_values, dtype=float)

        new_params = self.fit_rational(R, V)
        if new_params is None:
            self.status.setText("Rational fit failed (not enough points or singular data).")
            return

        # Load old params just for the status message (optional)
        try:
            prev_params = self.load_most_recent_calibration()
        except Exception:
            prev_params = None

        csv_path = self.save_calibration_to_csv(new_params, R, V)

        msg = f"Saved new calibration: {os.path.basename(csv_path)}"
        if prev_params is not None:
            msg += " (previous calibration found)."
        else:
            msg += " (no previous calibration)."
        self.status.setText(msg)

        # Update plot with the new fit
        self._update_plot(R, V, new_params)


    # -------------------------------------------------------------------------
    # Calibration file I/O
    # -------------------------------------------------------------------------

    def calibrations_dir(self) -> str:
        """
        Fixed calibration directory:
        /home/charlie/Documents/Calibrations/Resistance_calibration
        """
        cal_dir = "/home/charlie/Documents/Calibrations/Resistance_calibration"
        os.makedirs(cal_dir, exist_ok=True)
        return cal_dir

    def load_most_recent_calibration(self) -> Optional[Dict[str, Any]]:
        pattern = os.path.join(self.calibrations_dir(), "ResistanceCal_*.csv")
        files = glob.glob(pattern)
        if not files:
            return None

        def extract_ts(path: str) -> datetime:
            m = re.search(r"ResistanceCal_(\d{8}_\d{6})\.csv$", os.path.basename(path))
            if not m:
                return datetime.min
            try:
                return datetime.strptime(m.group(1), "%Y%m%d_%H%M%S")
            except ValueError:
                return datetime.min

        latest = max(files, key=extract_ts)

        model = None
        scale = None
        num_deg = None
        den_deg = None
        num = None
        den = None

        with open(latest, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("model="):
                    model = line.split("=", 1)[1].strip()
                elif line.startswith("scale="):
                    scale = float(line.split("=", 1)[1].strip())
                elif line.startswith("num_deg="):
                    num_deg = int(line.split("=", 1)[1].strip())
                elif line.startswith("den_deg="):
                    den_deg = int(line.split("=", 1)[1].strip())
                elif line.startswith("num="):
                    num = self._parse_array(line)
                elif line.startswith("den="):
                    den = self._parse_array(line)

        if model != "rational":
            raise ValueError(f"Unsupported model in {latest}: {model}")
        if scale is None or num_deg is None or den_deg is None or num is None or den is None:
            raise ValueError(f"Incomplete calibration file {latest}")

        return {
            "model": model,
            "scale": scale,
            "num_deg": num_deg,
            "den_deg": den_deg,
            "num": num,
            "den": den,
        }

    def save_calibration_to_csv(
        self,
        params: Dict[str, Any],
        R: np.ndarray,
        V: np.ndarray,
    ) -> str:
        """
        Save calibration CSV and store the equation summary string
        in models.latest_resistance_calibration, persisted via models.set_latest_resistance_calibration.
        """
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"ResistanceCal_{ts}.csv"
        path = os.path.join(self.calibrations_dir(), filename)

        R = np.asarray(R, dtype=float)
        V = np.asarray(V, dtype=float)

        scale = float(params["scale"])
        num = np.asarray(params["num"], dtype=float)
        den = np.asarray(params["den"], dtype=float)
        num_deg = int(params["num_deg"])
        den_deg = int(params["den_deg"])

        num_str = ", ".join(f"{x:.16g}" for x in num)
        den_str = ", ".join(f"{x:.16g}" for x in den)

        # One-line rational model equation summary
        eq_summary = (
            f"model=rational; "
            f"scale={scale:.16g}; "
            f"num_deg={num_deg}; den_deg={den_deg}; "
            f"num=[{num_str}]; den=[{den_str}]"
        )

        # Write the CSV file (including the parameters + data)
        with open(path, "w", newline="") as f:
            f.write("model=rational\n")
            f.write(f"scale={scale:.16g}\n")
            f.write(f"num_deg={num_deg}\n")
            f.write(f"den_deg={den_deg}\n")
            f.write(f"num=[{num_str}]\n")
            f.write(f"den=[{den_str}]\n")
            f.write("\n# Data points (Resistance_ohm,Voltage_V)\n")
            f.write("R_ohm,V_V\n")
            for r, v in zip(R, V):
                f.write(f"{r:.16g},{v:.16g}\n")

        # 🔴 HARD SAVE: persist into models.py so it survives restarts
        try:
            from domain import models as _models  # depending on how you import
        except Exception:
            import models as _models  # fallback

        if hasattr(_models, "set_latest_resistance_calibration"):
            _models.set_latest_resistance_calibration(eq_summary)
        else:
            # older style – at least keep it in-memory
            _models.latest_resistance_calibration = eq_summary

        return path




    @staticmethod
    def _parse_array(line: str) -> np.ndarray:
        m = re.search(r"\[(.*)\]", line)
        if not m:
            return np.array([], dtype=float)
        inner = m.group(1).strip()
        if not inner:
            return np.array([], dtype=float)

        parts = [p.strip() for p in inner.split(",")]
        vals = []
        for p in parts:
            try:
                vals.append(float(p))
            except ValueError:
                pass
        return np.array(vals, dtype=float)
