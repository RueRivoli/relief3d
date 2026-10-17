# Images to 3D

Upload photos of a room or landscape → get an explorable, measurable 3D point cloud.

- **Backend** (Python, FastAPI): COLMAP Structure-from-Motion via `pycolmap`, plus optional
  densification with a monocular depth model (Depth-Anything-V2-Small).
- **Frontend** (Vue 3 + three.js): upload, live progress, 3D viewer with camera positions,
  scene dimensions, a measuring tool and scale calibration.

## How it works

1. **Prepare** – apply EXIF rotation, downscale to 1600 px, keep focal-length EXIF tags.
2. **Features & matching** – SIFT keypoints, exhaustive matching (sequential above 120 images).
3. **Mapping** – incremental SfM recovers each photo's camera pose and a sparse 3D point cloud.
4. **Dense** (optional) – for each photo, Depth-Anything predicts a relative depth map, which
   is fitted (RANSAC) to the depths of the SfM points seen in that photo, then back-projected.
   Points are kept only where ≥ 2 photos agree, then merged on a voxel grid.
   Runs in a subprocess, because pycolmap and torch each ship an OpenMP runtime and loading
   both in one process crashes.
5. **Export** – the scene is re-oriented (Y up from the cameras' "down" direction, floor at
   y = 0, main walls aligned with X/Z) and written as `sparse.ply`, `dense.ply` and `scene.json`.

**Scale:** photos alone cannot recover absolute size, so dimensions start in arbitrary
"units". In the viewer, measure something you know (a door, a table) and click **Set length**
to switch everything to meters. The calibration is saved per scene in the browser.

## Setup

Requires Python 3.10+ and Node 20+.

```bash
# Backend
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements-dense.txt   # or requirements.txt for sparse-only (no torch)

# Frontend
cd ../frontend
npm install
```

The depth model (~100 MB) is downloaded from Hugging Face on first dense run.

## Run

```bash
./dev.sh            # backend on :8765, frontend on http://localhost:5288
```

Or single-process (serves the built frontend from FastAPI):

```bash
cd frontend && npm run build
cd ../backend && .venv/bin/uvicorn app.main:app --port 8765   # http://localhost:8765
```

CLI, without the web app:

```bash
cd backend && .venv/bin/python -m app.pipeline path/to/photos path/to/output [--no-dense]
```

## Taking good photos

- Move between shots (don't rotate on the spot); ~60% overlap between consecutive photos.
- Rooms: walk along the walls pointing at the opposite side; include corners. 20–60 photos.
- Static, sharp, well-lit scenes. Blank walls, glass and mirrors are hard for any method.

## API

| Method | Path | |
|---|---|---|
| `GET` | `/api/capabilities` | whether dense mode is available |
| `POST` | `/api/jobs` | multipart `files[]`, `name`, `dense` → job |
| `GET` | `/api/jobs`, `/api/jobs/{id}` | status, stage, progress, stats |
| `DELETE` | `/api/jobs/{id}` | delete a finished job |
| `GET` | `/api/jobs/{id}/output/{scene.json,sparse.ply,dense.ply}` | results |
| `GET` | `/api/jobs/{id}/images/{name}` | processed input photo |

Jobs are stored in `backend/data/jobs/` (override with `DATA_DIR`) and run one at a time.

## Possible next steps

- Meshing (Poisson) or Gaussian Splatting for photorealistic surfaces (needs a CUDA GPU for
  reasonable speed).
- Feed-forward multi-view models (VGGT, MASt3R) for better results from few photos — need
  several GB of weights and a GPU.
- Automatic room layout (floor/wall plane detection → floor plan with dimensions).
