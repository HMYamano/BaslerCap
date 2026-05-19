"""High-level controller wrapping a pypylon camera or a DummyCamera.

Hides the differences between real Basler cameras and the dummy backend so
that the rest of the app can use the same API.

Responsibilities:
- enumerate cameras
- open / close
- safe Feature get/set with existence + writability checks
- ROI helpers (full / center / set)
- start/stop grabbing
- retrieve frames in a thread-friendly way
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

from . import utils
from .dummy_camera import DummyCamera, list_dummy_devices
from .utils import CameraInfo

log = logging.getLogger(__name__)


# Try to import pypylon. Absence is recoverable (dummy mode still works).
try:
    from pypylon import pylon  # type: ignore
    from pypylon import genicam  # type: ignore
    PYPYLON_AVAILABLE = True
except Exception as e:  # noqa: BLE001 - we want to swallow everything
    pylon = None  # type: ignore
    genicam = None  # type: ignore
    PYPYLON_AVAILABLE = False
    _pypylon_import_error = repr(e)
else:
    _pypylon_import_error = ""


def pypylon_import_error() -> str:
    return _pypylon_import_error


# --- Discovery ---------------------------------------------------------------

def enumerate_cameras(include_dummy: bool = True,
                      dummy_count: int = 1) -> List[CameraInfo]:
    """Return a list of CameraInfo for every accessible camera.

    Errors are swallowed - on any pylon failure we still return the dummy list
    so the GUI can come up.
    """
    cams: List[CameraInfo] = []
    if PYPYLON_AVAILABLE:
        try:
            tl_factory = pylon.TlFactory.GetInstance()
            devices = tl_factory.EnumerateDevices()
            for d in devices:
                try:
                    cams.append(CameraInfo(
                        model_name=d.GetModelName(),
                        serial_number=d.GetSerialNumber(),
                        device_class=d.GetDeviceClass(),
                        friendly_name=d.GetFriendlyName(),
                        is_dummy=False,
                    ))
                except Exception as e:
                    log.warning("Failed to read device info: %s", e)
        except Exception as e:
            log.warning("EnumerateDevices failed: %s", e)
    if include_dummy:
        cams.extend(list_dummy_devices(dummy_count))
    return cams


# --- Controller --------------------------------------------------------------

_AUTO_NODE = {
    "exposure":      "ExposureAuto",
    "gain":          "GainAuto",
    "white_balance": "BalanceWhiteAuto",
}


@dataclass
class ROILimits:
    width_min: Optional[int]
    width_max: Optional[int]
    width_inc: Optional[int]
    height_min: Optional[int]
    height_max: Optional[int]
    height_inc: Optional[int]
    offsetx_min: Optional[int]
    offsetx_max: Optional[int]
    offsetx_inc: Optional[int]
    offsety_min: Optional[int]
    offsety_max: Optional[int]
    offsety_inc: Optional[int]


class CameraController:
    def __init__(self):
        self._camera: Any = None
        self._info: Optional[CameraInfo] = None
        self._is_dummy: bool = False

    # ------------------------------------------------------------------ open/close

    @property
    def info(self) -> Optional[CameraInfo]:
        return self._info

    @property
    def camera(self) -> Any:
        return self._camera

    @property
    def is_open(self) -> bool:
        if self._camera is None:
            return False
        try:
            return bool(self._camera.IsOpen())
        except Exception:
            return False

    @property
    def is_grabbing(self) -> bool:
        if self._camera is None:
            return False
        try:
            return bool(self._camera.IsGrabbing())
        except Exception:
            return False

    def open(self, info: CameraInfo) -> None:
        """Open the camera described by ``info``.

        Raises ``RuntimeError`` on failure. The controller is left in a
        cleanly closed state if open() raises.
        """
        self.close()
        if info.is_dummy:
            cam = DummyCamera(serial_number=info.serial_number)
            cam.Open()
            self._camera = cam
            self._info = info
            self._is_dummy = True
            return

        if not PYPYLON_AVAILABLE:
            raise RuntimeError(
                "pypylon is not installed. Install Basler pylon Software Suite and pip install pypylon."
            )

        try:
            tl_factory = pylon.TlFactory.GetInstance()
            devices = tl_factory.EnumerateDevices()
            target = None
            for d in devices:
                if d.GetSerialNumber() == info.serial_number:
                    target = d
                    break
            if target is None:
                raise RuntimeError(f"Camera with serial {info.serial_number} not found.")

            device = tl_factory.CreateDevice(target)
            cam = pylon.InstantCamera(device)
            cam.Open()
            self._camera = cam
            self._info = info
            self._is_dummy = False
        except Exception as e:
            self._camera = None
            self._info = None
            raise RuntimeError(f"Failed to open camera: {e}") from e

    def close(self) -> None:
        if self._camera is None:
            return
        try:
            if self.is_grabbing:
                self._camera.StopGrabbing()
        except Exception:
            pass
        try:
            self._camera.Close()
        except Exception:
            pass
        self._camera = None
        self._info = None
        self._is_dummy = False

    # ------------------------------------------------------------------ features

    def has(self, name: str) -> bool:
        return utils.has_feature(self._camera, name)

    def get(self, name: str, default=None):
        return utils.get_feature_value(self._camera, name, default)

    def set(self, name: str, value) -> bool:
        return utils.set_feature_value(self._camera, name, value)

    def min_max_inc(self, name: str):
        return utils.get_feature_min_max_inc(self._camera, name)

    def enum_entries(self, name: str) -> List[str]:
        return utils.get_enum_entries(self._camera, name)

    # ------------------------------------------------------------------ specific

    def pixel_format(self) -> str:
        return self.get("PixelFormat", "Mono8") or "Mono8"

    def set_pixel_format(self, value: str) -> bool:
        return self.set("PixelFormat", value)

    def pixel_format_options(self) -> List[str]:
        opts = self.enum_entries("PixelFormat")
        # Priority: mono first, bayer next
        order = ["Mono8", "Mono10", "Mono12", "Mono16",
                 "BayerRG8", "BayerBG8", "BayerGR8", "BayerGB8",
                 "BayerRG12", "BayerRG16"]
        opts.sort(key=lambda x: (order.index(x) if x in order else 99, x))
        return opts

    def exposure_us(self) -> Optional[float]:
        # Modern: ExposureTime (us); older: ExposureTimeAbs
        for name in ("ExposureTime", "ExposureTimeAbs"):
            if self.has(name):
                v = self.get(name)
                if v is not None:
                    return float(v)
        return None

    def set_exposure_us(self, value: float) -> bool:
        for name in ("ExposureTime", "ExposureTimeAbs"):
            if self.has(name) and utils.is_writable(self._camera, name):
                return self.set(name, value)
        return False

    def gain(self) -> Optional[float]:
        for name in ("Gain", "GainRaw"):
            if self.has(name):
                v = self.get(name)
                if v is not None:
                    return float(v)
        return None

    def set_gain(self, value: float) -> bool:
        for name in ("Gain", "GainRaw"):
            if self.has(name) and utils.is_writable(self._camera, name):
                return self.set(name, value)
        return False

    def target_fps(self) -> Optional[float]:
        v = self.get("AcquisitionFrameRate")
        if v is None:
            v = self.get("AcquisitionFrameRateAbs")
        return float(v) if v is not None else None

    def set_target_fps(self, value: float) -> bool:
        # Enable if a switch exists
        if self.has("AcquisitionFrameRateEnable"):
            self.set("AcquisitionFrameRateEnable", True)
        if self.has("AcquisitionFrameRate"):
            return self.set("AcquisitionFrameRate", value)
        if self.has("AcquisitionFrameRateAbs"):
            return self.set("AcquisitionFrameRateAbs", value)
        return False

    def resulting_fps(self) -> Optional[float]:
        return utils.get_resulting_fps(self._camera)

    # ------------------------------------------------------------------ ROI

    def roi_limits(self) -> ROILimits:
        wmin, wmax, winc = self.min_max_inc("Width")
        hmin, hmax, hinc = self.min_max_inc("Height")
        oxmin, oxmax, oxinc = self.min_max_inc("OffsetX")
        oymin, oymax, oyinc = self.min_max_inc("OffsetY")
        return ROILimits(
            wmin, wmax, winc,
            hmin, hmax, hinc,
            oxmin, oxmax, oxinc,
            oymin, oymax, oyinc,
        )

    def roi(self) -> Tuple[int, int, int, int]:
        w = int(self.get("Width") or 0)
        h = int(self.get("Height") or 0)
        ox = int(self.get("OffsetX") or 0)
        oy = int(self.get("OffsetY") or 0)
        return w, h, ox, oy

    def set_roi(self, width: int, height: int,
                offset_x: int, offset_y: int) -> Tuple[int, int, int, int]:
        """Apply ROI safely. Returns the actual (w, h, ox, oy)."""
        was_grabbing = self.is_grabbing
        if was_grabbing:
            self.stop_grabbing()
        try:
            # Reset offsets first so big ROIs can be set.
            self.set("OffsetX", 0)
            self.set("OffsetY", 0)
            self.set("Width", int(width))
            self.set("Height", int(height))
            self.set("OffsetX", int(offset_x))
            self.set("OffsetY", int(offset_y))
        finally:
            if was_grabbing:
                self.start_grabbing()
        return self.roi()

    def full_frame(self) -> Tuple[int, int, int, int]:
        limits = self.roi_limits()
        w = limits.width_max or self.get("WidthMax") or self.get("Width") or 0
        h = limits.height_max or self.get("HeightMax") or self.get("Height") or 0
        return self.set_roi(int(w), int(h), 0, 0)

    def center_roi(self, width: int, height: int) -> Tuple[int, int, int, int]:
        limits = self.roi_limits()
        sensor_w = limits.width_max or self.get("WidthMax") or width
        sensor_h = limits.height_max or self.get("HeightMax") or height
        inc_x = limits.offsetx_inc or 1
        inc_y = limits.offsety_inc or 1
        ox = max(0, (int(sensor_w) - int(width)) // 2)
        oy = max(0, (int(sensor_h) - int(height)) // 2)
        # Snap offsets to the increment
        ox = (ox // inc_x) * inc_x
        oy = (oy // inc_y) * inc_y
        return self.set_roi(width, height, ox, oy)

    # ------------------------------------------------------------------ auto

    def has_auto(self, kind: str) -> bool:
        """kind in {'exposure', 'gain', 'white_balance'}."""
        return self.has(_AUTO_NODE[kind])

    def set_auto(self, kind: str, value: str) -> bool:
        """value in {'Off', 'Once', 'Continuous'} (those supported by the cam)."""
        name = _AUTO_NODE[kind]
        if not self.has(name):
            return False
        return self.set(name, value)

    def get_auto(self, kind: str) -> Optional[str]:
        name = _AUTO_NODE[kind]
        if not self.has(name):
            return None
        return self.get(name)

    def auto_options(self, kind: str) -> List[str]:
        name = _AUTO_NODE[kind]
        return self.enum_entries(name) if self.has(name) else []

    # ------------------------------------------------------------------ trigger

    def trigger_supported(self) -> bool:
        return self.has("TriggerMode")

    def set_trigger(self, mode: str, source: Optional[str] = None,
                    activation: Optional[str] = None) -> None:
        if self.has("TriggerMode"):
            self.set("TriggerMode", mode)
        if source and self.has("TriggerSource"):
            self.set("TriggerSource", source)
        if activation and self.has("TriggerActivation"):
            self.set("TriggerActivation", activation)

    def software_trigger_supported(self) -> bool:
        """True if the camera exposes the ``TriggerSoftware`` command node."""
        return self.has("TriggerSoftware")

    def execute_software_trigger(self) -> bool:
        """Fire a single software-trigger pulse.

        Returns True on success. Most pypylon builds expose a convenience
        method ``ExecuteSoftwareTrigger()``; we fall back to the GenApi
        Command node if that's not present (and for the dummy camera).
        """
        if self._camera is None:
            return False
        # InstantCamera convenience method (pypylon ≥ 1.6) — only valid when
        # already grabbing. Tries first because it also nudges the driver.
        try:
            if hasattr(self._camera, "WaitForFrameTriggerReady"):
                try:
                    self._camera.WaitForFrameTriggerReady(1000)
                except Exception:
                    pass
            if hasattr(self._camera, "ExecuteSoftwareTrigger"):
                self._camera.ExecuteSoftwareTrigger()
                return True
        except Exception as e:
            log.debug("ExecuteSoftwareTrigger failed, falling back: %s", e)
        # Fall back to the Command node.
        node = utils._get_node(self._camera, "TriggerSoftware")
        if node is None:
            return False
        try:
            node.Execute()
            return True
        except Exception as e:
            log.warning("TriggerSoftware.Execute() failed: %s", e)
            return False

    # ------------------------------------------------------------------ grab

    def start_grabbing(self) -> None:
        if self._camera is None:
            return
        try:
            if self._is_dummy:
                self._camera.StartGrabbing()
            else:
                # GrabStrategy_LatestImages keeps the buffer small (preview) but
                # for recording we want GrabStrategy_OneByOne. The worker picks
                # the right behaviour; here we default to OneByOne so frames are
                # not dropped at the driver layer.
                self._camera.StartGrabbing(pylon.GrabStrategy_OneByOne)
        except Exception as e:
            log.exception("StartGrabbing failed: %s", e)
            raise

    def stop_grabbing(self) -> None:
        if self._camera is None:
            return
        try:
            self._camera.StopGrabbing()
        except Exception:
            pass

    def retrieve(self, timeout_ms: int = 1000):
        if self._camera is None:
            return None
        try:
            if self._is_dummy:
                return self._camera.RetrieveResult(timeout_ms)
            return self._camera.RetrieveResult(timeout_ms, pylon.TimeoutHandling_Return)
        except Exception as e:
            log.debug("RetrieveResult failed: %s", e)
            return None
