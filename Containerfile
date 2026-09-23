# LayerSmith application image.
#
# This builds the app itself, not the images it produces. It runs as a
# non-root user and needs no privileges of its own: builds go to the
# configured backend (a Podman socket or a remote build host).
FROM docker.io/library/python:3.12-slim

# The image carries no Podman client, so a container deployment talks to
# whichever socket is mounted through the Docker CLI installed below.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LAYERSMITH_DATA_DIR=/data \
    LAYERSMITH_BUILD_BACKEND=docker

# The Docker CLI (static binary, no daemon) is the client used in container
# deployments. It drives a mounted Docker socket, and equally a rootless
# Podman socket, which speaks the same API - so one small client covers both
# runtimes. Building directly on a host instead uses that host's own client.
ARG DOCKER_CLI_VERSION=27.5.1
ARG TARGETARCH=amd64
RUN set -eux; \
    arch="$(case "$TARGETARCH" in amd64) echo x86_64 ;; arm64) echo aarch64 ;; *) echo "$TARGETARCH" ;; esac)"; \
    apt-get update && apt-get install -y --no-install-recommends curl ca-certificates; \
    curl -fsSL "https://download.docker.com/linux/static/stable/${arch}/docker-${DOCKER_CLI_VERSION}.tgz" \
      | tar -xz -C /usr/local/bin --strip-components=1 docker/docker; \
    apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*; \
    docker --version

RUN useradd --create-home --uid 10001 layersmith \
    && mkdir -p /data \
    && chown layersmith:layersmith /data

WORKDIR /app
COPY backend/pyproject.toml ./pyproject.toml
COPY backend/layersmith ./layersmith
RUN pip install --no-cache-dir .

# Built web UI, produced by `npm run build` in frontend/.
COPY frontend/dist ./static

USER layersmith
EXPOSE 8080
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/api/health')"

CMD ["uvicorn", "layersmith.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]
