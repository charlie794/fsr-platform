# ui/operator_mode.py
from __future__ import annotations

import os
import threading
import numpy as np

from collections import deque
from PyQt5.QtCore import QThread, Qt, QMetaObject, pyqtSlot, QTimer, pyqtSignal
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QDialog, QHBoxLayout, QVBoxLayout, QWidget,
    QLineEdit, QPushButton, QFileDialog, QTextEdit,
    QPlainTextEdit, QMessageBox, QLabel, QFormLayout,
    QDialogButtonBox, QTabWidget, QSplitter,
)
import pyqtgraph as pg

# ---------------------------------------------------------------------------
# Imports — two-level fallback (package vs flat)
# ---------------------------------------------------------------------------
try:
    from Sensor_Testor.app_io.plan_loader import load_grid_plan_csv
except Exception:
    from app_io.plan_loader import load_grid_plan_csv   # type: ignore

try:
    from Sensor_Testor.domain.models import store, RunConfig
except Exception:
    from domain.models import store, RunConfig          # type: ignore

try:
    from Sensor_Testor.runner.grid_runner import GridRunner
except Exception:
    from runner.grid_runner import GridRunner           # type: ignore

try:
    from Sensor_Testor.runner.test_runner import TestRunnerWorker
except Exception:
    from runner.test_runner import TestRunnerWorker     # type: ignore

try:
    from Sensor_Testor.app_io.file_dialogs import save_prompt
except Exception:
    try:
        from app_io.file_dialogs import save_prompt     # type: ignore
    except Exception:
        def save_prompt(parent=None, default_folder_name=None):
            folder = os.path.join(os.path.expanduser("~"), "Documents",
                                  default_folder_name or "TestRun")
            os.makedirs(folder, exist_ok=True)
            base = "test"
            with open("file_path.txt", "w") as f:
                f.write(f"{folder}\n{base}\n")
            return folder, base

try:
    from Sensor_Testor.ui.job_details import get_job_details
except Exception:
    try:
        from ui.job_details import get_job_details      # type: ignore
    except Exception:
        class _JobDetailsDialog(QDialog):
            _FIELDS = [
                "Job Number", "Lot Number", "Customer", "Customer ID",
                "Internal P/N", "Internal Rev", "Customer P/N", "Customer Rev",
                "File", "Operator", "Comment",
                "Quantity of Good Parts", "Max Failed Parts",
            ]
            def __init__(self, parent=None):
                super().__init__(parent)
                self.setWindowTitle("Enter Job Details")
                lay = QVBoxLayout(self)
                form = QFormLayout()
                self.inputs = {}
                for f in self._FIELDS:
                    w = QLineEdit(self)
                    self.inputs[f] = w
                    form.addRow(QLabel(f + ":"), w)
                lay.addLayout(form)
                bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self)
                bb.accepted.connect(self.accept)
                bb.rejected.connect(self.reject)
                lay.addWidget(bb)
            def values(self):
                return {f: self.inputs[f].text() for f in self._FIELDS}

        def get_job_details(_csv=None):
            dlg = _JobDetailsDialog()
            return dlg.values() if dlg.exec_() == QDialog.Accepted else \
                   {f: "" for f in _JobDetailsDialog._FIELDS}

try:
    from Sensor_Testor.processing.criteria_loader import (
        parse_pass_fail_criteria_form, generate_smoothed_line,
    )
except Exception:
    try:
        from processing.criteria_loader import (         # type: ignore
            parse_pass_fail_criteria_form, generate_smoothed_line,
        )
    except Exception:
        def parse_pass_fail_criteria_form(path): return None
        def generate_smoothed_line(x, y, *a, **k): return x, y

try:
    from Sensor_Testor.hardware.duet_adapter import DuetAdapter
    from Sensor_Testor.hardware.smac_adapter import SmacAdapter
    from Sensor_Testor.hardware.daq_adapter  import DaqAdapter
except Exception:
    from hardware.duet_adapter import DuetAdapter       # type: ignore
    from hardware.smac_adapter import SmacAdapter       # type: ignore
    from hardware.daq_adapter  import DaqAdapter        # type: ignore

try:
    from Sensor_Testor.app_io.plan_reader import read_rows, extract_table_and_plugin
except Exception:
    from app_io.plan_reader import read_rows, extract_table_and_plugin  # type: ignore

try:
    from Sensor_Testor.hardware.home import run_home_sequence
except Exception:
    from hardware.home import run_home_sequence         # type: ignore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("true", "1", "yes", "y", "on", "t")


# ---------------------------------------------------------------------------
# OperatorModePopup
# ---------------------------------------------------------------------------

class OperatorModePopup(QDialog):
    # Thread-safe logging signals — any thread may emit; Qt delivers to GUI thread.
    _sig_log_terminal = pyqtSignal(str)
    _sig_log_debug    = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Operator Mode")
        self.setGeometry(200, 200, 1200, 800)

        # Run state
        self.should_stop       = False
        self.is_paused         = False
        self.folder_path       = None
        self.job_details       = None
        self.selected_file_path = None
        self.criteria_filename_hint = None

        # Thread / worker objects
        self._busy         = False
        self._closing      = False
        self._shutting_down = False
        self._debug_tab_shown = False
        self._thread: QThread | None = None
        self._worker = None
        self._runner = None
        self._tr_debugger = None

        # Devices
        self.duet = DuetAdapter()
        self.smac = SmacAdapter()

        # DAQ
        self._daq_ok = False
        self.daq     = None
        try:
            self.daq = DaqAdapter(channels=(0, 2), rate_hz=1000.0)
            self.daq.open()
            self._daq_ok = True
        except Exception as e:
            print(f"[DAQ] open error: {e}")

        # Plot state
        self._last_plot_n = 0
        self._criteria_x     = None
        self._criteria_y_max = None
        self._criteria_y_min = None

        # Graph flags (updated from plan settings)
        self.pass_fail_criteria              = False
        self.graph_force_x_resistance        = False
        self.graph_force_x_sample_number     = False
        self.graph_resistance_x_sample_number = False

        # Graph handles
        self.force_resistance_plot            = None
        self.force_resistance_curve           = None
        self.force_sample_number_plot         = None
        self.force_sample_number_curve        = None
        self.resistance_sample_number_plot    = None
        self.resistance_sample_number_curve   = None
        self.max_criteria_curve               = None
        self.min_criteria_curve               = None

        # Result counters
        self._last_passed_count = 0
        self._last_failed_count = 0

        # Thread-safe log signal → GUI slot connections
        self._sig_log_terminal.connect(self._append_terminal)
        self._sig_log_debug.connect(self._append_debug)

        # 30 Hz plot timer
        self._plot_timer = QTimer(self)
        self._plot_timer.setInterval(33)
        self._plot_timer.timeout.connect(self._flush_plot)
        self._plot_timer.start()

        self._build_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self):
        main_layout = QHBoxLayout(self)
        left  = QVBoxLayout()
        right = QVBoxLayout()
        main_layout.addLayout(left,  2)
        main_layout.addLayout(right, 3)

        # ── Left panel ────────────────────────────────────────────────
        left.addWidget(QLabel("Selected Plan:"))
        self.file_name_display = QLineEdit()
        self.file_name_display.setReadOnly(True)
        self.file_name_display.setPlaceholderText("No file selected")
        left.addWidget(self.file_name_display)

        self.choose_test_button = QPushButton("Choose Test")
        self.choose_test_button.clicked.connect(self.choose_test)
        left.addWidget(self.choose_test_button)

        self.start_test_button = QPushButton("Start Test")
        self.start_test_button.clicked.connect(self.start_test)
        self.start_test_button.setEnabled(False)
        left.addWidget(self.start_test_button)

        self.pause_test_button = QPushButton("Pause Test")
        self.pause_test_button.clicked.connect(self.pause_test)
        self.pause_test_button.setEnabled(False)
        left.addWidget(self.pause_test_button)

        self.stop_test_button = QPushButton("Stop Test")
        self.stop_test_button.clicked.connect(self.stop_test)
        self.stop_test_button.setEnabled(False)
        left.addWidget(self.stop_test_button)

        self.home_button = QPushButton("Home")
        self.home_button.clicked.connect(self.move_home)
        left.addWidget(self.home_button)

        self.soft_touch_button = QPushButton("Soft Touch")
        self.soft_touch_button.clicked.connect(self.run_soft_touch)
        if not self._daq_ok:
            self.soft_touch_button.setEnabled(False)
        left.addWidget(self.soft_touch_button)

        # ── Tabbed log pane ───────────────────────────────────────────
        self._log_tabs = QTabWidget()
        self._log_tabs.setTabPosition(QTabWidget.South)

        def _make_log_tab(label):
            w   = QWidget()
            lay = QVBoxLayout(w)
            lay.setContentsMargins(0, 0, 0, 0)
            hdr = QHBoxLayout()
            hdr.addWidget(QLabel(label))
            hdr.addStretch()
            btn_copy  = QPushButton("Copy");  btn_copy.setFixedWidth(52)
            btn_clear = QPushButton("Clear"); btn_clear.setFixedWidth(52)
            hdr.addWidget(btn_copy); hdr.addWidget(btn_clear)
            lay.addLayout(hdr)
            te = QPlainTextEdit()
            te.setReadOnly(True)
            te.setLineWrapMode(QPlainTextEdit.NoWrap)
            te.setFont(QFont("Monospace", 9))
            te.setMaximumBlockCount(10000)
            lay.addWidget(te, 1)
            btn_copy.clicked.connect(lambda: (te.selectAll(), te.copy()))
            btn_clear.clicked.connect(te.clear)
            return w, te

        op_w,  self.terminal       = _make_log_tab("Operator log")
        dbg_w, self.debug_terminal = _make_log_tab("Debug trace")
        self.debug_terminal.setStyleSheet("background:#0a0a0a; color:#d8d8d8;")
        self._log_tabs.addTab(op_w,  "Operator")
        self._log_tabs.addTab(dbg_w, "Debug ●")
        left.addWidget(self._log_tabs, 1)

        # ── Right panel — graphs ──────────────────────────────────────
        right.addWidget(QLabel("Force × Resistance"))
        self.graph_widget_top = pg.GraphicsLayoutWidget(show=True)
        self.graph_widget_top.setBackground("k")
        right.addWidget(self.graph_widget_top, 1)

        right.addWidget(QLabel("Sample Number Graph"))
        self.graph_widget_bottom = pg.GraphicsLayoutWidget(show=True)
        self.graph_widget_bottom.setBackground("k")
        right.addWidget(self.graph_widget_bottom, 1)

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------
    def _append_terminal(self, text: str) -> None:
        if self._shutting_down:
            return
        try:
            self.terminal.appendPlainText(text)
        except Exception:
            pass

    def _append_debug(self, text: str) -> None:
        try:
            self.debug_terminal.appendPlainText(text)
            if not self._debug_tab_shown:
                self._debug_tab_shown = True
                self._log_tabs.setCurrentIndex(1)
        except Exception:
            pass

    def _log(self, msg: str) -> None:
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
        text = str(msg)
        try:
            print(text, flush=True)
        except Exception:
            pass
        try:
            self._sig_log_debug.emit(text)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def _cleanup_run_objects(self):
        self._runner = None
        self._worker = None
        self._thread = None

    def closeEvent(self, event):
        if self._busy and self._thread is not None and self._thread.isRunning():
            self._log("[Close] Test running — stop the test first.")
            event.ignore()
            return
        try:
            self.set_run_controls(False)
        except Exception:
            pass
        for dev in (self.daq, self.duet, self.smac):
            try:
                if dev is not None:
                    dev.close()
            except Exception:
                pass
        event.accept()

    # ------------------------------------------------------------------
    # UI state
    # ------------------------------------------------------------------
    def set_run_controls(self, running: bool):
        busy = self._busy
        self.home_button.setEnabled(not running and not busy)
        self.choose_test_button.setEnabled(not running and not busy)
        self.start_test_button.setEnabled(
            not running and not busy and bool(self.selected_file_path)
        )
        self.pause_test_button.setEnabled(running)
        self.stop_test_button.setEnabled(running)
        self.soft_touch_button.setEnabled(not running and self._daq_ok and not busy)

    @pyqtSlot()
    def _on_home_finished(self):
        self.set_run_controls(False)

    def _reset_visual_state(self):
        try:
            self.terminal.clear()
        except Exception:
            pass
        try:
            self.graph_widget_top.clear()
            self.graph_widget_bottom.clear()
        except Exception:
            pass
        for attr in (
            "force_resistance_plot", "force_resistance_curve",
            "force_sample_number_plot", "force_sample_number_curve",
            "resistance_sample_number_plot", "resistance_sample_number_curve",
            "max_criteria_curve", "min_criteria_curve",
        ):
            setattr(self, attr, None)
        self._last_plot_n = 0

    # ------------------------------------------------------------------
    # Buttons
    # ------------------------------------------------------------------
    def stop_test(self):
        self.should_stop = True
        self._log("⏹ Stop requested.")
        try:
            if self._runner is not None:
                self._runner.request_stop()
        except Exception:
            pass

    def pause_test(self):
        self.is_paused = True
        self._log("⏸ Paused.")

    def resume_test(self):
        self.is_paused = False
        self._log("▶ Resumed.")

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

        # Display plan table
        try:
            rows  = read_rows(file_path)
            table, _ = extract_table_and_plugin(rows)
            lines = [",".join([str(c or "") for c in r]) for r in table]
            self.terminal.setPlainText("\n".join(lines))
        except Exception as e:
            self._log(f"[Plan] table read error: {e}")

        # Parse plan — strict loader only (lenient fallback removed: the strict
        # loader is comprehensive enough and the lenient duplicate is dead code)
        try:
            steps, settings = load_grid_plan_csv(file_path)
            store.set_plan(file_path, steps, settings)
            self._log(f"[Plan] {len(steps)} step(s) loaded")
        except Exception as e:
            self._log(f"[Plan] parse error: {e}")
            store.set_plan(file_path, [], {})

        self._read_settings_from_store()

        # Extract criteria filename from first step row
        try:
            for st in store.steps:
                cf = getattr(st, "criteria_file", None)
                if cf:
                    self.criteria_filename_hint = os.path.basename(str(cf))
                    self._log(f"[Criteria] hint: {self.criteria_filename_hint}")
                    break
        except Exception:
            pass

        try:
            self.initialize_plots(file_path)
        except Exception as e:
            self._log(f"[Plots] init error: {e}")

        self.start_test_button.setEnabled(True)

        try:
            with open("file_path.txt", "w") as f:
                f.write(self.selected_file_path)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------
    def _get_criteria_path(self) -> Optional[str]:
        """Resolve the criteria file path from the hint set during choose_test."""
        if not self.criteria_filename_hint or not self.selected_file_path:
            return None
        plan_dir = os.path.dirname(self.selected_file_path)
        crit_dir = os.path.join(plan_dir, "pass_fail_criteria_files")
        name     = os.path.basename(self.criteria_filename_hint)
        for candidate in [
            os.path.join(crit_dir, name),
            os.path.join(crit_dir, name + ".csv"),
            os.path.join(plan_dir, name),
            os.path.join(plan_dir, name + ".csv"),
        ]:
            if os.path.isfile(candidate):
                return candidate
        return None

    def _read_settings_from_store(self):
        S = store.settings or {}
        self.pass_fail_criteria               = _to_bool(S.get("Pass Fail Criteria", False))
        self.graph_force_x_resistance         = _to_bool(S.get("Force x Resistance", True))
        self.graph_force_x_sample_number      = _to_bool(S.get("Force x Sample Number", True))
        self.graph_resistance_x_sample_number = _to_bool(S.get("Resistance x Sample", True))
        self._log(
            f"[Settings] PassFail={self.pass_fail_criteria} "
            f"FxR={self.graph_force_x_resistance} "
            f"FxS={self.graph_force_x_sample_number} "
            f"RxS={self.graph_resistance_x_sample_number}"
        )

    # ------------------------------------------------------------------
    # Plot initialisation
    # ------------------------------------------------------------------
    def initialize_plots(self, _file_path: str):
        try:
            self.graph_widget_top.clear()
            self.graph_widget_bottom.clear()
        except Exception:
            pass

        if self.graph_force_x_resistance:
            self._init_force_resistance_plot()
        else:
            self.force_resistance_plot  = None
            self.force_resistance_curve = None
            self.max_criteria_curve     = None
            self.min_criteria_curve     = None

        bottom_plots = []
        if self.graph_force_x_sample_number:
            bottom_plots.append("force")
        if self.graph_resistance_x_sample_number:
            bottom_plots.append("res")

        if len(bottom_plots) == 2:
            self._init_combined_sample_plot()
        elif bottom_plots == ["force"]:
            self._init_force_sample_plot()
        elif bottom_plots == ["res"]:
            self._init_resistance_sample_plot()
        else:
            self.force_sample_number_plot          = None
            self.resistance_sample_number_plot     = None
            self.force_sample_number_curve         = None
            self.resistance_sample_number_curve    = None

        self._plot_criteria_overlays()

    def _init_force_resistance_plot(self):
        p = self.graph_widget_top.addPlot(row=0, col=0)
        p.setLabel("bottom", "Force (kg)")
        p.setLabel("left",   "Resistance (Ω)")
        p.showGrid(x=True, y=True, alpha=0.3)
        p.setDownsampling(auto=True, mode="peak")
        p.setClipToView(True)
        # Fixed default range — user can pan/zoom freely from here.
        # autoRange is NOT enabled so the view never resets while plotting.
        p.setXRange(0, 2, padding=0)
        p.setYRange(0, 100000, padding=0)
        p.setMouseEnabled(x=True, y=True)
        p.setAutoVisible(x=False, y=False)
        self.force_resistance_plot  = p
        self.force_resistance_curve = p.plot(
            [], [], pen=pg.mkPen(color=(255, 80, 80), width=1))
        self.max_criteria_curve = p.plot([], [], pen=pg.mkPen("b", width=2))
        self.min_criteria_curve = p.plot([], [], pen=pg.mkPen("g", width=2))

    def _init_force_sample_plot(self):
        p = self.graph_widget_bottom.addPlot(row=0, col=0)
        p.setLabel("left", "Force", units="kg")
        p.setLabel("bottom", "Sample #")
        p.showGrid(x=True, y=True, alpha=0.3)
        p.setDownsampling(mode="peak")
        p.setClipToView(True)
        self.force_sample_number_plot   = p
        self.force_sample_number_curve  = p.plot([], [], pen=pg.mkPen(width=1))
        self.resistance_sample_number_plot  = None
        self.resistance_sample_number_curve = None

    def _init_resistance_sample_plot(self):
        p = self.graph_widget_bottom.addPlot(row=0, col=0)
        p.setLabel("bottom", "Sample")
        p.setLabel("left",   "Resistance", units="Ω")
        p.showGrid(x=True, y=True, alpha=0.3)
        p.setDownsampling(mode="peak")
        p.setClipToView(True)
        self.resistance_sample_number_plot  = p
        self.resistance_sample_number_curve = p.plot([], [], pen=pg.mkPen(width=1))
        self.force_sample_number_plot  = None
        self.force_sample_number_curve = None

    def _init_combined_sample_plot(self):
        p = self.graph_widget_bottom.addPlot(row=0, col=0)
        p.setLabel("bottom", "Sample #")
        p.setLabel("left",   "Voltage (V)")
        p.showGrid(x=True, y=True, alpha=0.3)
        p.setDownsampling(auto=True, mode="peak")
        p.setClipToView(True)
        p.enableAutoRange(axis="xy")
        self.force_sample_number_plot  = p
        self.force_sample_number_curve = p.plot(
            [], [], pen=pg.mkPen(color=(255, 255, 255), width=1), name="CH0 force")

        p_res = pg.ViewBox()
        p.showAxis("right")
        p.scene().addItem(p_res)
        p.getAxis("right").linkToView(p_res)
        p_res.setXLink(p)

        def _update_views():
            p_res.setGeometry(p.vb.sceneBoundingRect())
            p_res.linkedViewChanged(p.vb, p_res.XAxis)
        p.vb.sigResized.connect(_update_views)

        self.resistance_sample_number_plot  = p_res
        self.resistance_sample_number_curve = pg.PlotCurveItem(
            pen=pg.mkPen("y", width=1), name="CH2 resistance")
        p_res.addItem(self.resistance_sample_number_curve)
        p.getAxis("right").setLabel("Voltage (V)")

    def _plot_criteria_overlays(self):
        if self.force_resistance_plot is None:
            return
        if not self.selected_file_path or not self.criteria_filename_hint:
            return

        plan_dir    = os.path.dirname(self.selected_file_path)
        crit_dir    = os.path.join(plan_dir, "pass_fail_criteria_files")
        name        = os.path.basename(self.criteria_filename_hint)

        candidates = [
            os.path.join(crit_dir, name),
            os.path.join(crit_dir, name + ".csv"),
            os.path.join(plan_dir, name),
            os.path.join(plan_dir, name + ".csv"),
        ]
        crit_path = next((c for c in candidates if os.path.isfile(c)), None)

        # Case-insensitive fallback
        if crit_path is None and os.path.isdir(crit_dir):
            stem = os.path.splitext(name)[0].lower()
            for fname in sorted(os.listdir(crit_dir)):
                if os.path.splitext(fname)[1].lower() == ".csv" and \
                   os.path.splitext(fname)[0].lower() == stem:
                    crit_path = os.path.join(crit_dir, fname)
                    break

        if crit_path is None:
            self._log(f"[Criteria] file not found: {name}")
            return

        try:
            max_pts, min_pts = parse_pass_fail_criteria_form(crit_path)
        except Exception as e:
            self._log(f"[Criteria] parse error: {e}")
            return

        def _prep(pts):
            if not pts:
                return [], []
            xs, ys = zip(*sorted(pts, key=lambda p: p[0]))
            return generate_smoothed_line(xs, ys, num_points=200)

        max_x, max_y = _prep(max_pts)
        min_x, min_y = _prep(min_pts)

        if self.max_criteria_curve and max_x:
            self.max_criteria_curve.setData(max_x, max_y)
        if self.min_criteria_curve and min_x:
            self.min_criteria_curve.setData(min_x, min_y)

        self._log(f"[Criteria] loaded: {os.path.basename(crit_path)}")

    # ------------------------------------------------------------------
    # Worker signal handlers
    # ------------------------------------------------------------------
    def on_step_started(self, idx: int):
        self._log(f"[Plot] Step {idx} — clearing plot")
        self._last_plot_n = 0
        for curve in (
            self.force_resistance_curve,
            self.force_sample_number_curve,
            self.resistance_sample_number_curve,
        ):
            if curve is not None:
                try:
                    curve.setData([], [])
                except Exception:
                    pass

    def on_sample_ready(self, *_):
        """Back-compat signal slot — plotting uses ring_snapshot() at 30 Hz."""
        pass

    def _flush_plot(self) -> None:
        """30 Hz GUI timer — pulls numpy snapshot from worker, calls setData."""
        if self._shutting_down:
            return
        worker = self._worker
        if worker is None or not hasattr(worker, "ring_snapshot"):
            return
        try:
            fs, rs, raw_f, raw_r = worker.ring_snapshot()
        except Exception:
            return
        n = len(raw_f)   # raw arrays always full length; fs/rs may be densified
        if n < 2 or n == self._last_plot_n:
            return
        self._last_plot_n = n

        xs = np.arange(n, dtype=np.float32)

        # Force × Resistance (top) — fs/rs are already smoothed (1/x shape)
        if self.force_resistance_curve is not None:
            try:
                self.force_resistance_curve.setData(x=fs, y=rs)
            except Exception:
                pass

        # Sample number (bottom) — use raw_f/raw_r with sample index
        if self.force_sample_number_curve is not None:
            try:
                self.force_sample_number_curve.setData(x=xs, y=raw_f)
            except Exception:
                pass
        if self.resistance_sample_number_curve is not None:
            try:
                self.resistance_sample_number_curve.setData(x=xs, y=raw_r)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Motion helpers
    # ------------------------------------------------------------------
    def move_home(self):
        if self._busy:
            return
        self._busy = True
        self.set_run_controls(True)
        self._log("[Home] Homing...")

        class _SafeTerminal:
            def __init__(self, fn): self._log = fn
            def append(self, msg): self._log(str(msg))

        t = _SafeTerminal(self._log)

        def _run():
            try:
                run_home_sequence(self.duet, self.smac, terminal=t, safe_z=55.0)
                self._log("[Home] Done.")
            except Exception as e:
                self._log(f"[Home] Error: {e}")
            finally:
                self._busy = False
                QMetaObject.invokeMethod(self, "_on_home_finished", Qt.QueuedConnection)

        threading.Thread(target=_run, daemon=True).start()

    def run_soft_touch(self):
        if self._busy or not self._daq_ok or self.daq is None:
            return
        self._busy = True
        self.set_run_controls(True)

        def _run():
            try:
                self.duet.soft_touch(
                    daq=self.daq, logger=self._log,
                    pre_x=70.0, pre_y=60.0,
                    threshold=0.15, approach_feedrate=60.0,
                    z_bottom_limit=60.0, stream_dt=0.05, notch_Q=30.0,
                )
            except Exception as e:
                self._log(f"[SoftTouch] Error: {e}")
            finally:
                self._busy = False
                QMetaObject.invokeMethod(self, "_on_home_finished", Qt.QueuedConnection)

        threading.Thread(target=_run, daemon=True).start()

    # ------------------------------------------------------------------
    # Fail dialog
    # ------------------------------------------------------------------
    def ask_fail_action(self, step_index: int, reason: str) -> str:
        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Critical)
        msg.setWindowTitle("Test FAILED")
        msg.setText(f"Step {step_index} failed.\n\n{reason or 'Live data failed pass/fail rules.'}")
        btn_continue = msg.addButton("Continue",  QMessageBox.AcceptRole)
        btn_stop     = msg.addButton("Stop Run",  QMessageBox.DestructiveRole)
        btn_redo     = msg.addButton("Redo Test", QMessageBox.ActionRole)
        msg.exec_()
        clicked = msg.clickedButton()
        if clicked is btn_continue: return "continue"
        if clicked is btn_redo:     return "redo"
        return "stop"

    # ------------------------------------------------------------------
    # Start test
    # ------------------------------------------------------------------
    def start_test(self):
        if not self.selected_file_path:
            QMessageBox.warning(self, "No Plan", "Select a test file first.")
            return

        # Ensure plan is loaded
        try:
            if not store.steps or not store.settings:
                steps, settings = load_grid_plan_csv(self.selected_file_path)
                store.set_plan(self.selected_file_path, steps, settings)
        except Exception as e:
            QMessageBox.critical(self, "Plan Error", f"Could not parse plan:\n{e}")
            return

        # Job details
        try:
            self.job_details = get_job_details(self.selected_file_path) or {}
        except Exception:
            self.job_details = {}

        job_num = (self.job_details.get("Job Number") or "").strip() or \
                  os.path.splitext(os.path.basename(self.selected_file_path))[0]

        # Output location
        try:
            self.folder_path, base_name = save_prompt(self, default_folder_name=job_num)
        except Exception as e:
            QMessageBox.critical(self, "Save Location", f"Could not open save dialog:\n{e}")
            return
        if not self.folder_path:
            return

        store.set_output_context(self.folder_path, base_name, self.job_details)
        self._log(f"[Start] output → {store.output_folder}  base='{store.output_base}'")

        # Build RunConfig from store.settings
        S = store.settings or {}
        def _b(k): return _to_bool(S.get(k, False))
        def _n(k, cast=float, default=None):
            try:
                v = S.get(k)
                return default if v is None or v == "" else cast(v)
            except Exception:
                return default

        flags = {k: _b(k) for k in (
            "Raw Data", "Filtered Data", "Data on Same Sheet", "Save Failed Data",
            "Pass Fail Criteria", "Force x Resistance", "Force x Sample Number",
            "Resistance x Sample", "Check Short Circuit", "Check Open Circuit",
            "Check if Preloaded",
        )}

        try:
            cfg = RunConfig(
                x_pitch=_n("x pitch(mm)", float, 0.0),
                y_pitch=_n("y pitch(mm)", float, 0.0),
                n_x=_n("Number of Sensors in x", int, 0),
                n_y=_n("Number of Sensors in y", int, 0),
                v_test=_n("actuator speed(mm/s)", float, 1.0),
                v_travel=_n("speed between spaces(mm/s)", float, 50.0),
                start_x=_n("start position x", float, 0.0),
                start_y=_n("start position y", float, 0.0),
                safe_z=_n("Safe Height (mm)", float, 10.0),
                test_z=_n("Test Height (mm)", float, 0.0),
                start_force=_n("start force(kg)", float, 0.0),
                max_force=_n("max force(kg)", float, 0.0),
                flags=flags,
                preload_res_threshold_ohm=_n("Preload Resistance Threshold (Ω)", float, None),
                short_circuit_threshold_ohm=_n("Short-Circuit Threshold (Ω)", float, None),
            )
            store.set_run_config(cfg)
        except Exception as e:
            QMessageBox.critical(self, "Settings Error", f"Could not build RunConfig:\n{e}")
            return

        # Criteria dict
        criteria = {
            "enabled": _b("Pass Fail Criteria"),
            "per_step": {
                st.test_id: {
                    "criteria_file": getattr(st, "criteria_file", None),
                    "force_target":  getattr(st, "force_target", None),
                }
                for st in store.steps
            },
        }

        # Writers
        try:
            try:
                from Sensor_Testor.app_io.writers import make_writers
            except Exception:
                from app_io.writers import make_writers  # type: ignore
            writers = make_writers(
                out_dir=store.output_folder,
                flags=cfg.flags,
                base_name=store.output_base,
                job_details=store.job_details,
                criteria_path=self._get_criteria_path(),
            )
        except Exception:
            class _NoopWriters:
                def write_step_result(self, *a, **k): pass
                def write_raw(self, *a, **k): pass
                def flush(self): pass
            writers = _NoopWriters()

        if self._busy:
            return

        # Construct worker + runner
        try:
            worker = TestRunnerWorker(
                store.run_config, store.steps, criteria,
                self.duet, self.smac, self.daq, writers,
                terminal_logger=self._log,
            )
        except Exception as e:
            QMessageBox.critical(self, "Worker Error", f"Could not build worker:\n{e}")
            return

        try:
            runner = GridRunner(cfg=store.run_config, steps=store.steps, worker=worker)
        except Exception as e:
            QMessageBox.critical(self, "Runner Error", f"Could not build runner:\n{e}")
            return

        self._worker = worker
        self._runner = runner
        self._thread = QThread(self)

        worker.moveToThread(self._thread)
        runner.moveToThread(self._thread)

        # Signal connections
        try:
            worker.log_message.connect(self._append_debug, Qt.QueuedConnection)
        except Exception:
            pass
        try:
            worker.step_started.connect(self.on_step_started)
        except Exception:
            pass

        runner.progress.connect(lambda i, m: self._log(f"[Run] {i}: {m}"))
        runner.error.connect(lambda m: self._log(f"[Run] ERROR: {m}"))

        def _on_result(i, ok):
            self._log(f"[Run] Step {i} → {'PASS' if ok else 'FAIL'}")
            if ok: self._last_passed_count += 1
            else:  self._last_failed_count += 1
        runner.result.connect(_on_result)

        def _on_run_summary(passed, failed, max_f):
            self._log(f"[Run] Passed={passed}  Failed={failed}" +
                      (f"  MaxFailed={max_f}" if max_f >= 0 else ""))
        try:
            runner.run_summary.connect(_on_run_summary)
        except Exception:
            pass

        def _on_step_decision(i, reason):
            action = self.ask_fail_action(i, reason)
            try:
                runner.set_step_action(action)
            except Exception:
                try: runner.set_step_action("stop")
                except Exception: pass
        try:
            runner.step_decision_requested.connect(_on_step_decision)
        except Exception:
            pass

        def _on_finished():
            try:
                if self._thread is not None:
                    self._thread.quit()
            except Exception:
                pass
        runner.finished.connect(_on_finished)

        @pyqtSlot()
        def _on_thread_done():
            # Flush writers — writes summary sheet then saves
            try:
                if writers is not None and hasattr(writers, "flush"):
                    writers.flush()
            except Exception:
                pass
            self._shutting_down = False
            self._debug_tab_shown = False
            self._busy = False
            self.set_run_controls(False)
            self._cleanup_run_objects()
            self._log(f"[Run] ══ COMPLETE — Passed: {self._last_passed_count}"
                      f"  Failed: {self._last_failed_count} ══")
            if self._closing:
                self._closing = False
                try: self.close()
                except Exception: pass
        self._thread.finished.connect(_on_thread_done)

        # Attach TestRunDebugger if available
        try:
            import importlib
            for _path in ("Sensor_Testor.runner.test_run_debugger",
                          "runner.test_run_debugger", "test_run_debugger"):
                try:
                    _m   = importlib.import_module(_path)
                    _cls = getattr(_m, "TestRunDebugger", None)
                    if _cls:
                        if self._tr_debugger is None:
                            self._tr_debugger = _cls()
                        self._tr_debugger.attach(self._worker, log_fn=self._log_debug)
                        self._log_debug("[Debugger] attached")
                        break
                except Exception:
                    pass
        except Exception:
            pass

        # Launch
        self._busy = True
        self._shutting_down = False
        self._last_passed_count = 0
        self._last_failed_count = 0
        self.set_run_controls(True)

        self._thread.started.connect(runner.run)
        self._log("[Start] launching grid run...")
        self._thread.start()
