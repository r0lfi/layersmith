# LayerSmith application image.
#
# This builds the app itself, not the images it produces. It runs as a
# non-root user and needs no privileges of its own: builds go to the
# configured backend (a Podman socket or a remote build host).
FROM docker.io/library/python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LAYERSMITH_DATA_DIR=/data

RUN useradd --create-home --uid 10001 layersmith \
    && mkdir -p /data \
    && chown layersmith:layersmith /data

WORKDIR /app
COPY backend/pyproject.toml ./pyproject.toml
COPY backend/layersmith ./layersmith
COPY README.md ../README.md
RUN pip install --no-cache-dir .

# Built web UI, produced by `npm run build` in frontend/.
COPY frontend/dist ./static

USER layersmith
EXPOSE 8080
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/api/health')"

CMD ["uvicorn", "layersmith.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]
