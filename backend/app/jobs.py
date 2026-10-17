"""Reconstruction jobs: persisted on disk, executed one at a time in a worker thread."""

from __future__ import annotations

import json
import logging
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import pipeline

log = logging.getLogger(__name__)

STAGES = ["queued", "prepare", "features", "matching", "mapping", "dense", "export", "done"]


@dataclass
class Job:
    id: str
    name: str
    created: float
    num_images: int
    dense: bool
    status: str = "queued"  # queued | running | done | error
    stage: str = "queued"
    progress: float = 0.0  # stage-local fraction
    message: str = "Waiting in queue"
    error: str | None = None
    stats: dict | None = None
    finished: float | None = None
    history: list[str] = field(default_factory=list)


class JobManager:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()
        # Reconstruction is CPU/GPU heavy: run one job at a time.
        self.executor = ThreadPoolExecutor(max_workers=1)
        self._load()

    def _load(self) -> None:
        for f in self.root.glob("*/job.json"):
            try:
                job = Job(**json.loads(f.read_text()))
            except (TypeError, json.JSONDecodeError):
                continue
            if job.status in ("queued", "running"):  # interrupted by a server restart
                job.status, job.error = "error", "Interrupted by server restart"
            self.jobs[job.id] = job

    def _save(self, job: Job) -> None:
        path = self.root / job.id / "job.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(job)))
        tmp.replace(path)

    def job_dir(self, job_id: str) -> Path:
        return self.root / job_id

    def create(self, name: str, uploads: list[tuple[str, bytes]], dense: bool) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], name=name, created=time.time(),
                  num_images=len(uploads), dense=dense)
        upload_dir = self.job_dir(job.id) / "uploads"
        upload_dir.mkdir(parents=True)
        for i, (filename, data) in enumerate(uploads):
            safe = Path(filename).name or f"image_{i}.jpg"
            (upload_dir / f"{i:04d}_{safe}").write_bytes(data)
        with self.lock:
            self.jobs[job.id] = job
            self._save(job)
        self.executor.submit(self._run, job.id)
        return job

    def _update(self, job: Job, **changes) -> None:
        with self.lock:
            for k, v in changes.items():
                setattr(job, k, v)
            self._save(job)

    def _run(self, job_id: str) -> None:
        job = self.jobs.get(job_id)
        if job is None:  # deleted while queued
            return
        job_dir = self.job_dir(job_id)
        last_save = 0.0

        def progress(stage: str, fraction: float, message: str) -> None:
            nonlocal last_save
            with self.lock:
                if stage != job.stage:
                    job.history.append(stage)
                job.stage, job.progress, job.message = stage, fraction, message
                now = time.time()
                if now - last_save > 1.0:
                    self._save(job)
                    last_save = now

        self._update(job, status="running", message="Starting")
        try:
            uploads = sorted((job_dir / "uploads").iterdir())
            scene = pipeline.reconstruct(uploads, job_dir, pipeline.Options(dense=job.dense), progress)
            self._update(job, status="done", stage="done", progress=1.0, message="Reconstruction complete",
                         stats=scene["stats"], finished=time.time())
            # Intermediate COLMAP data is large and no longer needed.
            shutil.rmtree(job_dir / "work" / "dense_views", ignore_errors=True)
            shutil.rmtree(job_dir / "uploads", ignore_errors=True)
        except pipeline.PipelineError as exc:
            self._update(job, status="error", error=str(exc), finished=time.time())
        except Exception as exc:  # noqa: BLE001
            log.exception("Job %s failed", job_id)
            self._update(job, status="error", error=f"Unexpected error: {exc}", finished=time.time())

    def list(self) -> list[Job]:
        return sorted(self.jobs.values(), key=lambda j: j.created, reverse=True)

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def delete(self, job_id: str) -> bool:
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None or job.status == "running":
                return False
            del self.jobs[job_id]
        shutil.rmtree(self.job_dir(job_id), ignore_errors=True)
        return True
