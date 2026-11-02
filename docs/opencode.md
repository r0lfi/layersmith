# opencode Shared Terminal

[opencode](https://opencode.ai/), the terminal coding agent, in one
always-running container on a host. Users type `opencode` on the host and are
working inside it as themselves, in the directory they were in. Several users
can work at the same time. Made for air-gapped networks with a local
OpenAI-compatible LLM endpoint (vLLM, Ollama, LiteLLM, TGI).

Category **Developer tools / AI coding agents**. In the web UI: **Create image →
opencode Shared Terminal** (or the tile on the dashboard). Through the API, a
project whose spec is `{"opencode": {...}}`.

## What you choose

| Setting | Default | Options |
|---|---|---|
| Base image | AlmaLinux 10 | AlmaLinux, Rocky Linux, CentOS Stream 10 or 9; or a custom EL9/EL10-compatible reference with a tag or digest |
| opencode | 1.19.5 | 1.19.4. Only 1.x: opencode's own installer installs nothing else |
| Target CPU | x64 (needs AVX2) | x64-baseline: any x86-64 CPU (older servers, virtual CPUs without AVX2) |
| Toolchains | Python 3 + pip | gcc/g++/make, Node.js + npm; none |

```json
{"opencode": {"base": "AlmaLinux 10", "opencode_version": "1.19.5", "cpu": "x64",
              "optional": ["python", "build-tools"]}}
```

The LLM endpoint is **not** part of the image: you write it on the host after
installing (see below), so one image serves any endpoint.

## Is a container a good idea for opencode?

Yes, for a shared air-gapped host:

* opencode is one self-contained binary; its terminal UI works through
  `podman exec -it` like on the host.
* The agent runs shell commands. In the container it only sees the shared
  directories (default `/home`), with the user's own uid and groups - not the
  rest of the host - and it has no capabilities and no way to become root.
* Every user gets the same tools, and the host stays clean.

What to keep in mind: the agent can only run what is in the image (choose the
toolchains your projects need), and SSH keys or git credentials are the users'
own files in their homes.

## How it works

```text
 user on the host                  shared container "opencode" (always running)
 $ cd ~/projects/app
 $ opencode  ──sudo──> opencode-enter ──podman exec --user <uid>──> opencode
                                                                     as you, your groups,
                                                                     in ~/projects/app
 /home shared at the same path · /etc/opencode read-only (site settings)
```

1. `opencode-container.service` keeps the container running
   (`podman run --init --cap-drop ALL --security-opt no-new-privileges
   --network host`, the shared directories mounted at the same path).
2. `/usr/local/bin/opencode` runs `sudo -n opencode-enter`. The sudo rule allows
   exactly that one program, for members of one group (default `opencode`).
3. `opencode-enter` takes `SUDO_UID` and the current directory - nothing else
   from the caller - looks the user up in the host's user database, and runs
   `ensure-user` as root in the container, which writes that user and their
   groups into the container's `/etc/passwd` and `/etc/group`.
4. It then runs `podman exec -it --user <uid> --workdir <cwd> opencode opencode
   <args>`: opencode runs with the user's uid, primary and supplementary groups,
   in their directory. Outside the shared directories it starts in the user's
   home and says so.

Sessions, history and personal settings are in the user's own home
(`~/.local/share/opencode`, `~/.config/opencode`), so they survive image
updates and nobody sees anyone else's.

## Offline

Set in the image:

| Variable | Why |
|---|---|
| `OPENCODE_CONFIG=/etc/opencode/opencode.json` | the site settings, mounted read-only from the host |
| `OPENCODE_DISABLE_AUTOUPDATE=1` | no update check |
| `OPENCODE_DISABLE_MODELS_FETCH=1` | no model catalogue download from models.dev (the copy built into opencode is used) |
| `OPENCODE_DISABLE_LSP_DOWNLOAD=1` | no language-server downloads |

ripgrep is installed because opencode otherwise downloads it from GitHub on
first use. The OpenAI-compatible provider (`@ai-sdk/openai-compatible`) is built
into opencode, so nothing is installed from npm. When the host trusts an internal
CA, the container passes the host's CA bundle to opencode (`NODE_EXTRA_CA_CERTS`),
so an endpoint with an internal certificate works.

## Checks after each build

Both run with networking disabled and every capability dropped.

* **image** (root): no sudo, no SSH server, no SSH keys or opencode credentials
  in the image; the offline settings; the host kit; `ensure-user` adds a user
  with three groups, follows a moved home and a dropped group, refuses uid 0,
  and turns a directory-service name like `user@domain` into `u<uid>`.
* **user** (uid 1000): opencode, ripgrep, git, jq, bash and the chosen
  toolchains report their pinned versions; `opencode debug config` loads the
  site settings; `opencode models` lists a local OpenAI-compatible model from a
  user config; `rg` finds code; data lands in `$HOME/.local/share/opencode`.

## Install on the host

The build page has the archive, its `.sha256`, `install-host.sh` and the README,
with **Download all**. On the host, as root:

```bash
sha256sum -c opencode-1.0.0.tar.sha256
sudo bash install-host.sh --archive opencode-1.0.0.tar --users "alice bob"
```

`install-host.sh` loads the image, takes the host files out of the image itself
(so they always match it), and installs:

| Where | What |
|---|---|
| `/usr/local/bin/opencode`, `opencode-shell` | the commands users type |
| `/usr/local/libexec/opencode-container/opencode-enter` | run through sudo; starts opencode as the caller |
| `/usr/local/libexec/opencode-container/opencode-container` | `start`, `stop`, `status`, `command` |
| `/etc/sudoers.d/opencode-container` | `%opencode ALL=(root) NOPASSWD: .../opencode-enter`, validated with `visudo -c` |
| `/etc/opencode-container/container.conf` | image, runtime, `MOUNTS`, `NETWORK`, `HOST_CA`, `EXTRA_ARGS` |
| `/etc/opencode/opencode.json` | site opencode settings; written once, never overwritten |
| `/etc/systemd/system/opencode-container.service` | enabled and started |

Options: `--mounts "/home /srv/projects"`, `--group NAME`, `--users "A B"`,
`--runtime podman|docker` (default: Podman if installed), `--image REF`,
`--no-start`, `--uninstall`. Running it again with a new archive updates the
image and the host files, keeps `container.conf` and the site settings, and
restarts the container (open sessions end; their history is kept).

Only the archive at hand? `podman load -i opencode-1.0.0.tar`, then
`podman run --rm --entrypoint cat localhost/opencode:1.0.0
/usr/local/share/opencode/host/install-host.sh > install-host.sh`.

## Point it at your LLM

Edit `/etc/opencode/opencode.json` on the host; the next `opencode` started uses
it. The file is JSONC (comments and trailing commas are fine):

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "autoupdate": false,
  "share": "disabled",
  "provider": {
    "local": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Local LLM",
      "options": { "baseURL": "http://LLM-HOST:8000/v1" },
      "models": { "MODEL-NAME": { "name": "MODEL-NAME", "limit": { "context": 131072, "output": 16384 } } }
    }
  },
  "model": "local/MODEL-NAME"
}
```

`MODEL-NAME` is the id the endpoint lists at `/v1/models` (for vLLM, the
`--served-model-name`). Everyone who can run opencode can read this file, so put
only a shared endpoint key there. Users add their own settings in
`~/.config/opencode/opencode.json`, a repository can carry an `opencode.json`;
opencode merges those over the site file.

## Operating it

```bash
sudo /usr/local/libexec/opencode-container/opencode-container status   # running?, image, open sessions
sudo systemctl restart opencode-container                               # after editing container.conf
journalctl -u opencode-container
sudo usermod -aG opencode carol                                         # access (log in again)
```

For many users, limit the container as a whole in `container.conf`, e.g.
`EXTRA_ARGS=(--memory 32g --pids-limit 8192)`; each opencode session uses a few
hundred MB.

## Tested

On Podman 5: LayerSmith's Podman backend built the image (both checks passed
with networking disabled), then a throwaway host with Podman inside ran the
`install-host.sh` from the build page against the exported archive. Verified
there: the service started the container; alice and bob ran `opencode run` at
the same time against a mock OpenAI-compatible endpoint that asked the agent to
run a shell command, and each command ran as that user with their groups, in
their project, writing files owned by them on the host; the opencode TUI started
through a real terminal; `opencode-shell` showed no sudo; a user outside the
`opencode` group was refused with a message; starting outside `/home` fell back
to the home directory; re-installing kept the settings; `--uninstall` removed the
commands and the sudo rule. Not run: the Docker runtime path on the host
(`--runtime docker`, same commands), a real systemd boot, and a real vLLM endpoint.
