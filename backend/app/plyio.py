"""Minimal binary PLY helpers for colored point clouds."""

from pathlib import Path

import numpy as np


def write_ply(path: Path, xyz: np.ndarray, rgb: np.ndarray) -> None:
    """Write an (N,3) float point array and (N,3) uint8 color array as binary PLY."""
    n = len(xyz)
    vertex = np.empty(
        n,
        dtype=[("x", "<f4"), ("y", "<f4"), ("z", "<f4"),
               ("red", "u1"), ("green", "u1"), ("blue", "u1")],
    )
    vertex["x"], vertex["y"], vertex["z"] = xyz[:, 0], xyz[:, 1], xyz[:, 2]
    vertex["red"], vertex["green"], vertex["blue"] = rgb[:, 0], rgb[:, 1], rgb[:, 2]
    header = (
        "ply\nformat binary_little_endian 1.0\n"
        f"element vertex {n}\n"
        "property float x\nproperty float y\nproperty float z\n"
        "property uchar red\nproperty uchar green\nproperty uchar blue\n"
        "end_header\n"
    )
    with open(path, "wb") as f:
        f.write(header.encode("ascii"))
        f.write(vertex.tobytes())


def ply_vertex_count(path: Path) -> int:
    with open(path, "rb") as f:
        for line in f:
            if line.startswith(b"element vertex"):
                return int(line.split()[-1])
            if line.startswith(b"end_header"):
                break
    return 0
