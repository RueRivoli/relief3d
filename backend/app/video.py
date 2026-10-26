"""Video input: pick sharp, evenly spaced frames to feed Structure-from-Motion.

Decoding uses PyAV, whose wheels bundle FFmpeg (no system install needed). The video is
split into equal time windows and the sharpest frame of each window is kept (variance of
the Laplacian), which skips most of the motion blur of handheld footage.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import numpy as np

try:
    import av
except ImportError:  # optional dependency
    av = None

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}
MAX_FPS = 4.0  # denser sampling adds little parallax between frames, only matching time
SHARPNESS_WIDTH = 320


class VideoError(RuntimeError):
    pass


def is_available() -> bool:
    return av is not None


def _sharpness(frame) -> float:
    height = max(3, round(frame.height * SHARPNESS_WIDTH / frame.width))
    g = frame.reformat(width=SHARPNESS_WIDTH, height=height, format="gray").to_ndarray().astype(np.float32)
    lap = 4 * g[1:-1, 1:-1] - g[:-2, 1:-1] - g[2:, 1:-1] - g[1:-1, :-2] - g[1:-1, 2:]
    return float(lap.var())


def _save(frame, path: Path, max_size: int) -> Path:
    img = frame.to_image()
    if frame.rotation:  # phones store portrait videos as rotated landscape frames
        img = img.rotate(frame.rotation, expand=True)
    img.thumbnail((max_size, max_size))
    img.save(path, quality=95)
    return path


def extract_frames(video: Path, dst: Path, max_frames: int, max_size: int,
                   progress: Callable[[float], None]) -> list[Path]:
    """Write up to `max_frames` JPGs (at most MAX_FPS per second) into `dst`, in time order."""
    dst.mkdir(parents=True, exist_ok=True)
    try:
        with av.open(str(video)) as container:
            stream = container.streams.video[0]
            stream.thread_type = "AUTO"
            if stream.duration is not None:
                duration = float(stream.duration * stream.time_base)
            elif container.duration:
                duration = container.duration / av.time_base
            else:
                raise VideoError(f"{video.name}: unknown duration")
            start = float(stream.start_time * stream.time_base) if stream.start_time else 0.0
            n_windows = max(1, min(max_frames, round(duration * MAX_FPS)))
            saved: list[Path] = []
            best = None  # (window, sharpness, frame) of the window being scanned

            for frame in container.decode(stream):
                if frame.time is None:
                    continue
                t = frame.time - start
                window = min(max(int(t / duration * n_windows), 0), n_windows - 1)
                if best is not None and window != best[0]:
                    saved.append(_save(best[2], dst / f"{video.stem}_{len(saved):04d}.jpg", max_size))
                    best = None
                    progress(min(t / duration, 1.0))
                score = _sharpness(frame)
                if best is None or score > best[1]:
                    best = (window, score, frame)
            if best is not None:
                saved.append(_save(best[2], dst / f"{video.stem}_{len(saved):04d}.jpg", max_size))
    except (av.error.FFmpegError, IndexError) as exc:  # IndexError: no video stream
        raise VideoError(f"{video.name}: not a readable video ({exc})") from exc
    return saved
