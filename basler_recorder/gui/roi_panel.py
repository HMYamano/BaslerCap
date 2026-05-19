"""ROI (Width / Height / Offsets + Center / Full Frame + drag-rectangle)."""
from __future__ import annotations

from typing import Optional, Tuple

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)


class ROIPanel(QWidget):
    roi_apply = Signal(int, int, int, int)   # width, height, offset_x, offset_y
    center_clicked = Signal()
    full_frame_clicked = Signal()

    # Toggle the drag-rectangle overlay on the preview; the main window owns
    # the rectangle widget and listens to this signal.
    draw_roi_toggled = Signal(bool)
    # Read the rectangle bounds and apply them as the camera ROI.
    apply_from_selection = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        box = QGroupBox("ROI")
        form = QFormLayout(box)

        self.sp_w = QSpinBox()
        self.sp_h = QSpinBox()
        self.sp_ox = QSpinBox()
        self.sp_oy = QSpinBox()
        for sp in (self.sp_w, self.sp_h, self.sp_ox, self.sp_oy):
            sp.setRange(0, 100_000)
            sp.setSingleStep(1)
            sp.setKeyboardTracking(False)

        form.addRow("Width", self.sp_w)
        form.addRow("Height", self.sp_h)
        form.addRow("Offset X", self.sp_ox)
        form.addRow("Offset Y", self.sp_oy)

        row = QHBoxLayout()
        self.btn_apply = QPushButton("Apply ROI")
        self.btn_center = QPushButton("Center ROI")
        self.btn_full = QPushButton("Full Frame")
        row.addWidget(self.btn_apply)
        row.addWidget(self.btn_center)
        row.addWidget(self.btn_full)
        form.addRow(row)

        # Drag-rectangle row
        row2 = QHBoxLayout()
        self.btn_draw_roi = QPushButton("Draw ROI on Image")
        self.btn_draw_roi.setCheckable(True)
        self.btn_draw_roi.setToolTip(
            "Show a draggable rectangle on the preview. "
            "Resize/move it, then click 'Apply Selection' to set the camera ROI."
        )
        self.btn_apply_selection = QPushButton("Apply Selection")
        self.btn_apply_selection.setEnabled(False)
        row2.addWidget(self.btn_draw_roi)
        row2.addWidget(self.btn_apply_selection)
        form.addRow(row2)

        self.lbl_actual = QLabel("Actual: -")
        form.addRow(self.lbl_actual)

        outer = QVBoxLayout(self)
        outer.addWidget(box)
        outer.setContentsMargins(0, 0, 0, 0)

        self.btn_apply.clicked.connect(self._emit_apply)
        self.btn_center.clicked.connect(self.center_clicked.emit)
        self.btn_full.clicked.connect(self.full_frame_clicked.emit)
        self.btn_draw_roi.toggled.connect(self._on_draw_toggled)
        self.btn_apply_selection.clicked.connect(self.apply_from_selection.emit)

    def _on_draw_toggled(self, checked: bool) -> None:
        self.btn_apply_selection.setEnabled(checked)
        self.draw_roi_toggled.emit(checked)

    def set_draw_roi_checked(self, checked: bool) -> None:
        """Programmatically clear the toggle (e.g. after the main window
        retracts the rectangle because no preview is available)."""
        self.btn_draw_roi.blockSignals(True)
        self.btn_draw_roi.setChecked(checked)
        self.btn_draw_roi.blockSignals(False)
        self.btn_apply_selection.setEnabled(checked)

    def _emit_apply(self):
        self.roi_apply.emit(
            self.sp_w.value(),
            self.sp_h.value(),
            self.sp_ox.value(),
            self.sp_oy.value(),
        )

    def set_limits(self, w_min, w_max, w_inc,
                   h_min, h_max, h_inc,
                   ox_min, ox_max, ox_inc,
                   oy_min, oy_max, oy_inc):
        def apply(sp, mn, mx, inc):
            if mn is not None and mx is not None:
                sp.setRange(int(mn), int(mx))
            if inc:
                sp.setSingleStep(max(1, int(inc)))
        apply(self.sp_w, w_min, w_max, w_inc)
        apply(self.sp_h, h_min, h_max, h_inc)
        apply(self.sp_ox, ox_min, ox_max, ox_inc)
        apply(self.sp_oy, oy_min, oy_max, oy_inc)

    def set_values(self, w: int, h: int, ox: int, oy: int):
        for sp, v in ((self.sp_w, w), (self.sp_h, h),
                      (self.sp_ox, ox), (self.sp_oy, oy)):
            sp.blockSignals(True)
            sp.setValue(int(v))
            sp.blockSignals(False)
        self.lbl_actual.setText(f"Actual: {w} x {h} @ ({ox},{oy})")

    def set_enabled_all(self, enabled: bool):
        for w in (self.sp_w, self.sp_h, self.sp_ox, self.sp_oy,
                  self.btn_apply, self.btn_center, self.btn_full,
                  self.btn_draw_roi):
            w.setEnabled(enabled)
        # Apply Selection only makes sense if Draw ROI is toggled on
        self.btn_apply_selection.setEnabled(enabled and self.btn_draw_roi.isChecked())
