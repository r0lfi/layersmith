# Terminal client (CLI and TUI)

LayerSmith runs as a server in a container. The terminal client is a
separate, small program you install on another machine - a jumphost, a
laptop, an admin server - to use that server from a terminal:

```
layersmith                         # interactive terminal interface (TUI)
layersmith projects list           # command line, for scripts and SSH sessions
```

It is the same LayerSmith with another interface, not a second product:

```
Browser  ──>  web UI   ─┐
Jumphost ──>  CLI     ──┼──>  LayerSmith HTTP API  ──>  builds, scans, history, files
Jumphost ──>  TUI     ──┘         (the server)
```

| The server owns | The client owns |
| --- | --- |
| projects, builds, versions, history | the terminal interface |
| Containerfile generation and validation | which server to talk to (its own config) |
| building, checks and scanning | showing the server's data |
| image archives and air-gap bundles | uploading files from the client machine |
| catalogs, templates and training profiles | downloading files to the client machine |

A project created in the terminal shows up in the web UI and the other way
round; build #12 is the same build everywhere. The client has no database,
no build engine and no copy of any catalog: every list of distributions,
templates, presets, tools and training profiles comes from the server.

The client does **not** need Docker, Podman, Trivy, a GPU, the LayerSmith
server package, a clone of this repository, or access to the container host.
You never need `docker exec` into the LayerSmith container.

- [Install](#install)
- [Connect to a server](#connect-to-a-server)
- [The interactive interface](#the-interactive-interface)
- [Command line](#command-line)
- [Spec files](#spec-files)
- [JSON output and exit codes](#json-output-and-exit-codes)
- [Jobs, logs and reconnecting](#jobs-logs-and-reconnecting)
- [Files: uploads and downloads](#files-uploads-and-downloads)
- [LLM training templates](#llm-training-templates)
- [Security and scanning](#security-and-scanning)
- [Network, TLS and authentication](#network-tls-and-authentication)
- [Offline use](#offline-use)
- [Doctor](#doctor)
- [Compatibility and limitations](#compatibility-and-limitations)

The function-by-function comparison with the web UI is in
[client-features.md](client-features.md).

## Install

Requirements on the client machine: **Python 3.9 or newer** with `venv` (or
`pipx`). That covers the stock `python3` of Enterprise Linux 9, Debian 12,
Ubuntu 22.04 and newer. The client is pure Python, so it runs on any Linux
architecture; it is tested on Linux x86_64.

The client is distributed as two files attached to every
[GitHub Release](https://github.com/r0lfi/layersmith/releases) from v0.4.0 on
(built by `client/scripts/build-release.sh`, see
[Building the client](#building-the-client)), with a `SHA256SUMS`:

| File | Contents |
| --- | --- |
| `layersmith_client-<version>-py3-none-any.whl` | the client alone; its dependencies come from PyPI |
| `layersmith-client-<version>-offline.tar.gz` | the client wheel plus every dependency for Python 3.9-3.13, `SHA256SUMS` and `INSTALL.txt` |

It is not published on PyPI. Download the file you need, or copy it to a
client machine without internet access:

```bash
curl -LO https://github.com/r0lfi/layersmith/releases/download/v0.4.0/layersmith-client-0.4.0-offline.tar.gz
```

With network access to PyPI, from the wheel:

```bash
pipx install ./layersmith_client-<version>-py3-none-any.whl
# or
python3 -m venv ~/.local/share/layersmith-client
~/.local/share/layersmith-client/bin/pip install ./layersmith_client-<version>-py3-none-any.whl
ln -s ~/.local/share/layersmith-client/bin/layersmith ~/.local/bin/layersmith
```

Without network access, from the offline archive (see [Offline use](#offline-use)):

```bash
tar -xzf layersmith-client-<version>-offline.tar.gz
cd layersmith-client-<version>-offline
sha256sum -c SHA256SUMS
pipx install --pip-args="--no-index --find-links=$PWD/wheelhouse" layersmith-client
# or, without pipx
python3 -m venv ~/.local/share/layersmith-client
~/.local/share/layersmith-client/bin/pip install --no-index --find-links wheelhouse layersmith-client
```

Either way one installation gives both the command line and the interactive
interface; there are no extras to choose. Nothing needs root.

```bash
layersmith version        # works without a server
layersmith --help
```

The server's container is unchanged: it still starts the web application,
and the client is not part of it.

## Connect to a server

```bash
layersmith config set server https://layersmith.example.org
layersmith config show
layersmith doctor
```

Or for one command:

```bash
layersmith --server https://layersmith.example.org projects list
```

Where the server address comes from, highest priority first:

| Source | Example |
| --- | --- |
| command-line flag | `--server https://layersmith.example.org` |
| environment variable | `LAYERSMITH_SERVER=https://layersmith.example.org` |
| client config file | `layersmith config set server https://layersmith.example.org` |
| default | `http://127.0.0.1:8080` - a server on the same machine, as in `compose.yml` |

`layersmith config show` prints every setting with the place it came from.
The config file is `$XDG_CONFIG_HOME/layersmith/client.json` (normally
`~/.config/layersmith/client.json`, mode 0600); `LAYERSMITH_CONFIG` or
`--config` points elsewhere. Use the server's base URL; a trailing `/api` is
removed.

| Setting | Environment | Meaning |
| --- | --- | --- |
| `server` | `LAYERSMITH_SERVER` | the LayerSmith server |
| `ca_cert` | `LAYERSMITH_CA_CERT` | CA bundle for the server certificate (`--ca-cert`) |
| `client_cert`, `client_key` | `LAYERSMITH_CLIENT_CERT`, `LAYERSMITH_CLIENT_KEY` | client certificate for a proxy that requires mutual TLS |
| `username` | `LAYERSMITH_USERNAME` | user for a proxy with HTTP Basic authentication |
| `password_file` | `LAYERSMITH_PASSWORD_FILE` | file with that password, mode 0600 |
| - | `LAYERSMITH_PASSWORD` | the password itself, for one session |
| `timeout` | `LAYERSMITH_TIMEOUT` | seconds to wait for the server (`--timeout`, default 30) |
| `download_dir` | `LAYERSMITH_DOWNLOAD_DIR` | the directory the TUI offers for downloads |

These are **client** settings. The server's storage paths are a different
thing, shown and changed with `layersmith settings show` and
`layersmith settings storage set` - those paths are on the server.

If the server cannot be reached the client says so and stops (exit code 3).
It never starts a server, never falls back to another address, and never
changes anything on the server's host.

## The interactive interface

```bash
layersmith          # or: layersmith tui
```

```
   ▄▄▄▄▄    LayerSmith                               Server https://layersmith.example.org
 ▄▄▄▄▄▄▄▄▄  Build container images with purpose                             ● Connected
▄▄▄▄▄▄▄▄▄▄▄ by xnett.org                          client 0.4.0  ·  server 0.4.0
─────────────────────────────────────────────────────────────────────────────────────────
╭────────────────────────╮ ╭ Projects 4 projects ─────────────────╮ ╭ Project details ──────────╮
│  Dashboard             │ │ Name        Template  Latest Status  │ │ net-tools                 │
│▍ Projects              │ │ net-tools   Network   1.1.0  READY   │ │ Packet capture tools      │
│  Create Image          │ │ ops-toolbox Linux Ad  2.3.0  FAILED  │ │ CONFIGURATION             │
│  AI / LLM Training     │ │ kube-clients Kubern   1.0.0  QUEUED  │ │ Template   Network Tools  │
│  Builds                │ │                                      │ │ LATEST BUILD              │
│  Scans                 │ ╰──────────────────────────────────────╯ │ #2  1.1.0  READY          │
│  Exports               │                                          │ [Build] [Open] [Logs]     │
│ ────────────────────── │ ╭ Log net-tools #2 1.1.0 READY following ──────────────────────────╮
│  Settings              │ │ STEP 1/6: FROM ...                                               │
│  Connection            │ │ WARNING: htop is only in EPEL on this distribution; skipped      │
│  Help                  │ │ Image ready: layersmith/net-tools:1.1.0 (118 MB archive)         │
╰────────────────────────╯ ╰──────────────────────────────────────────────────────────────────╯
 ⏎ Open  l Log  n New  b Build  / Search  ^q Quit  ? Help  f2 Menu
```

The header always shows which server the interface talks to, whether it
answers (Connected, Connecting, Disconnected) and both versions. If it does not, the **Connection** page opens: change the address,
retry, save it as the default, or run the doctor.

| Page | What it does |
| --- | --- |
| Dashboard | key figures, recent builds, server health, active builds (or "No active builds") and the log of the selected recent build |
| Projects | projects with their latest build, the selected project's details and the log of its latest build; `n` new, `b` build, `/` filter, `i` import a Dockerfile/Containerfile, `c` clone, `s` save as spec file, `d` delete, Enter opens the project (overview, Containerfile - editable for Advanced projects - and its builds) |
| Create Image | the general wizard: Purpose, Base, Version, Tools, Packages, Files, Scripts, Image, Review |
| AI / LLM Training | the training wizard: Purpose, Hardware, Software, Add-ons, Image, Review |
| Builds | history, details of the selected build, and its live log underneath; Enter opens the build (overview, log, Containerfile, security, training checks, downloads) |
| Scans | the server's scanner and the security status of recent images |
| Exports | image archives and air-gap bundles: `x` download archive, `a` create bundle, `g` download bundle |
| Settings | server settings and storage paths (`e` changes one), and this client's configuration |
| Connection | server address, connect, save, doctor |
| Help | keys and what happens where |

Keys: arrows move, **Enter** opens the highlighted menu page or item (moving
in the menu alone does not switch pages), **Tab**/**Shift+Tab** move between
the menu, lists, panels and fields, **Space** toggles, **Escape** closes a
dialog, clears a filter or goes back, **F2** jumps to the menu, **?** (or F1)
help, **Ctrl+R** reloads, **Ctrl+Q** quits (plain `q` too, except while
typing in a field). In lists: `l` goes to the log, `f` follows it again after
you scrolled back (new lines never pull you down while you read), and on
narrower terminals `t` switches the pane under the list between details and
log. The footer shows the keys that work where you are.

The wizard keeps everything you entered when you go back and forth, checks
the name and version before it lets you past the Image step, and shows the
server's verdict, warnings and generated Containerfile on the Review step.
**CLI command** shows the equivalent command line, **Save spec file** writes
the definition as a [spec file](#spec-files); both recreate exactly the same
definition. A generated Containerfile is never editable in the terminal (it
is regenerated on every build); Advanced-mode projects have an editor.

Every status is written as a word (READY, FAILED, NOT RUN, NOT SCANNED ...),
so the interface needs no colour: it follows the terminal's capabilities,
falls back from truecolor to 256 or 16 colours, and honours `NO_COLOR`. No
special fonts are needed. It needs at least 80x24 and says so if the
terminal is smaller. From 140x40 the list, details and log are side by side;
smaller, the list stays on top with details or log underneath; below 100x28
the header and spacing shrink. Selection, focus and filled-in forms survive a
resize. Network calls and log reads run in the background, so the interface
does not freeze on a slow server or a large log.

## Command line

`layersmith --help` and `layersmith <command> --help` describe everything.
A project can be named by id or name, a build by id or number (`12` or `#12`).

```bash
layersmith version
layersmith doctor
layersmith dashboard

# what the server offers
layersmith catalog                       # sections
layersmith catalog distributions
layersmith templates list
layersmith catalog presets
layersmith catalog packages

# projects
layersmith projects list
layersmith projects show net-tools
layersmith projects create --spec ./alpine-tools.json
layersmith projects create --name net-tools --template "Network Tools" \
    --distribution Ubuntu --distro-version 24.04 --package jq --file ./motd:/etc/motd
layersmith projects preview --spec ./alpine-tools.json      # Containerfile, nothing stored
layersmith projects import --name legacy --containerfile ./Containerfile
layersmith projects update legacy --containerfile ./Containerfile
layersmith projects update net-tools --spec ./net-tools.json
layersmith projects clone net-tools --name net-tools-arm
layersmith projects export-spec net-tools --output ./net-tools.json
layersmith projects delete net-tools-arm --yes

# builds
layersmith build net-tools --version 1.2.0 --wait
layersmith build net-tools --follow                  # log on stderr while waiting
layersmith builds list
layersmith builds show 12
layersmith builds logs 12 --follow
layersmith builds wait 12 --logs                     # re-attach to a running build
layersmith builds containerfile 12
layersmith builds manifest 12

# files, to and from THIS machine
layersmith upload ./tool.bin
layersmith export 12 --output ./image.tar
layersmith bundle create 12
layersmith bundle export 12 --output ./

# security
layersmith scanner
layersmith scan 12 --wait
layersmith scans list 12
layersmith scans show <scan-id> --severity critical
layersmith scans sbom <scan-id> --output ./

# LLM training templates
layersmith training profiles
layersmith training profile hf-finetune
layersmith training catalog concepts
layersmith training resolve --profile hf-finetune --target nvidia-cuda --intent task=sft --intent method=lora
layersmith training create --name llm-finetune --profile hf-finetune --build --follow
layersmith training checks 14
layersmith training guide 14
layersmith training gpu-report 14 --file ./gpu-report.json

# server settings
layersmith settings show
layersmith settings storage set image_dir /srv/layersmith/images --yes
```

Results go to **stdout**; progress, warnings and errors go to **stderr**.
Nothing asks a question unless stdin and stderr are a terminal and `--json`
is off. Destructive commands (`projects delete`, `settings storage set`) ask
for confirmation in a terminal and need `--yes` otherwise. `--yes` only
skips the question; the server still validates everything.

## Spec files

A spec file is a JSON document describing a project: the same fields the web
UI sends, in a small envelope. The server validates it - the client does not
keep a parallel set of rules.

```json
{
  "format": "layersmith-project/v1",
  "name": "alpine-tools",
  "description": "Small Alpine image with a few network tools and a message of the day",
  "template": "Minimal",
  "mode": "gui",
  "spec": {
    "base": {"distribution": "Alpine", "version": "3.22"},
    "architecture": "amd64",
    "packages": ["curl", "jq"],
    "files": [
      {"path": "motd.txt", "destination": "/etc/motd", "mode": "0644"}
    ],
    "env": [{"name": "TZ", "value": "UTC"}],
    "tests": ["curl --version", "jq --version", "test -s /etc/motd"]
  }
}
```

This example is [`client/examples/alpine-tools.json`](../client/examples/alpine-tools.json).

| Field | Meaning |
| --- | --- |
| `format` | `layersmith-project/v1` (optional) |
| `name` | project name: lowercase letters, digits, dashes; `--name` overrides it |
| `description`, `repository`, `template` | as in the web UI; `repository` defaults to the server's namespace plus the name |
| `mode` | `gui` (generated Containerfile) or `advanced` (your own) |
| `spec` | the image definition, exactly as in the API (`base`, `architecture`, `presets`, `packages`, `extra_packages`, `enable_epel`, `files`, `tools`, `scripts`, `env`, `labels`, `user`, `workdir`, `tests`, `tag_latest`, `training`) |
| `containerfile` / `containerfile_path` | for `advanced`: the text, or a local file to read it from |

One addition, handled by the client: an entry in `spec.files` or
`spec.tools` may give `"path"` - a file **on the client machine**, relative to
the spec file. The client uploads it and replaces `path` with the `sha256`
the server returns, the same as an upload in the web UI. A local path is
never sent to the server. Entries may also give `sha256` directly for a file
already uploaded (`layersmith upload`).

`layersmith projects export-spec` writes a project in this format, with
checksums instead of paths, and `projects create --spec` turns it back into
the same definition. `projects preview --spec` shows the Containerfile the
server would generate without storing anything (local files are only
checksummed for a preview, not uploaded).

## JSON output and exit codes

`--json` (before the command) prints machine-readable JSON on stdout, with
no colour, no escape sequences and no prompts:

```bash
layersmith --json projects list | jq -r '.[].name'
```

Commands that follow a job (`build --wait/--follow`, `builds wait`,
`builds logs --follow`, `training create --build --wait`) print **JSON Lines**
instead, one object per line, ending with a `done` record:

```
{"type": "status", "build_id": "...", "status": "building"}
{"type": "log", "build_id": "...", "line": "STEP 1/7: FROM ..."}
{"type": "done", "build": {"id": "...", "status": "ready", ...}}
```

An error in `--json` mode is also one JSON object on stdout
(`{"error": ..., "hint": ..., "status": ..., "exit_code": ...}`), and the
message is repeated on stderr.

| Exit code | Meaning |
| --- | --- |
| 0 | success |
| 1 | unexpected error, or a server error (HTTP 5xx) |
| 2 | invalid command line, or a confirmation was needed and not given |
| 3 | the server could not be reached (network, DNS, TLS); nothing was changed |
| 4 | the server refused the request (validation, conflict, too large) |
| 5 | project, build, scan or file not found |
| 6 | authentication needed or refused, a redirect, or an HTML page instead of the API |
| 7 | a change was sent but no answer came back: it may have happened; check before repeating |
| 8 | local file problem: exists, missing, checksum mismatch, interrupted transfer |
| 9 | with `--wait`: the build or scan finished unsuccessfully |
| 10 | the server is too old for this operation |
| 130 | interrupted with Ctrl+C; jobs on the server keep running |

## Jobs, logs and reconnecting

Builds and scans run **on the server**. Accepting a job and finishing it are
different things: without `--wait`, `layersmith build` prints the build id as
soon as the server has accepted it. With `--wait` or `--follow` it follows
the build and exits 9 if the build fails.

Quitting the client, closing the TUI, losing the SSH session to the jumphost
or losing the network does not stop a build or a scan. Ctrl+C while waiting
stops **waiting**, not the job, and prints how to re-attach:

```bash
layersmith builds wait <build-id> --logs     # or open it again under Builds in the TUI
layersmith builds logs <build-id> --follow
```

A lost connection while following is shown as lost and retried for up to
five minutes; it is never reported as a finished job. LayerSmith has no
cancel operation for builds, so the client offers none - and killing the
client does not cancel anything either.

The client never repeats a request that changes something. If a request to
start a build is sent and the answer is lost, exit code 7 says the build may
have started, and `layersmith builds list` shows whether it did. Reads are
retried a few times on temporary failures.

Logs are read incrementally from the server by byte offset, so following a
long build does not download the whole log again, and the TUI keeps at most
the last 5000 lines in memory (the full log stays on the server). The server
reports build states, not percentages, so the client shows states.

## Files: uploads and downloads

The client machine and the server have separate file systems, and the
client never assumes otherwise.

- **Uploads**: a file added to an image (`--file`, `"path"` in a spec, the
  wizard's Files step, `layersmith upload`) is read on the client machine and
  uploaded through the API. The server's limits apply (`layersmith settings
  show` lists them: upload size, files per image, build context size), and
  the server checks the checksum.
- **Downloads**: `export`, `bundle export` and `scans sbom` write to the
  client machine. The file is streamed to a temporary file in the target
  directory, checked against the SHA256 the server recorded, and only then
  given its name. A mismatch, a short transfer or an interruption leaves no
  file behind (exit 8). An existing file is never replaced without `--force`
  (or a confirmation in the TUI). If the server recorded no checksum, the
  download says it is *not verified* rather than calling it verified.
- When `--output` is a directory, the server's file name is used only if it
  is a plain file name; anything else falls back to a fixed name, so a
  server cannot write outside the directory.
- `has_export`/`has_airgap` from the server decide what is offered; a file
  removed after its status was read is reported as gone.
- Nothing is unpacked, executed or loaded into a container runtime on the
  client machine. Loading an archive is your step: `podman load -i image.tar`.

`bundle export` creates the bundle on the server first if there is none
(`--no-create` refuses instead); `bundle create` recreates it explicitly.
The bundle is LayerSmith's own air-gap bundle, the same as from the web UI.

## LLM training templates

Everything from the web UI's training wizard is available: the server's
profiles, intents (task, method, execution), targets, software stacks and
add-ons, with the tools they include, what each is for, locked versions,
compatibility conflicts with the server's suggested alternative, what to do
before building, the generated Containerfile, check results, Getting started
and what the export contains - and what you must bring separately (model
weights, datasets).

```bash
layersmith training profiles
layersmith training profile llama-factory
layersmith training resolve --profile pytorch-advanced --stack fa2-cu128 --addon deepspeed
layersmith training create --name ft --profile hf-finetune --build --follow
layersmith training checks <build>
layersmith training guide <build>
```

Check results keep their meaning: **PASSED**, **FAILED** and **NOT RUN** are
the server's statuses, and anything else is shown as **UNKNOWN**. The GPU
test is never run on the build host, so it stays NOT RUN until you run the
command shown by `training checks` on the training machine and record its
report with `training gpu-report`. The client needs no GPU and downloads no
models; nothing starts training.

## Security and scanning

`layersmith scanner` shows what the server has. Security status is reported
as one of: not scanned because no scanner is configured, not scanned because
the scanner is unavailable, not scanned yet, scan queued/running, scan
failed, completed without findings (which is "no match", not a guarantee),
or completed with findings - never as "clean" when nothing is known.
Findings come with severity and kind filters; the server caps a response at
the worst 500 (`--limit` up to 5000). Secret findings show the server's
masked description, never a value. Scanning never blocks a build, from any
interface.

Server names, descriptions, build logs and findings are treated as untrusted
text: terminal escape sequences and control characters are removed before
display, and the TUI never interprets them as markup.

## Network, TLS and authentication

The client talks HTTP(S) to the LayerSmith API - the same port as the web
UI, normally behind your reverse proxy. That is the only network access it
needs; open it from the jumphost to the server or proxy. The client never
changes ports, firewalls or the server's configuration. An SSH tunnel works
too (`ssh -L 8080:127.0.0.1:8080 server`, then `--server http://127.0.0.1:8080`),
but it is an alternative, not a requirement.

- **TLS** is verified, always. For a certificate from an internal CA,
  `layersmith config set ca_cert /etc/pki/ca-trust/source/anchors/internal-ca.pem`
  (or `--ca-cert`, or `LAYERSMITH_CA_CERT`). A verification failure is an
  error (exit 3) with that hint; there is no option to switch checking off.
- **Authentication**: LayerSmith itself has none, by design (see
  [deployment.md](deployment.md#authentication)). If a proxy in front of it
  requires credentials, the client supports:
  - **HTTP Basic**: `config set username ops` and the password from
    `LAYERSMITH_PASSWORD`, from a `password_file` with mode 0600, or a prompt
    in an interactive terminal. Never as a command-line argument.
  - **Client certificates** (mutual TLS): `client_cert` and `client_key`.

  A login page, a redirect to single sign-on, or any HTML answer is reported
  as an authentication problem (exit 6), never as success. Redirects are not
  followed, so credentials never reach another host. Bearer tokens and
  browser-based single sign-on are not supported, because LayerSmith has no
  token API to support.
- Credentials are never printed: not in errors, `config show`, the TUI,
  generated commands or logs. A server URL containing credentials is refused.
- `HTTPS_PROXY`/`NO_PROXY` from the environment are honoured, like curl.

## Offline use

Three separate things:

| Situation | What works |
| --- | --- |
| **Client machine without internet** | Everything, once installed from the offline archive and as long as it reaches the LayerSmith server. The client makes no other connections: no update checks, telemetry, catalog lookups or downloads. |
| **Server without internet** | Builds still need their inputs: base images, distribution packages and training dependencies must be reachable from the server or already present, and scanning needs its database. The client does not change that - see [deployment.md](deployment.md) and [scanning.md](scanning.md). |
| **Built image used air-gapped** | That is what image archives and air-gap bundles are for; download them with the client and carry them over. |

The offline archive holds the client wheel and every dependency as pure
Python wheels (`py3-none-any`) for Python 3.9 to 3.13, a `SHA256SUMS` and an
`INSTALL.txt`. It was tested with networking disabled on Python 3.9, 3.10,
3.11, 3.12 and 3.13 (Debian-based `python:*-slim`) with venv, and with pipx
on 3.12. The target needs Python and venv (or pipx) already installed.

## Doctor

```bash
layersmith doctor
layersmith --json doctor
```

Read-only. It reports the client (version, Python, configured server and
where that came from, config file, CA bundle) separately from the server
(reachable, version, API compatibility, build backend, server storage,
scanner). What it cannot determine is `unknown`, never `ok`. A missing
Docker, Trivy or GPU on the client machine is irrelevant and not reported.
Doctor installs nothing, starts nothing, and changes no configuration.

## Compatibility and limitations

The client and server versions follow the same release tags. The client
uses two small API additions made for remote clients (incremental log reads
and the upload limits in `/api/settings`) and lists them as `api_features`
in `/api/health`. Against an older server (0.3.x) everything else works; log
following falls back to the part of the log the server returns with a build,
and `doctor` says so.

Known limitations, also in [client-features.md](client-features.md):

- No build cancellation - the server has none.
- Scan progress is a state (queued, exporting, scanning), not a percentage;
  scan logs are not exposed by the API, so the client shows states only.
- The TUI's general wizard edits files and scripts but not every field of
  the definition (labels, user, working directory, environment variables,
  custom tools); use a spec file for those, as in the web UI.
- Air-gap bundles are created synchronously by the server; for a very large
  image `bundle create` can take minutes.
- Bearer-token and browser-based authentication are not supported (see above).
- Tested on Linux. macOS should work, as everything is pure Python, but has
  not been tested; Windows is not supported.

## Building the client

The client lives in `client/` of this repository, as its own package
(`layersmith-client`, module `layersmith_client`), separate from the server
package. To build the release files:

```bash
client/scripts/build-release.sh 0.4.0            # writes client/dist/
```

This needs Python with pip, `uv` (to resolve dependencies per Python
version) and access to PyPI. The version is stamped into a temporary copy,
never into the source; a source checkout reports `0.0.0.dev0`. See
[RELEASING.md](RELEASING.md) for how these files are attached to a release.
