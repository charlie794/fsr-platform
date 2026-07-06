# Sensor_Testor/app_io/file_dialogs.py
from __future__ import annotations
import os
from datetime import datetime
from PyQt5.QtWidgets import QFileDialog, QInputDialog, QMessageBox, QWidget


def save_prompt(parent: QWidget = None, default_folder_name: str | None = None):
    """
    Ask user to pick a directory, then a folder name (timestamp suffix if exists),
    then a base file name. Writes both to file_path.txt.
    Returns: (folder_path, initial_file_name)
    """
    directory = QFileDialog.getExistingDirectory(parent, "Select Directory to Save Folder", "")
    if not directory:
        directory = ""

    directory = os.path.normpath(directory)
    print("Selected directory:", directory)

    folder_name, ok = QInputDialog.getText(
        parent, "Folder Name",
        "Enter a name for the data folder:",
        text=default_folder_name if default_folder_name else ""
    )
    if not ok or not folder_name:
        folder_name = default_folder_name if default_folder_name else ""

    folder_path = os.path.join(directory, folder_name)
    print("Folder path will be:", folder_path)
    if os.path.exists(folder_path):
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        folder_path = f"{folder_path}_{timestamp}"
    os.makedirs(folder_path, exist_ok=True)

    initial_file_name, ok = QInputDialog.getText(
        parent, "Initial File Name",
        "Enter the base name for files in this folder:",
        text="test"
    )
    if not ok or not initial_file_name:
        initial_file_name = "test"

    with open('file_path.txt', 'w') as f:
        f.write(f"{folder_path}\n")
        f.write(f"{initial_file_name}\n")

    QMessageBox.information(
        parent, "Folder Created",
        f"Data will be saved in:\n{folder_path}\nwith base file name:\n{initial_file_name}"
    )
    return folder_path, initial_file_name
