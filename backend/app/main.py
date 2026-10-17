"""HTTP API for uploading photos and retrieving 3D reconstructions."""

from __future__ import annotations

import logging
import os
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import densify
from .jobs import STAGES, JobManager
from .pipeline import IMAGE_EXTS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("DATA_DIR", ROOT / "data" / "jobs"))
MAX_IMAGES = 200
MAX_UPLOAD_BYTES = 40 * 1024 * 1024  # per image

app = FastAPI(title="Images to 3D")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5288", "http://127.0.0.1:5288"],
    allow_methods=["*"],
    allow_headers=["*"],
)
jobs = JobManager(DATA_DIR)

OUTPUT_FILES = {"scene.json", "sparse.ply", "dense.ply"}


def _get_job(job_id: str):
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return job


@app.get("/api/capabilities")
def capabilities():
    return {"dense": densify.is_available(), "max_images": MAX_IMAGES, "stages": STAGES}


@app.get("/api/jobs")
def list_jobs():
    return [asdict(j) for j in jobs.list()]


@app.post("/api/jobs", status_code=201)
async def create_job(
    files: list[UploadFile] = File(...),
    name: str = Form(""),
    dense: bool = Form(True),
):
    images = [f for f in files if Path(f.filename or "").suffix.lower() in IMAGE_EXTS]
    if len(images) < 3:
        raise HTTPException(400, "Upload at least 3 images (JPG, PNG, HEIC, WEBP…).")
    if len(images) > MAX_IMAGES:
        raise HTTPException(400, f"At most {MAX_IMAGES} images per scene.")
    uploads = []
    for f in images:
        data = await f.read()
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, f"{f.filename} is larger than {MAX_UPLOAD_BYTES // 2**20} MB")
        uploads.append((f.filename or "image.jpg", data))
    job = jobs.create(name.strip() or f"Scene ({len(uploads)} photos)", uploads,
                      dense=dense and densify.is_available())
    return asdict(job)


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    return asdict(_get_job(job_id))


@app.delete("/api/jobs/{job_id}", status_code=204)
def delete_job(job_id: str):
    _get_job(job_id)
    if not jobs.delete(job_id):
        raise HTTPException(409, "Cannot delete a running job")


@app.get("/api/jobs/{job_id}/output/{filename}")
def get_output(job_id: str, filename: str):
    _get_job(job_id)
    path = jobs.job_dir(job_id) / "output" / filename
    if filename not in OUTPUT_FILES or not path.exists():
        raise HTTPException(404, "File not found")
    return FileResponse(path)


@app.get("/api/jobs/{job_id}/images/{filename}")
def get_image(job_id: str, filename: str):
    _get_job(job_id)
    image_dir = jobs.job_dir(job_id) / "images"
    path = (image_dir / filename).resolve()
    if path.parent != image_dir.resolve() or not path.is_file():
        raise HTTPException(404, "Image not found")
    return FileResponse(path, media_type="image/jpeg")


# Serve the built frontend (frontend/dist) when present, for single-process deployment.
DIST = ROOT.parent / "frontend" / "dist"
if DIST.is_dir():
    app.mount("/", StaticFiles(directory=DIST, html=True), name="frontend")
