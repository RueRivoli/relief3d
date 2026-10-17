"""Images → 3D reconstruction pipeline.

1. Normalize the uploaded images (EXIF rotation, max resolution).
2. Structure-from-Motion with COLMAP (pycolmap): camera poses + sparse points.
3. Optional densification from monocular depth aligned to the SfM points.
4. Re-orient the scene (Y up, floor at y=0, walls along X/Z) and export
   PLY point clouds plus a scene.json with cameras and dimensions.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
import pycolmap
from PIL import Image as PILImage, ImageOps

try:  # iPhone photos
    from pillow_heif import register_heif_opener

    register_heif_opener()
except ImportError:
    pass

from . import densify as dense_mod
from .plyio import write_ply

log = logging.getLogger(__name__)

Progress = Callable[[str, float, str], None]  # (stage, fraction 0-1, message)

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".bmp", ".tif", ".tiff"}


class PipelineError(RuntimeError):
    pass


@dataclass
class Options:
    dense: bool = True
    max_image_size: int = 1600
    max_features: int = 8192


def prepare_images(src_files: list[Path], dst: Path, max_size: int) -> list[str]:
    dst.mkdir(parents=True, exist_ok=True)
    names = []
    for i, src in enumerate(sorted(src_files)):
        try:
            img = PILImage.open(src)
            exif = img.getexif()
            img = ImageOps.exif_transpose(img).convert("RGB")
        except Exception as exc:  # noqa: BLE001 - skip unreadable uploads
            log.warning("Skipping unreadable image %s: %s", src.name, exc)
            continue
        img.thumbnail((max_size, max_size), PILImage.Resampling.LANCZOS)
        exif[0x0112] = 1  # orientation already applied; keep focal-length tags for COLMAP
        name = f"{i:04d}_{Path(src.name).stem}.jpg"
        img.save(dst / name, quality=95, exif=exif.tobytes())
        names.append(name)
    return names


def run_sfm(image_dir: Path, work: Path, opts: Options, progress: Progress) -> pycolmap.Reconstruction:
    db = work / "database.db"
    if db.exists():
        db.unlink()
    n_images = len(list(image_dir.glob("*.jpg")))

    progress("features", 0.0, f"Extracting features from {n_images} images")
    extraction = pycolmap.FeatureExtractionOptions()
    extraction.sift.max_num_features = opts.max_features
    # Uploads usually come from one device; share intrinsics when sizes match.
    sizes = {PILImage.open(p).size for p in image_dir.glob("*.jpg")}
    camera_mode = pycolmap.CameraMode.SINGLE if len(sizes) == 1 else pycolmap.CameraMode.AUTO
    pycolmap.extract_features(db, image_dir, camera_mode=camera_mode, extraction_options=extraction)

    progress("matching", 0.0, "Matching features between images")
    if n_images <= 120:
        pycolmap.match_exhaustive(db)
    else:
        pycolmap.match_sequential(db)

    progress("mapping", 0.0, "Estimating camera poses and 3D structure")
    sparse_dir = work / "sparse"
    shutil.rmtree(sparse_dir, ignore_errors=True)
    sparse_dir.mkdir(parents=True)
    registered = 0

    def on_next_image() -> None:
        nonlocal registered
        registered += 1
        progress("mapping", min(registered / n_images, 0.99), f"Registered {registered + 1}/{n_images} images")

    models = pycolmap.incremental_mapping(db, image_dir, sparse_dir, next_image_callback=on_next_image)
    if not models:
        raise PipelineError(
            "Could not reconstruct the scene: not enough overlapping features between images. "
            "Take more photos with ~60% overlap, avoid blank walls and motion blur."
        )
    best = max(models.values(), key=lambda r: r.num_reg_images())
    log.info("SfM: %s", best.summary())
    return best


def run_dense(
    rec: pycolmap.Reconstruction, image_dir: Path, work: Path, progress: Progress, stride: int = 4
) -> tuple[np.ndarray, np.ndarray] | None:
    """Export per-view inputs, then run the depth model in a subprocess (see densify.py)."""
    views_dir = work / "dense_views"
    shutil.rmtree(views_dir, ignore_errors=True)
    views_dir.mkdir(parents=True)
    for i in rec.reg_image_ids():
        img = rec.image(i)
        cam = img.camera
        pose = img.cam_from_world()
        R, t = pose.rotation.matrix(), pose.translation
        obs = [(p.xy, rec.point3D(p.point3D_id).xyz) for p in img.points2D if p.has_point3D()]
        if not obs:
            continue
        sparse_xy = np.array([o[0] for o in obs])
        sparse_z = (np.array([o[1] for o in obs]) @ R.T + t)[:, 2]
        gy, gx = np.mgrid[stride // 2:cam.height:stride, stride // 2:cam.width:stride]
        pix = np.stack([gx.ravel() + 0.5, gy.ravel() + 0.5], axis=1).astype(np.float64)
        rays = cam.cam_from_img(pix).reshape(*gx.shape, 2)
        np.savez(views_dir / f"{i:06d}.npz", name=img.name, R=R, t=t, sparse_xy=sparse_xy,
                 sparse_z=sparse_z, grid_x=gx, grid_y=gy, rays=rays.astype(np.float32))

    out_file = work / "dense.npz"
    out_file.unlink(missing_ok=True)
    log_file = work / "dense.log"
    with open(log_file, "w") as err:
        proc = subprocess.Popen(
            [sys.executable, "-m", "app.densify", str(views_dir), str(image_dir), str(out_file)],
            cwd=Path(__file__).resolve().parent.parent,
            stdout=subprocess.PIPE, stderr=err, text=True,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            if line.startswith("PROGRESS "):
                _, frac, msg = line.rstrip("\n").split(" ", 2)
                progress("dense", float(frac), msg)
        returncode = proc.wait()
    if returncode != 0 or not out_file.exists():
        # Densification is a bonus: fall back to the sparse cloud instead of failing the job.
        log.error("Densification failed:\n%s", log_file.read_text()[-4000:])
        return None
    data = np.load(out_file)
    return data["xyz"].astype(np.float64), data["rgb"]


def scene_transform(rec: pycolmap.Reconstruction, xyz: np.ndarray) -> tuple[np.ndarray, float, np.ndarray]:
    """Similarity (R, s, t) mapping COLMAP world → viewer world (Y up, floor at 0, ~10 units wide)."""
    poses = [rec.image(i).cam_from_world() for i in rec.reg_image_ids()]
    # Photos are mostly taken upright: the image "down" axis (+y in camera) points to the floor.
    down = np.mean([p.rotation.matrix().T @ np.array([0.0, 1.0, 0.0]) for p in poses], axis=0)
    up = -down / np.linalg.norm(down)

    # Rotation taking `up` to +Y.
    y = np.array([0.0, 1.0, 0.0])
    v, c = np.cross(up, y), float(np.dot(up, y))
    if np.linalg.norm(v) < 1e-8:
        R_up = np.eye(3) if c > 0 else np.diag([1.0, -1.0, -1.0])
    else:
        vx = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
        R_up = np.eye(3) + vx + vx @ vx * (1 / (1 + c))

    p = xyz @ R_up.T
    # Yaw: align the dominant horizontal directions (walls) with X and Z.
    lo, hi = np.percentile(p, [5, 95], axis=0)
    core = p[np.all((p >= lo) & (p <= hi), axis=1)]
    if len(core) < 10:
        core = p
    xz = core[:, [0, 2]] - core[:, [0, 2]].mean(axis=0)
    _, vecs = np.linalg.eigh(np.cov(xz.T))
    major = vecs[:, -1]
    ang = np.arctan2(major[1], major[0])
    ca, sa = np.cos(ang), np.sin(ang)
    R_yaw = np.array([[ca, 0, sa], [0, 1, 0], [-sa, 0, ca]])  # rotates `major` onto +X
    R = R_yaw @ R_up

    p = xyz @ R.T
    lo, hi = np.percentile(p, [2, 98], axis=0)
    diag = float(np.linalg.norm(hi - lo)) or 1.0
    s = 10.0 / diag
    center = (lo + hi) / 2
    t = -s * np.array([center[0], lo[1], center[2]])  # floor (2nd percentile) at y=0
    return R, s, t


def export_scene(
    rec: pycolmap.Reconstruction,
    out: Path,
    sparse: tuple[np.ndarray, np.ndarray],
    dense: tuple[np.ndarray, np.ndarray] | None,
    n_uploaded: int,
) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    ref_xyz = dense[0] if dense is not None and len(dense[0]) else sparse[0]
    R, s, t = scene_transform(rec, ref_xyz)

    def apply(x: np.ndarray) -> np.ndarray:
        return (s * x @ R.T + t).astype(np.float32)

    files = {}
    sx = apply(sparse[0])
    write_ply(out / "sparse.ply", sx, sparse[1])
    files["sparse"] = "sparse.ply"
    main_xyz = sx
    if dense is not None and len(dense[0]):
        dx = apply(dense[0])
        write_ply(out / "dense.ply", dx, dense[1])
        files["dense"] = "dense.ply"
        main_xyz = dx

    cameras = []
    for i in rec.reg_image_ids():
        img = rec.image(i)
        cam = img.camera
        pose = img.cam_from_world()
        Rc = pose.rotation.matrix()
        world_from_cam = R @ Rc.T  # camera axes in viewer world (OpenCV: x right, y down, z forward)
        cameras.append({
            "name": img.name,
            "position": apply(img.projection_center()[None])[0].tolist(),
            "rotation": world_from_cam.tolist(),
            "width": cam.width,
            "height": cam.height,
            "focal": float(cam.mean_focal_length()),
        })

    lo, hi = np.percentile(main_xyz, [2, 98], axis=0)
    scene = {
        "files": files,
        "cameras": cameras,
        "bounds": {"min": lo.tolist(), "max": hi.tolist(), "size": (hi - lo).tolist()},
        "stats": {
            "images_uploaded": n_uploaded,
            "images_registered": rec.num_reg_images(),
            "sparse_points": int(len(sx)),
            "dense_points": int(len(dense[0])) if dense is not None else 0,
            "reprojection_error": float(rec.compute_mean_reprojection_error()),
        },
        # SfM from photos alone has no absolute scale; the viewer lets users calibrate it.
        "units": "relative",
    }
    (out / "scene.json").write_text(json.dumps(scene))
    return scene


def reconstruct(src_files: list[Path], job_dir: Path, opts: Options, progress: Progress) -> dict:
    image_dir = job_dir / "images"
    work = job_dir / "work"
    out = job_dir / "output"
    work.mkdir(parents=True, exist_ok=True)

    progress("prepare", 0.0, "Preparing images")
    names = prepare_images(src_files, image_dir, opts.max_image_size)
    if len(names) < 3:
        raise PipelineError("At least 3 readable images are required.")

    rec = run_sfm(image_dir, work, opts, progress)

    ids = list(rec.point3D_ids())
    sparse_xyz = np.array([rec.point3D(i).xyz for i in ids], dtype=np.float64)
    sparse_rgb = np.array([rec.point3D(i).color for i in ids], dtype=np.uint8)
    if len(sparse_xyz) < 50:
        raise PipelineError("Reconstruction is too sparse; try more photos with more overlap.")

    dense = None
    if opts.dense and dense_mod.is_available():
        dense = run_dense(rec, image_dir, work, progress)
    elif opts.dense:
        log.warning("Dense mode requested but torch/transformers are not installed")

    progress("export", 0.0, "Exporting 3D scene")
    return export_scene(rec, out, (sparse_xyz, sparse_rgb), dense, len(src_files))


if __name__ == "__main__":
    import argparse
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="Reconstruct a 3D scene from a folder of images")
    ap.add_argument("images", type=Path)
    ap.add_argument("job_dir", type=Path)
    ap.add_argument("--no-dense", action="store_true")
    a = ap.parse_args()
    files = [p for p in a.images.iterdir() if p.suffix.lower() in IMAGE_EXTS]
    result = reconstruct(
        files, a.job_dir, Options(dense=not a.no_dense),
        lambda st, f, m: print(f"[{st} {f:4.0%}] {m}", file=sys.stderr),
    )
    print(json.dumps(result["stats"], indent=2), json.dumps(result["bounds"]))
