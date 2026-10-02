# LayerSmith agent rules

Public open-source project (GitHub origin). Follow CONTRIBUTING.md for setup,
tests and local builds; CI runs backend `pytest` and frontend `npm run build`.

- Code: backend in `backend/` (FastAPI, `layersmith` package, tests in
  `backend/tests`), frontend in `frontend/` (built assets are copied to
  `backend/static`). Do not hand-edit `frontend/dist` or `backend/static`.
- Run `.venv/bin/pytest` in `backend/` and `npm run build` in `frontend/`
  before committing.
- Commit as `r0lfi <rolf@linux.com>` (author and committer). No
  `Co-Authored-By` trailers.
- Releases: push a `vX.Y.Z` tag; GitHub Actions builds and publishes the
  container images. Pushing tags or commits needs explicit owner approval.
- Publication: no real users, credentials, internal hostnames, LAN addresses,
  private logs or screenshots. Use synthetic examples.
