"""Recording controls + preset save/load."""
from __future__ import annotations

import os
from typing import Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .widgets import NoWheelComboBox


SAVE_FORMATS = [
    ("MP4 video",         "mp4"),
    ("AVI video",         "avi"),
    ("TIFF sequence",     "tiff_sequence"),
    ("BigTIFF (multi-page)", "bigtiff"),
    ("Raw binary + JSON", "raw"),
    ("HDF5 (.h5)",        "hdf5"),
]


class RecordingPanel(QWidget):
    snapshot_clicked = Signal()
    start_recording = Signal(dict)    # job dict
    stop_recording = Signal()

    save_preset = Signal(str)
    load_preset = Signal(str)

    def __init__(
        self,
        parent=None,
        *,
        title: str = "Recording",
        default_prefix: str = "rec",
        default_format: str = "mp4",
        show_snapshot: bool = True,
        show_presets: bool = True,
        start_text: str = "Start Recording",
        stop_text: str = "Stop Recording",
    ):
        super().__init__(parent)
        self._show_snapshot = show_snapshot
        self._show_presets = show_presets

        box = QGroupBox(title)
        form = QFormLayout(box)

        # Output dir
        out_row = QHBoxLayout()
        self.edit_out_dir = QLineEdit(os.path.abspath("recordings"))
        self.btn_browse = QPushButton("Browse")
        out_row.addWidget(self.edit_out_dir)
        out_row.addWidget(self.btn_browse)
        form.addRow("Output dir", out_row)

        self.edit_prefix = QLineEdit(default_prefix)
        form.addRow("File prefix", self.edit_prefix)

        self.combo_format = NoWheelComboBox()
        for label, value in SAVE_FORMATS:
            self.combo_format.addItem(label, userData=value)
        for i in range(self.combo_format.count()):
            if self.combo_format.itemData(i) == default_format:
                self.combo_format.setCurrentIndex(i)
                break
        form.addRow("Save format", self.combo_format)

        # Stop conditions
        self.check_max_frames = QCheckBox("Limit by frame count")
        self.spin_max_frames = QSpinBox()
        self.spin_max_frames.setRange(1, 100_000_000)
        self.spin_max_frames.setValue(1000)
        self.spin_max_frames.setEnabled(False)
        form.addRow(self.check_max_frames, self.spin_max_frames)

        self.check_max_seconds = QCheckBox("Limit by duration (s)")
        self.spin_max_seconds = QDoubleSpinBox()
        self.spin_max_seconds.setRange(0.1, 100_000.0)
        self.spin_max_seconds.setValue(10.0)
        self.spin_max_seconds.setEnabled(False)
        form.addRow(self.check_max_seconds, self.spin_max_seconds)

        self.edit_note = QLineEdit("")
        self.edit_note.setPlaceholderText("user note (saved into metadata.json)")
        form.addRow("Note", self.edit_note)

        # Buttons
        btn_row = QHBoxLayout()
        self.btn_snapshot = QPushButton("Snapshot")
        self.btn_snapshot.setVisible(show_snapshot)
        self.btn_start = QPushButton(start_text)
        self.btn_stop = QPushButton(stop_text)
        self.btn_stop.setEnabled(False)
        if show_snapshot:
            btn_row.addWidget(self.btn_snapshot)
        btn_row.addWidget(self.btn_start)
        btn_row.addWidget(self.btn_stop)
        form.addRow(btn_row)

        # Progress
        self.lbl_progress = QLabel("Idle.")
        form.addRow(self.lbl_progress)

        # Presets
        self.preset_box = QGroupBox("Presets")
        pv = QVBoxLayout(self.preset_box)
        self.combo_preset = NoWheelComboBox()
        self.combo_preset.setEditable(True)
        pv.addWidget(self.combo_preset)
        prow = QHBoxLayout()
        self.btn_save_preset = QPushButton("Save Preset")
        self.btn_load_preset = QPushButton("Load Preset")
        prow.addWidget(self.btn_save_preset)
        prow.addWidget(self.btn_load_preset)
        pv.addLayout(prow)

        outer = QVBoxLayout(self)
        outer.addWidget(box)
        if show_presets:
            outer.addWidget(self.preset_box)
        outer.setContentsMargins(0, 0, 0, 0)

        # Wire signals
        self.btn_browse.clicked.connect(self._browse)
        self.check_max_frames.toggled.connect(self.spin_max_frames.setEnabled)
        self.check_max_seconds.toggled.connect(self.spin_max_seconds.setEnabled)
        self.btn_snapshot.clicked.connect(self.snapshot_clicked.emit)
        self.btn_start.clicked.connect(self._emit_start)
        self.btn_stop.clicked.connect(self.stop_recording.emit)
        self.btn_save_preset.clicked.connect(
            lambda: self.save_preset.emit(self.combo_preset.currentText().strip() or "preset")
        )
        self.btn_load_preset.clicked.connect(
            lambda: self.load_preset.emit(self.combo_preset.currentText().strip())
        )

    # --- UI plumbing -----------------------------------------------------

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "Select output folder",
                                             self.edit_out_dir.text())
        if d:
            self.edit_out_dir.setText(d)

    def _emit_start(self):
        job = {
            "output_dir": self.edit_out_dir.text().strip() or os.path.abspath("recordings"),
            "file_prefix": self.edit_prefix.text().strip() or "rec",
            "save_format": self.combo_format.currentData(),
            "max_frames": self.spin_max_frames.value() if self.check_max_frames.isChecked() else None,
            "max_seconds": self.spin_max_seconds.value() if self.check_max_seconds.isChecked() else None,
            "user_note": self.edit_note.text(),
        }
        self.start_recording.emit(job)

    def set_recording_state(self, recording: bool):
        self.btn_start.setEnabled(not recording)
        self.btn_stop.setEnabled(recording)
        if self._show_snapshot:
            self.btn_snapshot.setEnabled(not recording)
        for w in (self.edit_out_dir, self.edit_prefix, self.combo_format,
                  self.btn_browse, self.check_max_frames, self.check_max_seconds,
                  self.spin_max_frames, self.spin_max_seconds):
            w.setEnabled(not recording)
        # respect checkbox enable state
        if not recording:
            self.spin_max_frames.setEnabled(self.check_max_frames.isChecked())
            self.spin_max_seconds.setEnabled(self.check_max_seconds.isChecked())

    def set_progress(self, frames: int, elapsed_s: float, queue_len: int,
                     dropped: int, recording_fps: float):
        warn = ""
        if queue_len >= 64:
            warn = "  ⚠ disk queue large"
        if dropped > 0:
            warn += f"  ⚠ dropped={dropped}"
        self.lbl_progress.setText(
            f"Frames: {frames}  |  {elapsed_s:.1f} s  |  "
            f"rec fps: {recording_fps:.1f}  |  queue: {queue_len}"
            + warn
        )

    def set_presets(self, names):
        if not self._show_presets:
            return
        self.combo_preset.blockSignals(True)
        cur = self.combo_preset.currentText()
        self.combo_preset.clear()
        for n in names:
            self.combo_preset.addItem(n)
        if cur:
            self.combo_preset.setEditText(cur)
        self.combo_preset.blockSignals(False)

    def set_enabled_all(self, enabled: bool):
        # Used when no camera is connected
        if self._show_snapshot:
            self.btn_snapshot.setEnabled(enabled)
        self.btn_start.setEnabled(enabled)
        self.btn_stop.setEnabled(False)
