"""3D Gaussian Splatting, trained with Brush (https://github.com/ArthurBrussee/brush).

Brush is a standalone binary (wgpu: Metal on macOS, Vulkan/DX12 elsewhere) that
trains splats from a COLMAP dataset and exports a standard 3DGS .ply. It only
reports progress to a terminal, so it runs under a pseudo-terminal and the
"<step>/<total> Steps" progress bar is parsed from its output.
"""

from __future__ import annotations

import os
import pty
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
STEP_RE = re.compile(rb"(\d+)/(\d+)\s+Steps")


def brush_binary() -> str | None:
    """$BRUSH_BIN, then backend/bin/brush_app (see scripts/install-brush.sh), then PATH."""
    for candidate in (os.environ.get("BRUSH_BIN"), ROOT / "bin" / "brush_app"):
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return shutil.which("brush_app")


def is_available() -> bool:
    return brush_binary() is not None


def train(dataset: Path, output: Path, steps: int, progress: Callable[[float, str], None],
          log_file: Path) -> bool:
    """Train on a COLMAP dataset (images/ + sparse/ with pinhole cameras) and write `output`."""
    binary = brush_binary()
    if binary is None:
        return False
    output.unlink(missing_ok=True)
    cmd = [
        binary, str(dataset),
        "--total-steps", str(steps),
        "--export-every", str(steps),
        "--export-path", str(output.parent),
        "--export-name", output.name,
        "--eval-every", str(steps + 1),
        # Brush's defaults target 30k steps: densify faster and longer to fill the scene in ours.
        "--refine-every", "100",
        "--growth-select-fraction", "0.3",
        "--growth-stop-iter", str(int(steps * 0.7)),
        # ~250 bytes per splat in the .ply (SH degree 3): keep it loadable in a browser.
        "--max-splats", "1500000",
    ]
    master, slave = pty.openpty()
    # cwd: Brush writes its kernel autotune cache to ./target
    proc = subprocess.Popen(cmd, cwd=dataset.parent, stdin=subprocess.DEVNULL, stdout=slave, stderr=slave)
    os.close(slave)
    last_step = -1
    with open(log_file, "wb") as log:
        while True:
            try:
                chunk = os.read(master, 65536)
            except OSError:  # EIO once the child closes the terminal
                break
            if not chunk:
                break
            log.write(chunk)
            matches = STEP_RE.findall(chunk)
            if matches:
                step, total = (int(x) for x in matches[-1])
                if step != last_step and total:
                    last_step = step
                    progress(step / total, f"Training Gaussian splats: step {step}/{total}")
    os.close(master)
    return proc.wait() == 0 and output.exists()
