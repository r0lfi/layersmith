# LayerSmith terminal client

Command line and interactive terminal interface for a
[LayerSmith](https://github.com/r0lfi/layersmith) server. Install it on a
jumphost, laptop or admin server and use LayerSmith over its HTTP API - no
container runtime, no server installation and no access to the container
host needed.

```bash
layersmith config set server https://layersmith.example.org
layersmith                      # interactive interface
layersmith projects list        # command line
layersmith build my-image --follow
layersmith export 12 --output ./image.tar
```

Requires Python 3.9 or newer. Full documentation: `docs/client.md` in the
LayerSmith repository; feature coverage: `docs/client-features.md`.

Development, from the repository root:

```bash
cd client
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pip install -e ../backend    # optional: enables the tests against the real API
.venv/bin/pytest
```
