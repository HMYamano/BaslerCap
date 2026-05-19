"""Camera acquisition worker (QThread).

Lives in the Qt event-loop world, owns the camera (which cannot be shared
across processes), and:

- pulls frames as fast as the camera delivers
- forwards a throttled stream to the GUI for preview (~30 fps)
- forwards EVERY frame to the recorder (a multiprocessing.Queue) when
  recording is active

The recorder itself runs in a separate process (see ``recorder_worker``). This
hybrid model is intentional: pypylon objects don't pickle, but file I/O does
benefit from being a real OS process.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from queue import Full
from typing import Optional

import numpy as np
from PySide6.QtCore import QThread, Signal

from .camera_controller import CameraController

log = logging.getLogger(__name__)

RECORD_PROGRESS_INTERVAL_S = 0.2


@dataclass
class FrameInfo:
    width: int
    height: int
    pixel_format: str
    frame_id: int
    timestamp_ns: int


class CameraWorker(QThread):
    # Emitted with the latest (throttled) preview frame
    new_preview_frame = Signal(np.ndarray, object)  # (frame, FrameInfo)

    # Emitted regularly with (grab_fps, preview_fps, dropped_frames)
    fps_update = Signal(float, float, int)

    # Emitted when a grab error occurs (string)
    error = Signal(str)

    # Emitted with (recorded_frames, queue_len) while recording
    recording_progress = Signal(int, int)

    def __init__(self, controller: CameraController, parent=None):
        super().__init__(parent)
        self._controller = controller
        self._stop_requested = False
        self._preview_fps_target = 30.0

        # Recording state
        self._record_queue = None  # multiprocessing.Queue
        self._recording = False
        self._record_max_frames: Optional[int] = None
        self._record_max_seconds: Optional[float] = None
        self._record_start_time: Optional[float] = None
        self._record_frames_sent = 0
        self._record_dropped = 0
        self._record_put_timeout_s = 0.002
        self._last_recording_progress_emit = 0.0

        # FPS stats
        self._last_grab_t = 0.0
        self._grab_fps_ema = 0.0
        self._preview_fps_ema = 0.0
        self._last_preview_emit = 0.0
        self._last_stats_emit = 0.0
        self._grab_count_window = 0
        self._preview_count_window = 0
        self._stats_window_start = 0.0

    # --- public control --------------------------------------------------

    def request_stop(self) -> None:
        self._stop_requested = True

    def start_recording(self, queue, max_frames: Optional[int],
                        max_seconds: Optional[float],
                        target_fps: Optional[float] = None) -> None:
        self._record_queue = queue
        self._record_max_frames = max_frames
        self._record_max_seconds = max_seconds
        self._record_start_time = time.time()
        self._record_frames_sent = 0
        self._record_dropped = 0
        if target_fps and target_fps > 0:
            self._record_put_timeout_s = min(0.010, max(0.001, 0.25 / target_fps))
        else:
            self._record_put_timeout_s = 0.002
        self._last_recording_progress_emit = 0.0
        self._recording = True

    def stop_recording(self) -> int:
        self._recording = False
        self._record_queue = None
        return self._record_frames_sent

    @property
    def recording(self) -> bool:
        return self._recording

    @property
    def dropped(self) -> int:
        return self._record_dropped

    # --- thread body -----------------------------------------------------

    def run(self) -> None:
        try:
            self._controller.start_grabbing()
        except Exception as e:
            self.error.emit(f"Failed to start grabbing: {e}")
            return

        try:
            self._stats_window_start = time.time()
            while not self._stop_requested:
                result = self._controller.retrieve(timeout_ms=1000)
                if result is None:
                    continue
                try:
                    if not result.GrabSucceeded():
                        result.Release()
                        continue

                    frame = np.array(result.GetArray(), copy=True)  # detach from buffer
                    fid = int(result.GetImageNumber())
                    try:
                        ts_ns = int(result.GetTimeStamp())
                    except Exception:
                        ts_ns = time.time_ns()
                    result.Release()
                except Exception as e:
                    log.debug("Frame parse error: %s", e)
                    try:
                        result.Release()
                    except Exception:
                        pass
                    continue

                self._grab_count_window += 1

                info = FrameInfo(
                    width=int(frame.shape[1]),
                    height=int(frame.shape[0]),
                    pixel_format=self._controller.pixel_format(),
                    frame_id=fid,
                    timestamp_ns=ts_ns,
                )

                # --- forward to recorder (every frame) ------------------
                if self._recording and self._record_queue is not None:
                    record_now = time.time()
                    try:
                        self._record_queue.put(
                            (frame, ts_ns, fid),
                            timeout=self._record_put_timeout_s,
                        )
                        self._record_frames_sent += 1
                    except Full:
                        self._record_dropped += 1
                        if self._record_dropped % 30 == 1:
                            log.warning(
                                "Recorder queue full; dropping frames (total=%d)",
                                self._record_dropped,
                            )
                    if (
                        record_now - self._last_recording_progress_emit
                        >= RECORD_PROGRESS_INTERVAL_S
                    ):
                        qlen = 0
                        try:
                            qlen = self._record_queue.qsize()
                        except Exception:
                            pass
                        self.recording_progress.emit(self._record_frames_sent, qlen)
                        self._last_recording_progress_emit = record_now

                    # Auto-stop on duration / frame count
                    if self._record_max_frames is not None and \
                       self._record_frames_sent >= self._record_max_frames:
                        self._recording = False
                    elif self._record_max_seconds is not None and \
                         self._record_start_time is not None and \
                         (record_now - self._record_start_time) >= self._record_max_seconds:
                        self._recording = False

                # --- preview throttling --------------------------------
                now = time.time()
                preview_dt = 1.0 / max(self._preview_fps_target, 1.0)
                if now - self._last_preview_emit >= preview_dt:
                    self._last_preview_emit = now
                    self._preview_count_window += 1
                    self.new_preview_frame.emit(frame, info)

                # --- stats emit ~1 Hz ----------------------------------
                if now - self._stats_window_start >= 1.0:
                    dt = now - self._stats_window_start
                    grab_fps = self._grab_count_window / dt if dt > 0 else 0.0
                    prev_fps = self._preview_count_window / dt if dt > 0 else 0.0
                    # EMA smoothing
                    self._grab_fps_ema = (
                        grab_fps if self._grab_fps_ema == 0
                        else 0.6 * self._grab_fps_ema + 0.4 * grab_fps
                    )
                    self._preview_fps_ema = (
                        prev_fps if self._preview_fps_ema == 0
                        else 0.6 * self._preview_fps_ema + 0.4 * prev_fps
                    )
                    self.fps_update.emit(self._grab_fps_ema,
                                         self._preview_fps_ema,
                                         self._record_dropped)
                    self._grab_count_window = 0
                    self._preview_count_window = 0
                    self._stats_window_start = now
        except Exception as e:
            log.exception("CameraWorker crashed: %s", e)
            self.error.emit(f"Camera worker crashed: {e}")
        finally:
            try:
                self._controller.stop_grabbing()
            except Exception:
                pass
