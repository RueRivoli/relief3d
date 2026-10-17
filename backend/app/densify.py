"""Dense point cloud from monocular depth aligned to the SfM reconstruction.

For each registered image we predict a relative (affine-invariant) disparity map
with Depth-Anything-V2, fit it to the depths of the sparse SfM points seen by
that image, then back-project the pixels into world space using the recovered
camera pose.

This module runs in its own process (`python -m app.densify`) and must never
import pycolmap: pycolmap and torch each bundle an OpenMP runtime, and loading
both in one process aborts. The parent precomputes everything that needs
pycolmap (poses, sparse depths, undistorted pixel rays) into .npz files.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
from PIL import Image as PILImage

MODEL_ID = "depth-anything/Depth-Anything-V2-Small-hf"
MIN_SPARSE_POINTS = 25


def is_available() -> bool:
    import importlib.util

    return all(importlib.util.find_spec(m) is not None for m in ("torch", "transformers"))


class DepthModel:
    def __init__(self) -> None:
        import torch
        from transformers import AutoImageProcessor, AutoModelForDepthEstimation

        self.torch = torch
        if torch.backends.mps.is_available():
            self.device = "mps"
        elif torch.cuda.is_available():
            self.device = "cuda"
        else:
            self.device = "cpu"
        self.processor = AutoImageProcessor.from_pretrained(MODEL_ID)
        self.model = AutoModelForDepthEstimation.from_pretrained(MODEL_ID).to(self.device).eval()

    def disparity(self, image: PILImage.Image) -> np.ndarray:
        """Relative inverse depth at the image's full resolution."""
        torch = self.torch
        inputs = self.processor(images=image, return_tensors="pt").to(self.device)
        with torch.no_grad():
            pred = self.model(**inputs).predicted_depth  # (1, h, w)
        pred = torch.nn.functional.interpolate(
            pred[:, None], size=(image.height, image.width), mode="bilinear", align_corners=False
        )
        return pred[0, 0].float().cpu().numpy()


def fit_disparity(d: np.ndarray, inv_z: np.ndarray, iters: int = 200) -> tuple[float, float] | None:
    """Robustly fit inv_z ≈ a·d + b (RANSAC + least-squares refinement)."""
    rng = np.random.default_rng(0)
    n = len(d)
    best_inliers = None
    thresh = 0.1 * np.median(inv_z)
    for _ in range(iters):
        i, j = rng.choice(n, 2, replace=False)
        if abs(d[i] - d[j]) < 1e-6:
            continue
        a = (inv_z[i] - inv_z[j]) / (d[i] - d[j])
        if a <= 0:  # disparity must increase as depth decreases
            continue
        b = inv_z[i] - a * d[i]
        inliers = np.abs(a * d + b - inv_z) < thresh
        if best_inliers is None or inliers.sum() > best_inliers.sum():
            best_inliers = inliers
    if best_inliers is None or best_inliers.sum() < max(10, 0.3 * n):
        return None
    A = np.stack([d[best_inliers], np.ones(best_inliers.sum())], axis=1)
    a, b = np.linalg.lstsq(A, inv_z[best_inliers], rcond=None)[0]
    return (float(a), float(b)) if a > 0 else None


def sample(img: np.ndarray, xy: np.ndarray) -> np.ndarray:
    """Bilinear sampling of a 2D array at subpixel (x, y) positions."""
    h, w = img.shape
    x = np.clip(xy[:, 0] - 0.5, 0, w - 1.001)  # COLMAP pixel centers are at +0.5
    y = np.clip(xy[:, 1] - 0.5, 0, h - 1.001)
    x0, y0 = x.astype(int), y.astype(int)
    fx, fy = x - x0, y - y0
    return (img[y0, x0] * (1 - fx) * (1 - fy) + img[y0, x0 + 1] * fx * (1 - fy)
            + img[y0 + 1, x0] * (1 - fx) * fy + img[y0 + 1, x0 + 1] * fx * fy)


def backproject_view(view: dict, disp: np.ndarray, rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray] | None:
    """World-space points and colors for one view, or None if alignment fails."""
    xy, z = view["sparse_xy"], view["sparse_z"]
    keep = z > 0
    if keep.sum() < MIN_SPARSE_POINTS:
        return None
    fit = fit_disparity(sample(disp, xy[keep]), 1.0 / z[keep])
    if fit is None:
        return None
    a, b = fit
    z_max = np.percentile(z[keep], 98) * 1.5

    gx, gy = view["grid_x"], view["grid_y"]  # strided pixel grid (gh, gw)
    inv = a * disp[gy, gx] + b
    depth = np.where(inv > 1e-6, 1.0 / np.maximum(inv, 1e-6), np.inf)
    # Drop "flying pixels" at depth discontinuities, and anything too far away.
    dy, dx = np.gradient(np.where(np.isfinite(depth), depth, 0))
    rel_grad = np.hypot(dx, dy) / depth
    valid = np.isfinite(depth) & (depth < z_max) & (rel_grad < 0.05)

    rays = view["rays"][valid]  # undistorted normalized coords (x/z, y/z)
    cam_pts = np.concatenate([rays, np.ones((len(rays), 1))], axis=1) * depth[valid][:, None]
    world = (cam_pts - view["t"]) @ view["R"]  # R^T (p - t)
    return world.astype(np.float32), rgb[gy[valid], gx[valid]]


def _voxel_keys(xyz: np.ndarray, voxel: float) -> np.ndarray:
    cell = np.floor((xyz - xyz.min(axis=0)) / voxel).astype(np.int64)
    dims = cell.max(axis=0) + 1
    return (cell[:, 0] * dims[1] + cell[:, 1]) * dims[2] + cell[:, 2]


def fuse(xyz: np.ndarray, rgb: np.ndarray, view: np.ndarray, max_points: int) -> tuple[np.ndarray, np.ndarray]:
    """Fuse per-view point clouds.

    Monocular depth can hallucinate geometry, so a point survives only if its
    neighbourhood (a coarse voxel) is also observed by another view. Survivors
    are then averaged on a fine voxel grid to merge duplicates across views.
    """
    lo, hi = np.percentile(xyz, [1, 99], axis=0)
    extent = float(np.max(hi - lo))
    inside = np.all((xyz > lo - 0.5 * extent) & (xyz < hi + 0.5 * extent), axis=1)
    xyz, rgb, view = xyz[inside], rgb[inside], view[inside]

    coarse = _voxel_keys(xyz, extent / 300)
    pairs = np.unique(np.stack([coarse, view], axis=1), axis=0)
    voxel_ids, n_views = np.unique(pairs[:, 0], return_counts=True)
    consistent = np.isin(coarse, voxel_ids[n_views >= 2])
    xyz, rgb = xyz[consistent], rgb[consistent]

    voxel = extent / 1500
    while True:
        _, inverse, counts = np.unique(_voxel_keys(xyz, voxel), return_inverse=True, return_counts=True)
        if len(counts) <= max_points:
            break
        voxel *= 1.25
    n = len(counts)

    def mean(values: np.ndarray) -> np.ndarray:
        cols = [np.bincount(inverse, weights=values[:, c], minlength=n) for c in range(3)]
        return np.stack(cols, axis=1) / counts[:, None]

    return mean(xyz).astype(np.float32), mean(rgb.astype(np.float64)).round().astype(np.uint8)


def report(fraction: float, message: str) -> None:
    print(f"PROGRESS {fraction:.4f} {message}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("views_dir", type=Path, help="Directory of per-view .npz inputs")
    ap.add_argument("image_dir", type=Path)
    ap.add_argument("output", type=Path, help="Output .npz with xyz and rgb")
    ap.add_argument("--max-points", type=int, default=1_500_000)
    args = ap.parse_args()

    report(0.0, "Loading depth model")
    model = DepthModel()
    view_files = sorted(args.views_dir.glob("*.npz"))
    all_xyz, all_rgb, all_view = [], [], []
    for k, vf in enumerate(view_files):
        view = dict(np.load(vf))
        name = str(view["name"])
        report(k / len(view_files), f"Estimating depth for image {k + 1}/{len(view_files)}")
        pil = PILImage.open(args.image_dir / name).convert("RGB")
        result = backproject_view(view, model.disparity(pil), np.asarray(pil))
        if result is None:
            print(f"Skipping {name}: could not align depth to sparse points", file=sys.stderr)
            continue
        all_xyz.append(result[0])
        all_rgb.append(result[1])
        all_view.append(np.full(len(result[0]), k, dtype=np.int64))

    report(1.0, "Fusing depth maps")
    if all_xyz:
        xyz, rgb = fuse(np.concatenate(all_xyz), np.concatenate(all_rgb), np.concatenate(all_view),
                        args.max_points)
    else:
        xyz, rgb = np.zeros((0, 3), np.float32), np.zeros((0, 3), np.uint8)
    np.savez(args.output, xyz=xyz, rgb=rgb)


if __name__ == "__main__":
    main()
