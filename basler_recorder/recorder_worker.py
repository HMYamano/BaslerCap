"""Recorder worker (separate OS process).

This is launched with ``multiprocessing.Process`` so that heavy file I/O does
not contend with the GUI thread or the camera acquisition thread for the GIL
(à la YORU's multi-process architecture).

The worker pulls ``(frame_ndarray, timestamp_ns, frame_id)`` tuples from a
``multiprocessing.Queue`` and writes them to disk in one of the supported
formats:

- ``tiff_sequence`` : one TIFF file per frame, zero-padded sequence number
- ``bigtiff``       : a single multi-page BigTIFF file
- ``raw``           : flat little-endian binary with a sidecar ``.json``
                      describing shape/dtype
- ``mp4`` / ``avi`` : 8-bit video files via OpenCV VideoWriter

Per-frame timestamps are written to ``timestamps.csv`` (frame_id,
timestamp_ns, wallclock_ns).

For everything except the writer process itself, prefer the high-level
``RecorderManager`` class — it handles spawning, the status queue, and a
clean shutdown.
"""
from __future__ import annotations

import json
import logging
import multiprocessing as mp
import os
import re
import time
from dataclasses import dataclass, field, asdict
from queue import Empty
from typing import Optional

import numpy as np

from .utils import ycbcr422_to_rgb

log = logging.getLogger(__name__)


# Sentinel: tells the writer process to flush and exit cleanly.
STOP_SENTINEL = "__STOP__"
VIDEO_FORMATS = {
    "mp4": (".mp4", "mp4v"),
    "avi": (".avi", "MJPG"),
}


# --- Job description --------------------------------------------------------

@dataclass
class RecorderJob:
    output_dir: str
    file_prefix: str
    save_format: str            # "mp4" | "avi" | "tiff_sequence" | "bigtiff" | "raw"
    width: int
    height: int
    pixel_format: str           # for metadata only; dtype derived from first frame
    dtype: str                  # "uint8" or "uint16"
    fps: float
    max_frames: Optional[int] = None
    max_seconds: Optional[float] = None
    metadata: dict = field(default_factory=dict)


# --- Manager: spawns the process & owns the queues --------------------------

class RecorderManager:
    """High-level wrapper that the GUI/main process talks to."""

    def __init__(self, max_queue: int = 256):
        self._frame_queue: Optional[mp.Queue] = None
        self._status_queue: Optional[mp.Queue] = None
        self._process: Optional[mp.Process] = None
        self._max_queue = max_queue
        self._job: Optional[RecorderJob] = None
        self._started_at: Optional[float] = None

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.is_alive()

    @property
    def frame_queue(self) -> Optional[mp.Queue]:
        return self._frame_queue

    @property
    def status_queue(self) -> Optional[mp.Queue]:
        return self._status_queue

    @property
    def job(self) -> Optional[RecorderJob]:
        return self._job

    def start(self, job: RecorderJob, ready_timeout: float = 10.0) -> None:
        if self.is_running:
            raise RuntimeError("Recorder already running")
        os.makedirs(job.output_dir, exist_ok=True)
        self._frame_queue = mp.Queue(maxsize=self._max_queue)
        self._status_queue = mp.Queue(maxsize=256)
        self._job = job
        self._started_at = time.time()
        self._process = mp.Process(
            target=_recorder_main,
            args=(job, self._frame_queue, self._status_queue),
            daemon=True,
        )
        self._process.start()
        try:
            self._wait_until_ready(ready_timeout)
        except Exception:
            self._cleanup_failed_start()
            raise

    def _wait_until_ready(self, timeout: float) -> None:
        if self._process is None or self._status_queue is None:
            raise RuntimeError("Recorder process was not started")

        deadline = time.time() + max(0.1, timeout)
        last_error = ""
        while time.time() < deadline:
            try:
                msg = self._status_queue.get(timeout=0.05)
            except Empty:
                if not self._process.is_alive():
                    break
                continue

            if msg.get("ready"):
                return
            if msg.get("error"):
                last_error = str(msg.get("error") or "")
            if msg.get("finished"):
                break

        if last_error:
            raise RuntimeError(last_error)
        if self._process is not None and not self._process.is_alive():
            raise RuntimeError("Recorder process exited before it was ready.")
        raise RuntimeError("Timed out waiting for recorder process to become ready.")

    def _cleanup_failed_start(self) -> None:
        if self._process is not None and self._process.is_alive():
            try:
                self._process.join(timeout=1.0)
                if self._process.is_alive():
                    self._process.terminate()
                    self._process.join(timeout=2.0)
            except Exception:
                pass
        self._frame_queue = None
        self._status_queue = None
        self._process = None
        self._job = None

    def stop(self, timeout: float = 10.0) -> None:
        if self._frame_queue is not None:
            try:
                self._frame_queue.put(STOP_SENTINEL, timeout=2.0)
            except Exception:
                pass
        if self._process is not None:
            self._process.join(timeout=timeout)
            if self._process.is_alive():
                log.warning("Recorder process did not exit, terminating.")
                self._process.terminate()
                self._process.join(timeout=2.0)
        self._frame_queue = None
        self._status_queue = None
        self._process = None

    def queue_length(self) -> int:
        if self._frame_queue is None:
            return 0
        try:
            return self._frame_queue.qsize()
        except Exception:
            return 0


# --- The worker entry point -------------------------------------------------

def _recorder_main(job: RecorderJob, frame_q: mp.Queue, status_q: mp.Queue):
    """Process entry point. Must be a top-level function (picklable)."""
    # Reduce logging spam from libraries when spawned
    logging.basicConfig(level=logging.INFO, format="[recorder] %(message)s")
    writer = None
    timestamps_fp = None
    frames_written = 0
    bytes_written = 0
    started = time.time()
    last_status = started
    fatal_error = ""

    try:
        writer = _make_writer(job)
        ts_path = os.path.join(job.output_dir, f"{job.file_prefix}_timestamps.csv")
        timestamps_fp = open(ts_path, "w", buffering=1024 * 1024, encoding="utf-8")
        timestamps_fp.write("frame_id,camera_timestamp_ns,wallclock_ns\n")
        _send_status(status_q, frames_written, bytes_written, started, ready=True)

        while True:
            try:
                item = frame_q.get(timeout=1.0)
            except Empty:
                # Heartbeat
                _send_status(status_q, frames_written, bytes_written, started)
                continue

            if isinstance(item, str) and item == STOP_SENTINEL:
                break

            frame, ts_ns, fid = item
            try:
                writer.write_frame(
                    frame,
                    frame_id=fid,
                    frames_written=frames_written,
                    cam_ts_ns=ts_ns,
                )
                timestamps_fp.write(f"{fid},{ts_ns},{time.time_ns()}\n")
                frames_written += 1
                bytes_written += int(frame.nbytes)
            except Exception as e:
                log.exception("Write failed for frame %d: %s", fid, e)
                _send_status(status_q, frames_written, bytes_written, started,
                             error=str(e))
                # Continue trying — drop this frame.

            now = time.time()
            if now - last_status >= 0.5:
                _send_status(status_q, frames_written, bytes_written, started)
                last_status = now
    except Exception as e:
        fatal_error = str(e)
        log.exception("Recorder failed: %s", e)
        _send_status(status_q, frames_written, bytes_written, started,
                     finished=True, error=fatal_error)
    finally:
        if writer is not None:
            try:
                writer.close()
            except Exception:
                log.exception("Failed to close writer")
        if timestamps_fp is not None:
            try:
                timestamps_fp.close()
            except Exception:
                pass

        # Write final metadata
        try:
            meta = dict(job.metadata)
            meta.update({
                "number_of_recorded_frames": frames_written,
                "recording_end_time": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "bytes_written": bytes_written,
            })
            meta_path = os.path.join(job.output_dir, f"{job.file_prefix}_metadata.json")
            if fatal_error:
                meta["error"] = fatal_error
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2, default=str)
        except Exception:
            log.exception("Failed to write metadata.json")

        _send_status(status_q, frames_written, bytes_written, started,
                     finished=True, error=fatal_error)


def _send_status(status_q: mp.Queue, frames: int, bytes_written: int,
                 started: float, finished: bool = False, error: str = "",
                 ready: bool = False):
    try:
        status_q.put_nowait({
            "frames_written": frames,
            "bytes_written": bytes_written,
            "elapsed_s": time.time() - started,
            "finished": finished,
            "error": error,
            "ready": ready,
        })
    except Exception:
        pass


# --- Writer implementations -------------------------------------------------

class _WriterBase:
    def write_frame(self, frame: np.ndarray, frame_id: int,
                    frames_written: int, cam_ts_ns: int = 0) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass


class _TiffSequenceWriter(_WriterBase):
    def __init__(self, job: RecorderJob):
        import tifffile  # local import — avoids hard dep when not used
        self._tf = tifffile
        self._dir = job.output_dir
        self._prefix = job.file_prefix

    def write_frame(self, frame, frame_id, frames_written, cam_ts_ns=0):
        path = os.path.join(self._dir, f"{self._prefix}_{frames_written:08d}.tif")
        self._tf.imwrite(path, frame, photometric="minisblack")


class _BigTiffWriter(_WriterBase):
    def __init__(self, job: RecorderJob):
        import tifffile
        self._path = os.path.join(job.output_dir, f"{job.file_prefix}.tiff")
        self._writer = tifffile.TiffWriter(self._path, bigtiff=True, append=False)

    def write_frame(self, frame, frame_id, frames_written, cam_ts_ns=0):
        self._writer.write(frame, photometric="minisblack", contiguous=True)

    def close(self):
        try:
            self._writer.close()
        except Exception:
            pass


class _RawWriter(_WriterBase):
    def __init__(self, job: RecorderJob):
        self._path = os.path.join(job.output_dir, f"{job.file_prefix}.raw")
        self._fp = open(self._path, "wb", buffering=1024 * 1024)
        self._meta = {
            "width": job.width,
            "height": job.height,
            "dtype": job.dtype,
            "pixel_format": job.pixel_format,
            "byteorder": "little",
        }
        meta_path = os.path.join(job.output_dir, f"{job.file_prefix}_raw.json")
        with open(meta_path, "w") as f:
            json.dump(self._meta, f, indent=2)

    def write_frame(self, frame, frame_id, frames_written, cam_ts_ns=0):
        # Ensure consistent shape/dtype; the raw container assumes fixed geometry.
        self._fp.write(frame.tobytes(order="C"))

    def close(self):
        try:
            self._fp.close()
        except Exception:
            pass


def _nominal_max_for_video(pixel_format: str, dtype: np.dtype) -> Optional[float]:
    if np.dtype(dtype) == np.uint8:
        return 255.0
    pf = pixel_format or ""
    match = re.search(r"(?:Mono|Bayer|RGB|BGR|YUV|YCbCr)[A-Za-z_]*?(8|10|12|16)", pf)
    if match:
        return float((1 << int(match.group(1))) - 1)
    if np.issubdtype(np.dtype(dtype), np.integer):
        return float(np.iinfo(dtype).max)
    return None


def _to_uint8_for_video(frame: np.ndarray, pixel_format: str) -> np.ndarray:
    if frame.dtype == np.uint8:
        return frame
    max_value = _nominal_max_for_video(pixel_format, frame.dtype)
    if max_value is None or max_value <= 0:
        fmin = float(frame.min()) if frame.size else 0.0
        fmax = float(frame.max()) if frame.size else 0.0
        if fmax <= fmin:
            return np.zeros_like(frame, dtype=np.uint8)
        scaled = (frame.astype(np.float32, copy=False) - fmin) * (255.0 / (fmax - fmin))
    else:
        scaled = np.clip(frame.astype(np.float32, copy=False), 0.0, max_value)
        scaled *= 255.0 / max_value
    return scaled.astype(np.uint8)


def _frame_to_bgr8(frame: np.ndarray, pixel_format: str, cv2) -> np.ndarray:
    """Convert a raw camera frame to contiguous BGR8 for cv2.VideoWriter."""
    pf = pixel_format or ""

    if "YCbCr" in pf or "YUV" in pf:
        rgb = ycbcr422_to_rgb(frame)
        return np.ascontiguousarray(rgb[..., ::-1])

    if pf.startswith("Bayer") and len(pf) >= 7 and frame.ndim == 2:
        pattern = pf[5:7].upper()
        code = {
            "RG": cv2.COLOR_BayerRG2BGR,
            "GR": cv2.COLOR_BayerGR2BGR,
            "BG": cv2.COLOR_BayerBG2BGR,
            "GB": cv2.COLOR_BayerGB2BGR,
        }.get(pattern)
        if code is not None:
            mono8 = _to_uint8_for_video(frame, pf)
            return cv2.cvtColor(mono8, code)

    if frame.ndim == 3:
        arr = frame
        if arr.shape[0] in (3, 4) and arr.shape[2] not in (3, 4):
            arr = np.moveaxis(arr[:3], 0, -1)
        arr = _to_uint8_for_video(arr[..., :3], pf)
        if "RGB" in pf and "BGR" not in pf:
            arr = arr[..., ::-1]
        elif "BGR" not in pf:
            gray = arr[..., 0]
            return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
        return np.ascontiguousarray(arr)

    if frame.ndim == 2 and ("RGB" in pf or "BGR" in pf) and frame.shape[1] % 3 == 0:
        arr = frame.reshape(frame.shape[0], frame.shape[1] // 3, 3)
        arr = _to_uint8_for_video(arr, pf)
        if "RGB" in pf and "BGR" not in pf:
            arr = arr[..., ::-1]
        return np.ascontiguousarray(arr)

    gray = _to_uint8_for_video(frame, pf)
    return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)


class _VideoWriter(_WriterBase):
    def __init__(self, job: RecorderJob):
        try:
            import cv2  # type: ignore
        except Exception as e:
            raise RuntimeError(
                "MP4/AVI recording requires opencv-python. "
                "Install it with: python -m pip install opencv-python"
            ) from e

        fmt = job.save_format.lower()
        ext, fourcc_name = VIDEO_FORMATS[fmt]
        self._cv2 = cv2
        self._path = os.path.join(job.output_dir, f"{job.file_prefix}{ext}")
        self._fourcc_name = fourcc_name
        self._fps = float(job.fps) if job.fps and job.fps > 0 else 30.0
        self._pixel_format = job.pixel_format
        self._size = self._video_size(job.width, job.height)
        self._writer = self._open()

    @staticmethod
    def _video_size(width: int, height: int) -> tuple[int, int]:
        w = int(width)
        h = int(height)
        if w < 2 or h < 2:
            raise RuntimeError(f"Video frame is too small: {w}x{h}")
        if w % 2:
            w -= 1
        if h % 2:
            h -= 1
        return w, h

    def _open(self):
        fourcc = self._cv2.VideoWriter_fourcc(*self._fourcc_name)
        writer = self._cv2.VideoWriter(self._path, fourcc, self._fps, self._size)
        if not writer.isOpened():
            raise RuntimeError(
                f"Could not open video writer for {self._path} "
                f"with codec {self._fourcc_name}."
            )
        return writer

    def write_frame(self, frame, frame_id, frames_written, cam_ts_ns=0):
        bgr = _frame_to_bgr8(frame, self._pixel_format, self._cv2)
        w, h = self._size
        if bgr.shape[1] != w or bgr.shape[0] != h:
            bgr = bgr[:h, :w]
            if bgr.shape[1] != w or bgr.shape[0] != h:
                bgr = self._cv2.resize(bgr, self._size, interpolation=self._cv2.INTER_AREA)
        self._writer.write(np.ascontiguousarray(bgr))

    def close(self):
        if self._writer is not None:
            self._writer.release()
            self._writer = None


class _Hdf5Writer(_WriterBase):
    """HDF5 writer using a single chunked, growable ``frames`` dataset.

    Layout:
        /frames           uint8|uint16, shape (N, H, W) or (N, H, W, C), chunked per-frame
        /frame_ids        uint64, shape (N,)
        /camera_timestamp_ns  uint64, shape (N,)
        /wallclock_ns     uint64, shape (N,)
    File attributes mirror the RecorderJob metadata so the file is self-describing.

    Compression: ``lzf`` is used by default — it's bundled with h5py, near-lossless
    speed-wise, and gives a useful size reduction on natural images. Use
    ``compression=None`` if absolute peak throughput matters.
    """

    DEFAULT_COMPRESSION = "lzf"

    def __init__(self, job: RecorderJob):
        try:
            import h5py  # local import — only needed when HDF5 is requested
        except Exception as e:
            raise RuntimeError(
                "HDF5 recording requires h5py. "
                "Install it with: python -m pip install h5py"
            ) from e

        self._h5py = h5py
        self._path = os.path.join(job.output_dir, f"{job.file_prefix}.h5")
        self._file = h5py.File(self._path, "w")
        self._dset = None  # opened lazily on first frame so shape/dtype match exactly
        self._fid_dset = None
        self._cam_ts_dset = None
        self._wc_ts_dset = None
        self._frame_shape: Optional[tuple] = None
        self._frame_dtype = None
        self._job = job

        # Write the job's metadata as root-level attributes so the file is
        # self-describing without the sidecar .json (which we still write).
        for k, v in (job.metadata or {}).items():
            try:
                if v is None:
                    self._file.attrs[k] = "null"
                elif isinstance(v, (bool, int, float, str)):
                    self._file.attrs[k] = v
                else:
                    self._file.attrs[k] = json.dumps(v, default=str)
            except Exception:
                # Some types (e.g. nested dicts beyond JSON) might fail — skip them.
                pass

    def _ensure_open(self, frame: np.ndarray) -> None:
        if self._dset is not None:
            return
        self._frame_shape = tuple(frame.shape)
        self._frame_dtype = frame.dtype
        max_shape = (None,) + self._frame_shape
        # Chunk = one frame. This is the natural unit and avoids the chunk-cache
        # having to assemble multiple frames before flushing.
        chunks = (1,) + self._frame_shape
        try:
            self._dset = self._file.create_dataset(
                "frames",
                shape=(0,) + self._frame_shape,
                maxshape=max_shape,
                dtype=self._frame_dtype,
                chunks=chunks,
                compression=self.DEFAULT_COMPRESSION,
            )
        except Exception:
            # Some builds of h5py may not have lzf — retry without compression.
            self._dset = self._file.create_dataset(
                "frames",
                shape=(0,) + self._frame_shape,
                maxshape=max_shape,
                dtype=self._frame_dtype,
                chunks=chunks,
            )
        self._dset.attrs["pixel_format"] = self._job.pixel_format
        self._dset.attrs["width"] = int(self._job.width)
        self._dset.attrs["height"] = int(self._job.height)

        self._fid_dset = self._file.create_dataset(
            "frame_ids", shape=(0,), maxshape=(None,), dtype="uint64", chunks=(1024,)
        )
        self._cam_ts_dset = self._file.create_dataset(
            "camera_timestamp_ns", shape=(0,), maxshape=(None,),
            dtype="uint64", chunks=(1024,),
        )
        self._wc_ts_dset = self._file.create_dataset(
            "wallclock_ns", shape=(0,), maxshape=(None,),
            dtype="uint64", chunks=(1024,),
        )

    def write_frame(self, frame, frame_id, frames_written, cam_ts_ns=0):
        self._ensure_open(frame)
        if frame.shape != self._frame_shape or frame.dtype != self._frame_dtype:
            raise RuntimeError(
                f"HDF5 frame shape/dtype changed mid-recording "
                f"(expected {self._frame_shape}/{self._frame_dtype}, "
                f"got {frame.shape}/{frame.dtype})"
            )
        n = frames_written
        new_size = n + 1
        self._dset.resize((new_size,) + self._frame_shape)
        self._dset[n] = frame
        self._fid_dset.resize((new_size,))
        self._fid_dset[n] = np.uint64(frame_id)
        self._cam_ts_dset.resize((new_size,))
        self._cam_ts_dset[n] = np.uint64(cam_ts_ns)
        self._wc_ts_dset.resize((new_size,))
        self._wc_ts_dset[n] = np.uint64(time.time_ns())

    def close(self):
        try:
            if self._file is not None:
                self._file.flush()
                self._file.close()
        except Exception:
            pass
        self._file = None


def _make_writer(job: RecorderJob) -> _WriterBase:
    fmt = job.save_format.lower()
    if fmt in VIDEO_FORMATS:
        return _VideoWriter(job)
    if fmt == "tiff_sequence":
        return _TiffSequenceWriter(job)
    if fmt == "bigtiff":
        return _BigTiffWriter(job)
    if fmt == "raw":
        return _RawWriter(job)
    if fmt == "hdf5":
        return _Hdf5Writer(job)
    raise ValueError(f"Unknown save_format: {job.save_format}")
