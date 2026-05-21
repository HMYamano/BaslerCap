"""Main window — ties controllers, workers and panels together."""
from __future__ import annotations

import logging
import json
import math
import os
import re
import time
from queue import Empty
from typing import Optional

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import APP_NAME, APP_VERSION
from ..camera_controller import CameraController, enumerate_cameras, pypylon_import_error
from ..camera_worker import CameraWorker, FrameInfo
from ..metadata import build_metadata, save_metadata_json
from ..recorder_worker import RecorderJob, RecorderManager
from ..settings import (
    CameraPreset,
    DEFAULT_EXPOSURE_TIME_US,
    DEFAULT_GAIN_DB,
    DEFAULT_TARGET_FPS,
    list_presets,
    load_last_session_state,
    load_preset,
    save_last_session_state,
    save_preset,
)
from ..utils import (
    CameraInfo,
    autoscale_for_display,
    bayer_to_rgb,
    bytes_per_pixel,
    estimated_data_rate_mb_s,
    numpy_dtype_for_pixel_format,
    ycbcr422_to_rgb,
)
from .camera_panel import (
    CameraConnectionPanel,
    ExposureGainPanel,
    PixelFormatFpsPanel,
    TriggerPanel,
)
from .recording_panel import RecordingPanel
from .roi_panel import ROIPanel
from .status_panel import StatusPanel

log = logging.getLogger(__name__)

DISPLAY_LEVELS_8BIT = (0, 255)


def _nominal_max_for_display(pixel_format: str, dtype: np.dtype) -> Optional[float]:
    """Return the nominal sensor max value for fixed-range preview scaling."""
    if np.dtype(dtype) == np.uint8:
        return 255.0

    pf = pixel_format or ""
    match = re.search(r"(?:^|[A-Za-z_])(?:Mono|Bayer|RGB|BGR|YUV|YCbCr)[A-Za-z_]*?(8|10|12|16)", pf)
    if match:
        bits = int(match.group(1))
        return float((1 << bits) - 1)

    if np.issubdtype(np.dtype(dtype), np.integer):
        return float(np.iinfo(dtype).max)
    return None


def _to_uint8_for_display(frame: np.ndarray, pixel_format: str) -> np.ndarray:
    """Convert camera data to uint8 without stretching dark noise to white."""
    if frame.dtype == np.uint8:
        return frame

    max_value = _nominal_max_for_display(pixel_format, frame.dtype)
    if max_value is None or max_value <= 0:
        return autoscale_for_display(frame)

    scaled = np.clip(frame.astype(np.float32, copy=False), 0.0, max_value)
    scaled *= 255.0 / max_value
    return scaled.astype(np.uint8)


def _prepare_for_display(frame: np.ndarray, pixel_format: str):
    """Return a numpy array shaped for ``pyqtgraph.ImageView.setImage``.

    pyqtgraph expects ``(cols, rows)`` for grayscale and ``(cols, rows, ch)``
    for color, so we transpose the H/W axes of pypylon's ``(H, W[, C])``.

    Color handling:
      - **Mono8 / Mono10 / Mono12 / Mono16** → grayscale (auto-scaled if >8 bit)
      - **YCbCr422_8 / YUV422_8** → full-color RGB via BT.601 decode
      - **Bayer\\***                → debayered RGB at half resolution
      - **RGB8 / BGR8**             → passed through (BGR swapped to RGB)
    """
    if frame is None or frame.size == 0:
        return None

    pf = pixel_format or ""

    # ----- packed YCbCr / YUV 422 → RGB ------------------------------------
    if "YCbCr" in pf or "YUV" in pf:
        rgb = ycbcr422_to_rgb(frame)
        return rgb.transpose(1, 0, 2)

    # ----- Bayer → debayered RGB (half resolution) -------------------------
    if pf.startswith("Bayer") and len(pf) >= 7:
        # "BayerRG8" → "RG"; "BayerBG12" → "BG", etc.
        pattern = pf[5:7]
        if pattern.upper() in ("RG", "GR", "BG", "GB"):
            rgb = bayer_to_rgb(_to_uint8_for_display(frame, pf), pattern)
            return rgb.transpose(1, 0, 2)

    # ----- 3D color (RGB / BGR) --------------------------------------------
    if frame.ndim == 3:
        if frame.shape[0] in (3, 4) and frame.shape[2] not in (3, 4):
            # Some packed/planar formats may arrive as (C, H, W).
            frame = np.moveaxis(frame[:3], 0, -1)
        c = frame.shape[2]
        if c >= 3 and ("RGB" in pf or "BGR" in pf):
            rgb = _to_uint8_for_display(frame[..., :3], pf)
            if "BGR" in pf:
                rgb = rgb[..., ::-1]
            return rgb.transpose(1, 0, 2)
        # Unknown 3D format → first channel as grayscale
        first = frame[..., 0]
        return _to_uint8_for_display(first, pf).T

    # ----- packed RGB / BGR rows -------------------------------------------
    if frame.ndim == 2 and ("RGB" in pf or "BGR" in pf) and frame.shape[1] % 3 == 0:
        rgb = frame.reshape(frame.shape[0], frame.shape[1] // 3, 3)
        rgb = _to_uint8_for_display(rgb, pf)
        if "BGR" in pf:
            rgb = rgb[..., ::-1]
        return rgb.transpose(1, 0, 2)

    # ----- 2D mono ---------------------------------------------------------
    disp = _to_uint8_for_display(frame, pf)
    return disp.T


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} v{APP_VERSION}")
        self.resize(1280, 820)

        self.controller = CameraController()
        self.worker: Optional[CameraWorker] = None
        self.recorder = RecorderManager(max_queue=1024)
        self._recording_started_at: Optional[float] = None
        self._recording_meta: dict = {}
        self._preview_needs_autorange = True
        self._last_preview_shape: Optional[tuple[int, ...]] = None
        self._last_hist_update = 0.0
        self._hist_update_period = 0.2  # 5 Hz max
        self._hist_saturation_threshold = 0.01  # warn when >1% pixels at max
        self._last_session_state = self._load_last_session_state()
        self._last_session_camera_settings_applied = False

        self._build_ui()
        self._restore_session_ui_state()
        self._wire_signals()
        self._refresh_cameras()

        # poll recorder status ~5 Hz
        self._recorder_timer = QTimer(self)
        self._recorder_timer.timeout.connect(self._poll_recorder_status)
        self._recorder_timer.start(200)

        # also tick recording UI even if recorder is silent
        self._ui_timer = QTimer(self)
        self._ui_timer.timeout.connect(self._tick_ui)
        self._ui_timer.start(250)

        if not pypylon_import_error() == "" and pypylon_import_error():
            self._status_message(
                f"pypylon not available ({pypylon_import_error()}). "
                "Dummy camera mode only.",
                warning=True,
            )

    # =========================================================== build UI ==

    def _build_ui(self):
        # ---- Left: live preview --------------------------------------------------
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(4, 4, 4, 4)

        ctl_row = QHBoxLayout()
        self.btn_start_live = QPushButton("Start Live")
        self.btn_stop_live = QPushButton("Stop Live")
        self.btn_stop_live.setEnabled(False)
        self.btn_fit_view = QPushButton("Fit View")
        self.btn_fit_view.setToolTip("Re-center and fit the preview image to the view")
        ctl_row.addWidget(self.btn_start_live)
        ctl_row.addWidget(self.btn_stop_live)
        ctl_row.addWidget(self.btn_fit_view)
        ctl_row.addStretch(1)
        lv.addLayout(ctl_row)

        self.image_view = pg.ImageView(view=pg.PlotItem())
        self.image_view.ui.histogram.hide()
        self.image_view.ui.roiBtn.hide()
        self.image_view.ui.menuBtn.hide()
        self.image_view.setLevels(*DISPLAY_LEVELS_8BIT)
        self.image_view.getView().setAspectLocked(True)
        self.image_view.getView().invertY(True)
        lv.addWidget(self.image_view, stretch=1)

        # ---- Histogram + saturation strip ----
        self.hist_plot = pg.PlotWidget()
        self.hist_plot.setBackground(None)
        self.hist_plot.setFixedHeight(110)
        self.hist_plot.setMouseEnabled(x=False, y=False)
        self.hist_plot.hideButtons()
        self.hist_plot.setMenuEnabled(False)
        self.hist_plot.setLabel("bottom", "Pixel value")
        self.hist_plot.setLabel("left", "Count (log)")
        self.hist_plot.getPlotItem().getAxis("left").setStyle(showValues=False)
        self.hist_plot.setLogMode(x=False, y=True)
        # The histogram bars live in a single BarGraphItem we keep around
        # and update in place, so we don't churn QGraphicsItems each frame.
        self.hist_bars = pg.PlotCurveItem(
            fillLevel=0,
            brush=(120, 170, 220, 200),
            pen=pg.mkPen((40, 80, 140), width=1),
        )
        self.hist_plot.addItem(self.hist_bars)
        # Saturation vertical line at the bit-depth max
        self.sat_line = pg.InfiniteLine(angle=90, pen=pg.mkPen("r", width=1, style=Qt.DashLine))
        self.hist_plot.addItem(self.sat_line)
        lv.addWidget(self.hist_plot)

        self.lbl_saturation = QLabel("Saturation: -")
        self.lbl_saturation.setStyleSheet("color: #444;")
        lv.addWidget(self.lbl_saturation)

        # ---- Draggable ROI overlay (hidden by default) ----
        self.roi_rect = pg.RectROI(
            [0, 0], [100, 100],
            pen=pg.mkPen("y", width=2),
            hoverPen=pg.mkPen("y", width=3),
            handlePen=pg.mkPen("y", width=2),
            movable=True,
            resizable=True,
            rotatable=False,
        )
        self.roi_rect.addScaleHandle([0, 0], [1, 1])  # top-left corner handle
        self.roi_rect.addScaleHandle([0, 1], [1, 0])  # top-right
        self.roi_rect.addScaleHandle([1, 0], [0, 1])  # bottom-left
        self.image_view.getView().addItem(self.roi_rect)
        self.roi_rect.hide()

        # ---- Right: control panels ------------------------------------------------
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(4, 4, 4, 4)
        rv.setSpacing(6)

        self.panel_conn = CameraConnectionPanel()
        self.panel_exposure = ExposureGainPanel()
        self.panel_roi = ROIPanel()
        self.panel_pf = PixelFormatFpsPanel()
        self.panel_recording = RecordingPanel()
        self.panel_trigger = TriggerPanel()
        self.panel_trigger_recording = RecordingPanel(
            title="Trigger recording",
            default_prefix="trigger",
            # Default to raw because triggered acquisition is typically high
            # rate (microscope-sync, 1 kHz+); MJPG/MP4 encode is CPU-bound and
            # the most common cause of dropped frames in that regime.
            default_format="raw",
            show_snapshot=False,
            show_presets=False,
            start_text="Start Trigger Recording",
            stop_text="Stop Trigger Recording",
        )

        self.panel_conn.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        rv.addWidget(self.panel_conn)

        self.settings_tabs = QTabWidget()
        self.settings_tabs.addTab(
            self._make_tab(self.panel_exposure, self.panel_roi, self.panel_pf),
            "Camera parameters",
        )
        self.settings_tabs.addTab(
            self._make_tab(self.panel_recording),
            "Recording settings",
        )
        self.settings_tabs.addTab(
            self._make_tab(self.panel_trigger, self.panel_trigger_recording),
            "External trigger",
        )
        rv.addWidget(self.settings_tabs, 1)

        right.setMinimumWidth(380)
        right.setMaximumWidth(460)

        # Splitter
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        self.setCentralWidget(splitter)

        # ---- Status bar -----------------------------------------------------------
        self.status_panel = StatusPanel()
        self.statusBar().addPermanentWidget(self.status_panel, 1)

        # Disable settings until connected
        self._set_settings_enabled(False)
        self.panel_recording.set_enabled_all(False)
        self.panel_trigger_recording.set_enabled_all(False)

    def _make_tab(self, *widgets: QWidget) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)
        for widget in widgets:
            layout.addWidget(widget)
        layout.addStretch(1)
        return tab

    # =========================================================== wiring ==

    def _wire_signals(self):
        self.panel_conn.refresh_clicked.connect(self._refresh_cameras)
        self.panel_conn.connect_clicked.connect(self._connect_camera)
        self.panel_conn.disconnect_clicked.connect(self._disconnect_camera)

        self.btn_start_live.clicked.connect(self._start_live)
        self.btn_stop_live.clicked.connect(self._stop_live)
        self.btn_fit_view.clicked.connect(self._fit_view)

        self.panel_exposure.exposure_changed.connect(self._on_exposure_changed)
        self.panel_exposure.gain_changed.connect(self._on_gain_changed)
        self.panel_exposure.auto_changed.connect(self._on_auto_changed)

        self.panel_pf.pixel_format_changed.connect(self._on_pixel_format_changed)
        self.panel_pf.target_fps_changed.connect(self._on_target_fps_changed)
        self.panel_pf.fps_enable_changed.connect(self._on_fps_enable_changed)

        self.panel_roi.roi_apply.connect(self._on_roi_apply)
        self.panel_roi.center_clicked.connect(self._on_center_roi)
        self.panel_roi.full_frame_clicked.connect(self._on_full_frame)
        self.panel_roi.draw_roi_toggled.connect(self._on_draw_roi_toggled)
        self.panel_roi.apply_from_selection.connect(self._on_apply_roi_selection)

        self.panel_trigger.trigger_changed.connect(self._on_trigger_changed)
        self.panel_trigger.software_trigger_clicked.connect(self._on_software_trigger)

        self.panel_recording.snapshot_clicked.connect(self._on_snapshot)
        self.panel_recording.start_recording.connect(self._on_start_recording)
        self.panel_recording.stop_recording.connect(self._on_stop_recording)
        self.panel_recording.save_preset.connect(self._on_save_preset)
        self.panel_recording.load_preset.connect(self._on_load_preset_by_name)
        self.panel_trigger_recording.start_recording.connect(self._on_start_trigger_recording)
        self.panel_trigger_recording.stop_recording.connect(self._on_stop_recording)

    # ===================================================== session state ==

    def _load_last_session_state(self) -> dict:
        try:
            return load_last_session_state() or {}
        except Exception:
            log.exception("Failed to load last session settings")
            return {}

    def _restore_session_ui_state(self) -> None:
        state = self._last_session_state
        if not state:
            return
        self._restore_recording_panel_state(
            self.panel_recording,
            state.get("recording", {}),
        )
        self._restore_recording_panel_state(
            self.panel_trigger_recording,
            state.get("trigger_recording", {}),
        )
        tab_index = state.get("selected_tab_index")
        if isinstance(tab_index, int) and 0 <= tab_index < self.settings_tabs.count():
            self.settings_tabs.setCurrentIndex(tab_index)

    def _recording_panel_state(self, panel: RecordingPanel) -> dict:
        return {
            "output_dir": panel.edit_out_dir.text(),
            "file_prefix": panel.edit_prefix.text(),
            "save_format": panel.combo_format.currentData(),
            "limit_by_frames": panel.check_max_frames.isChecked(),
            "max_frames": panel.spin_max_frames.value(),
            "limit_by_seconds": panel.check_max_seconds.isChecked(),
            "max_seconds": panel.spin_max_seconds.value(),
            "user_note": panel.edit_note.text(),
        }

    def _restore_recording_panel_state(self, panel: RecordingPanel, state: dict) -> None:
        if not isinstance(state, dict):
            return

        if state.get("output_dir") is not None:
            panel.edit_out_dir.setText(str(state["output_dir"]))
        if state.get("file_prefix") is not None:
            panel.edit_prefix.setText(str(state["file_prefix"]))

        save_format = state.get("save_format")
        if save_format:
            for i in range(panel.combo_format.count()):
                if panel.combo_format.itemData(i) == save_format:
                    panel.combo_format.setCurrentIndex(i)
                    break

        if state.get("max_frames") is not None:
            try:
                panel.spin_max_frames.setValue(int(state["max_frames"]))
            except (TypeError, ValueError):
                pass
        if state.get("max_seconds") is not None:
            try:
                panel.spin_max_seconds.setValue(float(state["max_seconds"]))
            except (TypeError, ValueError):
                pass
        panel.check_max_frames.setChecked(bool(state.get("limit_by_frames", False)))
        panel.check_max_seconds.setChecked(bool(state.get("limit_by_seconds", False)))

        if state.get("user_note") is not None:
            panel.edit_note.setText(str(state["user_note"]))

    def _collect_camera_settings(self) -> dict:
        c = self.controller
        w, h, ox, oy = c.roi()
        auto = {}
        for kind in ("exposure", "gain", "white_balance"):
            if c.has_auto(kind):
                auto[kind] = c.get_auto(kind)
        return {
            "exposure_time_us": c.exposure_us(),
            "gain": c.gain(),
            "width": w,
            "height": h,
            "offset_x": ox,
            "offset_y": oy,
            "pixel_format": c.pixel_format(),
            "target_fps": c.target_fps(),
            "fps_enable": (
                bool(c.get("AcquisitionFrameRateEnable", True))
                if c.has("AcquisitionFrameRateEnable")
                else None
            ),
            "auto": auto,
            "trigger_mode": c.get("TriggerMode") if c.has("TriggerMode") else None,
            "trigger_source": c.get("TriggerSource") if c.has("TriggerSource") else None,
            "trigger_activation": (
                c.get("TriggerActivation") if c.has("TriggerActivation") else None
            ),
        }

    def _collect_session_state(self) -> dict:
        state = dict(self._last_session_state) if isinstance(self._last_session_state, dict) else {}
        selected_info = self.panel_conn.selected_info()
        selected_serial = selected_info.serial_number if selected_info is not None else None

        state.update({
            "schema_version": 1,
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "selected_tab_index": self.settings_tabs.currentIndex(),
            "selected_camera_serial": selected_serial,
            "recording": self._recording_panel_state(self.panel_recording),
            "trigger_recording": self._recording_panel_state(self.panel_trigger_recording),
        })

        if self.controller.is_open and self.controller.info is not None:
            info = self.controller.info
            state["camera"] = {
                "model_name": info.model_name,
                "serial_number": info.serial_number,
                "device_class": info.device_class,
                "friendly_name": info.friendly_name,
                "is_dummy": info.is_dummy,
            }
            state["camera_settings"] = self._collect_camera_settings()

        return state

    def _save_last_session_state(self) -> None:
        try:
            state = self._collect_session_state()
            path = save_last_session_state(state)
            self._last_session_state = state
            log.info("Last session settings saved: %s", path)
        except Exception:
            log.exception("Failed to save last session settings")

    def _apply_last_session_camera_state(self, info: CameraInfo) -> None:
        if self._last_session_camera_settings_applied:
            return
        state = self._last_session_state
        settings = state.get("camera_settings")
        if not isinstance(settings, dict):
            return

        camera_state = state.get("camera", {})
        saved_serial = camera_state.get("serial_number") if isinstance(camera_state, dict) else None
        if saved_serial and saved_serial != info.serial_number:
            log.info(
                "Skipped last session camera settings for serial %s; current serial is %s",
                saved_serial,
                info.serial_number,
            )
            return

        try:
            self._apply_camera_settings(settings)
        except Exception:
            log.exception("Failed to apply last session camera settings")
            self._status_message("Failed to apply last session camera settings.", warning=True)
            return

        self._last_session_camera_settings_applied = True
        self._status_message("Last session settings restored.")

    def _apply_camera_settings(self, settings: dict) -> None:
        c = self.controller
        was_live = self.worker is not None
        if was_live:
            self._stop_live()
        try:
            auto = settings.get("auto", {})
            if not isinstance(auto, dict):
                auto = {}

            for kind in ("exposure", "gain"):
                final_auto = auto.get(kind)
                if c.has_auto(kind) and final_auto != "Continuous":
                    c.set_auto(kind, final_auto or "Off")

            if settings.get("pixel_format"):
                c.set_pixel_format(str(settings["pixel_format"]))

            width = settings.get("width")
            height = settings.get("height")
            if width and height:
                c.set_roi(
                    int(width),
                    int(height),
                    int(settings.get("offset_x") or 0),
                    int(settings.get("offset_y") or 0),
                )

            if settings.get("exposure_time_us") is not None:
                c.set_exposure_us(float(settings["exposure_time_us"]))
            if settings.get("gain") is not None:
                c.set_gain(float(settings["gain"]))
            if settings.get("target_fps") is not None:
                c.set_target_fps(float(settings["target_fps"]))
            if settings.get("fps_enable") is not None and c.has("AcquisitionFrameRateEnable"):
                c.set("AcquisitionFrameRateEnable", bool(settings["fps_enable"]))

            if settings.get("trigger_mode") and c.has("TriggerMode"):
                c.set_trigger(
                    str(settings["trigger_mode"]),
                    str(settings["trigger_source"]) if settings.get("trigger_source") else None,
                    (
                        str(settings["trigger_activation"])
                        if settings.get("trigger_activation")
                        else None
                    ),
                )

            for kind, value in auto.items():
                if value is not None and c.has_auto(kind):
                    c.set_auto(kind, str(value))
        finally:
            self._refresh_settings_from_camera()
            if was_live:
                self._start_live()

    # =========================================================== cameras ==

    def _refresh_cameras(self):
        infos = enumerate_cameras(include_dummy=True, dummy_count=1)
        self.panel_conn.set_cameras(infos)
        if not self.controller.is_open:
            camera_state = self._last_session_state.get("camera", {})
            serial = self._last_session_state.get("selected_camera_serial")
            if not serial and isinstance(camera_state, dict):
                serial = camera_state.get("serial_number")
            if serial:
                self.panel_conn.select_camera_by_serial(serial)
        self.status_panel.set_camera(
            f"{len(infos)} device(s) found" if infos else "no devices"
        )

    def _connect_camera(self, info: CameraInfo):
        try:
            self.controller.open(info)
        except Exception as e:
            self._error_box("Connect failed", str(e))
            return
        self._apply_default_camera_settings()
        self.panel_conn.set_connected_state(True)
        self.status_panel.set_camera(info.short())
        self._refresh_settings_from_camera()
        self._set_settings_enabled(True)
        self.panel_recording.set_enabled_all(True)
        self.panel_trigger_recording.set_enabled_all(True)
        self._refresh_presets()
        self._apply_last_session_camera_state(info)

    def _disconnect_camera(self):
        self._save_last_session_state()
        self._stop_live()
        self.controller.close()
        self.panel_conn.set_connected_state(False)
        self._set_settings_enabled(False)
        self.panel_recording.set_enabled_all(False)
        self.panel_trigger_recording.set_enabled_all(False)
        self.status_panel.set_camera("disconnected")
        # Hide the drag-rectangle if it was active
        self.panel_roi.set_draw_roi_checked(False)
        self.roi_rect.hide()

    # =========================================================== settings ==

    def _set_settings_enabled(self, enabled: bool):
        self.panel_exposure.set_enabled_all(enabled)
        self.panel_pf.set_enabled_all(enabled)
        self.panel_roi.set_enabled_all(enabled)
        self.panel_trigger.set_enabled_all(enabled)
        self.btn_start_live.setEnabled(enabled)

    def _refresh_settings_from_camera(self):
        c = self.controller
        # Exposure
        if c.has("ExposureTime"):
            mn, mx, inc = c.min_max_inc("ExposureTime")
        else:
            mn, mx, inc = c.min_max_inc("ExposureTimeAbs")
        self.panel_exposure.set_exposure_limits(mn, mx, inc)
        # Gain
        if c.has("Gain"):
            gmn, gmx, ginc = c.min_max_inc("Gain")
        else:
            gmn, gmx, ginc = c.min_max_inc("GainRaw")
        self.panel_exposure.set_gain_limits(gmn, gmx, ginc)
        self.panel_exposure.set_values(c.exposure_us(), c.gain())

        # Auto exposure / gain / white balance
        for kind in ("exposure", "gain", "white_balance"):
            if c.has_auto(kind):
                opts = c.auto_options(kind) or ["Off", "Once", "Continuous"]
                self.panel_exposure.set_auto_options(kind, opts, c.get_auto(kind))
        self.panel_exposure.set_white_balance_visible(c.has_auto("white_balance"))

        # Pixel format
        self.panel_pf.set_pixel_formats(c.pixel_format_options(), c.pixel_format())

        # FPS
        fmn, fmx, _ = c.min_max_inc("AcquisitionFrameRate")
        if fmn is None:
            fmn, fmx, _ = c.min_max_inc("AcquisitionFrameRateAbs")
        self.panel_pf.set_fps_limits(fmn, fmx)
        fps_enable = c.get("AcquisitionFrameRateEnable", True)
        self.panel_pf.set_fps(c.target_fps(), bool(fps_enable) if fps_enable is not None else True)
        self._update_resulting_fps_label()

        # ROI
        lim = c.roi_limits()
        self.panel_roi.set_limits(
            lim.width_min, lim.width_max, lim.width_inc,
            lim.height_min, lim.height_max, lim.height_inc,
            lim.offsetx_min, lim.offsetx_max, lim.offsetx_inc,
            lim.offsety_min, lim.offsety_max, lim.offsety_inc,
        )
        w, h, ox, oy = c.roi()
        self.panel_roi.set_values(w, h, ox, oy)

        # Trigger
        if c.trigger_supported():
            modes = c.enum_entries("TriggerMode") or ["Off", "On"]
            srcs = c.enum_entries("TriggerSource") or ["Software", "Line1", "Line2"]
            acts = c.enum_entries("TriggerActivation") or ["RisingEdge", "FallingEdge"]
            self.panel_trigger.populate(
                modes, srcs, acts,
                c.get("TriggerMode", "Off"),
                c.get("TriggerSource", "Software"),
                c.get("TriggerActivation", "RisingEdge"),
            )
            self.panel_trigger.set_enabled_all(True)
            self.panel_trigger.set_software_trigger_supported(
                c.software_trigger_supported()
            )
        else:
            self.panel_trigger.set_enabled_all(False)
            self.panel_trigger.set_software_trigger_supported(False)

    def _apply_default_camera_settings(self):
        c = self.controller
        for kind in ("exposure", "gain"):
            if c.has_auto(kind):
                c.set_auto(kind, "Off")
        if not c.set_exposure_us(DEFAULT_EXPOSURE_TIME_US):
            self._status_message(
                f"Failed to set default exposure to {DEFAULT_EXPOSURE_TIME_US:.0f} us.",
                warning=True,
            )
        if not c.set_gain(DEFAULT_GAIN_DB):
            self._status_message(
                f"Failed to set default gain to {DEFAULT_GAIN_DB:.1f} dB.",
                warning=True,
            )
        if not (c.has("AcquisitionFrameRate") or c.has("AcquisitionFrameRateAbs")):
            return
        if not c.set_target_fps(DEFAULT_TARGET_FPS):
            self._status_message(
                f"Failed to set default target fps to {DEFAULT_TARGET_FPS:.0f}.",
                warning=True,
            )

    def _update_resulting_fps_label(self):
        rf = self.controller.resulting_fps()
        self.panel_pf.set_resulting_fps(rf)
        self.status_panel.set_resulting_max_fps(rf)
        self._update_data_rate(rf)

    def _update_data_rate(self, fps: Optional[float]):
        if not self.controller.is_open:
            self.panel_pf.set_data_rate(None)
            self.status_panel.set_data_rate(None)
            return
        w, h, _, _ = self.controller.roi()
        pf = self.controller.pixel_format()
        eff_fps = fps if fps is not None else (self.controller.target_fps() or 0.0)
        rate = estimated_data_rate_mb_s(w, h, pf, eff_fps)
        self.panel_pf.set_data_rate(rate)
        self.status_panel.set_data_rate(rate)

    # ----- camera setting callbacks --------------------------------------

    def _on_exposure_changed(self, value: float):
        if self.controller.set_exposure_us(value):
            self._update_resulting_fps_label()

    def _on_gain_changed(self, value: float):
        self.controller.set_gain(value)
        self._update_resulting_fps_label()

    def _on_auto_changed(self, kind: str, value: str):
        """Apply ExposureAuto / GainAuto / BalanceWhiteAuto."""
        if not self.controller.has_auto(kind):
            self._status_message(f"{kind} auto not supported on this camera",
                                 warning=True)
            return
        if not self.controller.set_auto(kind, value):
            self._status_message(f"Failed to set {kind} auto = {value}",
                                 warning=True)
            return
        # "Once" converges asynchronously — wait briefly then re-read values
        # and update the spin boxes. The camera resets the node to "Off" itself.
        if value == "Once":
            QTimer.singleShot(800, self._after_auto_once)
        elif value == "Off":
            self._after_auto_once()

    def _after_auto_once(self):
        """Re-read exposure/gain values after an Auto Once and refresh combos."""
        c = self.controller
        self.panel_exposure.set_values(c.exposure_us(), c.gain())
        for k in ("exposure", "gain", "white_balance"):
            if c.has_auto(k):
                self.panel_exposure.set_auto_value(k, c.get_auto(k))
        self._update_resulting_fps_label()

    def _on_pixel_format_changed(self, value: str):
        if self.worker is not None:
            self._status_message("Stop Live before changing pixel format.", warning=True)
            return
        if self.controller.set_pixel_format(value):
            self._update_resulting_fps_label()

    def _on_target_fps_changed(self, value: float):
        self.controller.set_target_fps(value)
        self._update_resulting_fps_label()

    def _on_fps_enable_changed(self, enabled: bool):
        if self.controller.has("AcquisitionFrameRateEnable"):
            self.controller.set("AcquisitionFrameRateEnable", enabled)
            self._update_resulting_fps_label()

    def _on_roi_apply(self, w: int, h: int, ox: int, oy: int):
        if self.recorder.is_running:
            self._status_message("Cannot change ROI while recording.", warning=True)
            return
        if self.worker is not None:
            self._stop_live()
            try:
                actual = self.controller.set_roi(w, h, ox, oy)
            finally:
                self._start_live()
        else:
            actual = self.controller.set_roi(w, h, ox, oy)
        self.panel_roi.set_values(*actual)
        self._update_resulting_fps_label()

    def _on_center_roi(self):
        w = self.panel_roi.sp_w.value()
        h = self.panel_roi.sp_h.value()
        if self.worker is not None:
            self._stop_live()
            try:
                actual = self.controller.center_roi(w, h)
            finally:
                self._start_live()
        else:
            actual = self.controller.center_roi(w, h)
        self.panel_roi.set_values(*actual)
        self._update_resulting_fps_label()

    def _on_draw_roi_toggled(self, enabled: bool) -> None:
        """Show/hide the drag-rectangle ROI on top of the preview.

        On first show we size the rectangle to the inner half of the current
        preview so the user has something visible to grab; subsequent toggles
        keep the previous position so workflow is not disrupted.
        """
        if not enabled:
            self.roi_rect.hide()
            return
        # Size the rect to the inner half of the current preview, in view coords.
        # The preview is transposed: view X = image columns, view Y = image rows.
        img = self.image_view.imageItem.image
        if img is None:
            self._status_message("Start Live first to draw an ROI.", warning=True)
            self.panel_roi.set_draw_roi_checked(False)
            return
        view_w = img.shape[0]   # transposed: shape[0] is columns
        view_h = img.shape[1]   # shape[1] is rows
        side_w = max(8, view_w // 2)
        side_h = max(8, view_h // 2)
        # If the ROI has never been placed, center it; otherwise leave alone.
        pos = self.roi_rect.pos()
        size = self.roi_rect.size()
        if size.x() < 2 or size.y() < 2 or pos.x() == 0 and pos.y() == 0:
            self.roi_rect.setPos((view_w - side_w) / 2, (view_h - side_h) / 2)
            self.roi_rect.setSize((side_w, side_h))
        # Constrain to the image bounds
        self.roi_rect.maxBounds = pg.QtCore.QRectF(0, 0, view_w, view_h)
        self.roi_rect.show()

    def _on_apply_roi_selection(self) -> None:
        """Read the drag-rectangle's bounds, convert to sensor coords, apply."""
        if not self.roi_rect.isVisible():
            return
        if not self.controller.is_open:
            return
        # Rectangle in view coords. The preview is transposed, so:
        #   view X (columns) → image columns → sensor X
        #   view Y (rows)    → image rows    → sensor Y
        pos = self.roi_rect.pos()
        size = self.roi_rect.size()
        sel_x = float(pos.x())
        sel_y = float(pos.y())
        sel_w = float(size.x())
        sel_h = float(size.y())
        if sel_w < 2 or sel_h < 2:
            self._status_message("ROI selection is too small.", warning=True)
            return
        # Current camera ROI — the preview shows what's inside this window,
        # so we must add the existing offsets to land in sensor coords.
        cur_w, cur_h, cur_ox, cur_oy = self.controller.roi()
        sel_x = max(0.0, min(sel_x, cur_w))
        sel_y = max(0.0, min(sel_y, cur_h))
        sel_w = min(sel_w, cur_w - sel_x)
        sel_h = min(sel_h, cur_h - sel_y)
        new_w = int(round(sel_w))
        new_h = int(round(sel_h))
        new_ox = int(round(cur_ox + sel_x))
        new_oy = int(round(cur_oy + sel_y))
        # Snap to feature increments so we don't trip pylon's clamp-and-warn.
        lim = self.controller.roi_limits()
        def snap(v, inc, mn, mx):
            if not inc:
                return v
            v = max(mn or 0, v)
            if mx is not None:
                v = min(mx, v)
            base = mn or 0
            return base + ((v - base) // inc) * inc
        new_w = snap(new_w, lim.width_inc, lim.width_min, lim.width_max)
        new_h = snap(new_h, lim.height_inc, lim.height_min, lim.height_max)
        new_ox = snap(new_ox, lim.offsetx_inc, lim.offsetx_min, lim.offsetx_max)
        new_oy = snap(new_oy, lim.offsety_inc, lim.offsety_min, lim.offsety_max)
        # Hide the rectangle so the next preview autoRange is clean
        self.panel_roi.set_draw_roi_checked(False)
        self.roi_rect.hide()
        self._on_roi_apply(int(new_w), int(new_h), int(new_ox), int(new_oy))

    def _on_full_frame(self):
        if self.worker is not None:
            self._stop_live()
            try:
                actual = self.controller.full_frame()
            finally:
                self._start_live()
        else:
            actual = self.controller.full_frame()
        self.panel_roi.set_values(*actual)
        self._update_resulting_fps_label()

    def _on_trigger_changed(self, mode: str, source: str, activation: str):
        self.controller.set_trigger(mode, source, activation)

    def _on_software_trigger(self):
        if not self.controller.is_open:
            return
        if not self.controller.software_trigger_supported():
            self._status_message(
                "This camera does not expose TriggerSoftware.", warning=True
            )
            return
        if self.controller.execute_software_trigger():
            self._status_message("Software trigger fired.")
        else:
            self._status_message(
                "Software trigger failed — is Trigger mode On + Source Software?",
                warning=True,
            )

    # =========================================================== live ==

    def _start_live(self):
        if self.worker is not None:
            return
        if not self.controller.is_open:
            return
        self._preview_needs_autorange = True
        self.worker = CameraWorker(self.controller)
        self.worker.new_preview_frame.connect(self._on_preview_frame)
        self.worker.fps_update.connect(self._on_fps_update)
        self.worker.error.connect(lambda msg: self._error_box("Camera worker error", msg))
        self.worker.start()
        self.btn_start_live.setEnabled(False)
        self.btn_stop_live.setEnabled(True)

    def _stop_live(self):
        if self.worker is None:
            return
        self.worker.request_stop()
        self.worker.wait(2000)
        self.worker = None
        self._preview_needs_autorange = True
        self.btn_start_live.setEnabled(self.controller.is_open)
        self.btn_stop_live.setEnabled(False)
        # Reset histogram strip
        self.hist_bars.setData([], [])
        self.lbl_saturation.setText("Saturation: -")
        self.lbl_saturation.setStyleSheet("color: #444;")

    def _fit_view(self):
        """Re-center and auto-range the preview to fit the current frame."""
        try:
            self.image_view.getView().autoRange()
        except Exception:
            pass

    def _on_preview_frame(self, frame: np.ndarray, info: FrameInfo):
        disp = _prepare_for_display(frame, info.pixel_format)
        if disp is None:
            return
        shape_changed = disp.shape != self._last_preview_shape
        self._last_preview_shape = disp.shape
        auto_range = self._preview_needs_autorange or shape_changed
        self.image_view.setImage(
            disp,
            levels=DISPLAY_LEVELS_8BIT,
            autoLevels=False,
            autoRange=auto_range,
            autoHistogramRange=False,
        )
        self._preview_needs_autorange = False

        # Throttle histogram update to a few Hz — even on 4 MP frames numpy's
        # histogram is fast (~5 ms) but we don't need to redraw it every frame.
        now = time.time()
        if now - self._last_hist_update >= self._hist_update_period:
            self._update_histogram(frame, info.pixel_format)
            self._last_hist_update = now

    def _update_histogram(self, frame: np.ndarray, pixel_format: str) -> None:
        """Compute a histogram of raw pixel values and update the strip.

        Operates on the raw frame (not the display-scaled uint8), so saturation
        readouts reflect the camera's actual bit depth.
        """
        if frame is None or frame.size == 0:
            return
        try:
            # If the frame is 3-channel, fold to luminance for a single histogram.
            sample = frame
            if sample.ndim == 3 and sample.shape[-1] in (3, 4):
                sample = sample[..., :3].mean(axis=-1)

            # Subsample large frames so the histogram is cheap on 4MP+ sensors.
            target = 200_000
            n = sample.size
            if n > target:
                step = max(1, int(n // target))
                sample = sample.ravel()[::step]

            nominal_max = _nominal_max_for_display(pixel_format, frame.dtype) or 255.0
            bins = 256
            edges = np.linspace(0.0, nominal_max, bins + 1)
            counts, _ = np.histogram(sample, bins=edges)

            # Centers, log-friendly counts (avoid log10(0))
            centers = (edges[:-1] + edges[1:]) * 0.5
            counts = counts.astype(np.float64)
            counts = np.maximum(counts, 0.5)

            self.hist_bars.setData(centers, counts)
            self.hist_plot.setXRange(0, nominal_max, padding=0)
            self._fit_histogram_y_range(counts)
            self.sat_line.setPos(nominal_max)

            # Saturation = fraction of pixels at the top bin (>= 99% of max)
            sat_threshold = nominal_max * 0.99
            sat_count = int(np.count_nonzero(sample >= sat_threshold))
            sat_frac = sat_count / max(1, sample.size)
            if sat_frac > self._hist_saturation_threshold:
                self.lbl_saturation.setText(
                    f"⚠ Saturation: {sat_frac * 100:.1f}% of sampled pixels ≥ "
                    f"{int(sat_threshold)}"
                )
                self.lbl_saturation.setStyleSheet("color: #b00; font-weight: bold;")
            else:
                self.lbl_saturation.setText(
                    f"Saturation: {sat_frac * 100:.2f}% (threshold {int(sat_threshold)})"
                )
                self.lbl_saturation.setStyleSheet("color: #444;")
        except Exception:
            log.debug("Histogram update failed", exc_info=True)

    def _fit_histogram_y_range(self, counts: np.ndarray) -> None:
        if counts.size == 0:
            return
        top = float(np.nanmax(counts))
        if not math.isfinite(top) or top <= 0:
            return
        y_min = math.log10(0.5)
        y_max = math.log10(max(1.0, top))
        pad = max(0.05, (y_max - y_min) * 0.08)
        self.hist_plot.setYRange(y_min, y_max + pad, padding=0)

    def _on_fps_update(self, grab_fps: float, preview_fps: float, dropped: int):
        self.status_panel.set_grab_fps(grab_fps)
        self.status_panel.set_preview_fps(preview_fps)
        self.status_panel.set_dropped(dropped)

    # =========================================================== recording ==

    def _on_snapshot(self):
        if not self.controller.is_open:
            return
        out_dir = self.panel_recording.edit_out_dir.text().strip() or "recordings"
        os.makedirs(out_dir, exist_ok=True)
        # Take one frame off the live preview; if not live, do a quick one-shot grab.
        frame = None
        if self.worker is not None and self.image_view.imageItem.image is not None:
            # ImageView stored the transposed display image; just grab a fresh frame instead
            pass
        try:
            self.controller.start_grabbing()
            try:
                result = self.controller.retrieve(timeout_ms=2000)
                if result is None or not result.GrabSucceeded():
                    self._error_box("Snapshot failed", "Could not grab a frame.")
                    return
                frame = np.array(result.GetArray(), copy=True)
                try:
                    result.Release()
                except Exception:
                    pass
            finally:
                # Only stop if we weren't already live
                if self.worker is None:
                    self.controller.stop_grabbing()
        except Exception as e:
            self._error_box("Snapshot failed", str(e))
            return

        if frame is None:
            return

        import tifffile
        ts = time.strftime("%Y%m%d_%H%M%S")
        path = os.path.join(out_dir, f"snapshot_{ts}.tif")
        tifffile.imwrite(path, frame, photometric="minisblack")
        meta = build_metadata(
            controller=self.controller,
            save_format="snapshot_tiff",
            output_path=path,
            target_fps=self.controller.target_fps(),
            resulting_max_fps=self.controller.resulting_fps(),
            user_note=self.panel_recording.edit_note.text(),
        )
        save_metadata_json(meta, path.replace(".tif", "_metadata.json"))
        self._status_message(f"Snapshot saved: {path}")

    def _on_start_recording(self, job_dict: dict):
        if not self.controller.is_open:
            return
        if self.recorder.is_running:
            return
        if job_dict.get("save_format") in ("mp4", "avi"):
            try:
                import cv2  # noqa: F401
            except Exception as e:
                self._error_box(
                    "Recorder start failed",
                    "MP4/AVI recording requires opencv-python.\n"
                    "Run setup.bat again or install it with:\n"
                    "python -m pip install opencv-python",
                )
                log.debug("OpenCV import failed: %s", e)
                return
        if job_dict.get("save_format") == "hdf5":
            try:
                import h5py  # noqa: F401
            except Exception as e:
                self._error_box(
                    "Recorder start failed",
                    "HDF5 recording requires h5py.\n"
                    "Install it with:\n"
                    "python -m pip install h5py",
                )
                log.debug("h5py import failed: %s", e)
                return
        if self.worker is None:
            # We require Live to be running so the camera worker streams frames.
            self._start_live()
            if self.worker is None:
                return

        w, h, _, _ = self.controller.roi()
        pf = self.controller.pixel_format()
        dtype = numpy_dtype_for_pixel_format(pf)
        fps = self.controller.target_fps() or 0.0
        rf = self.controller.resulting_fps()
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        file_prefix = f"{job_dict['file_prefix']}_{timestamp}"

        meta = build_metadata(
            controller=self.controller,
            save_format=job_dict["save_format"],
            output_path=self._recording_output_path(
                job_dict["output_dir"],
                file_prefix,
                job_dict["save_format"],
            ),
            target_fps=fps,
            resulting_max_fps=rf,
            user_note=job_dict.get("user_note", ""),
        )
        self._recording_meta = meta

        rec_job = RecorderJob(
            output_dir=job_dict["output_dir"],
            file_prefix=file_prefix,
            save_format=job_dict["save_format"],
            width=w,
            height=h,
            pixel_format=pf,
            dtype=dtype,
            fps=fps,
            max_frames=job_dict.get("max_frames"),
            max_seconds=job_dict.get("max_seconds"),
            metadata=meta,
        )

        try:
            self.recorder.start(rec_job)
        except Exception as e:
            self._error_box("Recorder start failed", str(e))
            return

        self.worker.start_recording(
            self.recorder.frame_queue,
            rec_job.max_frames,
            rec_job.max_seconds,
            rec_job.fps,
        )
        self._recording_started_at = time.time()
        self.panel_recording.set_recording_state(True)
        self.panel_trigger_recording.set_recording_state(True)
        self.status_panel.set_recording("RECORDING")
        # while recording, lock pixel format / ROI
        self.panel_pf.combo_pf.setEnabled(False)
        self.panel_roi.set_enabled_all(False)

    def _on_start_trigger_recording(self, job_dict: dict):
        if not self._enable_trigger_mode_from_panel():
            return
        self._on_start_recording(job_dict)

    def _enable_trigger_mode_from_panel(self) -> bool:
        if not self.controller.is_open:
            return False
        if not self.controller.trigger_supported():
            self._error_box(
                "Trigger recording",
                "This camera does not expose TriggerMode.",
            )
            return False

        if self.panel_trigger.combo_mode.findText("On") < 0:
            self._error_box(
                "Trigger recording",
                "TriggerMode=On is not available on this camera.",
            )
            return False

        self.panel_trigger.combo_mode.setCurrentText("On")
        mode = self.panel_trigger.combo_mode.currentText()
        source = self.panel_trigger.combo_source.currentText()
        activation = self.panel_trigger.combo_activation.currentText()
        self.controller.set_trigger(mode, source, activation)
        # set_trigger disables AcquisitionFrameRateEnable when mode=On so the
        # free-run limiter cannot cap incoming triggers. Reflect that in the UI
        # and re-read resulting fps so the user sees the change.
        if self.controller.has("AcquisitionFrameRateEnable"):
            self.panel_pf.set_fps(
                self.controller.target_fps(),
                bool(self.controller.get("AcquisitionFrameRateEnable", True)),
            )
            self._status_message(
                "Trigger mode On: free-run rate limit disabled "
                "(AcquisitionFrameRateEnable=False)."
            )
        self._update_resulting_fps_label()
        return True

    def _recording_output_path(self, output_dir: str, file_prefix: str, save_format: str) -> str:
        ext_by_format = {
            "mp4": ".mp4",
            "avi": ".avi",
            "bigtiff": ".tiff",
            "raw": ".raw",
            "hdf5": ".h5",
        }
        if save_format == "tiff_sequence":
            return os.path.join(output_dir, f"{file_prefix}_########.tif")
        return os.path.join(output_dir, f"{file_prefix}{ext_by_format.get(save_format, '')}")

    def _on_stop_recording(self):
        dropped = self.worker.dropped if self.worker else 0
        job = self.recorder.job
        if self.worker is not None and self.worker.recording:
            sent = self.worker.stop_recording()
            log.info("Worker sent %d frames before stop", sent)
        # Let the recorder drain the queue then exit
        self.recorder.stop(timeout=15.0)
        self._write_final_drop_count(job, dropped)
        self.panel_recording.set_recording_state(False)
        self.panel_trigger_recording.set_recording_state(False)
        self.status_panel.set_recording("idle")
        if self.controller.is_open:
            self.panel_roi.set_enabled_all(True)
            self.panel_pf.combo_pf.setEnabled(True)
        self._recording_started_at = None

    # =========================================================== timers ==

    def _poll_recorder_status(self):
        if self.recorder.status_queue is None:
            return
        last = None
        while True:
            try:
                msg = self.recorder.status_queue.get_nowait()
                last = msg
            except Empty:
                break
            except Exception:
                break
        if last is None:
            return
        if last.get("error"):
            self._status_message(f"Recorder error: {last['error']}", warning=True)
        if last.get("finished"):
            # Auto-stop if writer exited, including fatal writer errors.
            self._on_stop_recording()

    def _tick_ui(self):
        # Recording progress widget
        if self.recorder.is_running and self._recording_started_at is not None:
            elapsed = time.time() - self._recording_started_at
            frames = self.worker._record_frames_sent if self.worker else 0
            qlen = self.recorder.queue_length()
            dropped = self.worker.dropped if self.worker else 0
            rec_fps = frames / elapsed if elapsed > 0 else 0.0
            self.panel_recording.set_progress(frames, elapsed, qlen, dropped, rec_fps)
            self.panel_trigger_recording.set_progress(frames, elapsed, qlen, dropped, rec_fps)
            self.status_panel.set_queue(qlen)
            # Auto-stop check: if camera worker thinks it's done, stop everything
            if self.worker is not None and not self.worker.recording:
                self._on_stop_recording()
        elif self.worker is not None and self.worker.recording and not self.recorder.is_running:
            self._status_message("Recorder process stopped unexpectedly.", warning=True)
            self._on_stop_recording()

    def _write_final_drop_count(self, job: Optional[RecorderJob], dropped: int):
        if job is None:
            return
        meta_path = os.path.join(job.output_dir, f"{job.file_prefix}_metadata.json")
        if not os.path.exists(meta_path):
            return
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            meta["dropped_frame_count"] = int(dropped)
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2, default=str)
        except Exception:
            log.exception("Failed to update dropped frame count in metadata")

    # =========================================================== presets ==

    def _refresh_presets(self):
        names = [os.path.splitext(os.path.basename(p))[0] for p in list_presets()]
        self.panel_recording.set_presets(names)

    def _on_save_preset(self, name: str):
        c = self.controller
        if not c.is_open:
            return
        w, h, ox, oy = c.roi()
        preset = CameraPreset(
            name=name,
            exposure_time_us=c.exposure_us(),
            gain=c.gain(),
            width=w, height=h, offset_x=ox, offset_y=oy,
            pixel_format=c.pixel_format(),
            target_fps=c.target_fps(),
            fps_enable=bool(c.get("AcquisitionFrameRateEnable", True)) if c.has("AcquisitionFrameRateEnable") else None,
            trigger_mode=c.get("TriggerMode") if c.has("TriggerMode") else None,
            trigger_source=c.get("TriggerSource") if c.has("TriggerSource") else None,
            trigger_activation=c.get("TriggerActivation") if c.has("TriggerActivation") else None,
            save_format=self.panel_recording.combo_format.currentData(),
            output_folder=self.panel_recording.edit_out_dir.text(),
        )
        path = save_preset(preset)
        self._status_message(f"Preset saved: {path}")
        self._refresh_presets()

    def _on_load_preset_by_name(self, name: str):
        if not name:
            return
        candidates = [p for p in list_presets()
                      if os.path.splitext(os.path.basename(p))[0] == name]
        if not candidates:
            self._error_box("Load preset", f"Preset '{name}' not found.")
            return
        preset = load_preset(candidates[0])
        self._apply_preset(preset)

    def _apply_preset(self, preset: CameraPreset):
        c = self.controller
        was_live = self.worker is not None
        if was_live:
            self._stop_live()
        try:
            if preset.pixel_format:
                c.set_pixel_format(preset.pixel_format)
            if preset.width and preset.height:
                c.set_roi(preset.width, preset.height,
                          preset.offset_x or 0, preset.offset_y or 0)
            if preset.exposure_time_us is not None:
                c.set_exposure_us(preset.exposure_time_us)
            if preset.gain is not None:
                c.set_gain(preset.gain)
            if preset.target_fps is not None:
                c.set_target_fps(preset.target_fps)
            if preset.fps_enable is not None and c.has("AcquisitionFrameRateEnable"):
                c.set("AcquisitionFrameRateEnable", preset.fps_enable)
            if preset.trigger_mode and c.has("TriggerMode"):
                c.set_trigger(preset.trigger_mode,
                              preset.trigger_source or None,
                              preset.trigger_activation or None)
            if preset.save_format:
                for i in range(self.panel_recording.combo_format.count()):
                    if self.panel_recording.combo_format.itemData(i) == preset.save_format:
                        self.panel_recording.combo_format.setCurrentIndex(i)
                        break
            if preset.output_folder:
                self.panel_recording.edit_out_dir.setText(preset.output_folder)
        finally:
            self._refresh_settings_from_camera()
            if was_live:
                self._start_live()
        self._status_message(f"Preset loaded: {preset.name}")

    # =========================================================== misc ==

    def _status_message(self, text: str, warning: bool = False):
        self.statusBar().showMessage(text, 5000)
        if warning:
            log.warning(text)
        else:
            log.info(text)

    def _error_box(self, title: str, msg: str):
        log.error("%s: %s", title, msg)
        QMessageBox.critical(self, title, msg)

    def closeEvent(self, event):
        try:
            self._save_last_session_state()
            if self.recorder.is_running:
                self._on_stop_recording()
            self._stop_live()
            self.controller.close()
        except Exception:
            log.exception("Error during shutdown")
        super().closeEvent(event)
