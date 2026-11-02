"""README and ready-made commands for an opencode Shared Terminal image.

The same text goes into the image (/usr/share/doc/opencode-shared/README.md
and, through install-host.sh, /etc/opencode-container/README.md on the host),
the air-gap bundle and the build page.
"""

from layersmith.opencode import catalog

INSTALLER = "install-host.sh"


def commands(recipe: dict, image_ref: str, archive: str) -> dict:
    return {
        "verify": f"sha256sum -c {archive}.sha256",
        "install": f"sudo bash {INSTALLER} --archive {archive}",
        "install_users": f"sudo bash {INSTALLER} --archive {archive} --users \"alice bob\"",
        "update": f"sudo bash {INSTALLER} --archive {archive}",
        "extract_installer": (f"podman run --rm --entrypoint cat {image_ref} "
                              f"/usr/local/share/opencode/host/{INSTALLER} > {INSTALLER}"),
        "load": f"podman load -i {archive}",
        "add_user": "sudo usermod -aG opencode alice",
        "config": "sudo vi /etc/opencode/opencode.json",
        "status": "sudo /usr/local/libexec/opencode-container/opencode-container status",
        "restart": "sudo systemctl restart opencode-container",
        "uninstall": f"sudo bash {INSTALLER} --uninstall",
        "use": "cd ~/projects/my-app\nopencode",
        "shell": "opencode-shell",
        "image": image_ref,
    }


def markdown(recipe: dict, image_ref: str, archive: str = "<archive>.tar") -> str:
    c = commands(recipe, image_ref, archive)
    tools = "\n".join(f"| {t['label']} | {t['version'] if t['enabled'] else 'not included'} | {t['purpose']} |"
                      for t in recipe["tools"])
    env = "\n".join(f"| `{key}` | `{value}` |" for key, value in recipe["environment"].items())
    return f"""# {catalog.PROFILE['name']}

{catalog.PROFILE['description']}

Image `{image_ref}`, {recipe['base']['display']} base, {catalog.ARCHITECTURE},
opencode {recipe['opencode']['version']} ({recipe['opencode']['asset']}).
Built by LayerSmith with profile {recipe['profile']} v{recipe['profile_version']}; downloads verified {recipe['verified']}.

## How it works

```text
 user on the host                  shared container "opencode" (always running)
 ─────────────────                 ─────────────────────────────────────────────
 $ cd ~/projects/app
 $ opencode  ──sudo──> opencode-enter ──podman exec --user <you>──> opencode
                                                                     │ as your uid, your groups
                                                                     │ in ~/projects/app
                                                                     ▼
                                    /home (shared, same path)   your files, your permissions
                                    /etc/opencode (read-only)   site settings: the LLM endpoint
```

* **One container, many users.** It runs all the time (systemd unit
  `opencode-container`). Each `opencode` is a separate process in it, started as
  the user who typed it, with that user's groups, in the directory they were in.
  Several people can work at the same time; nobody sees anyone else's sessions.
* **Nobody enters the container by hand.** `opencode` on the host does it; your
  arguments are passed to opencode as they are (`opencode run "..."`,
  `opencode -c`, `opencode models`). `opencode-shell` opens a shell in the same
  place, as you: the environment the agent runs its commands in.
* **Your files stay where they are.** The shared directories (default `/home`)
  are mounted at the same path, so paths are the same inside and out, and
  everything is read and written with your own uid. Sessions, history and
  personal settings are in your home (`~/.local/share/opencode`,
  `~/.config/opencode`) and survive image updates.
* **No root inside.** The container has no capabilities and no sudo, and
  `no-new-privileges` is set. The only thing that runs as root in it is
  `ensure-user`, which writes your host user and groups into its `/etc/passwd`
  and `/etc/group` (values read from the host, never from you).

## Install on the host (once, as root)

Bring `{archive}`, `{archive}.sha256` and `{INSTALLER}` to the host, then:

```bash
{c['verify']}
{c['install']}
```

`{INSTALLER}` loads the image, takes the host files out of the image itself
(so they always match it) and installs:

| Where | What |
|---|---|
| `/usr/local/bin/opencode`, `opencode-shell` | the commands users type |
| `/usr/local/libexec/opencode-container/opencode-enter` | starts opencode in the container as the caller (run through sudo) |
| `/usr/local/libexec/opencode-container/opencode-container` | `start`, `stop`, `status`, `command` for administrators |
| `/etc/sudoers.d/opencode-container` | members of the `opencode` group may run `opencode-enter`, nothing else |
| `/etc/opencode-container/container.conf` | image, runtime, shared directories, network, extra runtime options |
| `/etc/opencode/opencode.json` | site opencode settings: **your LLM endpoint**; written once, never overwritten |
| `opencode-container.service` | keeps the container running; enabled and started |

Options: `--mounts "/home /srv/projects"` (directories to share), `--group NAME`,
`--users "alice bob"`, `--runtime docker`, `--image REF`, `--no-start`,
`--uninstall`. Podman is used when it is installed, otherwise Docker.

If you only carried the archive: `{c['load']}`, then
`{c['extract_installer']}`.

## Point it at your LLM

Edit `/etc/opencode/opencode.json` on the host. It is mounted read-only into the
container; the next `opencode` started uses it, no restart needed. Any
OpenAI-compatible endpoint works (vLLM, Ollama, LiteLLM, TGI); the provider
`@ai-sdk/openai-compatible` is built into opencode, nothing is installed from npm.

```jsonc
{{
  "$schema": "https://opencode.ai/config.json",
  "autoupdate": false,
  "share": "disabled",
  "provider": {{
    "local": {{
      "npm": "@ai-sdk/openai-compatible",
      "name": "Local LLM",
      "options": {{ "baseURL": "http://LLM-HOST:8000/v1" }},
      "models": {{ "MODEL-NAME": {{ "name": "MODEL-NAME", "limit": {{ "context": 131072, "output": 16384 }} }} }}
    }}
  }},
  "model": "local/MODEL-NAME"
}}
```

`MODEL-NAME` is what the endpoint calls the model (`curl http://LLM-HOST:8000/v1/models`).
Users can add their own providers and choices in `~/.config/opencode/opencode.json`,
and a repository can carry an `opencode.json`; those are merged over the site file.
The container uses the host's network by default, so `localhost` and the LAN are
reached as from the host. An endpoint with a certificate from an internal CA is
trusted when the host trusts it (`HOST_CA=auto`).

## Give people access

```bash
{c['add_user']}        # then they log in again
```

and as that user, in a project under a shared directory:

```bash
{c['use']}
```

Someone not in the group gets a message saying so. Started outside a shared
directory, opencode starts in the user's home instead.

## What is inside

| Tool | Version | Purpose |
|---|---|---|
{tools}

Also: bash, git, jq, vim, less, diff/patch, tar, gzip, xz, zip/unzip, file, tree,
findutils, procps-ng and the ssh client. **Anything the agent should run - compilers,
test runners, linters - must be in the image**; choose the toolchains when building it.

Set in the image so opencode never reaches for the internet on its own:

| Variable | Value |
|---|---|
{env}

opencode would otherwise check for updates, download the model catalogue from
models.dev, download language servers, and download ripgrep on first use.

## Operating it

```bash
{c['status']}     # running?, image, who has a session open
{c['restart']}                          # after changing container.conf (ends running sessions)
journalctl -u opencode-container
```

Many users at once: each opencode session uses a few hundred MB of memory;
limits for the whole container go in `container.conf`, e.g.
`EXTRA_ARGS=(--memory 32g --pids-limit 8192)`.

## Updating

Build a new version in LayerSmith, bring the new archive and `{INSTALLER}`, and run
`{c['update']}` again: it loads the image, refreshes the host files, keeps
`container.conf` and `/etc/opencode/opencode.json`, and restarts the container.
Everyone's sessions and settings are in their homes and stay.
`{c['uninstall']}` removes the commands, the unit and the sudoers rule
(settings, the group and users' data are kept).

## Security

* Users run as themselves: inside the container they can read and change exactly
  what they can on the host, within the shared directories. Nothing outside them
  is visible.
* No capabilities, no sudo, no SSH server, `no-new-privileges`; SELinux labelling
  is disabled for the container so shared homes need no relabelling.
* `opencode-enter` takes only `SUDO_UID` and the current directory from the caller;
  user name, groups and home come from the host's user database.
* Every download is pinned by sha256 and verified before it is installed. No
  installer script is downloaded and run. Packages come from the base's signed
  repositories and are updated at build time.
* The site settings file is readable by every opencode user: put only a shared
  endpoint key there. LayerSmith's image scanning applies when it is enabled.
"""
