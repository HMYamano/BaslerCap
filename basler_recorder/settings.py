"""Application settings + preset save / load."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from typing import Optional


PRESET_DIR_DEFAULT = os.path.abspath(os.path.join(
    os.path.dirname(__file__), os.pardir, "presets"
))
APP_STATE_DIR_DEFAULT = os.path.abspath(os.path.join(
    os.path.dirname(__file__), os.pardir, "app_state"
))
LAST_SESSION_PATH_DEFAULT = os.path.join(
    APP_STATE_DIR_DEFAULT, "last_session.json"
)

DEFAULT_EXPOSURE_TIME_US = 800.0
DEFAULT_GAIN_DB = 13.0
DEFAULT_TARGET_FPS = 1000.0


@dataclass
class CameraPreset:
    name: str = "default"
    exposure_time_us: Optional[float] = None
    gain: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None
    offset_x: Optional[int] = None
    offset_y: Optional[int] = None
    pixel_format: Optional[str] = None
    target_fps: Optional[float] = None
    fps_enable: Optional[bool] = None
    trigger_mode: Optional[str] = None
    trigger_source: Optional[str] = None
    trigger_activation: Optional[str] = None
    save_format: Optional[str] = None
    output_folder: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "CameraPreset":
        fields = {k: d.get(k) for k in cls.__dataclass_fields__.keys()}
        return cls(**fields)


def ensure_preset_dir(directory: str = PRESET_DIR_DEFAULT) -> str:
    os.makedirs(directory, exist_ok=True)
    return directory


def save_preset(preset: CameraPreset, directory: str = PRESET_DIR_DEFAULT) -> str:
    ensure_preset_dir(directory)
    safe = "".join(c for c in preset.name if c.isalnum() or c in ("_", "-")).strip("_")
    if not safe:
        safe = "preset"
    path = os.path.join(directory, f"{safe}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(preset.to_dict(), f, indent=2)
    return path


def load_preset(path: str) -> CameraPreset:
    with open(path, "r", encoding="utf-8") as f:
        d = json.load(f)
    return CameraPreset.from_dict(d)


def list_presets(directory: str = PRESET_DIR_DEFAULT) -> list[str]:
    ensure_preset_dir(directory)
    return sorted(
        os.path.join(directory, n)
        for n in os.listdir(directory)
        if n.lower().endswith(".json")
    )


def ensure_app_state_dir(directory: str = APP_STATE_DIR_DEFAULT) -> str:
    os.makedirs(directory, exist_ok=True)
    return directory


def save_last_session_state(
    state: dict,
    path: str = LAST_SESSION_PATH_DEFAULT,
) -> str:
    ensure_app_state_dir(os.path.dirname(path) or ".")
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, default=str)
    os.replace(tmp_path, path)
    return path


def load_last_session_state(path: str = LAST_SESSION_PATH_DEFAULT) -> Optional[dict]:
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        state = json.load(f)
    return state if isinstance(state, dict) else None
