"""Metadata dictionary builder + helpers."""
from __future__ import annotations

import datetime as dt
import json
import os
import time
from typing import Optional

from . import APP_NAME, APP_VERSION
from .camera_controller import CameraController
from .utils import (
    CameraInfo,
    bytes_per_pixel,
    estimated_data_rate_mb_s,
)


def build_metadata(*,
                   controller: CameraController,
                   save_format: str,
                   output_path: str,
                   target_fps: Optional[float],
                   resulting_max_fps: Optional[float],
                   user_note: str = "",
                   recording_start_time: Optional[str] = None) -> dict:
    info: Optional[CameraInfo] = controller.info
    width, height, off_x, off_y = controller.roi() if controller.is_open else (0, 0, 0, 0)
    pix = controller.pixel_format() if controller.is_open else "Mono8"
    exposure = controller.exposure_us() if controller.is_open else None
    gain = controller.gain() if controller.is_open else None

    data_rate = estimated_data_rate_mb_s(
        width, height, pix, resulting_max_fps or target_fps or 0.0
    )

    return {
        "app_name": APP_NAME,
        "app_version": APP_VERSION,
        "datetime": dt.datetime.now().isoformat(timespec="seconds"),
        "camera_model": info.model_name if info else "",
        "serial_number": info.serial_number if info else "",
        "device_class": info.device_class if info else "",
        "is_dummy": bool(info.is_dummy) if info else False,
        "width": int(width),
        "height": int(height),
        "offset_x": int(off_x),
        "offset_y": int(off_y),
        "exposure_time_us": float(exposure) if exposure is not None else None,
        "gain": float(gain) if gain is not None else None,
        "pixel_format": pix,
        "bytes_per_pixel": bytes_per_pixel(pix),
        "target_fps": float(target_fps) if target_fps is not None else None,
        "resulting_max_fps": float(resulting_max_fps) if resulting_max_fps is not None else None,
        "estimated_data_rate_MB_s": float(data_rate),
        "save_format": save_format,
        "output_path": os.path.abspath(output_path),
        "recording_start_time": recording_start_time or time.strftime("%Y-%m-%dT%H:%M:%S"),
        # These are filled in by the recorder process at the end:
        "recording_end_time": None,
        "number_of_recorded_frames": 0,
        "dropped_frame_count": 0,
        "user_note": user_note,
    }


def save_metadata_json(meta: dict, path: str) -> None:
    """Write metadata to a JSON file (utility used for snapshots)."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, default=str)
