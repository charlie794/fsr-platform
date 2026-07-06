import os
import sys

# --- Make sure the parent of "Sensor_Testor" is importable (Option A) ---
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))   # .../Sensor_Testor
PARENT_DIR   = os.path.dirname(PROJECT_ROOT)                 # .../TestbenchBackup
if PARENT_DIR not in sys.path:
    sys.path.insert(0, PARENT_DIR)

# Optional: better scaling on high-DPI displays
os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR", "1")

from PyQt5.QtWidgets import QApplication
# Import UIs from the package
from Sensor_Testor.ui.mode_dialog import ModeDialog
from Sensor_Testor.ui.operator_mode import OperatorModePopup
from Sensor_Testor.ui.engineering_mode import EngineeringMode  # your engineering dialog

def main():
    app = QApplication(sys.argv)

    # Startup chooser
    chooser = ModeDialog()
    if not chooser.exec_():   # user closed dialog5
        return

    mode = (chooser.mode or "operator").lower()

    if mode.startswith("eng"):
        win = EngineeringMode()       # separate engineering window
    else:
        win = OperatorModePopup()     # separate operator window

    win.show()
    sys.exit(app.exec_())

if __name__ == "__main__":
    main()
