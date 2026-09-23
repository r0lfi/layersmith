# LayerSmith application image.
#
# This builds LayerSmith itself, not the images its users create. Those are
# built by the configured build backend (docs/deployment.md), which is a
# container runtime on the host, reached through a mounted socket or a
# separate build host. This image therefore needs no privileges of its own.
#
# Build:   podman build -t layersmith:local -f Containerfile .
#          docker build  -t layersmith:local -f Containerfile .
# Publish: see .github/workflows/container.yml

# --------------------------------------------------------------- web UI
FROM docker.io/library/node:22-alpine AS ui

WORKDIR /ui
# Dependencies first, so a source-only change reuses the install layer.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY frontend/ ./
RUN npm run build


# ------------------------------------------------------- docker client
# The client used when LayerSmith talks to a mounted runtime socket. Copied
# from the official image rather than curled from a release URL at build
# time, so its content is pinned through the base image instead of being
# fetched unverified.
FROM docker.io/library/docker:27-cli AS cli


# ------------------------------------------------------------- runtime
FROM docker.io/library/python:3.12-slim AS runtime

# Stamped by the release build (see container.yml). The defaults mark a local
# build as a development build instead of letting it claim to be a release.
ARG VERSION=0.0.0-dev
ARG REVISION=unknown
ARG SOURCE_URL=https://github.com/r0lfi/layersmith

LABEL org.opencontainers.image.title="LayerSmith" \
      org.opencontainers.image.description="Build container images with purpose." \
      org.opencontainers.image.source="${SOURCE_URL}" \
      org.opencontainers.image.url="${SOURCE_URL}" \
      org.opencontainers.image.documentation="${SOURCE_URL}/blob/main/README.md" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.vendor="xnett.org"

# The image carries a Docker client, not a Podman one: it drives a Docker
# socket and equally a rootless Podman socket, which speaks the same API.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LAYERSMITH_DATA_DIR=/data \
    LAYERSMITH_STATIC_DIR=/app/static \
    LAYERSMITH_BUILD_BACKEND=docker \
    LAYERSMITH_VERSION=${VERSION} \
    LAYERSMITH_REVISION=${REVISION}

COPY --from=cli /usr/local/bin/docker /usr/local/bin/docker

WORKDIR /app

# Runtime dependencies only: no compiler or build tooling is kept here.
COPY backend/pyproject.toml ./pyproject.toml
COPY backend/layersmith ./layersmith
RUN pip install --no-cache-dir . \
    && find /usr/local -name '__pycache__' -type d -prune -exec rm -rf {} +

COPY --from=ui /ui/dist ./static

# Runs unprivileged. /data is a volume, so it must be writable by this user:
# a bind mount from the host needs to be owned by uid 10001, or use --user.
RUN useradd --create-home --uid 10001 layersmith \
    && mkdir -p /data \
    && chown -R layersmith:layersmith /data /app
USER layersmith

EXPOSE 8080
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import sys,urllib.request;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/health',timeout=4).status==200 else 1)"

CMD ["uvicorn", "layersmith.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]
