<p align="center">
  <img src="brand/logo.svg" alt="LayerSmith" width="360">
</p>

<p align="center"><strong>Build container images with purpose.</strong><br>
<sub>by xnett.org</sub></p>

---

LayerSmith is a self-hosted web application for building, versioning and
exporting OCI container images. Choose a base, choose a purpose, pick your
tools, and LayerSmith writes the Containerfile, builds the image with Podman,
streams the log to your browser and hands you a versioned image, an OCI
archive or an air-gap bundle.

Advanced users keep full control: import an existing Dockerfile or
Containerfile, or switch a project to Advanced mode and write it yourself.

## Why

Writing a good Containerfile by hand means remembering that DNS tools are
`bind-utils` on AlmaLinux, `dnsutils` on Ubuntu and `bind-tools` on Alpine,
that `apt-get` needs `--no-install-recommends` and a cleanup, and that a
`latest` tag quietly changes under you. LayerSmith knows those things.

## Features

- **Purpose-driven wizard** — Developer, Linux Admin, Network Tools, Ansible,
  Kubernetes, OpenShift, Minimal or Custom.
- **Distro-aware packages** — templates request capabilities, not package
  names; LayerSmith translates per distribution (dnf / apt / apk).
- **Generated Containerfile** — readable output you can inspect before
  building, or take over in Advanced mode.
- **Import** — paste or upload an existing Dockerfile/Containerfile.
- **Immutable, versioned builds** — a version is built once, never
  overwritten, and every build records the exact Containerfile, base digest,
  package list, file checksums and log.
- **Digest-pinned bases** — builds do not silently follow a moved tag.
- **Live build logs** — streamed to the browser over a WebSocket.
- **Export** — OCI archive with a recorded SHA256.
- **Air-gap bundles** — image, manifest, source Containerfile, `SHA256SUMS`
  and `INSTALL.txt` in one `.tar.gz` that loads with no network access.
- **Podman or Docker** — either runtime builds; `auto` picks whichever the
  host has. Bundles load with either, whatever built them.
- **API first** — the web UI uses the same HTTP API you can script against.

## Quick start

```
podman run -d \
  --name layersmith \
  -p 8080:8080 \
  -v layersmith-data:/data \
  ghcr.io/example/layersmith:latest
```

Open <http://localhost:8080>. See [docs/deployment.md](docs/deployment.md)
for building images inside a container, Docker/Compose, and running the API
and the builder on separate hosts.

## Configuration

Every path is configurable; the defaults live under one data directory so a
container needs a single volume.

| Variable | Default | Purpose |
| --- | --- | --- |
| `LAYERSMITH_DATA_DIR` | `/data` | Base directory for everything below |
| `LAYERSMITH_IMAGE_DIR` | `$DATA/images` | Exported archives and air-gap bundles |
| `LAYERSMITH_BUILD_DIR` | `$DATA/builds` | Build contexts |
| `LAYERSMITH_UPLOAD_DIR` | `$DATA/uploads` | Uploaded files and tool binaries |
| `LAYERSMITH_LOG_DIR` | `$DATA/logs` | Build logs |
| `LAYERSMITH_TMP_DIR` | `$DATA/tmp` | Temporary working space |
| `LAYERSMITH_DATABASE_URL` | `sqlite:///$DATA/layersmith.db` | Database |
| `LAYERSMITH_BUILD_BACKEND` | `auto` | `auto`, `podman` or `docker` |
| `LAYERSMITH_PODMAN_BINARY` | `podman` | Path to the Podman client |
| `LAYERSMITH_DOCKER_BINARY` | `docker` | Path to the Docker client |
| `LAYERSMITH_DEFAULT_ARCH` | `amd64` | Default architecture |
| `LAYERSMITH_DEFAULT_NAMESPACE` | `layersmith` | Default image namespace |
| `LAYERSMITH_STATIC_DIR` | `./static` next to the package | Built web UI, for installed deployments |

Missing directories are created at startup and checked for write access, so a
bad mount fails immediately with a clear message.

## Security

LayerSmith runs build jobs, so it is treated as security-sensitive:

- Commands are built as argv lists; no user value is interpreted by a shell.
- Image references, package names, paths, versions and environment variable
  names are matched against explicit patterns before use.
- Uploaded files are content-addressed and re-verified by checksum before
  they enter a build context.
- Environment variables that look like credentials are refused: secrets must
  not be baked into an image.
- Downloads are restricted to the configured image directory.

Report security issues privately rather than in a public issue.

## Development

```
cd backend
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest            # unit and API tests, no Podman required
.venv/bin/uvicorn layersmith.api:create_app --factory --reload
```

The test suite uses a fake build backend, so it runs anywhere. Building real
images needs Podman or Docker on the host running the backend:

```
LAYERSMITH_SMOKE=1 .venv/bin/pytest tests/test_runtime_smoke.py -v
```

That builds a real image with every runtime installed on the host, runs it to
confirm the packages are actually present, exports it and loads the archive
back. Runtimes that are not installed are skipped.

## License

MIT — see [LICENSE](LICENSE).
