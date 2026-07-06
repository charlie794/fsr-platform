from PyQt5.QtWidgets import QDialog, QVBoxLayout, QLabel, QPushButton, QHBoxLayout
from PyQt5.QtCore import Qt

# Relative imports inside the ui package
try:
    from .resistance_calibration import ResistanceCalibration
except Exception as e:
    print("[ModeDialog] error importing ResistanceCalibration:", e)
    ResistanceCalibration = None

try:
    from .force_calibration import ForceCalibration
except Exception as e:
    print("[ModeDialog] error importing ForceCalibration:", e)
    ForceCalibration = None


class ModeDialog(QDialog):
    """
    Simple popup: choose Operator or Engineering.
    Also offers Resistance and Force Calibration popups.
    Sets self.mode to 'operator' or 'engineering' on accept.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Choose Mode")
        self.setModal(True)
        self.mode = None

        v = QVBoxLayout(self)
        v.addWidget(QLabel("Select how you want to run:", self), alignment=Qt.AlignHCenter)

        # --- mode buttons ---
        btn_row = QHBoxLayout()
        btn_operator = QPushButton("Operator Mode", self)
        btn_engineer = QPushButton("Engineering Mode", self)
        btn_row.addWidget(btn_operator)
        btn_row.addWidget(btn_engineer)
        v.addLayout(btn_row)

        btn_operator.clicked.connect(self._pick_operator)
        btn_engineer.clicked.connect(self._pick_engineer)

        # --- calibration buttons ---
        cal_row = QHBoxLayout()

        btn_res_cal = QPushButton("Resistance Calibration", self)
        btn_force_cal = QPushButton("Force Calibration", self)

        btn_res_cal.clicked.connect(self._open_resistance_cal)
        btn_force_cal.clicked.connect(self._open_force_cal)

        cal_row.addStretch(1)
        cal_row.addWidget(btn_res_cal)
        cal_row.addWidget(btn_force_cal)
        cal_row.addStretch(1)

        v.addLayout(cal_row)

        self.setFixedSize(420, 160)

    def _pick_operator(self):
        self.mode = "operator"
        self.accept()

    def _pick_engineer(self):
        self.mode = "engineering"
        self.accept()

    def _open_resistance_cal(self):
        if ResistanceCalibration is None:
            print("[ModeDialog] ResistanceCalibration not available.")
            return
        dlg = ResistanceCalibration(self)
        dlg.exec_()

    def _open_force_cal(self):
        if ForceCalibration is None:
            print("[ModeDialog] ForceCalibration not available.")
            return
        dlg = ForceCalibration(self)
        dlg.exec_()
