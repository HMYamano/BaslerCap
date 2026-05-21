# Basler Recorder

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux-lightgrey.svg)]()

A generic, Python-based GUI recording app for Basler USB3 Vision / GigE Vision cameras.
The official **pylon Viewer** is great, but is awkward for routine recording / experiment
setup. This app aims to be a focused alternative:

- behavior recording
- microscopy recording
- high-speed ROI capture
- ordinary live preview
- pre-experiment camera setup
- external trigger capture (software trigger; hardware Line1/Line2 supported)

It is **not** specific to any one experiment; it is a general-purpose recorder.

---

## Status

- **v0.1** (camera + live preview + snapshot + dummy camera): done
- **v0.2** (MP4/AVI/TIFF/BigTIFF/raw recording + metadata JSON + per-frame timestamps): done
- **v0.3** (preset save/load, BigTIFF, trigger UI, dropped-frame detection,
  HDF5, drag-rectangle ROI, histogram + saturation overlay,
  software trigger pulse): done — see _Known limitations_ for any caveats.

---

## Quick start

```powershell
git clone https://github.com/HMYamano/BaslerCap.git
cd BaslerCap
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip -r requirements.txt
python main.py
```

No real Basler camera? The app still launches in **dummy camera** mode.

---

## Requirements

- Windows 10 / 11 (primary target; should also work on Linux)
- Python 3.10 or newer
- [Basler pylon Software Suite](https://www.baslerweb.com/en/downloads/software-downloads/)
  installed system-wide. This is the C++ runtime that `pypylon` wraps; the Python
  package alone is **not** sufficient for talking to real cameras.

### Python packages

See `requirements.txt`. Install with:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -r requirements.txt
```

If you only want to try the GUI in **dummy camera mode** (no real Basler hardware),
you can skip `pypylon` — the app will still launch and report that pypylon is not
available.

---

## Running

### Easy path (Windows + conda)

A project-local conda environment is created under `env\` by `setup.bat`:

```powershell
setup.bat    REM one time only — creates .\env and installs requirements
run.bat      REM every time — activates .\env and launches main.py
```

`setup.bat` looks for `%USERPROFILE%\miniconda3` (then `anaconda3`).
Both scripts contain only ASCII.

### Manual

```powershell
python main.py
```

No CLI arguments. Everything is configured from the GUI.

---

## Usage walkthrough

### 1. Connecting a Basler camera

1. Plug in the camera and wait until Windows enumerates it.
2. Click **Refresh** in the *Camera connection* panel.
3. The drop-down lists every detected device plus one **DUMMY** entry.
4. Select the device and click **Connect**.
5. If there is exactly one real camera, it is auto-selected.

If pypylon is missing or no cameras are found, only the dummy entry appears — the
app does not crash.

### 2. Dummy camera mode

The dummy device generates a moving sinusoid pattern and honors width/height/
offset/pixel-format/fps/exposure changes. Use it to develop and rehearse without
real hardware:

1. Pick `DummyBasler-1920x1200 [DUMMY-0000] (DUMMY)` from the drop-down.
2. Click **Connect**, then **Start Live**.

### 3. Live preview

Click **Start Live**. The image is rendered with pyqtgraph and throttled to
~30 fps regardless of grab fps. 12-/16-bit frames are auto-scaled to 8-bit for
display only — the saved data keeps the original bit-depth.

### 4. Changing ROI

In the *ROI* panel:

- Enter `Width`, `Height`, `OffsetX`, `OffsetY` and click **Apply ROI**.
- Or pick a width/height and click **Center ROI**.
- Or click **Full Frame** to reset.

The app stops grabbing, resets offsets to 0, then applies width/height/offsets
in the safe order so that pylon never complains about overflow. The final
*actual* values are read back and shown.

ROI is locked while recording.

### 5. Pixel format / fps

In *Pixel format / Frame rate*:

- Pixel format drop-down only lists formats the camera actually exposes.
- `Target fps` writes `AcquisitionFrameRate` (or `AcquisitionFrameRateAbs` on
  older firmware) and enables `AcquisitionFrameRateEnable` if it exists.
- `Resulting max fps` queries `BslResultingAcquisitionFrameRate` →
  `ResultingAcquisitionFrameRate` → `ResultingFrameRate`. If none exist it shows `N/A`.
- The estimated data rate is `W × H × bytes_per_pixel × fps`.

### 6. Recording

1. Pick an output folder, prefix and save format in *Recording*.
   MP4 is the default; AVI, TIFF sequence, BigTIFF and raw are also available.
2. Optionally tick `Limit by frame count` or `Limit by duration (s)`.
3. Add a note (saved to metadata).
4. Click **Start Recording**. If Live is not running, it is started for you.
   The recorder process and file writer are warmed up before frames are sent,
   which reduces dropped frames at recording start.
5. Click **Stop Recording** (or wait for the limit).

The status line under the buttons shows live frame count, elapsed time, on-disk
queue length, dropped frames, and the rolling "recording fps".

While recording is active, pixel format and ROI controls are locked.

### 7. Snapshot

The **Snapshot** button grabs one frame and writes
`recordings\snapshot_<timestamp>.tif` plus `*_metadata.json`.

### 8. Presets

In the *Presets* group at the bottom right:

- Type a name and click **Save Preset** → `presets\<name>.json`.
- Pick an existing name and click **Load Preset** to re-apply settings.

The dummy device + the panel work even with no real camera connected, so you
can rehearse preset workflows offline.

---

## Output files

For a recording with prefix `rec` in `recordings\`:

| File                                  | Format                       |
|---------------------------------------|------------------------------|
| `rec_metadata.json`                   | full metadata (always)       |
| `rec_timestamps.csv`                  | frame_id, cam timestamp, wallclock (always) |
| `rec.mp4`                             | when *MP4 video* chosen      |
| `rec.avi`                             | when *AVI video* chosen      |
| `rec_00000000.tif` …                  | when *TIFF sequence* chosen  |
| `rec.tiff`                            | when *BigTIFF (multi-page)*  |
| `rec.raw` + `rec_raw.json`            | when *Raw binary + JSON*     |
| `rec.h5`                              | when *HDF5 (.h5)*            |

`*_raw.json` documents shape and dtype so the raw file can be reloaded with
`numpy.fromfile(...).reshape(...)`.

`*_metadata.json` includes (excerpt):

```jsonc
{
  "app_name": "BaslerRecorder",
  "app_version": "0.2.0",
  "datetime": "...",
  "camera_model": "...",
  "serial_number": "...",
  "width": 1920, "height": 1200,
  "offset_x": 0, "offset_y": 0,
  "exposure_time_us": 800.0,
  "gain": 13.0,
  "pixel_format": "Mono8",
  "target_fps": 1000.0,
  "resulting_max_fps": 1000.0,
  "estimated_data_rate_MB_s": 2304.0,
  "save_format": "tiff_sequence",
  "output_path": "...",
  "recording_start_time": "...",
  "recording_end_time": "...",
  "number_of_recorded_frames": 1234,
  "dropped_frame_count": 0,
  "user_note": "..."
}
```

---

## Architecture

```
basler_recorder_app/
├── main.py                          # entry point (no argparse)
├── requirements.txt
├── README.md
└── basler_recorder/
    ├── camera_controller.py         # safe pypylon wrapper (real + dummy)
    ├── camera_worker.py             # QThread: grab frames, throttle preview
    ├── recorder_worker.py           # multiprocessing.Process: write to disk
    ├── metadata.py                  # build / save metadata dicts
    ├── settings.py                  # CameraPreset + JSON save/load
    ├── utils.py                     # safe GenApi feature helpers
    ├── dummy_camera.py              # generates synthetic frames
    └── gui/
        ├── main_window.py           # orchestrates everything
        ├── camera_panel.py
        ├── roi_panel.py
        ├── recording_panel.py
        └── status_panel.py
```

### Threading / process model

Inspired by YORU's multi-process pipeline:

```
┌───────────────┐       Qt signal      ┌───────────────┐
│ CameraWorker  │ ──── (throttled ──▶  │   GUI thread  │
│   (QThread)   │      ~30 fps)        │ (preview)     │
└──────┬────────┘                      └───────────────┘
       │
       │ every frame
       ▼
┌──────────────────┐   mp.Queue   ┌──────────────────────┐
│ multiprocessing  │ ◀────────── │ RecorderManager .start│
│ Process: writer  │              │  (in main process)    │
└──────────────────┘              └──────────────────────┘
```

- pypylon objects can't be pickled, so the camera lives in the main process and
  is read on a **QThread**.
- File I/O is offloaded to a **multiprocessing.Process** that gets frames
  through a `multiprocessing.Queue`. This sidesteps the GIL and keeps the GUI
  responsive even at sustained 200 MB/s+.

---

## Known limitations / TODO

- **Image-based ROI selection.** Click *Draw ROI on Image* in the ROI panel to
  toggle a draggable rectangle on the preview, resize / move it, then click
  *Apply Selection* to apply it as the camera ROI (offsets snapped to feature
  increments). Numeric / Center / Full controls still work as before.
- **Histogram / saturation overlay.** A throttled (~5 Hz) histogram of raw
  pixel values is shown below the preview, with a dashed line at the bit-depth
  max and a saturation readout that turns red when more than ~1 % of sampled
  pixels reach the top. Computed on the raw frame, so the readout reflects the
  camera's actual bit depth — not the display-scaled 8-bit copy.
- **HDF5 saving** writes a single chunked, growable `frames` dataset plus
  per-frame `frame_ids`, `camera_timestamp_ns`, and `wallclock_ns` datasets.
  Metadata is mirrored as root-level attributes so the file is self-describing.
  Compression defaults to `lzf` (falls back to none if unavailable).
- **MP4 / AVI are lossy monitoring formats.** Use HDF5, raw, or TIFF/BigTIFF
  when you need quantitative pixel values.
- **External trigger.** *Trigger mode On + Source Software* enables the
  *Send Software Trigger* button, which calls
  `InstantCamera.ExecuteSoftwareTrigger()` (falling back to the
  `TriggerSoftware` Command node). For Line1 / Line2 the panel shows a wiring
  hint — connect the external signal to the camera's I/O line and frames
  arrive automatically on each edge of the chosen polarity. The dummy camera
  honors this too: it only delivers a frame when a software pulse is pending.
- **Color (Bayer) cameras.** Preview uses a fast 2×2-block debayer (half
  resolution) and the recorder's MP4/AVI path uses OpenCV's full-resolution
  Bayer→BGR demosaic. Other save formats (TIFF/BigTIFF/raw/HDF5) keep the raw
  Bayer mosaic so quantitative analysis is unaffected.
- The recorder process uses a regular `mp.Queue`; for very high data rates
  (several GB/s) you'd want shared-memory ring buffers (e.g. `multiprocessing.
  shared_memory`). Out of scope for v0.2.
- Tested mostly on Windows 11 + Basler dart / ace USB3 cameras. GigE cameras
  should work but may need explicit packet-size / inter-packet-delay tuning.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| App starts but "0 device(s) found" and no real camera | pylon Software Suite not installed, USB cable, or the camera is owned by another process (close pylon Viewer). |
| `ImportError: pypylon` on startup | Install `pip install pypylon`. App still runs in dummy mode without it. |
| Grab fps far below target fps | Exposure too long, or `AcquisitionFrameRateEnable` is off, or USB bandwidth limit. Check the *Resulting max fps* label. |
| Dropped frames > 0 | Writer cannot keep up. MP4/AVI reduce disk I/O, AVI/MJPG is often lighter on CPU than MP4, and raw is best when exact pixels matter. Lower fps or smaller ROI if the queue still grows. |
| GUI freezes on Stop Recording | The recorder is draining its queue; wait. If it persists, file size or frame count was huge — recorder will still finalize the metadata. |

---

## Contributing

Issues and pull requests are welcome — especially:

- bug reports on real Basler hardware (please include camera model + pylon version)
- additional save formats or codec backends
- Linux / GigE-specific fixes
- usability tweaks for specific recording workflows (behavior, microscopy, …)

For larger changes, please open an issue first so we can agree on scope before
you spend time on a PR.

---

## License

This project is released under the [MIT License](LICENSE).

`pypylon` and the Basler pylon Software Suite are licensed separately by
Basler AG — see their respective terms.

---

## Acknowledgements

- [Basler AG](https://www.baslerweb.com/) for the pylon SDK and `pypylon`.
- The multi-process pipeline design was inspired by the YORU
  real-time animal-behavior framework.
- Built with [PySide6](https://doc.qt.io/qtforpython/),
  [pyqtgraph](https://www.pyqtgraph.org/),
  [tifffile](https://pypi.org/project/tifffile/),
  [h5py](https://www.h5py.org/), and [OpenCV](https://opencv.org/).
