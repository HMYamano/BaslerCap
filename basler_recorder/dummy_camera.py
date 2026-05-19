"""Dummy Basler-like camera for use when no real camera is connected.

Mimics just enough of the pypylon ``InstantCamera`` / NodeMap interface for
``CameraController`` and the utils helpers to work unchanged.
"""
from __future__ import annotations

import math
import time
from typing import Any, Iterable, Optional

import numpy as np


# --- Minimal GenApi-like nodes -----------------------------------------------

class _NumericNode:
    def __init__(self, kind: str, value, mn, mx, inc=1):
        self._dummy_kind = kind  # "int" or "float"
        self._value = value
        self._min = mn
        self._max = mx
        self._inc = inc
        self._writable = True

    def GetValue(self):
        return self._value

    def SetValue(self, v):
        v = max(self._min, min(self._max, v))
        if self._inc:
            v = self._min + round((v - self._min) / self._inc) * self._inc
            v = max(self._min, min(self._max, v))
        self._value = int(v) if self._dummy_kind == "int" else float(v)

    def GetMin(self):
        return self._min

    def GetMax(self):
        return self._max

    def GetInc(self):
        return self._inc

    def ToString(self):
        return str(self._value)

    def FromString(self, s):
        self.SetValue(float(s))

    def IsReadable(self):
        return True

    def IsWritable(self):
        return self._writable

    def IsAvailable(self):
        return True

    def GetPrincipalInterfaceType(self):
        return 4 if self._dummy_kind == "float" else 3  # intfIFloat / intfIInteger


class _EnumNode:
    def __init__(self, symbols: Iterable[str], value: str):
        self._symbols = list(symbols)
        self._value = value

    def GetValue(self):
        return self._value

    def SetValue(self, v):
        if v in self._symbols:
            self._value = v

    def ToString(self):
        return self._value

    def FromString(self, s):
        self.SetValue(s)

    def GetSymbolics(self):
        return list(self._symbols)

    def GetEntries(self):
        # Mimic GenApi GetEntries by returning lightweight wrappers
        out = []
        for s in self._symbols:
            class _E:
                def __init__(self, sym):
                    self._sym = sym

                def GetSymbolic(self):
                    return self._sym

                def ToString(self):
                    return self._sym
            out.append(_E(s))
        return out

    def IsReadable(self):
        return True

    def IsWritable(self):
        return True

    def IsAvailable(self):
        return True


class _BoolNode:
    def __init__(self, value: bool):
        self._value = bool(value)

    def GetValue(self):
        return self._value

    def SetValue(self, v):
        self._value = bool(v)

    def FromString(self, s):
        self._value = s.lower() in ("1", "true", "on", "yes")

    def ToString(self):
        return "true" if self._value else "false"

    def IsReadable(self):
        return True

    def IsWritable(self):
        return True

    def IsAvailable(self):
        return True


class _CommandNode:
    """A pypylon Command node — has Execute() and IsDone()."""

    def __init__(self, on_execute=None):
        self._on_execute = on_execute

    def Execute(self):
        if self._on_execute is not None:
            self._on_execute()

    def IsDone(self) -> bool:
        return True

    def IsAvailable(self):
        return True

    def IsReadable(self):
        return True

    def IsWritable(self):
        return True

    def ToString(self):
        return ""


class _NodeMap:
    def __init__(self, nodes: dict):
        self._nodes = nodes

    def GetNode(self, name: str):
        return self._nodes.get(name)


# --- Grab result -------------------------------------------------------------

class _DummyGrabResult:
    def __init__(self, array: np.ndarray, frame_id: int, timestamp_ns: int):
        self._array = array
        self._id = frame_id
        self._ts = timestamp_ns
        self._succeeded = True

    @property
    def Array(self):
        return self._array

    def GetArray(self):
        return self._array

    def GrabSucceeded(self) -> bool:
        return self._succeeded

    def GetImageNumber(self) -> int:
        return self._id

    def GetTimeStamp(self) -> int:
        return self._ts

    def Release(self):
        pass

    # Context manager support (pypylon style)
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.Release()


# --- The DummyCamera ---------------------------------------------------------

class DummyCamera:
    """Looks enough like ``pypylon.pylon.InstantCamera`` for our needs.

    Generates a moving sinusoidal pattern. Honors width/height/offset/
    pixel format / fps / exposure feature changes.
    """

    SENSOR_W = 1920
    SENSOR_H = 1200

    def __init__(self, serial_number: str = "DUMMY-0001"):
        self._serial = serial_number
        self._open = False
        self._grabbing = False
        self._frame_id = 0
        self._t0 = time.time()
        self._last_grab_time = 0.0
        self._build_nodes()

    # --- pypylon-compatible camera info -------------------------------------

    def GetDeviceInfo(self):
        cam = self

        class _Info:
            def GetModelName(self):
                return "DummyBasler-1920x1200"

            def GetSerialNumber(self):
                return cam._serial

            def GetDeviceClass(self):
                return "DummyDevice"

            def GetFriendlyName(self):
                return f"DummyBasler ({cam._serial})"
        return _Info()

    # --- NodeMap ------------------------------------------------------------

    def GetNodeMap(self) -> _NodeMap:
        return self._node_map

    def _build_nodes(self):
        # Width / Height / Offsets
        w = _NumericNode("int", self.SENSOR_W, 16, self.SENSOR_W, 16)
        h = _NumericNode("int", self.SENSOR_H, 16, self.SENSOR_H, 16)
        ox = _NumericNode("int", 0, 0, self.SENSOR_W - 16, 16)
        oy = _NumericNode("int", 0, 0, self.SENSOR_H - 16, 16)

        # ExposureTime in microseconds
        exposure = _NumericNode("float", 5000.0, 20.0, 1_000_000.0, 1.0)
        gain = _NumericNode("float", 0.0, 0.0, 36.0, 0.1)

        pixfmt = _EnumNode(
            ["Mono8", "Mono12", "Mono16"], value="Mono8"
        )

        fps_enable = _BoolNode(True)
        fps = _NumericNode("float", 30.0, 1.0, 500.0, 0.1)

        # Read-only resulting fps node — derived from current fps target & exposure
        cam = self

        class _ResultingFps:
            _dummy_kind = "float"

            def GetValue(self_inner):
                # Limit by 1 / exposure_seconds; also clamp to target
                exp_s = exposure.GetValue() * 1e-6
                max_by_exp = 1.0 / max(exp_s, 1e-9)
                if fps_enable.GetValue():
                    return float(min(fps.GetValue(), max_by_exp))
                return float(max_by_exp)

            def IsReadable(self_inner):
                return True

            def IsWritable(self_inner):
                return False

            def IsAvailable(self_inner):
                return True

            def ToString(self_inner):
                return str(self_inner.GetValue())

        # Trigger nodes (optional)
        trig_mode = _EnumNode(["Off", "On"], value="Off")
        trig_source = _EnumNode(["Software", "Line1", "Line2"], value="Software")
        trig_act = _EnumNode(["RisingEdge", "FallingEdge"], value="RisingEdge")

        self._w, self._h, self._ox, self._oy = w, h, ox, oy
        self._exposure = exposure
        self._gain = gain
        self._pixfmt = pixfmt
        self._fps_enable = fps_enable
        self._fps = fps
        self._trig_mode = trig_mode
        self._trig_source = trig_source

        # Pending-software-trigger count — decremented when RetrieveResult
        # delivers a frame while TriggerMode=On + Source=Software.
        self._pending_software_triggers = 0

        def _on_software_trigger():
            self._pending_software_triggers += 1

        trig_software = _CommandNode(on_execute=_on_software_trigger)

        self._node_map = _NodeMap({
            "Width": w,
            "Height": h,
            "OffsetX": ox,
            "OffsetY": oy,
            "WidthMax": _NumericNode("int", self.SENSOR_W, self.SENSOR_W, self.SENSOR_W),
            "HeightMax": _NumericNode("int", self.SENSOR_H, self.SENSOR_H, self.SENSOR_H),
            "ExposureTime": exposure,
            "ExposureTimeAbs": exposure,  # alias for older cams
            "Gain": gain,
            "GainRaw": gain,
            "PixelFormat": pixfmt,
            "AcquisitionFrameRate": fps,
            "AcquisitionFrameRateEnable": fps_enable,
            "AcquisitionFrameRateAbs": fps,
            "BslResultingAcquisitionFrameRate": _ResultingFps(),
            "ResultingFrameRate": _ResultingFps(),
            "TriggerMode": trig_mode,
            "TriggerSource": trig_source,
            "TriggerActivation": trig_act,
            "TriggerSoftware": trig_software,
        })

    # --- Lifecycle ----------------------------------------------------------

    def Open(self):
        self._open = True

    def Close(self):
        self._open = False
        self._grabbing = False

    def IsOpen(self) -> bool:
        return self._open

    def StartGrabbing(self, *args, **kwargs):
        self._grabbing = True
        self._t0 = time.time()
        self._frame_id = 0

    def StopGrabbing(self):
        self._grabbing = False

    def IsGrabbing(self) -> bool:
        return self._grabbing

    # --- Frame generation ---------------------------------------------------

    def RetrieveResult(self, timeout_ms: int, *args, **kwargs):
        # Honor TriggerMode=On + Source=Software: only deliver a frame if a
        # software trigger has been queued. Wait up to timeout_ms for one.
        if (self._trig_mode.GetValue() == "On"
                and self._trig_source.GetValue() == "Software"):
            deadline = time.time() + timeout_ms / 1000.0
            while self._pending_software_triggers <= 0:
                if time.time() >= deadline:
                    return None
                time.sleep(0.005)
            self._pending_software_triggers -= 1
            # Still respect exposure time as a minimum spacing — small.
            time.sleep(min(self._exposure.GetValue() * 1e-6, timeout_ms / 1000.0))
            self._last_grab_time = time.time()
        else:
            # Pace to target fps
            target = max(self._fps.GetValue(), 1.0)
            target_dt = 1.0 / target
            now = time.time()
            wait = target_dt - (now - self._last_grab_time)
            if wait > 0:
                time.sleep(min(wait, timeout_ms / 1000.0))
            self._last_grab_time = time.time()

        w = int(self._w.GetValue())
        h = int(self._h.GetValue())
        pf = self._pixfmt.GetValue()
        if pf == "Mono8":
            dtype = np.uint8
            max_val = 255
        else:
            dtype = np.uint16
            max_val = 4095 if pf == "Mono12" else 65535

        t = time.time() - self._t0
        # Create a synthetic moving sinusoid (cheap)
        xs = np.linspace(0, 4 * math.pi, w, dtype=np.float32)
        ys = np.linspace(0, 4 * math.pi, h, dtype=np.float32)
        gy, gx = np.meshgrid(ys, xs, indexing="ij")
        pattern = (np.sin(gx + t * 2.0) + np.cos(gy - t * 1.3) + 2.0) / 4.0
        # Add per-frame noise
        noise = np.random.rand(h, w).astype(np.float32) * 0.05
        # Exposure → brightness scaler (clipped)
        exp_norm = min(1.0, self._exposure.GetValue() / 50_000.0)
        gain_norm = 1.0 + self._gain.GetValue() / 20.0
        arr = ((pattern * exp_norm + noise) * gain_norm).clip(0, 1)
        frame = (arr * max_val).astype(dtype)

        self._frame_id += 1
        ts_ns = int(time.time_ns())
        return _DummyGrabResult(frame, self._frame_id, ts_ns)

    # Convenience getattr shortcut so feature access via attribute works too.
    def __getattr__(self, item):
        node = self.__dict__.get("_node_map")
        if node is None:
            raise AttributeError(item)
        n = node.GetNode(item)
        if n is None:
            raise AttributeError(item)
        return n


# --- Discovery helper --------------------------------------------------------

def list_dummy_devices(count: int = 1):
    """Return ``CameraInfo``-like records for ``count`` dummies."""
    from .utils import CameraInfo
    return [
        CameraInfo(
            model_name="DummyBasler-1920x1200",
            serial_number=f"DUMMY-{i:04d}",
            device_class="DummyDevice",
            friendly_name=f"Dummy Basler #{i}",
            is_dummy=True,
        )
        for i in range(count)
    ]
