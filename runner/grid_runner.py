from __future__ import annotations
from typing import Any, Optional

import os
import threading
from PyQt5.QtCore import QObject, pyqtSignal, pyqtSlot

try:
    from Sensor_Testor.domain import models
    from Sensor_Testor.domain.models import RunConfig, TestStep, store
except Exception:
    try:
        import domain.models as models  # type: ignore
        from domain.models import RunConfig, TestStep, store  # type: ignore
    except Exception:
        import models  # type: ignore
        from models import RunConfig, TestStep, store  # type: ignore

try:
    from Sensor_Testor.runner.test_runner import TestRunnerWorker, StepRunResult
except Exception:
    try:
        from runner.test_runner import TestRunnerWorker, StepRunResult  # type: ignore
    except Exception:
        from test_runner import TestRunnerWorker, StepRunResult  # type: ignore

try:
    from Sensor_Testor.runner.path_utils import resolve_criteria_path
except Exception:
    try:
        from runner.path_utils import resolve_criteria_path  # type: ignore
    except Exception:
        try:
            from path_utils import resolve_criteria_path  # type: ignore
        except Exception:
            resolve_criteria_path = None  # type: ignore


class GridRunner(QObject):
    progress = pyqtSignal(int, str)
    result = pyqtSignal(int, bool)
    finished = pyqtSignal()
    error = pyqtSignal(str)

    step_decision_requested = pyqtSignal(int, str)   # row_index, failure_reason
    run_summary = pyqtSignal(int, int, int)          # passed_count, failed_count, max_failed_or_minus_1

    def __init__(self, cfg: RunConfig, steps: list[TestStep], worker: TestRunnerWorker):
        super().__init__()
        self.cfg = cfg
        self.steps = steps
        self.worker = worker
        self._daq_ok = False

        self.failed_count = 0
        self.passed_count = 0
        self.max_failed_parts = self._read_max_failed_parts()

        self._pending_step_action = "stop"
        self._decision_event = threading.Event()
        self._stop_requested = False

        try:
            self.worker.progress.connect(self.progress.emit)
        except Exception:
            pass
        try:
            self.worker.error.connect(self.error.emit)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Logging / small helpers
    # ------------------------------------------------------------------
    def _log(self, msg: str) -> None:
        logger = getattr(self.worker, "terminal_logger", None)
        if callable(logger):
            try:
                logger(msg)
                return
            except Exception:
                pass
        try:
            self.progress.emit(-1, msg)
        except Exception:
            pass

    def _settings(self) -> dict:
        try:
            s = getattr(store, "settings", {}) or {}
            return s if isinstance(s, dict) else {}
        except Exception:
            return {}

    def _bool(self, settings: dict, key: str) -> bool:
        v = settings.get(key)
        if isinstance(v, bool):
            return v
        try:
            return str(v).strip().lower() in ("true", "1", "yes", "y", "on")
        except Exception:
            return False

    def _get(self, obj, names, cast=float, default=None):
        for name in names:
            if isinstance(obj, dict) and name in obj:
                try:
                    return cast(obj[name])
                except Exception:
                    pass
            if hasattr(obj, name):
                try:
                    return cast(getattr(obj, name))
                except Exception:
                    pass
        return default

    def _get_str(self, obj, names):
        for name in names:
            if isinstance(obj, dict) and name in obj:
                val = obj[name]
                return None if val is None else str(val)
            if hasattr(obj, name):
                val = getattr(obj, name)
                return None if val is None else str(val)
        return None

    def _read_max_failed_parts(self):
        try:
            jd = getattr(store, "job_details", {}) or {}
            raw = jd.get("Max Failed Parts", "")
            if raw is None:
                return None
            s = str(raw).strip()
            if s == "":
                return None
            return int(float(s))
        except Exception:
            return None

    def _emit_summary(self) -> None:
        try:
            self.run_summary.emit(
                int(self.passed_count),
                int(self.failed_count),
                int(self.max_failed_parts) if self.max_failed_parts is not None else -1,
            )
        except Exception:
            pass

    def _failure_budget_exceeded(self) -> bool:
        if self.max_failed_parts is None:
            return False
        return int(self.failed_count) > int(self.max_failed_parts)

    def set_step_action(self, action: str) -> None:
        action = str(action or "").strip().lower()
        if action not in ("continue", "stop", "redo"):
            action = "stop"
        self._pending_step_action = action
        self._decision_event.set()

    def request_stop(self) -> None:
        """Called by the UI Stop button. Signals runner and worker to exit cleanly."""
        self._stop_requested = True
        # Also set on worker so the descent wait-loop can check it
        try:
            if self.worker is not None:
                self.worker._stop_requested = True
        except Exception:
            pass
        # Unblock any pending decision wait immediately
        self._pending_step_action = "stop"
        self._decision_event.set()

    # ------------------------------------------------------------------
    # Writer lifecycle
    # ------------------------------------------------------------------
    def _flush_writers(self) -> None:
        try:
            writers = getattr(self.worker, "writers", None)
            if writers is not None and hasattr(writers, "flush"):
                self._log("[Writers] Flushing workbook to disk...")
                writers.flush()
                self._log("[Writers] Flush complete.")
        except Exception as e:
            self._log(f"[Writers] Flush error: {e}")

    # ------------------------------------------------------------------
    # Path / plotting helpers
    # ------------------------------------------------------------------
    def _resolve_criteria_path(self, filename: str | None) -> str | None:
        # Delegate to the shared utility so the search logic lives in one place.
        if resolve_criteria_path is not None:
            return resolve_criteria_path(filename)
        return os.path.abspath(str(filename)) if filename else None

    def _plot_criteria_if_available(self, idx: int, st: TestStep) -> None:
        crit_name = self._get_str(st, ("criteria_file", "pf_criteria", "p_f_criteria", "P/F Criteria"))
        abs_crit = self._resolve_criteria_path(crit_name)
        if abs_crit:
            self.progress.emit(idx, f"[Row {idx}] Plot criteria: {os.path.basename(abs_crit)}")
            plotter = getattr(self.worker, "plot_criteria", None)
            if callable(plotter):
                try:
                    plotter(abs_crit)
                except Exception as e:
                    self._log(f"[Row {idx}] Plot error: {e}")
            else:
                self._log(f"[Row {idx}] (No plot_criteria) Would plot: {abs_crit}")
        elif crit_name:
            self._log(f"[Row {idx}] Criteria file not found: {crit_name}")

    # ------------------------------------------------------------------
    # Device handshake / pre-scan
    # ------------------------------------------------------------------
    def _handshake_devices(self) -> None:
        self._log("[Handshake] Starting device handshake...")

        daq = getattr(self.worker, "daq", None)
        self._daq_ok = False
        try:
            if daq is not None:
                if not getattr(daq, "is_open", False):
                    daq.open()
                _t, _v0, _v2 = daq.capture_window(0.05)
                self._log("[Handshake][DAQ] MCC-128 OK")
                self._daq_ok = True
            else:
                self._log("[Handshake][DAQ] No DAQ instance provided.")
        except Exception as e:
            self._log(f"[Handshake][DAQ] Error: {e}")

        duet = getattr(self.worker, "duet", None)
        try:
            if duet is not None:
                try:
                    duet.send_gcode("M115")
                except Exception as e:
                    self._log(f"[Handshake][Duet] M115 error: {e}")
                try:
                    pos, _raw = duet.get_position(ok_timeout_s=1.5)
                    self._log(f"[Handshake][Duet] OK. Pos≈{pos}")
                except Exception as e:
                    self._log(f"[Handshake][Duet] M114 error: {e}")
            else:
                self._log("[Handshake][Duet] No Duet instance provided.")
        except Exception as e:
            self._log(f"[Handshake][Duet] Error: {e}")

        self._log("[Handshake] Done.")

    def _mini_daq_scan_if_needed(self) -> None:
        settings = self._settings()
        check_short = self._bool(settings, "Check Short Circuit")
        check_preld = self._bool(settings, "Check if Preloaded")

        if not (check_short or check_preld):
            self._log("[Pre-Scan] Skipped (flags disabled).")
            return

        daq = getattr(self.worker, "daq", None)
        if not self._daq_ok or daq is None:
            self._log("[Pre-Scan] Skipped (DAQ unavailable).")
            return

        preload_thr = settings.get("Preload Resistance Threshold (Ω)")
        short_thr = settings.get("Short-Circuit Threshold (Ω)")

        try:
            self._log("[Pre-Scan] Running 1.0s mini DAQ scan (due to flags)...")
            _t, v0, v2 = daq.capture_window(1.0)
            if len(v0) and len(v2):
                f_min, f_max = float(min(v0)), float(max(v0))
                r_min, r_max = float(min(v2)), float(max(v2))
                f_avg = float(sum(v0) / max(1, len(v0)))
                r_avg = float(sum(v2) / max(1, len(v2)))
                self._log(f"[Pre-Scan] CH0 force  min/avg/max = {f_min:.4f}/{f_avg:.4f}/{f_max:.4f} V")
                self._log(f"[Pre-Scan] CH2 resist min/avg/max = {r_min:.4f}/{r_avg:.4f}/{r_max:.4f} V")
                if preload_thr is not None:
                    self._log(f"[Pre-Scan] Preload threshold (Ω) = {preload_thr}")
                if short_thr is not None:
                    self._log(f"[Pre-Scan] Short-circuit threshold (Ω) = {short_thr}")
            else:
                self._log("[Pre-Scan] No samples captured.")
        except Exception as e:
            self._log(f"[Pre-Scan] Error: {e}")

    # ------------------------------------------------------------------
    # Motion helpers
    # ------------------------------------------------------------------
    def _move_safe_z(self, duet: Any, idx: int, safe_z: Optional[float], st=None) -> None:
        # Always read live from store.settings so CSV changes take effect immediately.
        try:
            S = getattr(store, "settings", {}) or {}
            safe_z = float(S.get("Safe Height (mm)") or safe_z or 10.0)
            v_travel = float(S.get("speed between spaces(mm/s)") or 3000.0)
        except Exception:
            v_travel = 3000.0
        try:
            self.progress.emit(idx, f"[Row {idx}] Move Z to Safe Height Z{safe_z:.3f} mm")
        except Exception:
            pass
        try:
            duet.send_gcode("G90")
            duet.send_gcode(f"G1 Z{safe_z:.3f} F{v_travel:.0f}")
            duet.send_gcode("M400")
        except Exception as e:
            self._log(f"[Row {idx}] Safe-Z move error: {e}")

    def _move_xy_for_step(self, duet: Any, idx: int, st: TestStep, y_length: float) -> None:
        x_plan = self._get(st, ("x", "x_pos", "x_position", "X Position"), float, 0.0)
        y_plan = self._get(st, ("y", "y_pos", "y_position", "Y Position"), float, 0.0)

        x_cmd = float(x_plan)
        y_cmd = float(y_length) - float(y_plan)

        self.progress.emit(
            idx,
            f"[Row {idx}] Move to X{x_cmd:.3f} (plan {x_plan:.3f}), "
            f"Y{y_cmd:.3f} (reversed from plan {y_plan:.3f})"
        )
        duet.send_gcode(f"G1 X{x_cmd:.3f} Y{y_cmd:.3f}")


    def _set_travel_speed(self, duet: Any, idx: int, st: TestStep) -> None:
        try:
            S = getattr(store, "settings", {}) or {}
            feed_mm_min = float(S.get("speed between spaces(mm/s)") or 3000.0)
        except Exception:
            feed_mm_min = 3000.0
        self.progress.emit(idx, f"[Row {idx}] Travel speed F{feed_mm_min:.0f} mm/min")
        try:
            duet.send_gcode(f"G1 F{feed_mm_min:.0f}")
        except Exception as e:
            self._log(f"[Row {idx}] Feed set error: {e}")

    # ------------------------------------------------------------------
    # Step result handling
    # ------------------------------------------------------------------
    def _interpret_step_result(self, result_obj: Any) -> tuple[bool, str]:
        passed = bool(getattr(result_obj, "passed", result_obj))
        failure_reason = str(getattr(result_obj, "failure_reason", "") or "")
        criteria_message = str(getattr(result_obj, "criteria_message", "") or "")
        if not failure_reason and criteria_message:
            failure_reason = criteria_message
        return passed, failure_reason

    def _wait_for_operator_decision(self, idx: int, failure_reason: str) -> str:
        self._pending_step_action = "wait"
        self._decision_event.clear()

        try:
            self.step_decision_requested.emit(idx, failure_reason or "Step failed.")
        except Exception:
            self._pending_step_action = "stop"
            return "stop"

        self._decision_event.wait()
        action = self._pending_step_action
        if action not in ("continue", "stop", "redo"):
            action = "stop"
        return action

    # ------------------------------------------------------------------
    # Main run pass
    # ------------------------------------------------------------------
    def _execute_plan_pass(self) -> None:
        duet = getattr(self.worker, "duet", None)
        if duet is None:
            self._log("[Plan] No Duet instance; skipping moves.")
            return

        steps = self.steps or []
        if not steps:
            self._log("[Plan] No steps to process.")
            return

        self._log(f"[Plan] Executing {len(steps)} step(s) in order...")
        if self.max_failed_parts is None:
            self._log("[Plan] Max Failed Parts: not set (no early-stop limit).")
        else:
            self._log(f"[Plan] Max Failed Parts: {self.max_failed_parts}")

        try:
            duet.send_gcode("M17")
            duet.send_gcode("G90")
        except Exception as e:
            self._log(f"[Plan] Prep (M17/G90) error: {e}")

        safe_z = getattr(self.cfg, "safe_z", None)
        y_length = getattr(models, "y_length", None)
        if y_length is None:
            y_length = 140.0

        for idx, st in enumerate(steps, start=1):
            if self._stop_requested:
                self._log("[Plan] Stop requested — aborting.")
                return
            while True:
                try:
                    self._move_safe_z(duet, idx, safe_z, st=st)
                    if self._stop_requested:
                        self._log("[Plan] Stop requested after safe-Z move.")
                        return
                    self._set_travel_speed(duet, idx, st)
                    self._plot_criteria_if_available(idx, st)
                    self._move_xy_for_step(duet, idx, st, float(y_length))
                    if self._stop_requested:
                        self._log("[Plan] Stop requested after XY move.")
                        return

                    result_obj = (
                        self.worker.run_step(st)
                        if hasattr(self.worker, "run_step")
                        else self.worker.run()
                    )

                    if self._stop_requested:
                        self._log("[Plan] Stop requested after step.")
                        return

                    passed, failure_reason = self._interpret_step_result(result_obj)
                    self.result.emit(idx, passed)

                    if passed:
                        self.passed_count += 1
                        self._log(f"[Row {idx}] PASS")
                        self._emit_summary()
                        break

                    self.failed_count += 1
                    self._log(f"[Row {idx}] FAIL: {failure_reason or 'step failed'}")
                    self._emit_summary()

                    if self._failure_budget_exceeded():
                        self._log(
                            f"[Run] Early stop: failed_count={self.failed_count} exceeds "
                            f"max_failed_parts={self.max_failed_parts}"
                        )
                        return

                    action = self._wait_for_operator_decision(idx, failure_reason or "Step failed.")

                    if action == "redo":
                        self.failed_count = max(0, self.failed_count - 1)
                        self._log(f"[Row {idx}] Redo requested.")
                        self._emit_summary()
                        continue

                    if action == "continue":
                        self._log(f"[Row {idx}] Continue after failure.")
                        break

                    self._log(f"[Row {idx}] Stop requested after failure.")
                    return

                except Exception as e:
                    self._log(f"[Row {idx}] Error: {e}")
                    break

        self._log("[Plan] Completed all rows.")

    # ------------------------------------------------------------------
    # Final parking
    # ------------------------------------------------------------------
    def _park_machine(self) -> None:
        try:
            duet = getattr(self.worker, "duet", None)
            if duet is None:
                return

            self._log("[Park] Moving to Z61 then X0 Y0...")
            try:
                duet.send_gcode("G90")
            except Exception:
                pass
            try:
                duet.send_gcode("G0 Z61")
                duet.send_gcode("M400")
            except Exception as e:
                self._log(f"[Park] Z move error: {e}")
            try:
                duet.send_gcode("G0 X0 Y0")
                duet.send_gcode("M400")
            except Exception as e:
                self._log(f"[Park] X/Y move error: {e}")
        except Exception:
            pass

    # ------------------------------------------------------------------
    @pyqtSlot()
    def run(self) -> None:
        try:
            self._handshake_devices()
        except Exception as e:
            self._log(f"[Runner] Handshake exception (non-fatal): {e}")

        try:
            self._mini_daq_scan_if_needed()
        except Exception as e:
            self._log(f"[Runner] Pre-scan exception (non-fatal): {e}")

        try:
            self._execute_plan_pass()
        except Exception as e:
            self._log(f"[Runner] Execute-plan exception: {e}")
        finally:
            # Critical now that Writers keeps workbook open in memory
            self._flush_writers()

        self._park_machine()

        try:
            self.finished.emit()
        except Exception:
            pass
