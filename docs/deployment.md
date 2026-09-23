# Deployment

LayerSmith is one service plus one data directory. The interesting question
is where the *builds* run, because a container that can build images is a
container with real power on the host. There are three supported shapes; pick
the one that matches how much you trust the people using the web UI.

## 1. Directly on a host (simplest)

Install the backend and run it as a normal user with rootless Podman:

```
python3 -m venv /opt/layersmith/venv
/opt/layersmith/venv/bin/pip install ./backend
LAYERSMITH_DATA_DIR=/var/lib/layersmith \
  /opt/layersmith/venv/bin/uvicorn layersmith.api:create_app --factory --host 0.0.0.0 --port 8080
```

Builds run as that user, with that user's Podman storage. No privileged
access is involved. This is the recommended setup for a single admin host.

## 2. Container, talking to a Podman socket

The application container has no build capability of its own; it asks a
Podman socket on the host to do the work.

```
systemctl --user enable --now podman.socket

podman run -d \
  --name layersmith \
  -p 8080:8080 \
  -v layersmith-data:/data \
  -v $XDG_RUNTIME_DIR/podman/podman.sock:/run/podman/podman.sock \
  -e CONTAINER_HOST=unix:///run/podman/podman.sock \
  ghcr.io/example/layersmith:latest
```

`compose.yml` in the repository root does the same thing.

**What this means:** anything that can talk to that socket can run containers
as the socket's owner. Mount the *rootless* socket, not the root one, and do
not expose LayerSmith to untrusted users in this configuration.

## 3. Separate build host (most isolated)

The `BuildBackend` interface (`backend/layersmith/backends/base.py`) has five
operations: `available`, `resolve_base`, `build`, `inspect`, `export`,
`remove`. A remote agent backend implements the same five against a build
host over SSH or HTTP, so the web application never touches a container
runtime at all. `LAYERSMITH_AGENT_URL` and `LAYERSMITH_AGENT_TOKEN` are
reserved for this. The agent itself is not part of 0.1.0.

This is the right shape if the web UI is reachable by more people than you
would trust with root on the build host.

## What not to do

Do not run the application container with `--privileged` or as root with the
host's root Podman socket mounted just to make builds work. That turns every
wizard form into root on the host. If you need more isolation than option 2
gives you, use option 3.

## Storage

| Variable | Default | Grows with |
| --- | --- | --- |
| `LAYERSMITH_DATA_DIR` | `/data` | everything below |
| `LAYERSMITH_IMAGE_DIR` | `$DATA/images` | exported archives and air-gap bundles |
| `LAYERSMITH_BUILD_DIR` | `$DATA/builds` | build contexts (removed after each build unless `LAYERSMITH_KEEP_BUILD_CONTEXTS=1`) |
| `LAYERSMITH_UPLOAD_DIR` | `$DATA/uploads` | uploaded files, one copy per checksum |
| `LAYERSMITH_LOG_DIR` | `$DATA/logs` | one log file per build |
| `LAYERSMITH_TMP_DIR` | `$DATA/tmp` | scratch space while bundling |

Exports are the big item: an archive is roughly the size of the image. Images
themselves live in the container runtime's storage, not in `$DATA`. Nothing
is deleted automatically; the Settings page shows what is used.

## Database

SQLite by default, at `$DATA/layersmith.db`. `LAYERSMITH_DATABASE_URL`
accepts any SQLAlchemy URL, so PostgreSQL works if you want it, and the
schema is created on startup.

Back up `$DATA` and you have the projects, build history and archives. The
images can always be rebuilt from the recorded Containerfile and base digest.

## Reverse proxy

Put LayerSmith behind a TLS-terminating proxy. The build log uses a
WebSocket at `/api/builds/{id}/logs`, so the proxy must be configured to
upgrade connections; without it, the UI falls back to the log stored with
the build but does not update live.

Nginx:

```
location / {
    proxy_pass http://127.0.0.1:8080;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 3600s;
}
```

## Authentication

0.1.0 has **no built-in authentication**. Anyone who can reach it can start
builds. Put it behind your existing authenticating proxy, or keep it on a
trusted network, until authentication ships.

## Building the application image

```
cd frontend && npm install && npm run build && cd ..
podman build -t layersmith:local -f Containerfile .
```

The Containerfile copies `frontend/dist` into the image and serves it from
the API, so the web UI and the API share one origin and one port.
