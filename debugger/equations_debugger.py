# Sensor_Testor/debugger/equations_debugger.py
"""
MCC-128 Equations/DAQ Debugger (continuous scan version)

Mode:
    - Hardware-paced continuous scan using a_in_scan_start/a_in_scan_read_numpy.
    - CH0: Force (volts -> optional calibrated force)
    - CH2: Resistance (volts -> optional calibrated resistance)

Features:
    - Top plot: X = Force (raw / calibrated), Y = Resistance (raw / calibrated)
    - Bottom plots: raw CH0 and raw CH2 vs sample index, with average lines
    - Toggle 50 Hz notch filter on calibrated Force only
    - RAW / CALIBRATED modes for Force and Resistance
    - Jog X/Y/Z with text entry for distance (mm), using DuetAdapter (G91/G1/G90)
    - Terminal showing compact debug info

Resistance calibration source (only two checks, in this order):
    1. models.latest_resistance_calibration  (string)
    2. MOST RECENT file in:
         /home/charlie/Documents/Calibrations/Resistance_calibration

Force calibration:
    - Uses models.latest_force_calibration if set.
    - Else falls back to offset/gain from "force_calibration.csv".
"""

from __future__ import annotations

from Sensor_Testor.processing.calibration import parse_resistance_calibration

import os
import sys
import math
import re
from collections import deque
from typing import Optional, Tuple, Dict, Any, List

import numpy as np

# ---------------------------------------------------------------------
# Filtering helpers (applied to BOTH channels when enabled)
#   - Optional 50 Hz notch on VOLTAGE (recommended before calibration)
#   - Low-pass IIR (EMA) on VOLTAGE to reduce random noise
# ---------------------------------------------------------------------

class _BiquadNotch:
    """Stateful RBJ biquad notch filter."""
    def __init__(self, fs_hz: float, f0_hz: float = 50.0, q: float = 30.0):
        fs = float(fs_hz)
        f0 = float(f0_hz)
        Q = float(q)

        w0 = 2.0 * math.pi * (f0 / fs)
        alpha = math.sin(w0) / (2.0 * Q)
        cw = math.cos(w0)

        b0 = 1.0
        b1 = -2.0 * cw
        b2 = 1.0
        a0 = 1.0 + alpha
        a1 = -2.0 * cw
        a2 = 1.0 - alpha

        self.b0 = b0 / a0
        self.b1 = b1 / a0
        self.b2 = b2 / a0
        self.a1 = a1 / a0
        self.a2 = a2 / a0

        self.x1 = 0.0
        self.x2 = 0.0
        self.y1 = 0.0
        self.y2 = 0.0

    def process_vector(self, x: np.ndarray) -> np.ndarray:
        if x.size == 0:
            return x
        y = np.empty_like(x, dtype=np.float64)

        b0, b1, b2 = self.b0, self.b1, self.b2
        a1, a2 = self.a1, self.a2
        x1, x2 = self.x1, self.x2
        y1, y2 = self.y1, self.y2

        for i, x0 in enumerate(x.astype(np.float64, copy=False)):
            y0 = (b0 * x0) + (b1 * x1) + (b2 * x2) - (a1 * y1) - (a2 * y2)
            y[i] = y0
            x2, x1 = x1, x0
            y2, y1 = y1, y0

        self.x1, self.x2 = x1, x2
        self.y1, self.y2 = y1, y2
        return y


class _EMAFilter:
    """Stateful EMA low-pass filter. Use on streams (voltage or calibrated units)."""
    def __init__(self, alpha: float = 0.02):
        self.alpha = float(alpha)
        self.y: Optional[float] = None

    def reset(self):
        self.y = None

    def process_vector(self, x: np.ndarray) -> np.ndarray:
        if x.size == 0:
            return x
        y = np.empty_like(x, dtype=np.float64)
        a = self.alpha
        yy = self.y

        for i, x0 in enumerate(x.astype(np.float64, copy=False)):
            if yy is None or not np.isfinite(yy):
                yy = float(x0)
            else:
                yy = (a * float(x0)) + ((1.0 - a) * yy)
            y[i] = yy

        self.y = yy
        return y


from PyQt5.QtCore import QTimer, Qt
from PyQt5.QtWidgets import (
    QApplication,
    QWidget,
    QHBoxLayout,
    QVBoxLayout,
    QGridLayout,
    QGroupBox,
    QPushButton,
    QLabel,
    QComboBox,
    QCheckBox,
    QPlainTextEdit,
    QLineEdit,
)

import pyqtgraph as pg

# daqhats imports
try:
    from daqhats import (
        mcc128,
        hat_list,
        HatIDs,
        OptionFlags,
        AnalogInputRange,
        AnalogInputMode,
    )
except Exception:
    mcc128 = None
    hat_list = None
    HatIDs = None
    OptionFlags = None
    AnalogInputRange = None
    AnalogInputMode = None


_THIS = os.path.abspath(__file__)
_DBG_DIR = os.path.dirname(_THIS)
_APP_DIR = os.path.dirname(_DBG_DIR)
_ROOT_DIR = os.path.dirname(_APP_DIR)
if _ROOT_DIR not in sys.path:
    sys.path.insert(0, _ROOT_DIR)

try:
    from Sensor_Testor.domain import models  # type: ignore
except Exception:
    try:
        import domain.models as models  # type: ignore
    except Exception:
        try:
            import models  # type: ignore
        except Exception:
            models = None  # type: ignore

try:
    from Sensor_Testor.hardware.duet_adapter import DuetAdapter  # type: ignore
except Exception:
    DuetAdapter = None  # type: ignore


# ---------------------------------------------------------------------
# MCC128 continuous-scan helper
# ---------------------------------------------------------------------
class MCC128ContinuousScan:
    def __init__(self, desired_rate_hz: float = 2000.0, log_func=None):
        self.log = log_func or (lambda msg: None)
        self.hat: Optional[mcc128] = None
        self.channel_mask = 0
        self.channel_count = 0
        self.rate_hz = 0.0
        self.running = False

        if mcc128 is None or hat_list is None or HatIDs is None:
            self.log("[MCC] daqhats not available; MCC-128 disabled.")
            return

        try:
            boards = hat_list(HatIDs.MCC_128)
        except Exception as e:
            self.log(f"[MCC] hat_list failed: {e}")
            return

        if not boards:
            self.log("[MCC] No MCC-128 found.")
            return

        address = boards[0].address
        try:
            self.hat = mcc128(address)
            self.log(f"[MCC] Using MCC-128 at address {address}.")
        except Exception as e:
            self.log(f"[MCC] Failed to open MCC-128: {e}")
            return

        try:
            self.hat.a_in_mode_write(AnalogInputMode.SE)
            # If CH0 is saturating near +5.0 V, you're hitting the +/-5 V input range.
            # Use +/-10 V when available so raw CH0 can exceed 5 V without clipping.
            rng = getattr(AnalogInputRange, "BIP_10V", AnalogInputRange.BIP_5V)
            self.hat.a_in_range_write(rng)
        except Exception as e:
            self.log(f"[MCC] Failed to set mode/range: {e}")

        channels = [0, 2]
        for ch in channels:
            self.channel_mask |= (1 << ch)
        self.channel_count = len(channels)

        try:
            actual = self.hat.a_in_scan_actual_rate(self.channel_count, desired_rate_hz)
        except Exception:
            actual = desired_rate_hz

        self.rate_hz = float(actual)
        self.log(
            f"[MCC] Continuous scan configured: {self.channel_count} ch, "
            f"target {desired_rate_hz:.1f} Hz, actual {self.rate_hz:.1f} Hz"
        )

    def start(self):
        if self.hat is None or self.running:
            return
        try:
            self.hat.a_in_scan_start(
                self.channel_mask,
                200000,
                self.rate_hz,
                OptionFlags.CONTINUOUS,
            )
            self.running = True
            self.log("[MCC] Continuous scan started.")
        except Exception as e:
            self.log(f"[MCC] a_in_scan_start failed: {e}")

    def stop(self):
        if self.hat is None:
            return
        try:
            if self.running:
                self.hat.a_in_scan_stop()
                self.running = False
        finally:
            try:
                self.hat.a_in_scan_cleanup()
            except Exception:
                pass

    # ⚠️ FIXED: Drain buffer completely every read
    def read(self, samples_per_channel: int = 20, timeout_s: float = 0.0):
        if self.hat is None or not self.running:
            return [], []

        try:
            result = self.hat.a_in_scan_read_numpy(-1, timeout_s)
        except Exception as e:
            self.log(f"[MCC] read error: {e}")
            return [], []

        data = result.data
        if data is None or data.size == 0:
            return [], []

        total = data.size
        if self.channel_count <= 0:
            return [], []

        per = total // self.channel_count
        if per == 0:
            return [], []

        ch0 = data[0::self.channel_count]
        ch2 = data[1::self.channel_count]
        return ch0.tolist(), ch2.tolist()

    def __del__(self):
        try:
            self.stop()
        except Exception:
            pass


# ---------------------------------------------------------------------
# Main debugger widget
# ---------------------------------------------------------------------
class EquationsDebugger(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("MCC-128 Debugger – Continuous Scan X/Y + Raw + Jog XYZ")

        self.GUI_TIMER_MS = 20
        self.SCAN_RATE_HZ = 2000.0
        self.SAMPLES_PER_CHUNK = 20

        self.HIST_POINTS_XY = 200000
        self.HIST_POINTS_RAW = 50000
        self.LOG_MAX_LINES = 1000
        self.LOG_TICK_STRIDE = 20
        self.JOG_FEED = 300.0

        self.scan_running = False
        self.daq: Optional[MCC128ContinuousScan] = None
        self.tick_idx = 0
        self.sample_idx = 0
        self.duet: Optional["DuetAdapter"] = None

        self.v0_hist = deque(maxlen=self.HIST_POINTS_XY)  # CH0 volts (post-filter)
        self.v2_hist = deque(maxlen=self.HIST_POINTS_XY)  # CH2 volts (post-filter)
        self.force_hist = deque(maxlen=self.HIST_POINTS_XY)  # displayed Force series (raw V or calibrated)
        self.res_hist = deque(maxlen=self.HIST_POINTS_XY)    # displayed Resistance series (raw V or calibrated)

        self._force_func = None
        self._resistance_func = None
        self._force_csv_cal = None
        self._notch_state = None

        # Global filtering state (applied to both channels when enabled)
        self._v0_notch: Optional[_BiquadNotch] = None
        self._v2_notch: Optional[_BiquadNotch] = None
        self._v0_lp = _EMAFilter(alpha=0.02)
        self._v2_lp = _EMAFilter(alpha=0.02)

        self._pending_logs: List[str] = []

        self._build_ui()
        self._init_plots()
        self._init_daq()
        self._refresh_eq_labels()

        self.timer = QTimer(self)
        self.timer.setInterval(self.GUI_TIMER_MS)
        self.timer.timeout.connect(self._poll_once)

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self):
        root = QHBoxLayout(self)

        # LEFT: controls + terminal
        left = QVBoxLayout()
        root.addLayout(left, 0)

        ctrl_box = QGroupBox("Controls")
        grid = QGridLayout(ctrl_box)
        left.addWidget(ctrl_box)

        # row 0: Start / Stop / Clear
        self.btn_start = QPushButton("Start")
        self.btn_stop = QPushButton("Stop")
        self.btn_stop.setEnabled(False)
        self.btn_clear = QPushButton("Clear")

        grid.addWidget(self.btn_start, 0, 0)
        grid.addWidget(self.btn_stop, 0, 1)
        grid.addWidget(self.btn_clear, 0, 2)

        # rows 1–3: Jog X/Y/Z with entry boxes
        grid.addWidget(QLabel("Jog X (mm):"), 1, 0)
        self.edit_jog_x = QLineEdit()
        self.edit_jog_x.setPlaceholderText("1.0")
        grid.addWidget(self.edit_jog_x, 1, 1)
        self.btn_jog_x = QPushButton("Jog X")
        grid.addWidget(self.btn_jog_x, 1, 2)

        grid.addWidget(QLabel("Jog Y (mm):"), 2, 0)
        self.edit_jog_y = QLineEdit()
        self.edit_jog_y.setPlaceholderText("1.0")
        grid.addWidget(self.edit_jog_y, 2, 1)
        self.btn_jog_y = QPushButton("Jog Y")
        grid.addWidget(self.btn_jog_y, 2, 2)

        grid.addWidget(QLabel("Jog Z (mm):"), 3, 0)
        self.edit_jog_z = QLineEdit()
        self.edit_jog_z.setPlaceholderText("-0.5")  # typical down move
        grid.addWidget(self.edit_jog_z, 3, 1)
        self.btn_jog_z = QPushButton("Jog Z")
        grid.addWidget(self.btn_jog_z, 3, 2)

        # row 4: notch toggle
        self.chk_notch = QCheckBox("50 Hz notch (applied before calibration)")
        self.chk_notch.setChecked(True)
        grid.addWidget(self.chk_notch, 4, 0, 1, 3)

        # Global filtering toggle: applies to ALL graphs/readouts when ON
        self.chk_filter = QCheckBox("Filtering ON (notch + low-pass) — applies to all graphs")
        self.chk_filter.setChecked(True)
        grid.addWidget(self.chk_filter, 5, 0, 1, 3)

        # row 5: Force axis mode
        grid.addWidget(QLabel("Force axis (X):"), 5, 0)
        self.combo_force = QComboBox()
        self.combo_force.addItems(["Raw (Volts)", "Calibrated"])
        self.combo_force.setCurrentIndex(0)
        grid.addWidget(self.combo_force, 5, 1, 1, 2)

        # row 6: Resistance axis mode
        grid.addWidget(QLabel("Resistance axis (Y):"), 6, 0)
        self.combo_res = QComboBox()
        self.combo_res.addItems(["Raw (Volts)", "Calibrated"])
        self.combo_res.setCurrentIndex(0)
        grid.addWidget(self.combo_res, 6, 1, 1, 2)

        # row 7: Force equation label
        grid.addWidget(QLabel("Force equation:"), 7, 0, alignment=Qt.AlignTop)
        self.lbl_force_eq = QLabel("")
        self.lbl_force_eq.setWordWrap(True)
        self.lbl_force_eq.setTextInteractionFlags(Qt.TextSelectableByMouse)
        grid.addWidget(self.lbl_force_eq, 7, 1, 1, 2)

        # row 8: Resistance equation label
        grid.addWidget(QLabel("Resistance equation:"), 8, 0, alignment=Qt.AlignTop)
        self.lbl_res_eq = QLabel("")
        self.lbl_res_eq.setWordWrap(True)
        self.lbl_res_eq.setTextInteractionFlags(Qt.TextSelectableByMouse)
        grid.addWidget(self.lbl_res_eq, 8, 1, 1, 2)

        # row 9: status
        self.lbl_status = QLabel("Status: idle")
        grid.addWidget(self.lbl_status, 9, 0, 1, 3)

        # Terminal
        term_box = QGroupBox("Live readings")
        term_layout = QVBoxLayout(term_box)
        left.addWidget(term_box, 1)

        self.terminal = QPlainTextEdit()
        self.terminal.setReadOnly(True)
        self.terminal.setMaximumBlockCount(self.LOG_MAX_LINES)
        self.terminal.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.terminal.setStyleSheet("font-family: monospace; font-size: 11px;")
        term_layout.addWidget(self.terminal)

        # RIGHT: plots
        right = QVBoxLayout()
        root.addLayout(right, 1)

        right.addWidget(QLabel("Top: X vs Y (Force vs Resistance – modes)"))
        self.graph_top = pg.GraphicsLayoutWidget()
        self.graph_top.setBackground("k")
        right.addWidget(self.graph_top, 3)

        right.addWidget(QLabel("Bottom: Force / Resistance vs sample index (modes + filtering)"))
        self.graph_bottom = pg.GraphicsLayoutWidget()
        self.graph_bottom.setBackground("k")
        right.addWidget(self.graph_bottom, 2)

        # Connections
        self.btn_start.clicked.connect(self.start)
        self.btn_stop.clicked.connect(self.stop)
        self.btn_clear.clicked.connect(self.clear)

        self.btn_jog_x.clicked.connect(lambda: self._jog_axis("X", self.edit_jog_x))
        self.btn_jog_y.clicked.connect(lambda: self._jog_axis("Y", self.edit_jog_y))
        self.btn_jog_z.clicked.connect(lambda: self._jog_axis("Z", self.edit_jog_z))

        self.combo_force.currentIndexChanged.connect(self._on_force_mode_changed)
        self.combo_res.currentIndexChanged.connect(self._on_res_mode_changed)

    def _init_plots(self):
        # Top: XY
        self.xy_plot = self.graph_top.addPlot()
        self.xy_plot.setYRange(-10000, 10000)
        self.xy_plot.enableAutoRange(axis='x', enable=True)
        self.xy_plot.enableAutoRange(axis='y', enable=False)

        self.xy_plot.showGrid(x=True, y=True, alpha=0.3)
        self.xy_plot.setClipToView(True)
        self.xy_curve = self.xy_plot.plot([], [], pen=pg.mkPen("c", width=2), symbol=None)
        self._update_xy_labels()

        # Bottom: CH0
        self.ch0_plot = self.graph_bottom.addPlot(row=0, col=0)
        self.ch0_plot.showGrid(x=True, y=True, alpha=0.3)
        self.ch0_plot.setLabel("left", "Force")
        self.ch0_plot.setLabel("bottom", "Sample index (history)")
        self.ch0_curve = self.ch0_plot.plot([], [], pen=pg.mkPen("w", width=1))
        self.ch0_avg_curve = self.ch0_plot.plot([], [], pen=pg.mkPen("y", width=2))

        # Bottom: CH2
        self.ch2_plot = self.graph_bottom.addPlot(row=1, col=0)
        self.ch2_plot.showGrid(x=True, y=True, alpha=0.3)
        self.ch2_plot.setLabel("left", "Resistance")
        self.ch2_plot.setLabel("bottom", "Sample index (history)")
        self.ch2_curve = self.ch2_plot.plot([], [], pen=pg.mkPen("w", width=1))
        self.ch2_avg_curve = self.ch2_plot.plot([], [], pen=pg.mkPen("y", width=2))

        # Keep bottom plot labels in sync with dropdown selection
        self._update_bottom_labels()

    def _update_xy_labels(self):
        fx = self.combo_force.currentText() if hasattr(self, "combo_force") else "?"
        ry = self.combo_res.currentText() if hasattr(self, "combo_res") else "?"
        self.xy_plot.setLabel("bottom", f"Force (X) – {fx}")
        self.xy_plot.setLabel("left", f"Resistance (Y) – {ry}")

    def _update_bottom_labels(self):
        """Keep bottom plots consistent with the dropdown modes."""
        fx = self.combo_force.currentText() if hasattr(self, "combo_force") else "?"
        ry = self.combo_res.currentText() if hasattr(self, "combo_res") else "?"
        self.ch0_plot.setLabel("left", f"Force – {fx}")
        self.ch2_plot.setLabel("left", f"Resistance – {ry}")


    # ------------------------------------------------------------------
    # Mode change handlers (keep graphs plotting what labels say)
    # ------------------------------------------------------------------
    def _on_force_mode_changed(self):
        # Keep labels up to date and rebuild ONLY the force display series
        self._update_xy_labels()
        self._update_bottom_labels()
        self._rebuild_force_series()
        self._redraw_all()

    def _on_res_mode_changed(self):
        # Keep labels up to date and rebuild ONLY the resistance display series
        self._update_xy_labels()
        self._update_bottom_labels()
        self._rebuild_res_series()
        self._redraw_all()

    def _rebuild_force_series(self):
        """Rebuild force_hist from stored CH0 volts (v0_hist) using current force mode."""
        self.force_hist.clear()
        use_force_cal = (self.combo_force.currentText() == "Calibrated")
        if not self.v0_hist:
            return
        if use_force_cal and self._force_func is not None:
            for v0 in self.v0_hist:
                try:
                    self.force_hist.append(float(self._force_func(float(v0))))
                except Exception:
                    self.force_hist.append(float("nan"))
        else:
            for v0 in self.v0_hist:
                self.force_hist.append(float(v0))

    def _rebuild_res_series(self):
        """Rebuild res_hist from stored CH2 volts (v2_hist) using current resistance mode."""
        self.res_hist.clear()
        use_res_cal = (self.combo_res.currentText() == "Calibrated")
        if not self.v2_hist:
            return
        if use_res_cal and self._resistance_func is not None:
            for v2 in self.v2_hist:
                try:
                    self.res_hist.append(float(self._resistance_func(float(v2))))
                except Exception:
                    self.res_hist.append(float("nan"))
        else:
            for v2 in self.v2_hist:
                self.res_hist.append(float(v2))

    def _redraw_all(self):
        """Redraw plots from current histories without changing them."""
        # XY
        if len(self.force_hist) >= 2 and len(self.res_hist) >= 2:
            x_arr = np.fromiter(self.force_hist, dtype=np.float64)
            y_arr = np.fromiter(self.res_hist, dtype=np.float64)
            y_arr = np.nan_to_num(y_arr, nan=0.0)
            self.xy_curve.setData(x_arr, y_arr)
        else:
            self.xy_curve.setData([], [])

        # Bottom FORCE (sample history)
        n0 = len(self.force_hist)
        if n0 >= 2:
            # show last HIST_POINTS_RAW points
            show = min(n0, self.HIST_POINTS_RAW)
            xs0 = np.arange(show, dtype=np.int32)
            f_arr = np.fromiter(list(self.force_hist)[-show:], dtype=np.float64)
            self.ch0_curve.setData(xs0, f_arr)
            avg0 = float(np.nanmean(f_arr))
            self.ch0_avg_curve.setData([xs0[0], xs0[-1]], [avg0, avg0])
        else:
            self.ch0_curve.setData([], [])
            self.ch0_avg_curve.setData([], [])

        # Bottom RESISTANCE (sample history)
        n2 = len(self.res_hist)
        if n2 >= 2:
            show = min(n2, self.HIST_POINTS_RAW)
            xs2 = np.arange(show, dtype=np.int32)
            r_arr = np.fromiter(list(self.res_hist)[-show:], dtype=np.float64)
            self.ch2_curve.setData(xs2, r_arr)
            avg2 = float(np.nanmean(r_arr))
            self.ch2_avg_curve.setData([xs2[0], xs2[-1]], [avg2, avg2])
        else:
            self.ch2_curve.setData([], [])
            self.ch2_avg_curve.setData([], [])
    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------
    def _log(self, msg: str):
        self._pending_logs.append(str(msg))

    def _flush_log(self):
        if not self._pending_logs:
            return
        try:
            self.terminal.appendPlainText("\n".join(self._pending_logs))
        except Exception:
            pass
        self._pending_logs.clear()

    # ------------------------------------------------------------------
    # DAQ init (continuous scan wrapper)
    # ------------------------------------------------------------------
    def _init_daq(self):
        def log(msg: str):
            self._log(msg)
            self._flush_log()

        self.daq = MCC128ContinuousScan(
            desired_rate_hz=self.SCAN_RATE_HZ,
            log_func=log,
        )
        if self.daq is None or self.daq.hat is None:
            self.lbl_status.setText("Status: NO MCC-128 FOUND")
        else:
            self.lbl_status.setText(
                f"Status: MCC-128 ready (continuous, {self.daq.rate_hz:.1f} Hz)"
            )

    # ------------------------------------------------------------------
    # Calibration helpers
    # ------------------------------------------------------------------
    def _compile_equation(self, eq_str: str):
        """Accepts 'y = ...' or just '...' and builds f(x)."""
        if not isinstance(eq_str, str):
            return None
        s = eq_str.strip()
        if not s:
            return None
        m = re.search(r"y\s*=\s*(.+)", s, flags=re.IGNORECASE)
        if m:
            expr = m.group(1).strip()
        else:
            expr = s
        expr = expr.replace("^", "**")

        def f(x: float) -> float:
            try:
                return float(
                    eval(expr, {"__builtins__": None, "math": math}, {"x": float(x)})
                )
            except Exception:
                return float("nan")

        return f

    def _load_force_csv(self) -> Tuple[float, float]:
        """CSV fallback for force calibration."""
        if self._force_csv_cal is not None:
            return self._force_csv_cal

        import csv
        candidates = [
            os.path.join(_APP_DIR, "force_calibration.csv"),
            os.path.join(_ROOT_DIR, "force_calibration.csv"),
            "force_calibration.csv",
        ]
        offset = 0.0
        gain = 1.0
        for path in candidates:
            if not os.path.exists(path):
                continue
            try:
                with open(path, "r") as f:
                    rows = list(csv.reader(f))
                nums = [float(c) for row in rows for c in row if c]
                if len(nums) >= 2:
                    offset, gain = nums[0], nums[1]
                    self._log(
                        f"[ForceCal] Loaded from {os.path.basename(path)} "
                        f"offset={offset:.6g}, gain={gain:.6g}"
                    )
                    break
            except Exception as e:
                self._log(f"[ForceCal] Error reading '{path}': {e}")

        self._force_csv_cal = (offset, gain)
        return self._force_csv_cal

    def _build_resistance_func_from_summary(self, s: str):
        """Build R(V) from a 'model=power_rational' summary string.

        Parsing lives in processing/calibration.py so this debugger, the live
        runner and the oscilloscope can never disagree about what a saved
        calibration means.  The obsolete 'model=rational' polynomial form is
        no longer produced or accepted.
        """
        if not isinstance(s, str):
            return None
        model = parse_resistance_calibration(s)
        if model is None:
            self._log("[ResCal] Not a valid power_rational summary.")
            return None

        def r_from_v(V_target: float) -> float:
            arr = model.r_from_v_array(np.asarray([float(V_target)], dtype=float))
            return float(arr[0])

        self._log(f"[ResCal] Using power_rational summary: {model!r}")
        return r_from_v


    def _load_resistance_from_latest_file(self) -> Tuple[Optional[Any], Optional[str]]:
        """
        Fallback for resistance:
            newest file in /home/charlie/Documents/Calibrations/Resistance_calibration

        Files are typically named like 'ResistanceCal<timestamp>'.
        We:
            - list all regular files in that directory
            - prefer ones whose name starts with 'ResistanceCal'
            - pick the one with the latest modification time
            - read its header lines (until blank or comment/data header)
        """
        base_dir = "/home/charlie/Documents/Calibrations/Resistance_calibration"
        if not os.path.isdir(base_dir):
            self._log(f"[ResCal] Calibration directory not found: {base_dir}")
            return None, None

        try:
            all_files = [
                os.path.join(base_dir, name)
                for name in os.listdir(base_dir)
                if os.path.isfile(os.path.join(base_dir, name))
            ]
        except Exception as e:
            self._log(f"[ResCal] Error listing {base_dir}: {e}")
            return None, None

        if not all_files:
            self._log(f"[ResCal] No files found in {base_dir}.")
            return None, None

        # Prefer files starting with 'ResistanceCal', fall back to all files if none
        pref_files = [f for f in all_files if os.path.basename(f).startswith("ResistanceCal")]
        candidates = pref_files if pref_files else all_files

        try:
            latest_path = max(candidates, key=os.path.getmtime)
        except Exception as e:
            self._log(f"[ResCal] Failed to select latest calibration file: {e}")
            return None, None

        self._log(f"[ResCal] Using latest calibration file: {os.path.basename(latest_path)}")

        try:
            with open(latest_path, "r") as f:
                lines = f.readlines()
        except Exception as e:
            self._log(f"[ResCal] Error reading {latest_path}: {e}")
            return None, None

        # Take config lines until blank or comment/data header
        config_lines: List[str] = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                break
            if stripped.startswith("#") or stripped.startswith("R_ohm"):
                break
            config_lines.append(stripped)

        if not config_lines:
            self._log("[ResCal] No config lines found in latest calibration file.")
            return None, None

        summary = "\n".join(config_lines)
        func = self._build_resistance_func_from_summary(summary)
        if func is None:
            return None, None

        return func, summary

    def _refresh_eq_labels(self):
        """
        Force:
            - models.latest_force_calibration OR force_calibration.csv.

        Resistance:
            - models.latest_resistance_calibration (if non-empty)
            - ELSE newest file in /home/charlie/Documents/Calibrations/Resistance_calibration
            - no other locations or fallbacks.
        """
        self._update_xy_labels()
        self._update_bottom_labels()

        # --- Force ---
        force_eq = getattr(models, "latest_force_calibration", None) if models is not None else None
        if isinstance(force_eq, str) and force_eq.strip():
            self._force_func = self._compile_equation(force_eq)
            self.lbl_force_eq.setText(force_eq.strip())
            self._log(f"[ForceCal] Using models.latest_force_calibration: {force_eq}")
        else:
            offset, gain = self._load_force_csv()
            self._force_func = lambda v, o=offset, g=gain: (float(v) - o) * g
            self.lbl_force_eq.setText(f"(x - {offset}) * {gain} (force_calibration.csv)")
            self._log("[ForceCal] Using CSV-based fallback calibration.")

        # --- Resistance ---
        res_func: Optional[Any] = None
        res_label: str = "<none>"

        # 1) models.latest_resistance_calibration
        eq_str = None
        if models is not None:
            eq_str = getattr(models, "latest_resistance_calibration", None)

        if isinstance(eq_str, str) and eq_str.strip():
            s = eq_str.strip()
            if "power_rational" in s:
                res_func = self._build_resistance_func_from_summary(s)
                if res_func is not None:
                    res_label = s
            else:
                func = self._compile_equation(s)
                if func is not None:
                    res_func = func
                    res_label = s

        # 2) latest file in calibration directory
        if res_func is None:
            res_func, file_label = self._load_resistance_from_latest_file()
            if res_func is not None and file_label is not None:
                res_label = file_label

        if res_func is None:
            self._log(
                "[ResCal] No usable resistance calibration found "
                "(models var + latest file both missing/invalid). "
                "Calibrated mode will use raw CH2."
            )
            res_label = "<none>"

        self._resistance_func = res_func
        self.lbl_res_eq.setText(res_label)

        # Rebuild display series so plots match current dropdown modes
        self._rebuild_force_series()
        self._rebuild_res_series()
        self._redraw_all()
        self._flush_log()

    # ------------------------------------------------------------------
    # Notch filter (for calibrated Force only)
    # ------------------------------------------------------------------
    def _init_notch(self, fs_hz: float, f0_hz: float = 50.0, q: float = 30.0):
        try:
            fs = float(fs_hz)
            f0 = float(f0_hz)
            Q = float(q)
            if fs <= 0 or f0 <= 0 or f0 >= (fs / 2.0):
                self._notch_state = None
                return

            w0 = 2.0 * math.pi * (f0 / fs)
            alpha = math.sin(w0) / (2.0 * Q)
            cw = math.cos(w0)

            b0 = 1.0
            b1 = -2.0 * cw
            b2 = 1.0
            a0 = 1.0 + alpha
            a1 = -2.0 * cw
            a2 = 1.0 - alpha

            b0 /= a0
            b1 /= a0
            b2 /= a0
            a1 /= a0
            a2 /= a0

            self._notch_state = {
                "b0": b0,
                "b1": b1,
                "b2": b2,
                "a1": a1,
                "a2": a2,
                "x1": 0.0,
                "x2": 0.0,
                "y1": 0.0,
                "y2": 0.0,
            }
        except Exception:
            self._notch_state = None

    def _notch_step(self, x: float) -> float:
        st = self._notch_state
        if not isinstance(st, dict):
            return float(x)

        b0 = st["b0"]; b1 = st["b1"]; b2 = st["b2"]
        a1 = st["a1"]; a2 = st["a2"]

        x0 = float(x)
        y0 = (b0 * x0) + (b1 * st["x1"]) + (b2 * st["x2"]) - (a1 * st["y1"]) - (a2 * st["y2"])

        st["x2"] = st["x1"]
        st["x1"] = x0
        st["y2"] = st["y1"]
        st["y1"] = y0

        return float(y0)

    # ------------------------------------------------------------------
    # Control (start/stop/clear)
    # ------------------------------------------------------------------
    def start(self):
        if self.daq is None or self.daq.hat is None:
            self._log("[DAQ] No MCC-128 available.")
            self._flush_log()
            return
        if self.scan_running:
            return

        self.scan_running = True
        self.tick_idx = 0
        self.sample_idx = 0

        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.lbl_status.setText("Status: continuous scan running")

        # Start DAQ continuous scan
        self.daq.start()
        # Init notch filters using actual scan rate
        fs = self.daq.rate_hz or self.SCAN_RATE_HZ
        if self.chk_notch.isChecked():
            # Existing notch init (kept)
            self._init_notch(fs_hz=fs, f0_hz=50.0, q=30.0)
            # NEW: notch on voltage streams (used when global filtering enabled)
            self._v0_notch = _BiquadNotch(fs_hz=fs, f0_hz=50.0, q=30.0)
            self._v2_notch = _BiquadNotch(fs_hz=fs, f0_hz=50.0, q=30.0)
        else:
            self._notch_state = None
            self._v0_notch = None
            self._v2_notch = None

        # Reset low-pass states so you don't smear old data into a new run
        self._v0_lp.reset()
        self._v2_lp.reset()

        self._log(
            f"[DAQ] Continuous scan started: rate={self.daq.rate_hz:.1f} Hz, "
            f"GUI tick={self.GUI_TIMER_MS} ms, chunk={self.SAMPLES_PER_CHUNK} samples/channel."
        )
        self._flush_log()
        self.timer.start()

    def stop(self):
        if not self.scan_running:
            return

        self.timer.stop()
        self.scan_running = False

        if self.daq is not None:
            self.daq.stop()

        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.lbl_status.setText("Status: stopped")

        self._log("[DAQ] Continuous scan stopped.")
        self._flush_log()

    def clear(self):
        self.tick_idx = 0
        self.sample_idx = 0

        self.v0_hist.clear()
        self.v2_hist.clear()
        self.force_hist.clear()
        self.res_hist.clear()
        self._pending_logs.clear()

        self.xy_curve.setData([], [])
        self.ch0_curve.setData([], [])
        self.ch2_curve.setData([], [])
        self.ch0_avg_curve.setData([], [])
        self.ch2_avg_curve.setData([], [])

        self.terminal.clear()
        self._log("[UI] Cleared.")
        self._flush_log()

    def closeEvent(self, event):
        try:
            self.stop()
        except Exception:
            pass
        event.accept()

    # ------------------------------------------------------------------
    # Jog helpers (X/Y/Z with entry boxes)
    # ------------------------------------------------------------------
    def _ensure_duet(self) -> bool:
        if DuetAdapter is None:
            self._log("[Duet] DuetAdapter not available (import failed).")
            self._flush_log()
            return False

        if self.duet is None:
            try:
                self.duet = DuetAdapter()
                self._log("[Duet] Created DuetAdapter for jog.")
            except Exception as e:
                self._log(f"[Duet] Failed to create DuetAdapter: {e}")
                self._flush_log()
                return False

        return True

    def _jog_axis(self, axis: str, edit: QLineEdit):
        if not self._ensure_duet():
            return

        text = edit.text().strip()
        if not text:
            text = edit.placeholderText().strip() or "0"

        try:
            dist = float(text)
        except Exception:
            self._log(f"[Duet] Invalid jog distance for {axis}: {text!r}")
            self._flush_log()
            return

        try:
            self.duet.send_gcode("G91")
            self.duet.send_gcode(f"G1 {axis}{dist:.3f} F{self.JOG_FEED:.0f}")
            self.duet.send_gcode("G90")
            self._log(f"[Duet] Jog {axis}: {dist:.3f} mm at F{self.JOG_FEED:.0f}.")
            self._flush_log()
        except Exception as e:
            self._log(f"[Duet] Jog {axis} error: {e}")
            self._flush_log()

    # ------------------------------------------------------------------
    # Polling: read from continuous scan buffer + plotting
    # ------------------------------------------------------------------
    def _poll_once(self):
        if not self.scan_running or self.daq is None:
            return

        self.tick_idx += 1

        v0_list, v2_list = self.daq.read(
            samples_per_channel=self.SAMPLES_PER_CHUNK,
            timeout_s=0.0,
        )
        if not v0_list or not v2_list:
            return

        n = min(len(v0_list), len(v2_list))
        self.sample_idx += n

        use_force_cal = (self.combo_force.currentText() == "Calibrated")
        use_res_cal = (self.combo_res.currentText() == "Calibrated")
        # Convert lists to arrays for vector filtering
        v0_arr = np.asarray(v0_list[:n], dtype=np.float64)  # CH0 volts (raw force channel)
        v2_arr = np.asarray(v2_list[:n], dtype=np.float64)  # CH2 volts (raw resistance channel)

        # Global filtering applies to ALL graphs when enabled
        if hasattr(self, "chk_filter") and self.chk_filter.isChecked():
            if self.chk_notch.isChecked() and self._v0_notch is not None and self._v2_notch is not None:
                v0_arr = self._v0_notch.process_vector(v0_arr)
                v2_arr = self._v2_notch.process_vector(v2_arr)
            v0_arr = self._v0_lp.process_vector(v0_arr)
            v2_arr = self._v2_lp.process_vector(v2_arr)

        for i in range(n):
            # Channel volts (possibly filtered if Filtering ON)
            v0 = float(v0_arr[i])  # CH0 volts
            v2 = float(v2_arr[i])  # CH2 volts

            # Store volt histories (so switching modes can rebuild displays correctly)
            self.v0_hist.append(v0)
            self.v2_hist.append(v2)

            # Build displayed force/resistance series according to dropdowns
            if use_force_cal and self._force_func is not None:
                try:
                    fx = float(self._force_func(v0))
                except Exception:
                    fx = float("nan")
            else:
                fx = v0

            if use_res_cal and self._resistance_func is not None:
                try:
                    ry = float(self._resistance_func(v2))
                except Exception:
                    ry = float("nan")
            else:
                ry = v2

            self.force_hist.append(fx)
            self.res_hist.append(ry)
        # Update plots (all plots use the same series the labels claim)
        self._redraw_all()
        # Logging (not for every sample, just per GUI tick)
        if (self.tick_idx % self.LOG_TICK_STRIDE) == 0:
            last_raw0 = v0_list[-1]
            last_raw2 = v2_list[-1]
            self._log(
                f"[DBG] tick={self.tick_idx:06d}  n={n:4d}  total_samples={self.sample_idx:7d}  "
                f"CH0_last={last_raw0:+.6f}V  CH2_last={last_raw2:+.6f}V"
            )
            self._flush_log()






# =============================================================================
# DBG — SoftTouchDebugger
#
# Writes debug events to a log file. An xterm window tails that file live.
# This is the simplest possible approach — no sockets, no races, no sync.
#
# HOW IT WORKS:
#   1. attach(duet) sets duet.soft_touch_debug_hook = self._hook
#   2. On first event (baseline_done), opens an xterm running "tail -f logfile"
#   3. Every event writes a formatted line to the log file immediately
#   4. xterm displays it instantly because tail -f watches the file
#
# TO DISABLE: set DBG_SOFT_TOUCH = False in duet_adapter.py
# TO REMOVE:  comment out _attach_soft_touch_debugger(w) in main() below
# Every line in this section is tagged  # DBG  for easy grep/delete
# =============================================================================

import os as _os                    # DBG
import subprocess as _subprocess    # DBG
import threading as _threading      # DBG
import time as _time                # DBG


_DBG_LOG_PATH = _os.path.join(                                     # DBG
    _os.path.expanduser("~"), "soft_touch_debug.log"               # DBG
)                                                                   # DBG


class SoftTouchDebugger:  # DBG

    def __init__(self):  # DBG
        self._duet = None          # DBG
        self._xterm_proc = None    # DBG
        self._run_number = 0       # DBG
        self._run_start = 0.0      # DBG
        self._step_counts = {}     # DBG
        self._daq_counts = {}      # DBG
        self._log_file = None      # DBG
        self._lock = _threading.Lock()  # DBG

    # ------------------------------------------------------------------
    # DBG — Public
    # ------------------------------------------------------------------
    def attach(self, duet) -> None:  # DBG
        self._duet = duet           # DBG
        duet.soft_touch_debug_hook = self._hook  # DBG
        print("[SoftTouchDebugger] Attached — xterm will open when soft touch runs.")  # DBG

    def detach(self) -> None:  # DBG
        if self._duet is not None:          # DBG
            self._duet.soft_touch_debug_hook = None  # DBG
            self._duet = None               # DBG

    # ------------------------------------------------------------------
    # DBG — Hook — called from DuetAdapter background threads
    # Only file I/O here — completely thread safe, never blocks
    # ------------------------------------------------------------------
    def _hook(self, event: str, data: dict) -> None:  # DBG
        ts = _time.time()  # DBG

        if event == "baseline_done":  # DBG
            self._run_number += 1       # DBG
            self._run_start = ts        # DBG
            self._step_counts = {}      # DBG
            self._daq_counts = {}       # DBG
            self._open_log_and_terminal()  # DBG

        line = self._format(event, data, ts)  # DBG
        if line:                              # DBG
            self._write(line)                 # DBG

    # ------------------------------------------------------------------
    # DBG — Open log file and xterm tail
    # ------------------------------------------------------------------
    def _open_log_and_terminal(self) -> None:  # DBG
        with self._lock:  # DBG
            # Close previous log file if open  # DBG
            if self._log_file is not None:  # DBG
                try:                        # DBG
                    self._log_file.close()  # DBG
                except Exception:           # DBG
                    pass                    # DBG
                self._log_file = None       # DBG

            # Open fresh log file (overwrite each run)  # DBG
            try:  # DBG
                self._log_file = open(_DBG_LOG_PATH, "w", encoding="utf-8", buffering=1)  # DBG
                # buffering=1 = line buffered — every write flushes immediately  # DBG
            except Exception as e:  # DBG
                print(f"[SoftTouchDebugger] Cannot open log file: {e}")  # DBG
                return  # DBG

            # Kill previous xterm if still open  # DBG
            if self._xterm_proc is not None:  # DBG
                try:                          # DBG
                    self._xterm_proc.terminate()  # DBG
                except Exception:             # DBG
                    pass                      # DBG
                self._xterm_proc = None       # DBG

            # Spawn xterm tailing the log file  # DBG
            try:  # DBG
                self._xterm_proc = _subprocess.Popen(  # DBG
                    [  # DBG
                        "xterm",  # DBG
                        "-title", f"Soft Touch Debug — Run #{self._run_number}",  # DBG
                        "-geometry", "110x40",  # DBG
                        "-bg", "black",         # DBG
                        "-fg", "#e0e0e0",        # DBG
                        "-fa", "Monospace",      # DBG
                        "-fs", "10",             # DBG
                        "-e", f"tail -f {_DBG_LOG_PATH}",  # DBG
                    ],  # DBG
                    stdout=_subprocess.DEVNULL,  # DBG
                    stderr=_subprocess.DEVNULL,  # DBG
                )  # DBG
            except FileNotFoundError:  # DBG
                print("[SoftTouchDebugger] xterm not found — install: sudo apt install xterm")  # DBG
            except Exception as e:  # DBG
                print(f"[SoftTouchDebugger] Cannot open xterm: {e}")  # DBG

    # ------------------------------------------------------------------
    # DBG — Write a line to the log file
    # ------------------------------------------------------------------
    def _write(self, line: str) -> None:  # DBG
        with self._lock:               # DBG
            f = self._log_file         # DBG
        if f is None:                  # DBG
            return                     # DBG
        try:                           # DBG
            f.write(line + "\n")       # DBG
        except Exception:              # DBG
            pass                       # DBG

    # ------------------------------------------------------------------
    # DBG — Format each event type into a readable line
    # Add a new elif block here when you add new _dbg calls elsewhere
    # ------------------------------------------------------------------
    def _format(self, event: str, data: dict, ts: float) -> str:  # DBG
        elapsed = ts - self._run_start  # DBG
        t = f"[+{elapsed:7.3f}s]"      # DBG

        if event == "baseline_done":  # DBG
            return (  # DBG
                f"\n{'='*70}\n"  # DBG
                f"  SOFT TOUCH RUN #{self._run_number}   {_time.strftime('%H:%M:%S')}\n"  # DBG
                f"{'='*70}\n"  # DBG
                f"{t} BASELINE DONE\n"  # DBG
                f"           samples     = {data.get('samples')}\n"  # DBG
                f"           duration    = {data.get('duration_s', 0):.3f} s\n"  # DBG
                f"           sample rate = {data.get('fs_est_hz', 0):.1f} Hz\n"  # DBG
                f"           offset      = {data.get('offset_v', 0):.6f} V  "  # DBG
                f"(this will be subtracted from every reading)\n"  # DBG
            )  # DBG

        elif event == "phase1_start":  # DBG
            return (  # DBG
                f"{t} PHASE 1 START (coarse descent)\n"  # DBG
                f"           feedrate    = {data.get('eff_feed_coarse', 0):.1f} mm/min\n"  # DBG
                f"           step size   = {data.get('dz_step_coarse', 0):.4f} mm per tick\n"  # DBG
                f"           tick period = {data.get('stream_dt', 0)*1000:.1f} ms\n"  # DBG
                f"           Z start     = {data.get('z_start_coarse', 0):.3f} mm\n"  # DBG
                f"           max descent = {data.get('z_bottom_limit', 0):.1f} mm from start\n"  # DBG
                f"           threshold   = {data.get('threshold', 0):.5f} V above baseline\n"  # DBG
                f"           ignore win  = {data.get('coarse_ignore_s', 0):.2f} s\n"  # DBG
                f"           confirm N   = {data.get('confirm_count')} consecutive samples\n"  # DBG
            )  # DBG

        elif event == "daq_sample":  # DBG
            phase = data.get("phase", "?")                              # DBG
            n = self._daq_counts.get(phase, 0) + 1                     # DBG
            self._daq_counts[phase] = n                                 # DBG
            above = data.get("filt_v", 0) > data.get("threshold", 1e9) # DBG
            ignoring = data.get("in_ignore_window", False)             # DBG
            consec = data.get("consec", 0)                             # DBG
            # Only print every 100th sample unless above threshold      # DBG
            if not above and n % 100 != 0:                             # DBG
                return ""                                               # DBG
            flag = ""                                                   # DBG
            if ignoring:                                                # DBG
                flag = "  [ignore window active]"                      # DBG
            elif above:                                                 # DBG
                flag = f"  <<< ABOVE THRESHOLD consec={consec} >>>"   # DBG
            return (  # DBG
                f"{t} DAQ  n={n:06d} ph={phase}  "  # DBG
                f"raw={data.get('raw_v', 0):+.5f}V  "  # DBG
                f"adj={data.get('adj_v', 0):+.5f}V  "  # DBG
                f"filt={data.get('filt_v', 0):+.5f}V  "  # DBG
                f"thr={data.get('threshold', 0):.5f}V{flag}"  # DBG
            )  # DBG

        elif event == "feeder_step":  # DBG
            phase = data.get("phase", "?")                  # DBG
            n = self._step_counts.get(phase, 0) + 1         # DBG
            self._step_counts[phase] = n                    # DBG
            # Print first 5, then every 20th               # DBG
            if n > 5 and n % 20 != 0:                       # DBG
                return ""                                   # DBG
            return (  # DBG
                f"{t} STEP n={n:05d} ph={phase}  "  # DBG
                f"step={data.get('step_mm', 0):.4f}mm  "  # DBG
                f"total={data.get('z_descended_mm', 0):.4f}mm  "  # DBG
                f"limit={data.get('z_bottom_limit', 0):.2f}mm"  # DBG
            )  # DBG

        elif event == "feeder_write_fail":  # DBG
            return (  # DBG
                f"{t} *** FEEDER WRITE FAIL ph={data.get('phase','?')} "  # DBG
                f"after {data.get('z_descended_mm', 0):.4f}mm ***\n"  # DBG
                f"           Serial write to Duet failed.\n"  # DBG
                f"           Likely cause: serial timeout or port disconnected."  # DBG
            )  # DBG

        elif event == "exit":  # DBG
            reason = data.get("reason", "unknown")  # DBG
            descended = data.get("z_descended_mm", 0)  # DBG
            crossing = data.get("crossing_v")  # DBG

            descriptions = {  # DBG
                "bottom_limit_coarse": (  # DBG
                    f"BOTTOM LIMIT HIT (coarse) — no surface found after {descended:.3f}mm\n"  # DBG
                    f"           The force sensor never crossed the threshold.\n"  # DBG
                    f"           Check: threshold too high? sensor not reading? table too far?"  # DBG
                ),  # DBG
                "bottom_limit_fine": (  # DBG
                    f"BOTTOM LIMIT HIT (fine) — no surface found after {descended:.3f}mm\n"  # DBG
                    f"           Coarse triggered but fine did not. May have missed on back-off."  # DBG
                ),  # DBG
                "threshold_triggered_coarse": (  # DBG
                    f"SURFACE FOUND coarse at {descended:.3f}mm  "  # DBG
                    f"crossing_v={crossing:.5f}V"  # DBG
                ),  # DBG
                "threshold_triggered_fine": (  # DBG
                    f"SURFACE FOUND fine at {descended:.3f}mm  "  # DBG
                    f"crossing_v={crossing:.5f}V"  # DBG
                ),  # DBG
                "feeder_died_coarse": (  # DBG
                    f"FEEDER DIED (coarse) after {descended:.3f}mm\n"  # DBG
                    f"           Serial write failed — check Duet connection."  # DBG
                ),  # DBG
                "feeder_died_fine": (  # DBG
                    f"FEEDER DIED (fine) after {descended:.3f}mm\n"  # DBG
                    f"           Serial write failed — check Duet connection."  # DBG
                ),  # DBG
                "no_serial": (  # DBG
                    "SIMULATION MODE — pyserial not installed or Duet not connected.\n"  # DBG
                    "           The baseline scan ran but all motion was skipped.\n"  # DBG
                    "           Connect the Duet USB cable and install pyserial."  # DBG
                ),  # DBG
            }  # DBG
            desc = descriptions.get(reason, f"UNKNOWN EXIT: {reason}")  # DBG
            total_steps = sum(self._step_counts.values())  # DBG
            total_daq = sum(self._daq_counts.values())     # DBG
            return (  # DBG
                f"\n{'-'*70}\n"  # DBG
                f"{t} EXIT: {desc}\n"  # DBG
                f"           steps total = {total_steps}   daq total = {total_daq}\n"  # DBG
                f"{'-'*70}\n"  # DBG
                f"\n(xterm stays open — close it manually when done reading)\n"  # DBG
            )  # DBG

        return ""  # DBG


# ---------------------------------------------------------------------------
# DBG — Wire debugger into operator_mode via import
# This function is imported and called from operator_mode.py __init__
# ---------------------------------------------------------------------------
def _attach_soft_touch_debugger(operator_popup) -> None:  # DBG
    """Attach a SoftTouchDebugger to the DuetAdapter in operator_popup."""  # DBG
    try:  # DBG
        dbg = SoftTouchDebugger()  # DBG
        dbg.attach(operator_popup.duet)  # DBG
        operator_popup._st_debugger = dbg  # DBG
    except Exception as e:  # DBG
        print(f"[SoftTouchDebugger] attach failed: {e}")  # DBG


def main():
    app = QApplication([])
    w = EquationsDebugger()
    w.resize(1320, 900)
    w.show()
    app.exec_()


if __name__ == "__main__":
    main()
