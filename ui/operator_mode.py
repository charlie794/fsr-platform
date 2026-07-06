from __future__ import annotations

import os
import numpy as np

from collections import deque
from PyQt5.QtCore import QThread, Qt, QMetaObject, Q_ARG, pyqtSlot, QTimer, pyqtSignal
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QVBoxLayout,
    QWidget,
    QLineEdit,
    QPushButton,
    QFileDialog,
    QTextEdit,
    QPlainTextEdit,
    QMessageBox,
    QLabel,
    QFormLayout,
    QDialogButtonBox,
    QTabWidget,
    QSplitter,
)
import pyqtgraph as pg

try:
    from Sensor_Testor.app_io.plan_loader import load_grid_plan_csv
except Exception:
    from app_io.plan_loader import load_grid_plan_csv

try:
    from Sensor_Testor.domain.models import store, RunConfig
except Exception:
    try:
        from domain.models import store, RunConfig
    except Exception:
        from models import store, RunConfig

try:
    from Sensor_Testor.runner.grid_runner import GridRunner
except Exception:
    try:
        from runner.grid_runner import GridRunner
    except Exception:
        from grid_runner import GridRunner

try:
    from Sensor_Testor.runner.test_runner import TestRunnerWorker
except Exception:
    try:
        from runner.test_runner import TestRunnerWorker
    except Exception:
        TestRunnerWorker = None


# ---------- Robust imports with inline fallback ----------
try:
    from Sensor_Testor.app_io.file_dialogs import save_prompt
except Exception:
    try:
        from app_io.file_dialogs import save_prompt
    except Exception:
        def save_prompt(parent=None, default_folder_name=None):
            folder = os.path.join(os.path.expanduser("~"), "Documents", default_folder_name or "TestRun")
            os.makedirs(folder, exist_ok=True)
            base = "test"
            with open("file_path.txt", "w") as f:
                f.write(f"{folder}\n{base}\n")
            return folder, base


try:
    from Sensor_Testor.ui.job_details import get_job_details
except Exception:
    try:
        from ui.job_details import get_job_details
    except Exception:
        class _JobDetailsDialog(QDialog):
            def __init__(self, parent=None):
                super().__init__(parent)
                self.setWindowTitle("Enter Job Details")
                self.layout = QVBoxLayout(self)
                self.form = QFormLayout()
                self.fields = [
                    "Job Number", "Lot Number", "Customer", "Customer ID", "Internal P/N",
                    "Internal Rev", "Customer P/N", "Customer Rev", "File", "Operator", "Comment",
                    "Quantity of Good Parts", "Max Failed Parts",
                ]
                self.inputs = {}
                for f in self.fields:
                    w = QLineEdit(self)
                    self.inputs[f] = w
                    self.form.addRow(QLabel(f + ":"), w)
                self.layout.addLayout(self.form)
                bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self)
                bb.accepted.connect(self.accept)
                bb.rejected.connect(self.reject)
                self.layout.addWidget(bb)

            def values(self):
                return {f: self.inputs[f].text() for f in self.fields}

        def get_job_details(_csv_file_path=None):
            dlg = _JobDetailsDialog()
            return dlg.values() if dlg.exec_() == QDialog.Accepted else {
                f: "" for f in _JobDetailsDialog().fields
            }


try:
    from Sensor_Testor.processing.criteria_loader import (
        parse_pass_fail_criteria_form,
        generate_smoothed_line,
    )
except Exception:
    try:
        from processing.criteria_loader import (
            parse_pass_fail_criteria_form,
            generate_smoothed_line,
        )
    except Exception:
        def parse_pass_fail_criteria_form(path: str):
            return None

        def generate_smoothed_line(x, y, *_args, **_kwargs):
            return x, y


try:
    from Sensor_Testor.hardware.duet_adapter import DuetAdapter
    from Sensor_Testor.hardware.smac_adapter import SmacAdapter
    from Sensor_Testor.hardware.daq_adapter import DaqAdapter
except Exception:
    from hardware.duet_adapter import DuetAdapter
    from hardware.smac_adapter import SmacAdapter
    from hardware.daq_adapter import DaqAdapter

try:
    from Sensor_Testor.app_io.plan_reader import read_rows, extract_table_and_plugin
except Exception:
    from app_io.plan_reader import read_rows, extract_table_and_plugin

try:
    from Sensor_Testor.hardware.home import run_home_sequence
except Exception:
    from hardware.home import run_home_sequence

# =============================================================================
# DBG — SoftTouchDebugger defined directly here so it always loads.
# No separate file, no import path issues.
# Prints all soft_touch events to THIS terminal (the one running main.py)
# so you can select and copy the output.
# Also saves to ~/soft_touch_debug.log as a backup.
# To disable everything: set DBG_SOFT_TOUCH = False in duet_adapter.py
# To remove: delete from here to the matching END DBG comment below.
# =============================================================================
import os as _dbg_os          # DBG
import threading as _dbg_th   # DBG
import time as _dbg_t         # DBG

_DBG_LOG = _dbg_os.path.join(_dbg_os.path.expanduser("~"), "soft_touch_debug.log")  # DBG


class SoftTouchDebugger:  # DBG
    def __init__(self):  # DBG
        self._duet  = None   # DBG
        self._f     = None   # DBG — log file handle
        self._lock  = _dbg_th.Lock()  # DBG
        self._run   = 0      # DBG
        self._t0    = 0.0    # DBG
        self._steps = {}     # DBG
        self._daqs  = {}     # DBG

    def attach(self, duet) -> None:  # DBG
        self._duet = duet  # DBG
        duet.soft_touch_debug_hook = self._hook  # DBG
        print("[DBG] SoftTouchDebugger attached — will print here when soft touch runs")  # DBG

    def _hook(self, event: str, data: dict) -> None:  # DBG
        ts = _dbg_t.time()  # DBG
        if event == "baseline_done":  # DBG
            self._run += 1  # DBG
            self._t0   = ts  # DBG
            self._steps = {}  # DBG
            self._daqs  = {}  # DBG
            self._open_log()  # DBG
        text = self._fmt(event, data, ts)  # DBG
        if text:  # DBG
            self._out(text)  # DBG

    def _open_log(self) -> None:  # DBG
        with self._lock:  # DBG
            if self._f:  # DBG
                try: self._f.close()  # DBG
                except Exception: pass  # DBG
            try:  # DBG
                self._f = open(_DBG_LOG, "w", encoding="utf-8", buffering=1)  # DBG
            except Exception as e:  # DBG
                print(f"[DBG] log file error: {e}")  # DBG

    def _out(self, text: str) -> None:  # DBG
        print(text, end="", flush=True)  # DBG — prints to this terminal, copyable
        with self._lock:  # DBG
            f = self._f  # DBG
        if f:  # DBG
            try: f.write(text); f.flush()  # DBG
            except Exception: pass  # DBG

    def _fmt(self, event: str, data: dict, ts: float) -> str:  # DBG
        e = ts - self._t0  # DBG
        t = f"[+{e:7.3f}s]"  # DBG

        if event == "baseline_done":  # DBG
            return (  # DBG
                f"\n{'='*65}\n"  # DBG
                f"  SOFT TOUCH RUN #{self._run}   {_dbg_t.strftime('%H:%M:%S')}\n"  # DBG
                f"{'='*65}\n"  # DBG
                f"{t} BASELINE\n"  # DBG
                f"         samples  = {data.get('samples')}\n"  # DBG
                f"         duration = {data.get('duration_s',0):.3f}s\n"  # DBG
                f"         fs       = {data.get('fs_est_hz',0):.1f} Hz\n"  # DBG
                f"         offset   = {data.get('offset_v',0):.6f} V\n"  # DBG
            )  # DBG

        elif event == "phase1_start":  # DBG
            return (  # DBG
                f"{t} PHASE 1 START\n"  # DBG
                f"         feed     = {data.get('eff_feed_coarse',0):.1f} mm/min\n"  # DBG
                f"         step     = {data.get('dz_step_coarse',0):.4f} mm\n"  # DBG
                f"         Z start  = {data.get('z_start_coarse',0):.3f} mm\n"  # DBG
                f"         limit    = {data.get('z_bottom_limit',0):.1f} mm descent\n"  # DBG
                f"         thresh   = {data.get('threshold',0):.5f} V\n"  # DBG
                f"         ignore   = {data.get('coarse_ignore_s',0):.2f} s\n"  # DBG
                f"         confirm  = {data.get('confirm_count')} samples\n"  # DBG
            )  # DBG

        elif event == "daq_sample":  # DBG
            ph = data.get("phase", "?")  # DBG
            n  = self._daqs.get(ph, 0) + 1  # DBG
            self._daqs[ph] = n  # DBG
            above    = data.get("filt_v", 0) > data.get("threshold", 1e9)  # DBG
            ignoring = data.get("in_ignore_window", False)  # DBG
            if not above and n % 100 != 0:  # DBG
                return ""  # DBG
            flag = " [ignore]" if ignoring else (f" *** ABOVE thr consec={data.get('consec',0)} ***" if above else "")  # DBG
            return (  # DBG
                f"{t} DAQ #{n:06d} ph={ph} "  # DBG
                f"raw={data.get('raw_v',0):+.5f}V "  # DBG
                f"adj={data.get('adj_v',0):+.5f}V "  # DBG
                f"filt={data.get('filt_v',0):+.5f}V "  # DBG
                f"thr={data.get('threshold',0):.5f}V{flag}\n"  # DBG
            )  # DBG

        elif event == "feeder_step":  # DBG
            ph = data.get("phase", "?")  # DBG
            n  = self._steps.get(ph, 0) + 1  # DBG
            self._steps[ph] = n  # DBG
            if n > 5 and n % 20 != 0:  # DBG
                return ""  # DBG
            return (  # DBG
                f"{t} STEP #{n:05d} ph={ph} "  # DBG
                f"step={data.get('step_mm',0):.4f}mm "  # DBG
                f"total={data.get('z_descended_mm',0):.4f}mm "  # DBG
                f"limit={data.get('z_bottom_limit',0):.2f}mm\n"  # DBG
            )  # DBG

        elif event == "feeder_write_fail":  # DBG
            return (  # DBG
                f"{t} *** FEEDER WRITE FAIL ph={data.get('phase','?')} "  # DBG
                f"after {data.get('z_descended_mm',0):.4f}mm — serial write failed\n"  # DBG
            )  # DBG

        elif event == "exit":  # DBG
            reason   = data.get("reason", "?")  # DBG
            desc = {  # DBG
                "bottom_limit_coarse":        f"BOTTOM LIMIT (coarse) — no surface found after {data.get('z_descended_mm',0):.3f}mm",  # DBG
                "bottom_limit_fine":          f"BOTTOM LIMIT (fine)   — no surface found after {data.get('z_descended_mm',0):.3f}mm",  # DBG
                "threshold_triggered_coarse": f"SURFACE FOUND (coarse) at {data.get('z_descended_mm',0):.3f}mm  v={data.get('crossing_v')}",  # DBG
                "threshold_triggered_fine":   f"SURFACE FOUND (fine)   at {data.get('z_descended_mm',0):.3f}mm  v={data.get('crossing_v')}",  # DBG
                "feeder_died_coarse":         f"FEEDER DIED (coarse) after {data.get('z_descended_mm',0):.3f}mm",  # DBG
                "feeder_died_fine":           f"FEEDER DIED (fine)   after {data.get('z_descended_mm',0):.3f}mm",  # DBG
                "no_serial":                  "SIMULATION MODE — Duet not connected, motion skipped",  # DBG
            }.get(reason, f"UNKNOWN EXIT: {reason}")  # DBG
            return (  # DBG
                f"\n{'-'*65}\n"  # DBG
                f"{t} EXIT: {desc}\n"  # DBG
                f"         steps={sum(self._steps.values())}  daq={sum(self._daqs.values())}\n"  # DBG
                f"{'-'*65}\n\n"  # DBG
            )  # DBG

        return ""  # DBG
# =============================================================================
# END DBG — SoftTouchDebugger
# =============================================================================

def _to_bool_any(v):
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    return s in ("true", "1", "yes", "y", "on", "t")


def _load_grid_plan_csv_lenient(csv_path):
    """
    Fallback parser for plans that use True/False instead of TRUE/FALSE.
    Also supports a plain table whose first real header row starts with 'Test'.
    """
    import csv

    try:
        from Sensor_Testor.domain.models import TestStep
    except Exception:
        try:
            from domain.models import TestStep
        except Exception:
            from models import TestStep

    with open(csv_path, "r", encoding="utf-8", errors="replace", newline="") as f:
        rows = [r for r in csv.reader(f) if r and any(str(c).strip() for c in r)]

    if not rows:
        return [], {}

    header_idx = 0
    for i, row in enumerate(rows):
        if str(row[0]).strip().lower() == "test":
            header_idx = i
            break

    settings = {}
    for row in rows[:header_idx]:
        if len(row) < 2:
            continue
        key = str(row[0] or "").strip()
        val = str(row[1] or "").strip()
        if not key:
            continue

        if val.lower() in ("true", "false", "yes", "no", "on", "off", "1", "0", "y", "n", "t", "f"):
            settings[key] = _to_bool_any(val)
        else:
            try:
                settings[key] = float(val) if ("." in val or "e" in val.lower()) else int(val)
            except Exception:
                settings[key] = val

    header = [str(c or "").strip() for c in rows[header_idx]]
    hlow = [h.lower() for h in header]

    def col(*names):
        for name in names:
            if name.lower() in hlow:
                return hlow.index(name.lower())
        return -1

    i_test = col("test")
    i_x = col("x position", "x")
    i_y = col("y position", "y")
    i_force = col("force", "force target")
    i_v_test = col("speed of test")
    i_v_between = col("speed between test", "speed between")
    i_safe_h = col("safe height (mm)", "safe height")
    i_test_h = col("test height (mm)", "test height")
    i_golden = col("golden curve")
    i_pf = col("p/f criteria", "pf criteria", "pass fail criteria", "criteria")

    settings.setdefault("Force x Resistance", True)
    settings.setdefault("Force x Sample Number", True)
    settings.setdefault("Resistance x Sample", True)
    if i_pf >= 0:
        settings.setdefault("Pass Fail Criteria", True)

    def fnum(row, idx, default=0.0):
        if idx < 0:
            return default
        s = str(row[idx] or "").strip()
        if not s:
            return default
        try:
            return float(s)
        except Exception:
            return default

    steps = []
    for row in rows[header_idx + 1:]:
        if len(row) < len(header):
            row = row + [""] * (len(header) - len(row))

        test_id = str(row[i_test] if i_test >= 0 else "").strip()
        if not test_id:
            continue

        steps.append(TestStep(
            test_id=test_id,
            x=fnum(row, i_x, 0.0),
            y=fnum(row, i_y, 0.0),
            force_target=fnum(row, i_force, 0.0),
            v_test=(fnum(row, i_v_test, 0.0) or None),
            v_travel=(fnum(row, i_v_between, 0.0) or None),
            safe_z=(fnum(row, i_safe_h, 0.0) or None),
            test_z=(fnum(row, i_test_h, 0.0) or None),
            golden_curve=(str(row[i_golden]).strip() if i_golden >= 0 and str(row[i_golden]).strip() else None),
            criteria_file=(str(row[i_pf]).strip() if i_pf >= 0 and str(row[i_pf]).strip() else None),
        ))

    return steps, settings


class OperatorModePopup(QDialog):
    # Thread-safe logging signals. ANY thread may emit these; Qt delivers them
    # to the GUI thread (auto/queued connection) where the slot touches widgets.
    # This is the ONLY safe way to update a QTextEdit from a worker thread.
    _sig_log_terminal = pyqtSignal(str)
    _sig_log_debug = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Operator Mode")
        self.setGeometry(200, 200, 1200, 800)

        # state
        self.should_stop = False
        self.is_paused = False
        self.folder_path = None
        self.job_details = None
        self.selected_file_path = None
        self.criteria_filename_hint = None

        # thread/run objects
        self._busy = False
        self._closing = False
        self._shutting_down = False   # blocks UI updates after shutdown begins
        self._debug_tab_shown = False # flips True once debug output arrives
        self._thread: QThread | None = None
        self._worker = None
        self._runner = None
        self._tr_debugger = None      # TestRunDebugger — attached in start_test()

        # devices
        self.duet = DuetAdapter()
        self.smac = SmacAdapter()

        # SoftTouchDebugger (stdout path) disabled — in-app TestRunDebugger
        # handles all run debug output. The class is kept for soft_touch_debug_hook
        # inside DuetAdapter but we no longer print to the launching terminal.
        self._st_debugger = None

        self._daq_ok = False
        self.daq = None
        try:
            self.daq = DaqAdapter(channels=(0, 2), rate_hz=1000.0)
            self.daq.open()
            self._daq_ok = True
        except Exception as e:
            print(f"[DAQ] open error: {e}")

        # ── live plot ring buffers (numpy for zero-copy setData) ──────────────
        _MAX_LIVE = 5000
        self._live_buf_f    = np.empty(_MAX_LIVE, dtype=np.float32)
        self._live_buf_r    = np.empty(_MAX_LIVE, dtype=np.float32)
        self._live_buf_i    = np.empty(_MAX_LIVE, dtype=np.int32)
        self._live_buf_head = 0     # next write index (ring)
        self._live_buf_n    = 0     # valid entries so far

        # back-compat deques — kept so any code reading self.live_force etc. works
        self.live_force      = deque(maxlen=_MAX_LIVE)
        self.live_resistance = deque(maxlen=_MAX_LIVE)
        self.sample_indices  = deque(maxlen=_MAX_LIVE)

        # Connect thread-safe logging signals to their GUI-thread slots.
        # Qt auto-detects the cross-thread case and queues the call.
        try:
            self._sig_log_terminal.connect(self._append_terminal)
            self._sig_log_debug.connect(self._append_debug)
        except Exception:
            pass

        # Pending (f, r, idx) tuples from sample_ready signal.
        # _plot_timer drains them at 30 Hz — one setData call per plot per frame.
        self._pending_samples: list = []
        self._plot_timer = QTimer(self)
        self._plot_timer.setInterval(33)          # ~30 Hz
        self._plot_timer.timeout.connect(self._flush_pending_samples)
        self._plot_timer.start()

        self._table_cache = []

        # graph flags
        self.pass_fail_criteria = False
        self.graph_force_x_resistance = False
        self.graph_force_x_sample_number = False
        self.graph_resistance_x_sample_number = False
        self.scaling_buttons_added = False
        self.save_raw_data = False
        self.save_filtered_data = False
        self.save_data_on_same_sheet = False
        self.save_failed_data = False
        self.activation_force_criterion = [0]
        self.test_results = []

        # graph handles
        self.force_resistance_plot = None
        self.force_resistance_curve = None
        self.force_sample_number_plot = None
        self.force_sample_number_curve = None
        self.resistance_sample_number_plot = None
        self.resistance_sample_number_curve = None
        self.combined_sample_number_plot = None
        self.force_curve = None
        self.resistance_curve = None
        self.right_axis = None
        self.straight_line_curve = None
        self.max_criteria_curve = None
        self.min_criteria_curve = None

        self._criteria_x = None
        self._criteria_y_max = None
        self._criteria_y_min = None

        # --- Layout ---
        main_layout = QHBoxLayout(self)
        left_layout = QVBoxLayout()
        right_layout = QVBoxLayout()
        main_layout.addLayout(left_layout, 2)
        main_layout.addLayout(right_layout, 3)

        left_layout.addWidget(QLabel("Selected Plan:"))
        self.file_name_display = QLineEdit()
        self.file_name_display.setReadOnly(True)
        self.file_name_display.setPlaceholderText("No file selected")
        left_layout.addWidget(self.file_name_display)

        self.choose_test_button = QPushButton("Choose Test")
        self.choose_test_button.clicked.connect(self.choose_test)
        left_layout.addWidget(self.choose_test_button)

        self.start_test_button = QPushButton("Start Test")
        self.start_test_button.clicked.connect(self.start_test)
        self.start_test_button.setEnabled(False)
        left_layout.addWidget(self.start_test_button)

        self.pause_test_button = QPushButton("Pause Test")
        self.pause_test_button.clicked.connect(self.pause_test)
        self.pause_test_button.setEnabled(False)
        left_layout.addWidget(self.pause_test_button)

        self.stop_test_button = QPushButton("Stop Test")
        self.stop_test_button.clicked.connect(self.stop_test)
        self.stop_test_button.setEnabled(False)
        left_layout.addWidget(self.stop_test_button)

        self.home_button = QPushButton("Home")
        self.home_button.clicked.connect(self.move_home)
        left_layout.addWidget(self.home_button)

        self.soft_touch_button = QPushButton("Soft Touch (filtered thr=0.05)")
        self.soft_touch_button.clicked.connect(self.run_soft_touch)
        left_layout.addWidget(self.soft_touch_button)

        # ── Tabbed log pane: "Operator" (normal) + "Debug" (rich trace) ──
        self._log_tabs = QTabWidget()
        self._log_tabs.setTabPosition(QTabWidget.South)

        # -- Operator tab --
        _op_w = QWidget()
        _op_lay = QVBoxLayout(_op_w)
        _op_lay.setContentsMargins(0, 0, 0, 0)
        _op_lay.setSpacing(2)
        _op_hdr = QHBoxLayout()
        _op_hdr.addWidget(QLabel("Operator log"))
        _op_hdr.addStretch()
        _op_copy = QPushButton("Copy"); _op_copy.setFixedWidth(52)
        _op_clr  = QPushButton("Clear"); _op_clr.setFixedWidth(52)
        _op_hdr.addWidget(_op_copy); _op_hdr.addWidget(_op_clr)
        _op_lay.addLayout(_op_hdr)

        self.terminal = QPlainTextEdit()
        self.terminal.setReadOnly(True)
        self.terminal.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.terminal.setFont(QFont("Monospace", 9))
        self.terminal.setMaximumBlockCount(5000)
        _op_lay.addWidget(self.terminal, 1)
        _op_copy.clicked.connect(self._copy_terminal)
        _op_clr.clicked.connect(self.terminal.clear)
        self._log_tabs.addTab(_op_w, "Operator")

        # -- Debug tab --
        _dbg_w = QWidget()
        _dbg_lay = QVBoxLayout(_dbg_w)
        _dbg_lay.setContentsMargins(0, 0, 0, 0)
        _dbg_lay.setSpacing(2)
        _dbg_hdr = QHBoxLayout()
        _dbg_hdr.addWidget(QLabel("Debug trace"))
        _dbg_hdr.addStretch()
        _dbg_copy = QPushButton("Copy"); _dbg_copy.setFixedWidth(52)
        _dbg_clr  = QPushButton("Clear"); _dbg_clr.setFixedWidth(52)
        _dbg_hdr.addWidget(_dbg_copy); _dbg_hdr.addWidget(_dbg_clr)
        _dbg_lay.addLayout(_dbg_hdr)

        self.debug_terminal = QPlainTextEdit()
        self.debug_terminal.setReadOnly(True)
        self.debug_terminal.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.debug_terminal.setFont(QFont("Monospace", 9))
        self.debug_terminal.setStyleSheet(
            "background:#0a0a0a; color:#d8d8d8;"
        )
        self.debug_terminal.setMaximumBlockCount(10000)
        _dbg_lay.addWidget(self.debug_terminal, 1)
        _dbg_copy.clicked.connect(self._copy_debug)
        _dbg_clr.clicked.connect(self.debug_terminal.clear)
        self._log_tabs.addTab(_dbg_w, "Debug ●")

        left_layout.addWidget(self._log_tabs, 1)

        right_layout.addWidget(QLabel("Force × Resistance"))
        self.graph_widget_top = pg.GraphicsLayoutWidget(show=True)
        self.graph_widget_top.setBackground("k")
        right_layout.addWidget(self.graph_widget_top, 1)

        right_layout.addWidget(QLabel("Sample Number Graph"))
        self.graph_widget_bottom = pg.GraphicsLayoutWidget(show=True)
        self.graph_widget_bottom.setBackground("k")
        right_layout.addWidget(self.graph_widget_bottom, 1)

        if not self._daq_ok:
            self.soft_touch_button.setEnabled(False)
            try:
                self._log("[SoftTouch] Disabled: no DAQ detected.")
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Logging / lifecycle
    # ------------------------------------------------------------------
    def _copy_terminal(self):
        self.terminal.selectAll()
        self.terminal.copy()

    def _copy_debug(self):
        self.debug_terminal.selectAll()
        self.debug_terminal.copy()

    def _append_terminal(self, text: str) -> None:
        """Main-thread only. Appends one line to the operator QPlainTextEdit."""
        if self._shutting_down:
            return
        try:
            self.terminal.appendPlainText(text)
        except Exception:
            pass

    def _append_debug(self, text: str) -> None:
        """Main-thread only. Appends one line to the debug QPlainTextEdit."""
        try:
            self.debug_terminal.appendPlainText(text)
            # Auto-switch to Debug tab when content first arrives
            if getattr(self, "_debug_tab_shown", False) is False:
                self._debug_tab_shown = True
                self._log_tabs.setCurrentIndex(1)
        except Exception:
            pass

    def _log(self, msg: str):
        # Thread-safe: emit a signal that Qt delivers to the GUI thread.
        # Safe to call from the worker thread. Also echo to stdout (uxterm).
        text = str(msg)
        try:
            print(text, flush=True)
        except Exception:
            pass
        if self._shutting_down:
            return
        try:
            self._sig_log_terminal.emit(text)
        except Exception:
            pass

    def _log_debug(self, msg: str) -> None:
        """Route a debug line to the Debug tab. Thread-safe via signal.
        No _shutting_down guard — debug output must survive shutdown too."""
        text = str(msg)
        try:
            print(text, flush=True)
        except Exception:
            pass
        try:
            self._sig_log_debug.emit(text)
        except Exception:
            pass

    def _cleanup_run_objects(self):
        self._runner = None
        self._worker = None
        self._thread = None

    def closeEvent(self, event):
        if self._busy and self._thread is not None and self._thread.isRunning():
            self._log("[Close] Test running — ignoring close request. Stop the test first.")
            event.ignore()
            # NOTE: do NOT connect thread.finished → self.close here.
            # That connection is permanent and fires even on normal run completion,
            # causing the window to close silently after every test.
            return

        try:
            self.set_run_controls(False)
        except Exception:
            pass

        try:
            if self.daq is not None:
                self.daq.close()
        except Exception:
            pass

        try:
            self.duet.close()
        except Exception:
            pass

        try:
            self.smac.close()
        except Exception:
            pass

        event.accept()

    # ------------------------------------------------------------------
    # UI state
    # ------------------------------------------------------------------
    def set_run_controls(self, running: bool):
        self.home_button.setEnabled((not running) and not self._busy)
        self.choose_test_button.setEnabled((not running) and not self._busy)
        self.start_test_button.setEnabled((not running) and bool(self.selected_file_path) and not self._busy)
        self.pause_test_button.setEnabled(running)
        self.stop_test_button.setEnabled(running)
        self.soft_touch_button.setEnabled((not running) and self._daq_ok and not self._busy)

    @pyqtSlot()
    def _on_home_finished(self):
        """Slot called on the main thread after home / soft-touch threads finish."""
        self.set_run_controls(False)

    def _reset_visual_state(self):
        try:
            self.terminal.clear()
        except Exception:
            pass

        if hasattr(self, "csv_lines"):
            del self.csv_lines

        try:
            self.graph_widget_top.clear()
            self.graph_widget_bottom.clear()
        except Exception:
            pass

        for attr in (
            "force_resistance_plot", "force_resistance_curve",
            "force_sample_number_plot", "force_sample_number_curve",
            "resistance_sample_number_plot", "resistance_sample_number_curve",
            "combined_sample_number_plot", "force_curve", "resistance_curve",
            "straight_line_curve", "max_criteria_curve", "min_criteria_curve",
            "right_axis"
        ):
            setattr(self, attr, None)

        self.live_force.clear()
        self.live_resistance.clear()
        self.sample_indices.clear()
        self._live_buf_head = 0
        self._live_buf_n    = 0
        self._pending_samples = []

    # ------------------------------------------------------------------
    # Buttons
    # ------------------------------------------------------------------
    def stop_test(self):
        self.should_stop = True
        self._log("⏹️ Stop requested.")
        try:
            if self._runner is not None:
                self._runner.request_stop()
        except Exception:
            pass

    def pause_test(self):
        self.is_paused = True
        self._log("⏸️ Paused.")

    def resume_test(self):
        self.is_paused = False
        self._log("▶️ Resumed.")

    def choose_test(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Select Test File", "", "CSV Files (*.csv);;All Files (*)"
        )
        if not file_path:
            return

        self.selected_file_path = file_path
        self.file_name_display.setText(os.path.basename(file_path))
        self.criteria_filename_hint = None

        try:
            self.terminal.clear()
        except Exception:
            pass

        try:
            rows = read_rows(file_path)
            table, plugin = extract_table_and_plugin(rows)
            self._render_table_only(table)
            self._table_cache = table
        except Exception as e:
            self._log(f"[Plan] Error reading table: {e}")
            self._table_cache = []

        try:
            steps, settings = load_grid_plan_csv(file_path)
            store.set_plan(file_path, steps, settings)
            self._log(f"[Plan] Strict parse OK: {len(steps)} step(s), settings={list((settings or {}).keys())}")
        except Exception as e:
            self._log(f"[Plan] Strict parse failed: {e}")
            try:
                steps, settings = _load_grid_plan_csv_lenient(file_path)
                store.set_plan(file_path, steps, settings)
                self._log(f"[Plan] Lenient parse OK: {len(steps)} step(s), settings={list((settings or {}).keys())}")
            except Exception as e2:
                self._log(f"[Plan] Lenient parse failed: {e2}")
                store.set_plan(file_path, [], {})

        try:
            self.read_settings(file_path)
        except Exception as e:
            self._log(f"[Settings] Error: {e}")

        try:
            table = getattr(self, "_table_cache", None)
            if table and len(table) > 1:
                header = [str(h or "").strip().lower() for h in table[0]]
                cand_names = ["p/f criteria", "pf criteria", "pass fail criteria", "criteria"]

                idx = -1
                for name in cand_names:
                    if name in header:
                        idx = header.index(name)
                        break

                if idx >= 0:
                    for r in table[1:]:
                        if idx < len(r):
                            val = str(r[idx] or "").strip()
                            if val:
                                self.criteria_filename_hint = os.path.basename(val)
                                self._log(f"[Criteria] Using file from first test row: {self.criteria_filename_hint}")
                                break

            if self.criteria_filename_hint:
                self.pass_fail_criteria = True
        except Exception as e:
            self._log(f"[Criteria] Couldn't read filename after header: {e}")

        try:
            fixed_dir = os.path.join(os.path.dirname(self.selected_file_path), "pass_fail_criteria_files")
            self._log(f"[Criteria] Looking in: {fixed_dir}")
            self.initialize_plots(file_path)
        except Exception as e:
            self._log(f"[Plots] Init error: {e}")

        self.start_test_button.setEnabled(True)

        try:
            with open("file_path.txt", "w") as f:
                f.write(self.selected_file_path)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Worker signal handlers
    # ------------------------------------------------------------------
    def on_step_started(self, idx: int):
        self._log(f"[Plot] Step {idx} started — clearing plot.")
        self.live_force.clear()
        self.live_resistance.clear()
        self.sample_indices.clear()
        self._last_plot_n = 0   # reset skip-unchanged guard

        try:
            if self.force_resistance_curve is not None:
                self.force_resistance_curve.setData([], [])
        except Exception:
            pass

        try:
            if self.force_sample_number_curve is not None:
                self.force_sample_number_curve.setData([], [])
        except Exception:
            pass

        try:
            if self.resistance_sample_number_curve is not None:
                self.resistance_sample_number_curve.setData([], [])
        except Exception:
            pass

    def on_criteria_ready(self, payload: object):
        try:
            if not isinstance(payload, dict):
                return
            if self.force_resistance_plot is None:
                return

            x = payload.get("x")
            y_max = payload.get("y_max")
            y_min = payload.get("y_min")
            name = payload.get("name") or "criteria"

            self._criteria_x = x
            self._criteria_y_max = y_max
            self._criteria_y_min = y_min

            pen_blue = pg.mkPen("b", width=2)
            pen_green = pg.mkPen("g", width=2)

            if x is not None and y_max is not None:
                if self.max_criteria_curve is None:
                    self.max_criteria_curve = self.force_resistance_plot.plot([], [], pen=pen_blue)
                else:
                    self.max_criteria_curve.setPen(pen_blue)
                self.max_criteria_curve.setData(x, y_max)

            if x is not None and y_min is not None:
                if self.min_criteria_curve is None:
                    self.min_criteria_curve = self.force_resistance_plot.plot([], [], pen=pen_green)
                else:
                    self.min_criteria_curve.setPen(pen_green)
                self.min_criteria_curve.setData(x, y_min)

            self._log(f"[Criteria] Overlay updated: {name}")
        except Exception as e:
            self._log(f"[Criteria] Overlay update error: {e}")

    def on_sample_ready(self, force: float, resistance: float, idx: int):
        """Kept for back-compat signal connection — no longer used for plotting.
        Plotting now goes through _plot_timer → ring_snapshot() directly."""
        pass

    def _flush_pending_samples(self) -> None:
        """30 Hz GUI timer — pulls snapshot from worker numpy buffer, calls setData.
        No pending queue, no signal per chunk. Same pattern as DAQ oscilloscope."""
        if self._shutting_down:
            return

        worker = self._worker
        if worker is None or not hasattr(worker, "ring_snapshot"):
            return

        try:
            fs, rs, raw_f, raw_r = worker.ring_snapshot()
        except Exception:
            return

        n = len(fs)
        if n < 2:
            return

        # Only update if new samples arrived since last frame
        last_n = getattr(self, "_last_plot_n", 0)
        if n == last_n:
            return
        self._last_plot_n = n

        # float32 is fine — pyqtgraph renders it natively, no copy needed
        xs = np.arange(n, dtype=np.float32)

        # ── Force × Resistance (top) ─────────────────────────────────────────
        if self.force_resistance_curve is not None:
            try:
                self.force_resistance_curve.setData(x=fs, y=rs)
            except Exception:
                pass

        # ── Sample number (bottom) ────────────────────────────────────────────
        if self.force_sample_number_curve is not None:
            try:
                self.force_sample_number_curve.setData(x=xs, y=fs)
            except Exception:
                pass
        if self.resistance_sample_number_curve is not None:
            try:
                self.resistance_sample_number_curve.setData(x=xs, y=rs)
            except Exception:
                pass

    def _on_sample_main(self, f: float, r: float, idx: int) -> None:
        """Legacy shim — no longer used."""
        pass

    # ------------------------------------------------------------------
    # Plan / settings helpers
    # ------------------------------------------------------------------
    def _render_table_only(self, table_rows):
        if not table_rows:
            self._log("[Plan] No table rows found.")
            return
        lines = [",".join([str(c or "") for c in row]) for row in table_rows]
        self.terminal.clear()
        self.terminal.setPlainText("\n".join(lines))
        self.csv_lines = lines

    def read_settings(self, file_path: str):
        try:
            S = store.settings or {}
        except Exception:
            S = {}

        def _to_bool(k: str) -> bool:
            return _to_bool_any(S.get(k, False))

        self.pass_fail_criteria = _to_bool("Pass Fail Criteria")
        self.graph_force_x_resistance = _to_bool("Force x Resistance")
        self.graph_force_x_sample_number = _to_bool("Force x Sample Number")
        self.graph_resistance_x_sample_number = _to_bool("Resistance x Sample")

        self.save_raw_data = _to_bool("Raw Data")
        self.save_filtered_data = _to_bool("Filtered Data")
        self.save_data_on_same_sheet = _to_bool("Data on Same Sheet")
        self.save_failed_data = _to_bool("Save Failed Data")

        self._log(
            "[Settings] "
            f"PassFail={self.pass_fail_criteria} "
            f"FxR={self.graph_force_x_resistance} "
            f"FxS={self.graph_force_x_sample_number} "
            f"RxS={self.graph_resistance_x_sample_number} "
            f"raw={self.save_raw_data} filtered={self.save_filtered_data}"
        )

    # ------------------------------------------------------------------
    # Plot initialization
    # ------------------------------------------------------------------
    def initialize_plots(self, _file_path: str):
        flags = {}
        try:
            if store.run_config and isinstance(store.run_config.flags, dict):
                flags = store.run_config.flags or {}
            else:
                flags = store.settings or {}
        except Exception:
            flags = {}

        def _f(name: str, default: bool = False) -> bool:
            return _to_bool_any(flags.get(name, default))

        self.graph_force_x_resistance = _f("Force x Resistance", True)
        self.graph_force_x_sample_number = _f("Force x Sample Number", True)
        self.graph_resistance_x_sample_number = _f("Resistance x Sample", True)
        self.pass_fail_criteria = _f("Pass Fail Criteria", False) or bool(self.criteria_filename_hint)

        self._log(
            "[Plots] init "
            f"FxR={self.graph_force_x_resistance} "
            f"FxS={self.graph_force_x_sample_number} "
            f"RxS={self.graph_resistance_x_sample_number} "
            f"PassFail={self.pass_fail_criteria} "
            f"criteria_hint={self.criteria_filename_hint!r}"
        )

        try:
            self.graph_widget_top.clear()
            self.graph_widget_bottom.clear()
        except Exception as e:
            self._log(f"[Plots] clear warning: {e}")

        if self.graph_force_x_resistance:
            self.init_force_resistance_plot()
            self._log(f"[Plots] force_resistance_plot created={self.force_resistance_plot is not None}")
        else:
            self.force_resistance_plot = None
            self.force_resistance_curve = None
            self.straight_line_curve = None
            self.max_criteria_curve = None
            self.min_criteria_curve = None

        bottom_plots = []
        if self.graph_force_x_sample_number:
            bottom_plots.append("force")
        if self.graph_resistance_x_sample_number:
            bottom_plots.append("res")

        if len(bottom_plots) == 2:
            self.init_combined_sample_number_plot()
            self._log("[Plots] bottom mode=both")
        elif bottom_plots == ["force"]:
            self.init_force_sample_number_plot()
            self._log("[Plots] bottom mode=force_only")
        elif bottom_plots == ["res"]:
            self.init_resistance_sample_number_plot()
            self._log("[Plots] bottom mode=res_only")
        else:
            self.force_sample_number_plot = None
            self.resistance_sample_number_plot = None
            self.force_sample_number_curve = None
            self.resistance_sample_number_curve = None
            self._log("[Plots] bottom mode=none")

        self.plot_criteria_overlays()

    def init_force_resistance_plot(self):
        self.force_resistance_plot = self.graph_widget_top.addPlot(row=0, col=0)
        p = self.force_resistance_plot
        p.setLabel("bottom", "Force (kg)")
        p.setLabel("left",   "Resistance (Ω)")
        p.showGrid(x=True, y=True, alpha=0.3)
        p.setDownsampling(auto=True, mode="peak")
        p.setClipToView(True)
        p.enableAutoRange(axis="xy")
        p.getViewBox().setMouseEnabled(x=True, y=True)

        self.force_resistance_curve = p.plot(
            [], [], pen=pg.mkPen(color=(255, 80, 80), width=1), symbol=None)
        self.max_criteria_curve = p.plot([], [], pen=pg.mkPen("b", width=2))
        self.min_criteria_curve = p.plot([], [], pen=pg.mkPen("g", width=2))

    def init_force_sample_number_plot(self):
        self.force_sample_number_plot = self.graph_widget_bottom.addPlot(row=0, col=0)
        p = self.force_sample_number_plot
        p.setLabel("left", "Force", units="kg")
        p.setLabel("bottom", "Sample #")
        p.showGrid(x=True, y=True, alpha=0.3)
        p.setDownsampling(mode="peak")
        p.setClipToView(True)
        self.force_sample_number_curve = p.plot([], [], pen=pg.mkPen(width=1))

        self.resistance_sample_number_plot = None
        self.resistance_sample_number_curve = None

    def init_resistance_sample_number_plot(self):
        self.resistance_sample_number_plot = self.graph_widget_bottom.addPlot(row=0, col=0)
        p = self.resistance_sample_number_plot

        p.setLabel("bottom", "Sample")
        p.setLabel("left", "Resistance", units="Ω")
        p.showGrid(x=True, y=True, alpha=0.3)
        p.setDownsampling(mode="peak")
        p.setClipToView(True)

        self.resistance_sample_number_curve = p.plot([], [], pen=pg.mkPen(width=1))
        self.force_sample_number_plot = None
        self.force_sample_number_curve = None

    def init_combined_sample_number_plot(self):
        self.force_sample_number_plot = self.graph_widget_bottom.addPlot(row=0, col=0)
        p = self.force_sample_number_plot

        p.setLabel("bottom", "Sample")
        p.setLabel("left",   "Force (kg)")
        p.showGrid(x=True, y=True, alpha=0.3)
        p.setDownsampling(auto=True, mode="peak")
        p.setClipToView(True)
        p.enableAutoRange(axis="xy")
        p.getViewBox().setMouseEnabled(x=True, y=True)

        self.force_sample_number_curve = p.plot(
            [], [], pen=pg.mkPen(color=(255, 255, 255), width=1))

        p_res = pg.ViewBox()
        p.showAxis("right")
        p.scene().addItem(p_res)
        p.getAxis("right").linkToView(p_res)
        p_res.setXLink(p)

        def update_views():
            p_res.setGeometry(p.vb.sceneBoundingRect())
            p_res.linkedViewChanged(p.vb, p_res.XAxis)

        p.vb.sigResized.connect(update_views)

        self.resistance_sample_number_plot = p_res
        self.resistance_sample_number_curve = pg.PlotCurveItem(pen=pg.mkPen("y", width=1))
        p_res.addItem(self.resistance_sample_number_curve)
        p.getAxis("right").setLabel("Resistance", units="Ω")

    def plot_criteria_overlays(self):
        try:
            self._log("[CriteriaDBG] enter plot_criteria_overlays")

            if self.force_resistance_plot is None:
                self._log("[CriteriaDBG] abort: force_resistance_plot is None")
                return

            if not self.selected_file_path:
                self._log("[CriteriaDBG] abort: selected_file_path missing")
                return

            crit_name = (self.criteria_filename_hint or "").strip()
            if not crit_name:
                self._log("[CriteriaDBG] abort: criteria_filename_hint empty")
                return

            plan_dir = os.path.dirname(self.selected_file_path)
            criteria_dir = os.path.join(plan_dir, "pass_fail_criteria_files")
            name_only = os.path.basename(crit_name)

            candidates = []
            if os.path.isabs(crit_name):
                candidates.append(crit_name)

            candidates.append(os.path.join(criteria_dir, name_only))
            if not name_only.lower().endswith(".csv"):
                candidates.append(os.path.join(criteria_dir, name_only + ".csv"))

            candidates.append(os.path.join(plan_dir, name_only))
            if not name_only.lower().endswith(".csv"):
                candidates.append(os.path.join(plan_dir, name_only + ".csv"))

            crit_path = None
            for cand in candidates:
                self._log(f"[CriteriaDBG] candidate={cand} exists={os.path.isfile(cand)}")
                if crit_path is None and os.path.isfile(cand):
                    crit_path = cand

            if crit_path is None and os.path.isdir(criteria_dir):
                target_stem = os.path.splitext(name_only)[0].lower()
                for fname in sorted(os.listdir(criteria_dir)):
                    fstem, fext = os.path.splitext(fname)
                    if fext.lower() == ".csv" and fstem.lower() == target_stem:
                        crit_path = os.path.join(criteria_dir, fname)
                        self._log(f"[CriteriaDBG] case-insensitive match -> {crit_path}")
                        break

            if crit_path is None:
                self._log("[CriteriaDBG] abort: criteria file not found")
                return

            self._log(f"[CriteriaDBG] resolved_path={crit_path}")

            # Use the cached criteria parser from criteria_loader so the file
            # is only read and parsed once per mtime change, not on every plan load.
            max_points, min_points = parse_pass_fail_criteria_form(crit_path)
            self._log(f"[CriteriaDBG] max_points={max_points[:5]} total={len(max_points)}")
            self._log(f"[CriteriaDBG] min_points={min_points[:5]} total={len(min_points)}")

            if not max_points and not min_points:
                self._log("[CriteriaDBG] abort: no usable points")
                return

            def _prep(pts):
                if not pts:
                    return [], []
                xs, ys = zip(*sorted(pts, key=lambda p: p[0]))
                return generate_smoothed_line(xs, ys, num_points=200)

            max_x_plot, max_y_plot = _prep(max_points)
            min_x_plot, min_y_plot = _prep(min_points)

            pen_blue = pg.mkPen("b", width=2)
            pen_green = pg.mkPen("g", width=2)

            try:
                if self.max_criteria_curve is None:
                    self.max_criteria_curve = self.force_resistance_plot.plot([], [], pen=pen_blue)
                else:
                    self.max_criteria_curve.setPen(pen_blue)
                self.max_criteria_curve.setData(max_x_plot, max_y_plot)
            except Exception as e:
                self._log(f"[CriteriaDBG] max curve error: {e}")

            try:
                if self.min_criteria_curve is None:
                    self.min_criteria_curve = self.force_resistance_plot.plot([], [], pen=pen_green)
                else:
                    self.min_criteria_curve.setPen(pen_green)
                self.min_criteria_curve.setData(min_x_plot, min_y_plot)
            except Exception as e:
                self._log(f"[CriteriaDBG] min curve error: {e}")

            try:
                self.force_resistance_plot.enableAutoRange()
                self.force_resistance_plot.autoRange()
            except Exception as e:
                self._log(f"[CriteriaDBG] autorange error: {e}")

            self._log(f"[Criteria] Loaded and plotted: {os.path.basename(crit_path)}")
        except Exception as e:
            self._log(f"[CriteriaDBG] fatal error: {e}")
            self._log(f"[Criteria] Error: {e}")

    # ------------------------------------------------------------------
    # Motion helpers
    # ------------------------------------------------------------------
    def move_home(self):
        if self._busy:
            self._log("[Home] Busy; ignoring request.")
            return
        self._busy = True
        self.set_run_controls(True)
        self._log("[Home] Starting homing sequence...")

        # home.py calls terminal.append(msg) directly, so we give it a
        # lightweight wrapper that routes through our thread-safe _log()
        # instead of touching the QTextEdit from the background thread.
        class _SafeTerminal:
            def __init__(self, log_fn):
                self._log = log_fn
            def append(self, msg):
                self._log(str(msg))

        _safe_terminal = _SafeTerminal(self._log)

        def _run():
            try:
                run_home_sequence(self.duet, self.smac, terminal=_safe_terminal, safe_z=55.0)
                self._log("[Home] Done.")
            except Exception as e:
                self._log(f"[Home] Aborted: {e}")
            finally:
                self._busy = False
                # set_run_controls touches Qt widgets — must be called on the
                # main thread.  QMetaObject.invokeMethod queues it safely.
                QMetaObject.invokeMethod(
                    self, "_on_home_finished", Qt.QueuedConnection
                )

        import threading
        threading.Thread(target=_run, daemon=True).start()

    def run_soft_touch(self):
        if self._busy:
            self._log("[SoftTouch] Busy; ignoring request.")
            return
        if not self._daq_ok or self.daq is None:
            self._log("[SoftTouch] No DAQ available.")
            return

        self._busy = True
        self.set_run_controls(True)

        # Run in a background thread so the UI stays live throughout the
        # full soft-touch sequence (baseline scan + XY move + descent).
        import threading

        def _run():
            try:
                result = self.duet.soft_touch(
                    daq=self.daq,
                    logger=self._log,
                    pre_x=70.0,
                    pre_y=60.0,
                    threshold=0.15,  # 0.15V delta above rest; avoids noise spikes near 0.10V
                    approach_feedrate=60.0,
                    z_bottom_limit=60.0,
                    stream_dt=0.05,
                    notch_Q=30.0,
                )
                if result is None:
                    self._log("[SoftTouch] No result (surface not found or hardware unavailable).")
            except Exception as e:
                self._log(f"[SoftTouch] Error: {e}")
            finally:
                self._busy = False
                # set_run_controls touches Qt widgets — must run on main thread.
                QMetaObject.invokeMethod(
                    self, "_on_home_finished", Qt.QueuedConnection
                )

        threading.Thread(target=_run, daemon=True).start()

    # ------------------------------------------------------------------
    # Fail dialog
    # ------------------------------------------------------------------
    def ask_fail_action(self, step_index: int, reason: str) -> str:
        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Critical)
        msg.setWindowTitle("Test FAILED")
        msg.setText(f"Step {step_index} failed.\n\n{reason or 'Live data failed pass/fail rules.'}")

        continue_btn = msg.addButton("Continue", QMessageBox.AcceptRole)
        stop_btn = msg.addButton("Stop Run", QMessageBox.DestructiveRole)
        redo_btn = msg.addButton("Redo Test", QMessageBox.ActionRole)

        msg.exec_()

        clicked = msg.clickedButton()
        if clicked is continue_btn:
            return "continue"
        if clicked is redo_btn:
            return "redo"
        if clicked is stop_btn:
            return "stop"
        return "stop"

    # ------------------------------------------------------------------
    # Run construction / launch
    # ------------------------------------------------------------------
    def start_test(self):
        if not self.selected_file_path:
            QMessageBox.warning(self, "No Plan", "Select a test file first.")
            return

        try:
            if not store.steps or not store.settings:
                steps, settings = load_grid_plan_csv(self.selected_file_path)
                store.set_plan(self.selected_file_path, steps, settings)
                self._log(f"[Start] Parsed plan: {len(steps)} step(s).")
        except Exception as e:
            QMessageBox.critical(self, "Plan Error", f"Could not parse plan:\n{e}")
            return

        try:
            self.job_details = get_job_details(self.selected_file_path) or {}
        except Exception:
            self.job_details = {}

        job_num_default = (self.job_details.get("Job Number") or "").strip()
        if job_num_default == "":
            job_num_default = os.path.splitext(os.path.basename(self.selected_file_path))[0]

        try:
            self.folder_path, base_name = save_prompt(self, default_folder_name=job_num_default)
        except Exception as e:
            QMessageBox.critical(self, "Save Location", f"Could not open save dialog:\n{e}")
            return

        if not self.folder_path:
            self._log("[Start] Cancelled: no output folder chosen.")
            return

        try:
            store.set_output_context(self.folder_path, base_name, self.job_details)
            self._log(f"[Start] Output -> {store.output_folder}  base='{store.output_base}'")
        except Exception:
            try:
                store.output_folder = self.folder_path
            except Exception:
                pass
            try:
                store.output_base = base_name
            except Exception:
                pass

        # Open the debug log NOW — before writers/handshake — so every
        # subsequent line (including writers import diagnostics) is captured
        # in the file, not just the terminal.
        try:
            from Sensor_Testor.debugger.debug_log import open_log as _open_log_early, set_ui_sink as _set_ui_sink_early
        except Exception:
            try:
                from debugger.debug_log import open_log as _open_log_early, set_ui_sink as _set_ui_sink_early
            except Exception:
                from debug_log import open_log as _open_log_early, set_ui_sink as _set_ui_sink_early
        try:
            _log_folder_early = getattr(store, "output_folder", None) or self.folder_path
            _lp_early = _open_log_early(_log_folder_early)
            _set_ui_sink_early(self._log_debug)
            self._log(f"[Start] Debug log opened early: {_lp_early}")
        except Exception as _e_early:
            self._log(f"[Start] Could not open debug log early: {_e_early}")
            try:
                store.job_details = dict(self.job_details or {})
            except Exception:
                pass

        def _bool(settings: dict, key: str) -> bool:
            v = settings.get(key, False)
            if isinstance(v, bool):
                return v
            try:
                return str(v).strip().lower() in ("true", "1", "yes", "y", "on")
            except Exception:
                return False

        def _num(key, cast=float, default=None):
            S = store.settings or {}
            try:
                if key not in S or S[key] == "" or S[key] is None:
                    return default
                return cast(S[key])
            except Exception:
                return default

        S = store.settings or {}
        try:
            flags = {
                "Raw Data": _bool(S, "Raw Data"),
                "Filtered Data": _bool(S, "Filtered Data"),
                "Data on Same Sheet": _bool(S, "Data on Same Sheet"),
                "Save Failed Data": _bool(S, "Save Failed Data"),
                "Pass Fail Criteria": _bool(S, "Pass Fail Criteria"),
                "Force x Resistance": _bool(S, "Force x Resistance"),
                "Force x Sample Number": _bool(S, "Force x Sample Number"),
                "Resistance x Sample": _bool(S, "Resistance x Sample"),
                "Check Short Circuit": _bool(S, "Check Short Circuit"),
                "Check Open Circuit": _bool(S, "Check Open Circuit"),
                "Check if Preloaded": _bool(S, "Check if Preloaded"),
            }

            preload_thr = _num("Preload Resistance Threshold (Ω)", float, None)
            short_thr = _num("Short-Circuit Threshold (Ω)", float, None)

            cfg = RunConfig(
                x_pitch=_num("x pitch(mm)", float, 0.0),
                y_pitch=_num("y pitch(mm)", float, 0.0),
                n_x=_num("Number of Sensors in x", int, 0),
                n_y=_num("Number of Sensors in y", int, 0),
                v_test=_num("actuator speed(mm/s)", float, 1.0),
                v_travel=_num("speed between spaces(mm/s)", float, 50.0),
                start_x=_num("start position x", float, 0.0),
                start_y=_num("start position y", float, 0.0),
                safe_z=_num("Safe Height (mm)", float, 10.0),
                test_z=_num("Test Height (mm)", float, 0.0),
                start_force=_num("start force(kg)", float, 0.0),
                max_force=_num("max force(kg)", float, 0.0),
                flags=flags,
                preload_res_threshold_ohm=preload_thr,
                short_circuit_threshold_ohm=short_thr,
            )

            try:
                store.set_run_config(cfg)
            except Exception:
                store.run_config = cfg

            self._log("[Start] RunConfig built from plan settings.")
        except Exception as e:
            QMessageBox.critical(self, "Settings Error", f"Could not build RunConfig:\n{e}")
            return

        try:
            criteria = {
                "enabled": _bool(S, "Pass Fail Criteria"),
                "per_step": {
                    st.test_id: {
                        "criteria_file": getattr(st, "criteria_file", None),
                        "golden_curve": getattr(st, "golden_curve", None),
                        "force_target": getattr(st, "force_target", None),
                        "speed_between": getattr(st, "v_travel", None),
                    }
                    for st in store.steps
                },
                "max_force_kg": _num("max force(kg)", float, None),
                "start_force_kg": _num("start force(kg)", float, None),
            }
        except Exception as e:
            QMessageBox.critical(self, "Criteria Error", f"Could not build criteria:\n{e}")
            return

        writers = None
        _writers_err = None
        for _imp in ("Sensor_Testor.app_io.writers", "app_io.writers", "writers"):
            try:
                self._log(f"[Start] Trying writers import: '{_imp}' ...")
                _mod = __import__(_imp, fromlist=["make_writers"])
                make_writers = getattr(_mod, "make_writers")
                writers = make_writers(
                    out_dir=store.output_folder,
                    flags=cfg.flags,
                    base_name=store.output_base,
                    job_details=store.job_details,
                )
                _xlsx_path = getattr(writers, "xlsx_path", "?")
                self._log(f"[Start] Writers READY via '{_imp}'")
                self._log(f"[Start] xlsx will be saved to: {_xlsx_path}")
                break
            except Exception as _e:
                import traceback
                _writers_err = _e
                self._log(f"[Start]   '{_imp}' failed: {_e}")
                self._log(traceback.format_exc())
                continue

        if writers is None:
            import traceback
            self._log(f"[Start] ALL WRITERS IMPORTS FAILED: {_writers_err}")
            self._log(traceback.format_exc())
            class _NoopWriters:
                def __init__(self, root):
                    self.root = root
                def write_raw(self, *a, **k): pass
                def write_step_result(self, *a, **k): pass
                def write_filtered(self, *a, **k): pass
                def write_summary(self, *a, **k): pass
                def write_meta(self, *a, **k): pass
                def flush(self): pass
            writers = _NoopWriters(getattr(store, "output_folder", self.folder_path))
            self._log("[Start] ⚠ Using _NoopWriters — NO xlsx will be saved. FIX IMPORT ABOVE.")

        if self._busy:
            self._log("[Start] Busy; ignoring request.")
            return

        if TestRunnerWorker is None:
            QMessageBox.critical(self, "Worker Error", "TestRunnerWorker not available.")
            return

        try:
            try:
                worker = TestRunnerWorker(
                    store.run_config,
                    store.steps,
                    criteria,
                    self.duet,
                    self.smac,
                    self.daq,
                    writers,
                    terminal_logger=self._log,
                )
            except TypeError:
                worker = TestRunnerWorker(
                    store.run_config,
                    store.steps,
                    criteria,
                    self.duet,
                    self.smac,
                    self.daq,
                    writers,
                )
        except Exception as e:
            QMessageBox.critical(self, "Worker Error", f"Could not construct TestRunnerWorker:\n{e}")
            return

        try:
            self._worker = worker
            self._runner = GridRunner(cfg=store.run_config, steps=store.steps, worker=self._worker)
        except Exception as e:
            QMessageBox.critical(self, "Runner Error", f"Could not create GridRunner:\n{e}")
            return

        try:
            if hasattr(self._worker, "sample_ready"):
                self._worker.sample_ready.connect(self.on_sample_ready)
        except Exception:
            pass

        # Thread-safe log delivery: worker emits log_message on the worker
        # thread, Qt queues it to the GUI thread where _append_debug runs.
        try:
            if hasattr(self._worker, "log_message"):
                from PyQt5.QtCore import Qt as _Qt
                self._worker.log_message.connect(self._append_debug, _Qt.QueuedConnection)
        except Exception:
            pass

        try:
            if hasattr(self._worker, "criteria_ready"):
                self._worker.criteria_ready.connect(self.on_criteria_ready)
        except Exception:
            pass

        try:
            if hasattr(self._worker, "step_started"):
                self._worker.step_started.connect(self.on_step_started)
        except Exception:
            pass

        self._thread = QThread(self)

        try:
            self._worker.moveToThread(self._thread)
        except Exception:
            pass

        try:
            self._runner.moveToThread(self._thread)
        except Exception:
            pass

        def _on_progress(i, msg):
            self._log(f"[Run] {i}: {msg}")

        def _on_result(i, ok):
            self._log(f"[Run] Step {i} => {'PASS' if ok else 'FAIL'}")
            if ok:
                self._last_passed_count = getattr(self, "_last_passed_count", 0) + 1
            else:
                self._last_failed_count = getattr(self, "_last_failed_count", 0) + 1

        def _on_error(msg):
            self._log(f"[Run] ERROR: {msg}")

        def _on_run_summary(passed, failed, max_failed):
            if max_failed is None or int(max_failed) < 0:
                self._log(f"[Run] Passed={passed}, Failed={failed}")
            else:
                self._log(f"[Run] Passed={passed}, Failed={failed}, MaxFailed={max_failed}")

        def _on_step_decision_requested(i, reason):
            action = self.ask_fail_action(i, reason)
            try:
                self._runner.set_step_action(action)
            except Exception as e:
                self._log(f"[Run] Failed to send step action '{action}': {e}")
                try:
                    self._runner.set_step_action("stop")
                except Exception:
                    pass

        def _on_finished():
            # runner.finished fires on the QThread — DO NOT touch any Qt
            # widget or call self._log here (causes QTextBlock segfault).
            # Just ask the thread to quit; all UI work is in _on_thread_done.
            try:
                if self._thread is not None:
                    self._thread.quit()
            except Exception:
                pass

        @pyqtSlot()
        def _on_thread_done():
            # Runs on the main thread after QThread has fully exited.
            # Safe to touch all Qt objects here.
            self._shutting_down = False
            self._debug_tab_shown = False
            self._busy = False
            self.set_run_controls(False)
            self._cleanup_run_objects()
            self._log("[Run] Thread finished — ready for next run.")

            # Close the debug log file for this run.
            try:
                from Sensor_Testor.debugger.debug_log import close_log
            except Exception:
                try:
                    from debugger.debug_log import close_log
                except Exception:
                    try:
                        from debug_log import close_log
                    except Exception:
                        close_log = None
            if close_log is not None:
                try:
                    close_log()
                except Exception:
                    pass

            # Log summary to terminal — no popup, no close.
            try:
                passed = getattr(self, "_last_passed_count", "?")
                failed = getattr(self, "_last_failed_count", "?")
                self._log(f"[Run] ══ RUN COMPLETE — Passed: {passed}  Failed: {failed} ══")
            except Exception:
                pass

            # Only close the window if the user explicitly requested it
            # by clicking X while a test was running (sets _closing=True).
            # Normal test completion never closes the window.
            if self._closing:
                self._closing = False
                try:
                    self.close()
                except Exception:
                    pass

        self._runner.progress.connect(_on_progress)
        self._runner.result.connect(_on_result)
        self._runner.error.connect(_on_error)
        self._runner.finished.connect(_on_finished)
        self._thread.finished.connect(_on_thread_done)

        try:
            self._runner.run_summary.connect(_on_run_summary)
        except Exception:
            pass

        self._busy = True
        self._shutting_down = False
        self._last_passed_count = 0
        self._last_failed_count = 0
        self.set_run_controls(True)

        # Debug log already opened earlier (right after output folder was set),
        # so all writers/handshake diagnostics are captured. Nothing to do here.


        try:
            self._runner.step_decision_requested.connect(_on_step_decision_requested)
        except Exception:
            pass

        self._thread.started.connect(self._runner.run)

        self._log("[Start] Launching grid run...")
        self._thread.start()
