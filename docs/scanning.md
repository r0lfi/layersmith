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
  and the scanner can read that archive format, the file is reused. The
  association is re-verified by checksum first, so an archive that was
  replaced, truncated or written by another build is never scanned in place
  of the image it claims to be.
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

Secret detection reports *that* something matched, never the value. A finding
carries the rule, the file and the line, and a note of the match's length -
nothing more.

Two places in a scanner's output carry values, and LayerSmith strips both
before anything is stored:

* the secret findings themselves, and
* the image's build history (`Metadata.ImageConfig`), which holds every RUN
  command and therefore any secret passed on one. LayerSmith already records
  the Containerfile it built from, so nothing is lost by dropping it.

The stored report is written to disk, served through the API and goes into
air-gap bundles, so this matters more than it might appear.

LayerSmith also refuses environment variables that look like credentials at
build time: an image should not contain a secret to be found in the first
place.

## Trivy

Trivy is the scanner LayerSmith ships support for. It is **never installed
into the LayerSmith application image**; it runs either as its own container
or as a binary on the host.

Enable it by pulling the image on the machine that builds:

```bash
docker pull ghcr.io/aquasecurity/trivy:0.74.0   # or podman pull
```

That is the whole opt-in. `LAYERSMITH_SCANNER=auto` picks it up, and
**Settings → Security** shows it as available. LayerSmith deliberately does
not pull several hundred megabytes on its own.

The scan container is started like this - and nothing else is added:

```
<runtime> run --rm \
  --security-opt no-new-privileges \
  --cap-drop ALL \
  -v layersmith-trivy-db:/root/.cache \
  -v <archive>:/scan/image:ro,z \
  ghcr.io/aquasecurity/trivy:0.74.0 image --input /scan/image ...
```

No socket. No `--privileged`. No host directories. The `z` (shared) SELinux
relabel is deliberate: `Z` would give the archive a private label and make a
reused export unreadable to LayerSmith itself.

Because every capability is dropped, the scanner cannot bypass file
permissions — not even as root inside its own container. LayerSmith
therefore writes its image archives mode `0644`; they are already served
over HTTP without authentication, so the data directory is the access
boundary rather than the file mode. Without that, scanning fails with
`permission denied` on any host whose umask is `077`.

LayerSmith starts that container through the runtime it already uses to
build. That is LayerSmith spending a privilege it already holds; the scanner
is given none of it.

### When LayerSmith runs in a container

The scanner is then a *sibling* container, and `/data` inside LayerSmith is
not a path the runtime knows. Tell it how to reach the archive, one of two
ways:

| Option | Use when |
| --- | --- |
| `LAYERSMITH_SCANNER_DATA_VOLUME` | the data directory is a named volume (compose). It is mounted read-only into the scanner and paths are unchanged. |
| `LAYERSMITH_SCANNER_HOST_DATA_DIR` | the data directory is bind-mounted from the host. Paths are translated to their host form. |

Running LayerSmith directly on a host needs neither.

### Trivy options

All of these are `LAYERSMITH_SCANNER_<KEY>`, passed through without the rest
of LayerSmith knowing them:

| Key | Default | Meaning |
| --- | --- | --- |
| `IMAGE` | `ghcr.io/aquasecurity/trivy:0.74.0` | pinned; a scanner that changes under you is a poor scanner |
| `MODE` | `auto` | `container`, or `binary` for a host install |
| `BINARY` | `trivy` | the executable, in binary mode |
| `RUNTIME` | the build runtime | which runtime starts the scan container |
| `CACHE_VOLUME` | `layersmith-trivy-db` | where the vulnerability database lives |
| `DATA_VOLUME` / `HOST_DATA_DIR` | — | see above |
| `NETWORK` | runtime default | the network the scan container joins |
| `MOUNT_FLAGS` | `ro,z` | mount options for the archive |
| `OFFLINE` | `0` | never download the database; use what is already there |

### What Trivy finds, and what it does not

Verified against Trivy 0.74.0: vulnerabilities with fixed versions, secrets
with a rule, file and line, and misconfigurations. Two things worth knowing:

* Trivy allow-lists well-known documentation values, so a planted
  `AKIAIOSFODNN7EXAMPLE` is *not* reported. A real-looking key is.
* Trivy reads a docker-archive tar or an OCI layout *directory*. It cannot
  read an oci-archive tar, which is what Podman writes by default, so a scan
  on a Podman host exports a docker-archive rather than reusing the stored
  one. That is a temporary export, and it is deleted afterwards.

## Air-gapped use

Two separate things travel offline: what a built image's bundle says about
itself, and the scanner's own vulnerability database.

### What a bundle carries

An air-gap bundle gets a `security/` folder holding the newest completed
scan of that exact image:

| File | What it is |
| --- | --- |
| `SECURITY.txt` | the scan in plain words: date, scanner, database version, counts |
| `scan.json` | every finding, in LayerSmith's scanner-neutral shape |
| `scan-report.json` | the scanner's own output, with secret values stripped |
| `sbom.cyclonedx.json` | the bill of materials |

All of it is covered by the bundle's `SHA256SUMS`. If the image was never
scanned, the folder holds `NOT-SCANNED.txt` saying exactly that — a bundle
should not be silent about what nobody checked.

`SECURITY.txt` says in as many words that a scan is a snapshot of what one
database knew on one date, not a statement that the image is secure. The
recipient of an air-gapped bundle cannot check for themselves, which is
precisely why that has to be written down.

### Seeding the database offline

The scanner needs its vulnerability database, and an air-gapped host cannot
download one. Fill the cache volume on a connected machine and carry it
across. Verified with Trivy 0.74.0 (the database is about 1.3 GB unpacked):

```bash
# On a connected machine
podman run --rm -v layersmith-trivy-db:/root/.cache \
  ghcr.io/aquasecurity/trivy:0.74.0 image --download-db-only

podman run --rm -v layersmith-trivy-db:/root/.cache -v "$PWD":/out:z \
  docker.io/library/alpine:3.22 tar czf /out/trivy-db.tar.gz -C /root/.cache .
```

Carry `trivy-db.tar.gz` and the Trivy image (`podman save`) across, then:

```bash
# On the air-gapped machine
podman load -i trivy.tar
podman run --rm -v layersmith-trivy-db:/root/.cache -v "$PWD":/in:ro,z \
  docker.io/library/alpine:3.22 tar xzf /in/trivy-db.tar.gz -C /root/.cache
```

Then tell LayerSmith never to try to update it:

```
LAYERSMITH_SCANNER_OFFLINE=1
```

Scans then use the database as imported, and say so: a scan performed with
offline data is marked as such in the UI and in the bundle, because its age
is the thing that matters most about it.

## Configuration

See [.env.example](../.env.example). In short:

| Variable | Default | Purpose |
| --- | --- | --- |
| `LAYERSMITH_SCANNER` | `auto` | `auto`, `none`, or a scanner name |
| `LAYERSMITH_SCAN_AFTER_BUILD` | `1` | scan each image once built |
| `LAYERSMITH_SCAN_KINDS` | `vulnerability,secret` | what to look for |
| `LAYERSMITH_GENERATE_SBOM` | `1` | CycloneDX SBOM alongside a scan |
| `LAYERSMITH_SCANNER_TIMEOUT` | `600` | seconds per scan |
| `LAYERSMITH_SCAN_DIR` | `$DATA/scans` | where reports and SBOMs are kept |
| `LAYERSMITH_SCANNER_<KEY>` | — | passed through to the scanner |

**Settings → Security** shows which scanner is in use, what it can look for,
and how old its vulnerability data is. `GET /api/scanner` returns the same
thing to a script.
