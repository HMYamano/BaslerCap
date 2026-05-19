"""Utility helpers for safe pypylon Feature access and pixel-format math.

pypylon Feature names differ between camera models, so every getter/setter
goes through a try-except wrapper. These helpers never raise on missing
features; they return ``None`` or the supplied default instead.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Tuple


# --- Pixel format → bytes/pixel mapping --------------------------------------

# Mapping from common Basler PixelFormat names to (numpy_dtype_str, bytes_per_pixel).
# 10/12/16-bit formats are unpacked to 16-bit by pylon by default.
PIXEL_FORMAT_INFO = {
    "Mono8":         ("uint8",  1),
    "Mono10":        ("uint16", 2),
    "Mono10p":       ("uint16", 2),
    "Mono12":        ("uint16", 2),
    "Mono12p":       ("uint16", 2),
    "Mono16":        ("uint16", 2),
    "BayerRG8":      ("uint8",  1),
    "BayerBG8":      ("uint8",  1),
    "BayerGR8":      ("uint8",  1),
    "BayerGB8":      ("uint8",  1),
    "BayerRG12":     ("uint16", 2),
    "BayerBG12":     ("uint16", 2),
    "BayerGR12":     ("uint16", 2),
    "BayerGB12":     ("uint16", 2),
    "BayerRG16":     ("uint16", 2),
    "BayerBG16":     ("uint16", 2),
    "RGB8":          ("uint8",  3),
    "BGR8":          ("uint8",  3),
}


def bytes_per_pixel(pixel_format: str) -> int:
    info = PIXEL_FORMAT_INFO.get(pixel_format)
    return info[1] if info else 1


def numpy_dtype_for_pixel_format(pixel_format: str) -> str:
    info = PIXEL_FORMAT_INFO.get(pixel_format)
    return info[0] if info else "uint8"


def estimated_data_rate_mb_s(width: int, height: int,
                             pixel_format: str, fps: float) -> float:
    if fps is None or fps <= 0:
        return 0.0
    bpp = bytes_per_pixel(pixel_format)
    return (width * height * bpp * fps) / 1e6


# --- Safe pypylon Feature access ---------------------------------------------
# All helpers accept a pypylon camera (InstantCamera) OR an object exposing the
# same NodeMap-like interface (our DummyCamera does this).

def _get_node(camera: Any, name: str):
    """Return the GenApi node for ``name`` or None."""
    if camera is None:
        return None
    # pypylon: camera.GetNodeMap().GetNode(name); also camera has __getattr__ shortcut.
    try:
        node_map = camera.GetNodeMap()
        node = node_map.GetNode(name)
        return node
    except Exception:
        pass
    try:
        return getattr(camera, name)
    except Exception:
        return None


def has_feature(camera: Any, name: str) -> bool:
    node = _get_node(camera, name)
    if node is None:
        return False
    # IsAvailable / IsReadable are GenApi helpers
    try:
        import pypylon.genicam as gi  # type: ignore
        return bool(gi.IsAvailable(node))
    except Exception:
        # Dummy node or no genicam
        try:
            return bool(node.IsAvailable())
        except Exception:
            return True  # if we have a node and no way to check, assume yes


def is_readable(camera: Any, name: str) -> bool:
    node = _get_node(camera, name)
    if node is None:
        return False
    try:
        import pypylon.genicam as gi  # type: ignore
        return bool(gi.IsReadable(node))
    except Exception:
        try:
            return bool(node.IsReadable())
        except Exception:
            return True


def is_writable(camera: Any, name: str) -> bool:
    node = _get_node(camera, name)
    if node is None:
        return False
    try:
        import pypylon.genicam as gi  # type: ignore
        return bool(gi.IsWritable(node))
    except Exception:
        try:
            return bool(node.IsWritable())
        except Exception:
            return True


def get_feature_value(camera: Any, name: str, default: Any = None) -> Any:
    if not is_readable(camera, name):
        return default
    node = _get_node(camera, name)
    try:
        return node.GetValue()
    except Exception:
        # Try string for enums
        try:
            return node.ToString()
        except Exception:
            return default


def set_feature_value(camera: Any, name: str, value: Any) -> bool:
    """Set a feature, returning True on success.

    Handles numeric clamping to (min, max, inc) when relevant.
    """
    if not is_writable(camera, name):
        return False
    node = _get_node(camera, name)
    try:
        # Numeric features: try to clamp/round to inc
        if isinstance(value, (int, float)):
            mn, mx, inc = get_feature_min_max_inc(camera, name)
            if mn is not None and mx is not None:
                value = max(mn, min(mx, value))
                if inc:
                    value = mn + round((value - mn) / inc) * inc
                    value = max(mn, min(mx, value))
            # Use the proper setter based on feature type
            try:
                node.SetValue(value if not isinstance(value, float) or _is_float_node(node) else int(value))
                return True
            except Exception:
                pass
        # Enum / string features
        try:
            node.FromString(str(value))
            return True
        except Exception:
            pass
        try:
            node.SetValue(value)
            return True
        except Exception:
            return False
    except Exception:
        return False


def _is_float_node(node: Any) -> bool:
    try:
        return node.GetPrincipalInterfaceType() == 4  # intfIFloat
    except Exception:
        # heuristic
        return hasattr(node, "GetInc") and hasattr(node, "GetMin") and isinstance(
            getattr(node, "_dummy_kind", None), str
        ) and node._dummy_kind == "float"


def get_feature_min_max_inc(camera: Any, name: str) -> Tuple[Optional[float],
                                                             Optional[float],
                                                             Optional[float]]:
    """Return (min, max, inc) for a numeric feature, or (None, None, None)."""
    node = _get_node(camera, name)
    if node is None:
        return (None, None, None)
    try:
        mn = node.GetMin()
    except Exception:
        mn = None
    try:
        mx = node.GetMax()
    except Exception:
        mx = None
    try:
        inc = node.GetInc()
    except Exception:
        inc = None
    return (mn, mx, inc)


def get_enum_entries(camera: Any, name: str) -> list[str]:
    """Return available enum entry symbolic names for an enum feature."""
    node = _get_node(camera, name)
    if node is None:
        return []
    try:
        entries = node.GetEntries()
        out = []
        for e in entries:
            try:
                import pypylon.genicam as gi  # type: ignore
                if not gi.IsAvailable(e):
                    continue
            except Exception:
                pass
            try:
                out.append(e.GetSymbolic())
            except Exception:
                try:
                    out.append(e.ToString())
                except Exception:
                    continue
        return out
    except Exception:
        # Fallback for our dummy
        try:
            return list(node.GetSymbolics())
        except Exception:
            return []


# --- Resulting FPS detection -------------------------------------------------

RESULTING_FPS_FEATURES = (
    "BslResultingAcquisitionFrameRate",
    "ResultingAcquisitionFrameRate",
    "ResultingFrameRate",
)


def get_resulting_fps(camera: Any) -> Optional[float]:
    """Try several known feature names; return None if none exist."""
    for name in RESULTING_FPS_FEATURES:
        if has_feature(camera, name) and is_readable(camera, name):
            v = get_feature_value(camera, name)
            if v is not None:
                try:
                    return float(v)
                except Exception:
                    continue
    return None


# --- Image helpers ----------------------------------------------------------

def ycbcr422_to_rgb(frame) -> "np.ndarray":
    """Decode Basler ``YCbCr422_8`` (CbYCrY packed) → RGB8 numpy array.

    Accepts either ``(H, W, 2)`` or flat ``(H, W*2)`` layouts that pypylon
    may return. Uses BT.601 (SD) coefficients, which match what pylon's own
    Image Format Converter emits.
    """
    import numpy as np  # local — utils is also imported by recorder

    if frame.ndim == 3 and frame.shape[2] == 2:
        h, w_px, _ = frame.shape
        flat = frame.reshape(h, w_px * 2)
    elif frame.ndim == 2:
        flat = frame
    else:
        # Unknown layout — degrade to grayscale replicated to 3 channels
        gray = frame[..., 0] if frame.ndim == 3 else frame
        return np.stack([gray, gray, gray], axis=-1)

    h, total = flat.shape
    if total < 4 or total % 4 != 0:
        # Each 4-byte unit is one CbYCrY group (= 2 pixels). If we can't
        # group cleanly, just take the Y bytes and replicate.
        y = flat[:, 1::2]
        return np.stack([y, y, y], axis=-1)

    w_px = total // 2
    groups = flat.reshape(h, w_px // 2, 4).astype(np.float32)
    Cb = groups[..., 0]
    Y0 = groups[..., 1]
    Cr = groups[..., 2]
    Y1 = groups[..., 3]

    cb = Cb - 128.0
    cr = Cr - 128.0

    # BT.601 full-range coefficients
    r_term = 1.402 * cr
    g_term = -0.344136 * cb - 0.714136 * cr
    b_term = 1.772 * cb

    R0 = Y0 + r_term
    G0 = Y0 + g_term
    B0 = Y0 + b_term
    R1 = Y1 + r_term
    G1 = Y1 + g_term
    B1 = Y1 + b_term

    out = np.empty((h, w_px, 3), dtype=np.float32)
    out[:, 0::2, 0] = R0
    out[:, 0::2, 1] = G0
    out[:, 0::2, 2] = B0
    out[:, 1::2, 0] = R1
    out[:, 1::2, 1] = G1
    out[:, 1::2, 2] = B1
    return np.clip(out, 0, 255).astype(np.uint8)


def bayer_to_rgb(frame, pattern: str = "RG") -> "np.ndarray":
    """Half-resolution naive 2x2-block debayer for preview only.

    Each 2x2 Bayer cell is collapsed into one RGB pixel — the result is
    ``(H/2, W/2, 3)``. Recorded data is untouched; this is a cheap, dependency-
    free debayer good enough for live preview at any reasonable fps. For
    quantitative analysis the full-resolution raw frame is what's saved.

    ``pattern`` is the layout of the top-left 2x2 block:
      ``RG`` → ``[[R,G],[G,B]]``
      ``GR`` → ``[[G,R],[B,G]]``
      ``BG`` → ``[[B,G],[G,R]]``
      ``GB`` → ``[[G,B],[R,G]]``
    """
    import numpy as np

    h, w = frame.shape[:2]
    h2, w2 = h - (h % 2), w - (w % 2)
    f = frame[:h2, :w2]
    if f.dtype != np.uint8:
        f = autoscale_for_display(f)

    tl = f[0::2, 0::2]
    tr = f[0::2, 1::2]
    bl = f[1::2, 0::2]
    br = f[1::2, 1::2]

    # Each pattern: (red, blue, green_a, green_b) — the two greens are averaged.
    #   RG → [[R,G],[G,B]] : green at tr & bl
    #   GR → [[G,R],[B,G]] : green at tl & br
    #   BG → [[B,G],[G,R]] : green at tr & bl
    #   GB → [[G,B],[R,G]] : green at tl & br
    layout = {
        "RG": (tl, br, tr, bl),
        "GR": (tr, bl, tl, br),
        "BG": (br, tl, tr, bl),
        "GB": (bl, tr, tl, br),
    }
    r, b, g_a, g_b = layout.get(pattern.upper(), (tl, br, tr, bl))
    g = ((g_a.astype(np.uint16) + g_b.astype(np.uint16)) // 2).astype(np.uint8)
    return np.stack([r, g, b], axis=-1)


def autoscale_for_display(frame, target_dtype="uint8"):
    """Scale a 16-bit / 12-bit frame to 8-bit for display.

    Uses simple min/max scaling. Returns numpy.ndarray of target dtype.
    """
    import numpy as np
    if frame is None:
        return None
    if frame.dtype == np.uint8 and target_dtype == "uint8":
        return frame
    if frame.size == 0:
        return frame.astype(target_dtype)
    fmin = float(frame.min())
    fmax = float(frame.max())
    if fmax <= fmin:
        return np.zeros_like(frame, dtype=target_dtype)
    scaled = (frame.astype("float32") - fmin) / (fmax - fmin) * 255.0
    return scaled.astype(target_dtype)


@dataclass
class CameraInfo:
    model_name: str = ""
    serial_number: str = ""
    device_class: str = ""        # e.g. "BaslerUsb", "BaslerGigE"
    friendly_name: str = ""
    is_dummy: bool = False

    def short(self) -> str:
        kind = "DUMMY" if self.is_dummy else self.device_class
        return f"{self.model_name} [{self.serial_number}] ({kind})"
