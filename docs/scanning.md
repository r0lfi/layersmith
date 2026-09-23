# Image scanning

Scanning in LayerSmith is optional, pluggable and honest.

- **Optional** — nothing is installed or required. With no scanner configured,
  LayerSmith builds, exports and bundles images exactly as before.
- **Pluggable** — LayerSmith talks to a `ScannerBackend`, never to a named
  scanner. Swapping or adding one touches `layersmith/scanners/` and nothing
  else.
- **Honest** — an image that was not scanned is reported as *unknown*, never as
  clean and never as "secure". A scan is a snapshot of what one database knew
  at one moment.

## What it is not

LayerSmith does not block builds on findings. The default is **scan and
report**. A policy model that can block comes later, off by default, and will
be an explicit choice an operator makes rather than a surprise.

## The interface

`layersmith/scanners/base.py` defines it:

| Method | Purpose |
| --- | --- |
| `available()` | is it usable, and if not, why |
| `health()` | connectivity detail for "Test connection" |
| `version()` | scanner version |
| `database_info()` | vulnerability data version, age, offline or downloaded |
| `scan_image()` | findings for one image |
| `generate_sbom()` | a CycloneDX SBOM for the same image |

Results come back as `Finding`, `ScanResult` and `SbomResult` — scanner-neutral
shapes. A finding has a kind (`vulnerability`, `secret`, `misconfiguration`), a
severity from a fixed set, and package/version fields that a scanner may leave
empty.

Selection mirrors build backends: `LAYERSMITH_SCANNER` takes `auto` (default)
or a scanner name, `make_scanner()` never fails on an unusable scanner so the
Settings page can explain the problem, and `NoneScanner` is the fallback.

Scanner-specific options are passed through: any `LAYERSMITH_SCANNER_<KEY>`
variable arrives in `settings.scanner_options` as `<key>`. Core code never
learns one scanner's option names.

## How the image reaches the scanner

A scanner declares its `input_kind`. The default, and the one LayerSmith
prefers, is `image_archive`: **the scanner gets an exported TAR, not a socket.**

A scanner container with the Docker or Podman socket mounted can start
containers as the socket's owner. That is a large privilege to hand a component
whose whole job is parsing untrusted image contents, and it is not worth the
I/O it saves. So:

- The scanner container gets **no runtime socket** by default, no
  `--privileged`, and no host filesystem mounts beyond the one archive.
- If LayerSmith already holds an export for the **exact same image digest**,
  that file is reused. The digest association is verified first, so a replaced
  or stale archive cannot be scanned in place of the image it claims to be.
- Otherwise a **temporary** export is made, scanned, and deleted — on success
  and on failure alike. Requesting a scan never leaves a permanent "Download
  TAR" behind.
- Concurrent scans of the same image do not share or delete each other's
  temporary files.
- Which route was taken (`existing_export` or `temporary_export`) is recorded
  in scan metadata for troubleshooting.

Preparing that archive is the service layer's job, not the scanner's, so a
future backend that wants a different transport — a registry reference, a
remote agent — declares `image_reference` and inherits none of this machinery.

## Secrets

Secret detection reports *that* something matched, never the value. Matches are
masked before they are stored, and scan results are not written into build
logs, manifests, SBOMs or air-gap bundles as plaintext. LayerSmith also refuses
environment variables that look like credentials at build time: an image should
not contain a secret to be found in the first place.

## Configuration

See [.env.example](../.env.example). In short:

| Variable | Default | Purpose |
| --- | --- | --- |
| `LAYERSMITH_SCANNER` | `auto` | `auto`, `none`, or a scanner name |
| `LAYERSMITH_SCAN_AFTER_BUILD` | `1` | scan each image once built |
| `LAYERSMITH_SCAN_KINDS` | `vulnerability,secret` | what to look for |
| `LAYERSMITH_GENERATE_SBOM` | `1` | CycloneDX SBOM alongside a scan |
| `LAYERSMITH_SCANNER_TIMEOUT` | `600` | seconds per scan |
| `LAYERSMITH_SCANNER_<KEY>` | — | passed through to the scanner |

**Settings → Security** shows which scanner is in use, what it can look for,
and how old its vulnerability data is. `GET /api/scanner` returns the same
thing to a script.
