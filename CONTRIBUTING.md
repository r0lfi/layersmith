# Contributing to LayerSmith

Thanks for looking. Issues and pull requests are welcome.

## Development setup

LayerSmith is a FastAPI backend that also serves a React frontend. The test
suite uses a fake build backend, so you do not need Podman or Docker to work
on most of it.

```bash
git clone https://github.com/r0lfi/layersmith.git
cd layersmith

# Backend
cd backend
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/uvicorn layersmith.api:create_app --factory --reload --port 8080

# Frontend, in a second terminal
cd frontend
npm install
npm run dev          # http://localhost:5173, proxies /api to port 8080
```

## Running the tests

```bash
cd backend
.venv/bin/pytest                 # the whole suite, no container runtime needed
```

Against a real runtime, which builds an Alpine image, runs it to confirm the
packages are there, exports it and loads the archive back:

```bash
LAYERSMITH_SMOKE=1 .venv/bin/pytest tests/test_runtime_smoke.py -v
```

Runtimes that are not installed are skipped, so this runs what your machine
can actually run.

## Building locally

```bash
cd frontend && npm run build && cd ..
podman build -t layersmith:local -f Containerfile .   # or docker build
```

The Containerfile builds the frontend itself, so building the image needs no
local Node.

## What the code expects

- **Never build a command as a string.** Runtime commands are argv lists.
  Anything a user supplies is matched against an explicit pattern in
  `backend/layersmith/core/spec.py` before it reaches a command, a path or a
  Containerfile.
- **A build record is immutable.** It snapshots the Containerfile, base
  digest, packages, file checksums and log, so an old build can still be
  explained. A version is built once and never overwritten.
- **Secrets never enter an image.** Environment variables that look like
  credentials are refused, on purpose.
- **Tests come with the change.** A bug fix should fail before it and pass
  after; that is how the existing regression tests were written.
- Comments explain *why*, not what the line already says.

## Pull requests

1. Branch from `main`.
2. Keep the change focused; unrelated cleanups are easier to review separately.
3. Make sure `pytest` and `npm run build` both pass — CI runs exactly those.
4. Describe what changes for a user, and anything you deliberately left out.

## Reporting bugs

Open an issue with what you did, what happened, and what you expected. Useful
extras: the version from **Settings → About**, your runtime
(`podman --version` / `docker --version`), and the build log if a build failed.

For anything security-sensitive, do **not** open a public issue — see
[SECURITY.md](SECURITY.md).
