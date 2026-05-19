"""Right-hand panel: camera connection + exposure/gain/pixel-format + fps + trigger."""
from __future__ import annotations

from typing import Callable, List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..settings import DEFAULT_TARGET_FPS
from ..utils import CameraInfo
from .widgets import NoWheelComboBox


class CameraConnectionPanel(QWidget):
    refresh_clicked = Signal()
    connect_clicked = Signal(object)        # CameraInfo
    disconnect_clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._infos: List[CameraInfo] = []

        layout = QVBoxLayout(self)
        box = QGroupBox("Camera connection")
        v = QVBoxLayout(box)

        self.combo = NoWheelComboBox()
        self.combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        v.addWidget(self.combo)

        row = QHBoxLayout()
        self.btn_refresh = QPushButton("Refresh")
        self.btn_connect = QPushButton("Connect")
        self.btn_disconnect = QPushButton("Disconnect")
        self.btn_disconnect.setEnabled(False)
        row.addWidget(self.btn_refresh)
        row.addWidget(self.btn_connect)
        row.addWidget(self.btn_disconnect)
        v.addLayout(row)

        self.info_label = QLabel("(no camera selected)")
        self.info_label.setWordWrap(True)
        v.addWidget(self.info_label)

        layout.addWidget(box)
        layout.addStretch(1)

        self.btn_refresh.clicked.connect(self.refresh_clicked.emit)
        self.btn_connect.clicked.connect(self._emit_connect)
        self.btn_disconnect.clicked.connect(self.disconnect_clicked.emit)
        self.combo.currentIndexChanged.connect(self._update_info_label)

    def set_cameras(self, infos: List[CameraInfo]) -> None:
        self._infos = infos
        self.combo.blockSignals(True)
        self.combo.clear()
        for i in infos:
            self.combo.addItem(i.short())
        self.combo.blockSignals(False)
        # Auto-select if only one (counting real first, then dummy if alone)
        real = [i for i in infos if not i.is_dummy]
        if len(real) == 1:
            self.combo.setCurrentIndex(infos.index(real[0]))
        elif len(infos) == 1:
            self.combo.setCurrentIndex(0)
        self._update_info_label()

    def _selected(self) -> Optional[CameraInfo]:
        idx = self.combo.currentIndex()
        if 0 <= idx < len(self._infos):
            return self._infos[idx]
        return None

    def selected_info(self) -> Optional[CameraInfo]:
        return self._selected()

    def select_camera_by_serial(self, serial_number: str) -> bool:
        for idx, info in enumerate(self._infos):
            if info.serial_number == serial_number:
                self.combo.setCurrentIndex(idx)
                return True
        return False

    def _update_info_label(self):
        info = self._selected()
        if info is None:
            self.info_label.setText("(no camera selected)")
            return
        self.info_label.setText(
            f"Model: {info.model_name}\n"
            f"Serial: {info.serial_number}\n"
            f"Class: {info.device_class}"
            + ("  (dummy)" if info.is_dummy else "")
        )

    def _emit_connect(self):
        info = self._selected()
        if info is not None:
            self.connect_clicked.emit(info)

    def set_connected_state(self, connected: bool):
        self.btn_connect.setEnabled(not connected)
        self.btn_disconnect.setEnabled(connected)
        self.btn_refresh.setEnabled(not connected)
        self.combo.setEnabled(not connected)


class ExposureGainPanel(QWidget):
    exposure_changed = Signal(float)        # µs
    gain_changed = Signal(float)            # dB
    # kind in {'exposure', 'gain', 'white_balance'}, value in {'Off','Once','Continuous'}
    auto_changed = Signal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        box = QGroupBox("Exposure / Gain")
        form = QFormLayout(box)

        # --- Exposure row ---
        self.spin_exposure = QDoubleSpinBox()
        self.spin_exposure.setRange(1.0, 10_000_000.0)
        self.spin_exposure.setDecimals(1)
        self.spin_exposure.setSingleStep(100.0)
        self.spin_exposure.setSuffix(" µs")
        self.spin_exposure.setKeyboardTracking(False)

        self.combo_auto_exp = NoWheelComboBox()
        self.combo_auto_exp.addItems(["Off", "Once", "Continuous"])
        self.combo_auto_exp.setToolTip("ExposureAuto: Once = run once and freeze; "
                                       "Continuous = keep adjusting")

        exp_row = QHBoxLayout()
        exp_row.setContentsMargins(0, 0, 0, 0)
        exp_row.addWidget(self.spin_exposure, 1)
        exp_row.addWidget(QLabel("Auto:"))
        exp_row.addWidget(self.combo_auto_exp)
        exp_wrap = QWidget()
        exp_wrap.setLayout(exp_row)
        form.addRow("Exposure", exp_wrap)

        # --- Gain row ---
        self.spin_gain = QDoubleSpinBox()
        self.spin_gain.setRange(0.0, 60.0)
        self.spin_gain.setDecimals(2)
        self.spin_gain.setSingleStep(0.5)
        self.spin_gain.setSuffix(" dB")
        self.spin_gain.setKeyboardTracking(False)

        self.combo_auto_gain = NoWheelComboBox()
        self.combo_auto_gain.addItems(["Off", "Once", "Continuous"])
        self.combo_auto_gain.setToolTip("GainAuto: Once or Continuous")

        gain_row = QHBoxLayout()
        gain_row.setContentsMargins(0, 0, 0, 0)
        gain_row.addWidget(self.spin_gain, 1)
        gain_row.addWidget(QLabel("Auto:"))
        gain_row.addWidget(self.combo_auto_gain)
        gain_wrap = QWidget()
        gain_wrap.setLayout(gain_row)
        form.addRow("Gain", gain_wrap)

        # --- White balance (color cameras only) ---
        self.combo_auto_wb = NoWheelComboBox()
        self.combo_auto_wb.addItems(["Off", "Once", "Continuous"])
        self.combo_auto_wb.setToolTip("BalanceWhiteAuto: color cameras only")
        self.lbl_wb = QLabel("Auto white balance")
        form.addRow(self.lbl_wb, self.combo_auto_wb)
        # Will be hidden if the camera does not expose BalanceWhiteAuto
        self.set_white_balance_visible(False)

        outer = QVBoxLayout(self)
        outer.addWidget(box)
        outer.setContentsMargins(0, 0, 0, 0)

        self.spin_exposure.valueChanged.connect(self.exposure_changed.emit)
        self.spin_gain.valueChanged.connect(self.gain_changed.emit)
        self.combo_auto_exp.currentTextChanged.connect(
            lambda v: self.auto_changed.emit("exposure", v))
        self.combo_auto_gain.currentTextChanged.connect(
            lambda v: self.auto_changed.emit("gain", v))
        self.combo_auto_wb.currentTextChanged.connect(
            lambda v: self.auto_changed.emit("white_balance", v))

    def set_exposure_limits(self, mn: Optional[float], mx: Optional[float], inc: Optional[float]):
        if mn is not None and mx is not None:
            self.spin_exposure.setRange(float(mn), float(mx))
        if inc:
            self.spin_exposure.setSingleStep(max(1.0, float(inc)))

    def set_gain_limits(self, mn: Optional[float], mx: Optional[float], inc: Optional[float]):
        if mn is not None and mx is not None:
            self.spin_gain.setRange(float(mn), float(mx))
        if inc:
            self.spin_gain.setSingleStep(max(0.01, float(inc)))

    def set_values(self, exposure_us: Optional[float], gain: Optional[float]):
        if exposure_us is not None:
            self.spin_exposure.blockSignals(True)
            self.spin_exposure.setValue(float(exposure_us))
            self.spin_exposure.blockSignals(False)
        if gain is not None:
            self.spin_gain.blockSignals(True)
            self.spin_gain.setValue(float(gain))
            self.spin_gain.blockSignals(False)

    def set_enabled_all(self, enabled: bool):
        self.spin_exposure.setEnabled(enabled)
        self.spin_gain.setEnabled(enabled)
        self.combo_auto_exp.setEnabled(enabled)
        self.combo_auto_gain.setEnabled(enabled)
        self.combo_auto_wb.setEnabled(enabled)

    def set_white_balance_visible(self, visible: bool):
        self.lbl_wb.setVisible(visible)
        self.combo_auto_wb.setVisible(visible)

    def set_auto_options(self, kind: str, options: List[str], current: Optional[str]):
        """Restrict the combo to the entries the camera actually supports."""
        combo = {
            "exposure":      self.combo_auto_exp,
            "gain":          self.combo_auto_gain,
            "white_balance": self.combo_auto_wb,
        }[kind]
        combo.blockSignals(True)
        combo.clear()
        for o in options or ["Off"]:
            combo.addItem(o)
        if current and current in (options or []):
            combo.setCurrentText(current)
        combo.blockSignals(False)

    def set_auto_value(self, kind: str, value: Optional[str]):
        """Update the combo display without triggering a write back to camera."""
        if value is None:
            return
        combo = {
            "exposure":      self.combo_auto_exp,
            "gain":          self.combo_auto_gain,
            "white_balance": self.combo_auto_wb,
        }[kind]
        combo.blockSignals(True)
        if combo.findText(value) >= 0:
            combo.setCurrentText(value)
        combo.blockSignals(False)


class PixelFormatFpsPanel(QWidget):
    pixel_format_changed = Signal(str)
    target_fps_changed = Signal(float)
    fps_enable_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        box = QGroupBox("Pixel format / Frame rate")
        form = QFormLayout(box)

        self.combo_pf = NoWheelComboBox()

        self.check_fps_enable = QCheckBox("Enable target fps")
        self.check_fps_enable.setChecked(True)

        self.spin_fps = QDoubleSpinBox()
        self.spin_fps.setRange(0.1, 10_000.0)
        self.spin_fps.setDecimals(2)
        self.spin_fps.setSingleStep(1.0)
        self.spin_fps.setKeyboardTracking(False)
        self.spin_fps.setValue(DEFAULT_TARGET_FPS)

        self.lbl_resulting = QLabel("Resulting max fps: N/A")
        self.lbl_data_rate = QLabel("Data rate: N/A")

        form.addRow("Pixel format", self.combo_pf)
        form.addRow(self.check_fps_enable)
        form.addRow("Target fps", self.spin_fps)
        form.addRow(self.lbl_resulting)
        form.addRow(self.lbl_data_rate)

        outer = QVBoxLayout(self)
        outer.addWidget(box)
        outer.setContentsMargins(0, 0, 0, 0)

        self.combo_pf.currentTextChanged.connect(self.pixel_format_changed.emit)
        self.spin_fps.valueChanged.connect(self.target_fps_changed.emit)
        self.check_fps_enable.toggled.connect(self.fps_enable_changed.emit)

    def set_pixel_formats(self, options: List[str], current: Optional[str]):
        self.combo_pf.blockSignals(True)
        self.combo_pf.clear()
        for o in options:
            self.combo_pf.addItem(o)
        if current and current in options:
            self.combo_pf.setCurrentText(current)
        self.combo_pf.blockSignals(False)

    def set_fps_limits(self, mn: Optional[float], mx: Optional[float]):
        if mn is not None and mx is not None:
            self.spin_fps.setRange(float(mn), float(mx))

    def set_fps(self, fps: Optional[float], enabled: Optional[bool] = None):
        if fps is not None:
            self.spin_fps.blockSignals(True)
            self.spin_fps.setValue(float(fps))
            self.spin_fps.blockSignals(False)
        if enabled is not None:
            self.check_fps_enable.blockSignals(True)
            self.check_fps_enable.setChecked(enabled)
            self.check_fps_enable.blockSignals(False)

    def set_resulting_fps(self, fps: Optional[float]):
        if fps is None:
            self.lbl_resulting.setText("Resulting max fps: N/A")
        else:
            self.lbl_resulting.setText(f"Resulting max fps: {fps:.2f}")

    def set_data_rate(self, mb_s: Optional[float]):
        if mb_s is None or mb_s <= 0:
            self.lbl_data_rate.setText("Data rate: N/A")
        else:
            self.lbl_data_rate.setText(f"Data rate: {mb_s:.1f} MB/s")

    def set_enabled_all(self, enabled: bool):
        self.combo_pf.setEnabled(enabled)
        self.spin_fps.setEnabled(enabled)
        self.check_fps_enable.setEnabled(enabled)


class TriggerPanel(QWidget):
    trigger_changed = Signal(str, str, str)   # mode, source, activation
    software_trigger_clicked = Signal()       # user fired a software pulse

    LINE1_HINT = (
        "Line1: wire an external 5 V/24 V signal between the camera's I/O\n"
        "Line1 input and its GND. Polarity is set by Trigger activation.\n"
        "Frames arrive automatically on each edge; no software pulse needed."
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        box = QGroupBox("Trigger")
        form = QFormLayout(box)

        self.combo_mode = NoWheelComboBox()
        self.combo_mode.addItems(["Off", "On"])
        self.combo_source = NoWheelComboBox()
        self.combo_source.addItems(["Software", "Line1", "Line2"])
        self.combo_activation = NoWheelComboBox()
        self.combo_activation.addItems(["RisingEdge", "FallingEdge"])

        form.addRow("Trigger mode", self.combo_mode)
        form.addRow("Trigger source", self.combo_source)
        form.addRow("Trigger activation", self.combo_activation)

        # Action row: send a software pulse + optional hint label
        action_row = QHBoxLayout()
        self.btn_software_trigger = QPushButton("Send Software Trigger")
        self.btn_software_trigger.setToolTip(
            "Execute TriggerSoftware once. "
            "Only meaningful when Trigger mode=On and Source=Software."
        )
        self.btn_software_trigger.setEnabled(False)
        action_row.addWidget(self.btn_software_trigger)
        action_row.addStretch(1)
        form.addRow(action_row)

        self.lbl_hint = QLabel("")
        self.lbl_hint.setWordWrap(True)
        self.lbl_hint.setStyleSheet("color: #666;")
        form.addRow(self.lbl_hint)

        outer = QVBoxLayout(self)
        outer.addWidget(box)
        outer.setContentsMargins(0, 0, 0, 0)

        for c in (self.combo_mode, self.combo_source, self.combo_activation):
            c.currentTextChanged.connect(self._emit)
        self.combo_mode.currentTextChanged.connect(self._update_hint)
        self.combo_source.currentTextChanged.connect(self._update_hint)
        self.btn_software_trigger.clicked.connect(self.software_trigger_clicked.emit)
        self._update_hint()

    def _emit(self, _=None):
        self.trigger_changed.emit(
            self.combo_mode.currentText(),
            self.combo_source.currentText(),
            self.combo_activation.currentText(),
        )

    def _update_hint(self, _=None):
        mode = self.combo_mode.currentText()
        source = self.combo_source.currentText()
        if mode == "Off":
            self.lbl_hint.setText("Trigger mode Off → camera runs freely at target fps.")
            self.btn_software_trigger.setEnabled(False)
            return
        if source == "Software":
            self.lbl_hint.setText(
                "Software trigger: each frame is captured when you click\n"
                "'Send Software Trigger' (or the camera's API receives one)."
            )
            self.btn_software_trigger.setEnabled(self.combo_mode.isEnabled())
            return
        if source.startswith("Line"):
            self.lbl_hint.setText(self.LINE1_HINT.replace("Line1", source))
            self.btn_software_trigger.setEnabled(False)
            return
        self.lbl_hint.setText("")
        self.btn_software_trigger.setEnabled(False)

    def populate(self, modes, sources, activations, current_mode, current_source, current_act):
        def fill(combo, items, current):
            combo.blockSignals(True)
            combo.clear()
            for i in items or []:
                combo.addItem(i)
            if current and current in (items or []):
                combo.setCurrentText(current)
            combo.blockSignals(False)
        fill(self.combo_mode, modes, current_mode)
        fill(self.combo_source, sources, current_source)
        fill(self.combo_activation, activations, current_act)
        self._update_hint()

    def set_software_trigger_supported(self, supported: bool) -> None:
        """Hide / show the pulse button depending on camera capability."""
        self.btn_software_trigger.setVisible(supported)

    def set_enabled_all(self, enabled: bool):
        for c in (self.combo_mode, self.combo_source, self.combo_activation):
            c.setEnabled(enabled)
        if not enabled:
            self.btn_software_trigger.setEnabled(False)
        else:
            self._update_hint()
