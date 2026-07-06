from PyQt5.QtWidgets import (
    QDialog, QFormLayout, QLabel, QLineEdit, QDialogButtonBox, QVBoxLayout
)

class _JobDetailsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Enter Job Details")
        self.layout = QVBoxLayout(self)
        self.form = QFormLayout()
        self.fields = _FIELDS
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

# Field list as a module-level constant so we never need to instantiate
# a dialog just to read field names (fixes the double-instantiation bug).
_FIELDS = [
    "Job Number", "Lot Number", "Customer", "Customer ID", "Internal P/N",
    "Internal Rev", "Customer P/N", "Customer Rev", "File", "Operator", "Comment",
    "Quantity of Good Parts", "Max Failed Parts"
]

def get_job_details(_csv_file_path=None):
    dlg = _JobDetailsDialog()
    return dlg.values() if dlg.exec_() == QDialog.Accepted else {
        f: "" for f in _FIELDS
    }
