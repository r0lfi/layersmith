# Terminal client: feature matrix

Every user function of the web UI, the API it uses, and where it is in the
terminal client. "Tests" names the client test module that covers it
(`client/tests/`): `api` = HTTP client with a mocked transport, `cli` =
command line, `ops` = shared operations, `server` = CLI against the real API
(fake build runtime), `tui` = headless TUI against the real API. "manual" = run by hand with the
CLI against a real server during development; "not tested" says what was
not. "E2E" means
also verified from a separate client container against a server doing real
Podman builds (see [client.md](client.md)).

## Overview and catalog

| Function | API | CLI | TUI | Tests / status |
| --- | --- | --- | --- | --- |
| Dashboard: counts, recent builds | `GET /api/stats` | `dashboard` | Dashboard | manual |
| Server status on the dashboard | `GET /api/settings` | `doctor`, `settings show` | Dashboard (Server panel) | server |
| Distributions and versions | `GET /api/catalog` | `catalog distributions` | Create Image: Base, Version | tui |
| Architectures | `GET /api/catalog` | `catalog architectures`, `--arch` | Create Image: Version | tui |
| Templates | `GET /api/catalog` | `templates list`, `catalog templates` | Create Image: Purpose | tui |
| Presets (tool sets) | `GET /api/catalog` | `catalog presets`, `--preset` | Create Image: Tools | manual |
| Individual tools (capabilities by category) | `GET /api/catalog` | `catalog packages`, `--package` | Create Image: Tools | server |
| Custom tool install paths | `GET /api/catalog` | `catalog tools` | - (spec file) | manual; custom tools in a spec not tested |

## Projects and image definition

| Function | API | CLI | TUI | Tests / status |
| --- | --- | --- | --- | --- |
| List projects | `GET /api/projects` | `projects list` | Projects | server, tui, E2E |
| Project details, resolved packages, warnings | `GET /api/projects/{id}` | `projects show` | Projects, project page | server |
| Create from the wizard | `POST /api/projects` | `projects create` (flags or `--spec`) | Create Image | server, tui, E2E |
| Custom base image + package family | `POST /api/projects` | `--base-image --family` | Create Image: Base | manual |
| Extra packages, EPEL | `POST /api/projects` | `--extra-package`, `--enable-epel` | Create Image: Packages | tui |
| Files into the image (upload) | `POST /api/uploads` | `--file`, spec `"path"`, `upload` | Create Image: Files | api, ops, server, E2E |
| Scripts (pre_build, post_install, entrypoint) | `POST /api/projects` | `--pre-build`, `--post-install`, `--entrypoint` | Create Image: Scripts | E2E |
| Environment, labels, user, workdir, tests, custom tools | `POST /api/projects` | spec file; `--env`, `--test` | - (spec file) | server (env refusal) |
| Containerfile preview, nothing stored | `POST /api/preview` | `projects preview` | Create Image: Review | tui, manual |
| Equivalent command / spec file from the wizard | - | `projects export-spec` | Review: CLI command, Save spec file; project page `v`, `s` | server (round trip) |
| Import Dockerfile/Containerfile (Advanced mode) | `POST /api/projects/import` | `projects import` | Projects `i` | server |
| Edit Advanced-mode Containerfile | `PUT /api/projects/{id}` | `projects update --containerfile` | project page: Containerfile, Edit | server |
| Change a generated definition | `PUT /api/projects/{id}` | `projects update --spec` / flags | - (the web UI has no such editor either) | manual |
| Clone | `POST /api/projects/{id}/clone` | `projects clone` | Projects `c` | server |
| Delete (builds and archives kept) | `DELETE /api/projects/{id}` | `projects delete` (confirm / `--yes`) | Projects `d` (confirm) | cli, server |
| Base update available | in project JSON | `projects show` | Projects details | not tested (display only) |

## Builds, versions and logs

| Function | API | CLI | TUI | Tests / status |
| --- | --- | --- | --- | --- |
| Start a build (next or given version) | `POST /api/projects/{id}/builds` | `build`, `--version` | Projects `b`, project page `b`, wizard "Create & build" | server, E2E |
| Immutable versions (409 on rebuild) | same | exit 4 with the server's message | dialog error | server |
| Build history | `GET /api/builds` | `builds list` | Builds | tui |
| Build details, manifest | `GET /api/builds/{id}` | `builds show`, `builds manifest` | build page Overview | server |
| Build's Containerfile | `GET /api/builds/{id}` | `builds containerfile` | build page Containerfile | manual |
| Live log | `GET /api/builds/{id}/log` (new, offset based) | `builds logs --follow`, `build --follow` | Builds log panel, build page Log | ops, server, tui, E2E |
| Wait for a running build / reconnect | `GET /api/builds/{id}` | `builds wait --logs` | Builds, build page | server, E2E |
| Failed build reported as failed | same | exit 9 | FAILED status | server |
| Cancel a build | **not in the API** | not offered | not offered | documented limitation |
| Build progress in percent | **not reported by the server** | states only | states only | documented |

## Exports and air-gap bundles

| Function | API | CLI | TUI | Tests / status |
| --- | --- | --- | --- | --- |
| Download image archive | `GET /api/builds/{id}/download/export` | `export --output` | Exports `x`, build page Downloads | api, server, E2E |
| Create/recreate air-gap bundle | `POST /api/builds/{id}/airgap` | `bundle create` | Exports `a`, build page | server, E2E |
| Download air-gap bundle | `GET /api/builds/{id}/download/airgap` | `bundle export` (creates if missing) | Exports `g`, build page | server, E2E |
| Availability (`has_export`, `has_airgap`) | in build JSON | checked before download | shown per build | server |
| SHA256 verification | `export_sha256`, `airgap_sha256` | always; "not verified" if missing | always | api, E2E |

## Scanning

| Function | API | CLI | TUI | Tests / status |
| --- | --- | --- | --- | --- |
| Scanner status and capabilities | `GET /api/scanner` | `scanner` | Scans, Dashboard | server |
| Scan / rescan | `POST /api/builds/{id}/scan` | `scan [--kind] [--wait]` | Scans `s`, build page Security | server (refused without scanner) |
| Scan history | `GET /api/builds/{id}/scans` | `scans list` | build page Security | server |
| Findings with kind/severity filters | `GET /api/scans/{id}` | `scans show --kind --severity --limit` | build page Security filters | not tested: no scanner in the test environment (404 for an unknown scan tested) |
| SBOM download | `GET /api/scans/{id}/sbom` | `scans sbom --output` | build page Security | not tested: no scanner (same download code as exports) |
| Not configured / unavailable / running / failed / clean kept apart | - | `scans list` status line | Scans, Security tab | ops (all cases), server |

## LLM training templates

| Function | API | CLI | TUI | Tests / status |
| --- | --- | --- | --- | --- |
| Category, concepts, intents, targets, stacks, add-ons, tools | `GET /api/training/catalog` | `training catalog <section>` | AI / LLM Training: Purpose | server, tui |
| Profiles with descriptions | `GET /api/training/catalog` | `training profiles`, `training profile` | Purpose | server, tui |
| Compatibility check, recommendation, working alternative | `POST /api/training/resolve` | `training resolve` | every step; "Use the working alternative" | server, tui |
| Recipe: base, Python, PyTorch, CUDA, tools, locked versions | `POST /api/training/resolve` | `training profile`, `training resolve` | Software | manual |
| Before you build, export contents, bring separately | `POST /api/training/resolve` | `training resolve`, `training guide` | Review | server |
| Create training project and build | `POST /api/projects` | `training create [--build]` | Review: Create & build | server |
| Check results (image, deps, cpu, gpu, offline) | `GET /api/builds/{id}` | `training checks` | build page Training | server (gpu NOT RUN) |
| Getting started for the build | `GET /api/builds/{id}` | `training guide` | build page Training | server |
| Record a GPU report from the target machine | `POST /api/builds/{id}/gpu-report` | `training gpu-report --file` | - (command shown; record with the CLI) | refusal for a non-training build by hand; recording a real report not tested |

## Settings and operations

| Function | API | CLI | TUI | Tests / status |
| --- | --- | --- | --- | --- |
| About: version, revision, source, licence | `GET /api/settings` | `settings show` | Settings | manual |
| Build backend and runtimes | `GET /api/settings` | `settings show`, `doctor` | Settings | server |
| Server storage paths, where each comes from | `GET /api/settings` | `settings show` | Settings | manual |
| Change a server storage path | `PUT /api/settings/storage` | `settings storage set` (confirm / `--yes`) | Settings `e` (confirm) | CLI manual; TUI not tested |
| Upload and build-context limits | `GET /api/settings` (`limits`, new) | `settings show` | Settings | backend test |
| Health and API features | `GET /api/health` (`api_features`, new) | `doctor` | header | api, server |

## Client-only functions

| Function | CLI | TUI | Tests |
| --- | --- | --- | --- |
| Server address: flag > environment > config > default | `--server`, `config set/show/unset/path` | Connection | config, cli, tui |
| Unreachable server, retry, change address | exit 3 | Connection page | cli, tui |
| Doctor (read-only, client and server apart) | `doctor` | Connection: Run doctor | server |
| Internal CA, client certificates, HTTP Basic | `--ca-cert`, config | same config | api; TLS by hand |
| JSON / JSON Lines output | `--json` | - | cli, server |
| Too-small terminal, resize, 80x24 | - | yes | tui |

## Not available, deliberately

- **Build cancellation**: the server has no cancel operation; killing the
  client is not a cancel.
- **Scan logs**: the server publishes them only on an internal channel, not
  through the API; the client shows scan states.
- **Loading images into a local runtime**, unpacking bundles or running
  their scripts: never done by the client.
- **Bearer tokens / browser SSO**: LayerSmith has no such API.
- **Log in, users, permissions**: LayerSmith has none (see deployment.md).
