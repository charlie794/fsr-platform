from __future__ import annotations

import csv
import os
from datetime import datetime

from Sensor_Testor.domain import models  # to update models.resistance_cal_file
import numpy as np
from PyQt5.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QLabel,
    QPushButton,
    QLineEdit,
    QHBoxLayout,
)

# Hardware: DAQ + DUET adapters
try:
    from Sensor_Testor.hardware.daq_adapter import DaqAdapter
except Exception:
    from hardware.daq_adapter import DaqAdapter

try:
    from Sensor_Testor.hardware.duet_adapter import DuetAdapter
except Exception:
    from hardware.duet_adapter import DuetAdapter


class ForceCalibration(QDialog):
    """
    Behaviour-clone of the ForceCalibration in GUI.py, but:
      - DAQ reading via DaqAdapter (channel 0 = force).
      - Jogging done via DuetAdapter.send_gcode using relative moves.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Force Calibration")
        self.setGeometry(100, 100, 400, 300)

        # calibration state
        self.offset = None        # volts at zero force
        self.force_gain = None    # grams per volt
        self._daq = None
        self._duet = None

        # calibration folder
        self.save_dir = "/home/charlie/Documents/Calibration_Folder"
        os.makedirs(self.save_dir, exist_ok=True)

        layout = QVBoxLayout(self)

        # --- Offset calibration ---
        self.instruction_label = QLabel(
            "Ensure nothing is touching the load cell,\n"
            "then click 'Calibrate Offset'",
            self,
        )
        layout.addWidget(self.instruction_label)

        self.offset_button = QPushButton("Calibrate Offset", self)
        self.offset_button.clicked.connect(self.calibrate_offset)
        layout.addWidget(self.offset_button)

        self.offset_label = QLabel("Offset: Not calibrated", self)
        layout.addWidget(self.offset_label)

        # --- Force gain calibration ---
        self.force_instruction = QLabel(
            "Now, apply force and enter the force value (in grams):",
            self,
        )
        self.force_instruction.setVisible(False)
        layout.addWidget(self.force_instruction)

        self.force_entry = QLineEdit(self)
        self.force_entry.setPlaceholderText("Enter force in grams")
        self.force_entry.setVisible(False)
        layout.addWidget(self.force_entry)

        self.gain_button = QPushButton("Calibrate Force Gain", self)
        self.gain_button.clicked.connect(self.calibrate_force_gain)
        self.gain_button.setVisible(False)
        layout.addWidget(self.gain_button)

        self.gain_label = QLabel("Force Gain: Not calibrated", self)
        self.gain_label.setVisible(False)
        layout.addWidget(self.gain_label)

        # --- Jog controls (X / Y / Z) ---
        # X
        jog_x_layout = QHBoxLayout()
        self.jog_x_value = QLineEdit(self)
        self.jog_x_value.setPlaceholderText("ΔX (mm)")
        self.jog_x_button = QPushButton("Jog X", self)
        self.jog_x_button.clicked.connect(self.jog_x)
        jog_x_layout.addWidget(self.jog_x_value)
        jog_x_layout.addWidget(self.jog_x_button)
        layout.addLayout(jog_x_layout)

        # Y
        jog_y_layout = QHBoxLayout()
        self.jog_y_value = QLineEdit(self)
        self.jog_y_value.setPlaceholderText("ΔY (mm)")
        self.jog_y_button = QPushButton("Jog Y", self)
        self.jog_y_button.clicked.connect(self.jog_y)
        jog_y_layout.addWidget(self.jog_y_value)
        jog_y_layout.addWidget(self.jog_y_button)
        layout.addLayout(jog_y_layout)

        # Z
        jog_z_layout = QHBoxLayout()
        self.jog_z_value = QLineEdit(self)
        self.jog_z_value.setPlaceholderText("ΔZ (mm)")
        self.jog_z_button = QPushButton("Jog Z", self)
        self.jog_z_button.clicked.connect(self.jog_z)
        jog_z_layout.addWidget(self.jog_z_value)
        jog_z_layout.addWidget(self.jog_z_button)
        layout.addLayout(jog_z_layout)

    # ----------------- helpers -----------------

    @property
    def daq(self) -> DaqAdapter:
        if self._daq is None:
            self._daq = DaqAdapter(channels=(0, 2), rate_hz=1000.0)  # CH0 diff=force, CH2 diff=resistance
            try:
                self._daq.open()
            except Exception as e:
                print(f"[ForceCalibration] DAQ open error: {e}")
        return self._daq

    @property
    def duet(self) -> DuetAdapter:
        if self._duet is None:
            self._duet = DuetAdapter()
        return self._duet

    # ----------------- DAQ readings -----------------

    def force_offset(self) -> float:
        """
        Uses DaqAdapter, treating channel 0 as the force channel,
        and returns the mean voltage over ~1 second.
        """
        try:
            _t, v_force, _v_res = self.daq.capture_window(1.0)
            if v_force is None or len(v_force) == 0:
                return 0.0
            return float(np.mean(v_force))
        except Exception as e:
            print(f"[ForceCalibration] Error in force_offset: {e}")
            return 0.0

    # ----------------- calibration logic -----------------

    def calibrate_offset(self):
        """Measure and show offset voltage with no load."""
        self.offset = self.force_offset()
        self.offset_label.setText(f"Offset: {self.offset:.5f} V")

        # Reveal step 2 UI
        self.force_instruction.setVisible(True)
        self.force_entry.setVisible(True)
        self.gain_button.setVisible(True)

    def calibrate_force_gain(self):
        """
        - user types known force in grams
        - we measure voltage (with load)
        - gain = grams / (V_load - V_offset)
        - save equation + numbers to CSV with timestamped filename.
        """
        if self.offset is None:
            self.gain_label.setText("Offset not calibrated yet.")
            self.gain_label.setVisible(True)
            return

        try:
            force_value = float(self.force_entry.text())
        except ValueError:
            self.gain_label.setText("Invalid force input. Please enter a valid number.")
            self.gain_label.setVisible(True)
            return

        measured_voltage = self.force_offset()
        voltage_diff = measured_voltage - self.offset

        if voltage_diff == 0:
            self.gain_label.setText("Voltage difference is zero, cannot compute gain.")
            self.gain_label.setVisible(True)
            return

        self.force_gain = force_value / voltage_diff
        self.gain_label.setText(f"Force Gain: {self.force_gain:.5f} grams/V")
        self.gain_label.setVisible(True)

        self.save_calibration(self.offset, self.force_gain)

    def save_calibration(self, offset: float, gain: float):
        """
        Save calibration CSV and store the equation string in models.latest_force_calibration.
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = os.path.join(self.save_dir, f"force_calibration_{timestamp}.csv")

        # ✅ FIXED EQUATION: subtract offset first, then multiply by gain
        equation = f"y = (x - {offset:.5f}) * {gain:.5f}"

        try:
            with open(filename, mode="w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([equation])      # readable equation
                writer.writerow([offset, gain])  # numeric values

            # 🔴 HARD SAVE into models.py
            try:
                from domain import models as _models
            except Exception:
                import models as _models

            if hasattr(_models, "set_latest_force_calibration"):
                _models.set_latest_force_calibration(equation)
            else:
                _models.latest_force_calibration = equation

            print(f"Calibration saved to {filename}")
            print(f"Equation: {equation}")

        except Exception as e:
            print("Error saving calibration:", e)

    # ----------------- jogging (via DuetAdapter) -----------------

    def _jog_relative(self, axis: str, delta: float, feedrate: float = 2000.0):
        """
        Do a simple relative jog using Duet in G91 mode, then restore G90.
        """
        try:
            self.duet.send_gcode("G91")  # relative
            self.duet.send_gcode(f"G1 {axis.upper()}{delta:.3f} F{feedrate:.0f}")
            self.duet.send_gcode("G90")  # back to absolute
        except Exception as e:
            print(f"[ForceCalibration] Jog {axis} error: {e}")

    def jog_x(self):
        try:
            value = float(self.jog_x_value.text())
            self._jog_relative("X", value, feedrate=2000.0)
        except ValueError:
            print("Invalid X value")

    def jog_y(self):
        try:
            value = float(self.jog_y_value.text())
            self._jog_relative("Y", value, feedrate=2000.0)
        except ValueError:
            print("Invalid Y value")

    def jog_z(self):
        try:
            value = float(self.jog_z_value.text())
            self._jog_relative("Z", value, feedrate=800.0)
        except ValueError:
            print("Invalid Z value")

    # ----------------- cleanup -----------------

    def closeEvent(self, event):
        try:
            if self._daq is not None:
                self._daq.close()
        except Exception:
            pass
        super().closeEvent(event)
