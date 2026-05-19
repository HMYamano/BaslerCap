"""Bottom status bar widget."""
from __future__ import annotations

from PySide6.QtWidgets import QLabel, QWidget, QHBoxLayout, QFrame


class StatusPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        h = QHBoxLayout(self)
        h.setContentsMargins(4, 2, 4, 2)

        self.lbl_camera = QLabel("Camera: disconnected")
        self.lbl_preview = QLabel("Preview: -")
        self.lbl_grab = QLabel("Grab: -")
        self.lbl_record = QLabel("Recording: idle")
        self.lbl_dropped = QLabel("Dropped: 0")
        self.lbl_queue = QLabel("Queue: 0")
        self.lbl_resulting = QLabel("Max fps: N/A")
        self.lbl_rate = QLabel("Rate: N/A")

        for w in (self.lbl_camera, self.lbl_preview, self.lbl_grab,
                  self.lbl_record, self.lbl_dropped, self.lbl_queue,
                  self.lbl_resulting, self.lbl_rate):
            h.addWidget(w)
            h.addWidget(_sep())

        h.addStretch(1)

    def set_camera(self, text: str):
        self.lbl_camera.setText(f"Camera: {text}")

    def set_preview_fps(self, fps: float):
        self.lbl_preview.setText(f"Preview: {fps:.1f}")

    def set_grab_fps(self, fps: float):
        self.lbl_grab.setText(f"Grab: {fps:.1f}")

    def set_recording(self, text: str):
        self.lbl_record.setText(f"Recording: {text}")

    def set_dropped(self, n: int):
        self.lbl_dropped.setText(f"Dropped: {n}")

    def set_queue(self, n: int):
        self.lbl_queue.setText(f"Queue: {n}")

    def set_resulting_max_fps(self, fps):
        if fps is None:
            self.lbl_resulting.setText("Max fps: N/A")
        else:
            self.lbl_resulting.setText(f"Max fps: {fps:.1f}")

    def set_data_rate(self, mb_s):
        if mb_s is None or mb_s <= 0:
            self.lbl_rate.setText("Rate: N/A")
        else:
            self.lbl_rate.setText(f"Rate: {mb_s:.1f} MB/s")


def _sep() -> QFrame:
    f = QFrame()
    f.setFrameShape(QFrame.VLine)
    f.setFrameShadow(QFrame.Sunken)
    return f
